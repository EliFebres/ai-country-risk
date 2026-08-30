"""Which scorer, measured against a fixed reference rather than against a price list.

A cheaper model is not a cheaper instrument. The pilot's whole claim is that
every row in a ten-year series was produced by one scorer under one prompt, so
changing the scorer is not a procurement decision that happens to touch the
code — it is an instrument change, and the only honest way to make one is to
re-run a fixed set of anchors through both and look at what moved.

The fixed set is US, 2019, weekly Mondays, 52 anchors. Small enough to cost a few
dollars, long enough that a rank correlation means something.

**This is not gate 2, and the two must not be conflated.** Gate 2 is PT/2019 and
is the *pilot's* regression reference, so it has to be taken on the corpus the
pilot will actually run against. This is a *bake-off* reference, and it needs
evidence two scorers can disagree about: PT 2019 holds 57 articles, all NYT and
all `degraded-title-only`, because the Guardian harvest hit its daily quota on
2026-08-15 having never covered PT past 2016. Five headlines a week measures the
selector rather than the scorer, and never exercises `rewrite_body` at all. US
2019 holds 3,344 Guardian articles with bodies.

Consequently the reference rows this compares against are US masked rows, and
they are legitimate pilot rows only for as long as the pilot stays on `gpt-4o`.
Adopt a candidate and they have to be deleted before the pilot runs, or the
series carries 52 anchors from a different instrument — which `score.FROZEN_FIELDS`
would catch, but as a mid-run refusal rather than a decision taken up front.

**Rank correlation is the meter.** A constant level offset is survivable — the
calibration anchors in the prompt can be moved and the whole series shifts with
them. Reordering is not: it means the candidate disagrees about which weeks were
risky, and no amount of recalibration fixes disagreement about the ordering. So
Spearman and Kendall are reported first and the mean shift second, which is the
opposite of the order anybody asks the question in.

Nothing here writes `risk_snapshot` or `run_ledger`. The scoring arm calls the
live path with `upsert=False`, which is the same switch the two diagnostic arms
already use, so a candidate cannot overwrite the reference it is being compared
against.

Lint was the exception until it was fixed, and it was worse than it looked.
`data_push.upsert_lint_findings` does not write a side table — it does
`INSERT INTO risk_snapshot (country_iso2, as_of, lint) ... ON CONFLICT
(country_iso2, as_of) DO UPDATE`, deliberately, so that lint (phase 5) and the
snapshot (phase 7) can be written in either order. It sets no `scoring_mode`, so
the schema's CHECK cannot stop it. That meant a candidate run overwrote the
reference row's `lint`, and on any anchor with no row yet **created a stub
production row with a NULL score**. Invisibly, too: this file reads lint back out
of the in-memory manifest while `reports` reads the table. It now follows
`upsert` like everything else.

The one write that remains is the shared digest cache, and it is the point: it is
keyed on the digest model, which no scoring candidate moves, so every candidate
reads the identical digests the incumbent read. That is what isolates the scorer
as the only variable.

    python -m backend.util.tools.bakeoff smoke minimax-m3
    python -m backend.util.tools.bakeoff capture-baseline
    python -m backend.util.tools.bakeoff score minimax-m3
    python -m backend.util.tools.bakeoff compare
"""

import argparse
import contextlib
import datetime
import json
import logging
import os
import pathlib
import statistics
import sys
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from dotenv import load_dotenv  # noqa: E402

# Every other entry point does this and this one did not, so the documented
# `python -m backend.util.tools.bakeoff smoke ...` died on MissingKey before it
# read a single vendor key. `PROJECT_ROOT` is `backend/` here — the shared
# convention across `util/tools/` — so the env file sits directly under it.
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv()

import pandas as pd  # noqa: E402

from backend.llm import client as ai_client  # noqa: E402
from backend.llm import constants as ai_constants  # noqa: E402
from backend.llm import usage  # noqa: E402
from backend.util import config  # noqa: E402
from backend.util import provenance  # noqa: E402
from backend.util.pilot import reports  # noqa: E402

logger = logging.getLogger(__name__)

# Where the per-anchor results live. Committed, because the notebook reads them
# and because a measurement that survives only in a terminal is the failure this
# repo has already made twice — the gate-2 numbers that lived in a task brief and
# the probe result that lived in a commit message.
RESULTS_DIR = PROJECT_ROOT / "bakeoff"

# The bake-off's own governor, deliberately not `config.PILOT_BUDGET_USD`. That
# number guards a run somebody authorized; this one guards an experiment, and
# tying them together would let a comparison eat the pilot's headroom.
BAKEOFF_BUDGET_USD = 25.0

# One country, one year. `pd.date_range` over `config.CADENCE` gives 52 Mondays,
# 2019-01-07 to 2019-12-30 — the anchors `score.projection` names when it refuses
# to guess.
#
# US, not PT, and deliberately *not* the same window as gate 2. Gate 2 is the
# pilot's regression reference and must be taken on the corpus the pilot runs
# against; this is a bake-off reference and needs evidence the scorers can
# actually disagree about. PT 2019 has 57 articles, all of them NYT and all of
# them `degraded-title-only`, because the Guardian harvest hit its daily quota on
# 2026-08-15 and never covered PT past 2016. Comparing two scorers on ~5 headlines
# a week measures the selector, not the scorer, and never exercises `rewrite_body`
# at all. US 2019 has 3,344 Guardian articles with bodies.
#
# US is the identifiability *ceiling* country, which does not matter here: the
# bake-off compares scorers against each other on identical evidence, not masking
# efficacy. It would matter for gate 2, which is why gate 2 stays on PT.
COUNTRY = "US"
SINCE = datetime.date(2019, 1, 1)
UNTIL = datetime.date(2019, 12, 31)

# The window is overridable because one window cannot answer the question.
#
# Rank correlation on a *flat* series is depressed by construction: if every
# anchor sits in one band, there is barely an ordering to agree about, and noise
# decides most of the pairwise comparisons. US 2019 is exactly that — all 52
# anchors land in Moderate. So a middling rho there has two readings that the US
# window alone cannot separate: the candidates really do disagree, or the test
# window had no ordering to reproduce.
#
# Running the identical candidate set on a volatile country-year separates them.
# Results are filed per window rather than per candidate alone, so the two
# comparisons coexist instead of the second silently overwriting the first.
def set_window(country: str, since: datetime.date, until: datetime.date) -> None:
    """Point the harness at a different country-year for this process."""
    global COUNTRY, SINCE, UNTIL
    COUNTRY, SINCE, UNTIL = country, since, until


def window_slug() -> str:
    """``US-2019``. What the results directory for this window is called."""
    return f"{COUNTRY}-{SINCE.year}"

# The four ledgers, plus the model's own composite. There is no `composite`
# field anywhere in the schema and this is deliberate — the stored score *is*
# `score_12m`, rescaled and otherwise untouched, and no code combines the
# ledgers into it. Comparing a weighted blend nothing computes would be
# measuring this file rather than the models.
#
# `llm_score` and not `score`, for the reason `pilot.score.score_one` gives: it
# is the scorer's number reported back, not a second module assigning one. The
# tripwire in `test_llm.TestNothingElseAssignsAScore` greps for exactly that,
# and it caught this file on the first run.
METRICS: Tuple[str, ...] = ("llm_score", "score_3m", "friction", "order_uncertainty",
                            "information_capacity", "edge_vitality")

# The prompt's own bands. Lower bound inclusive; the tails are open because the
# model may return 0-4 or 99-100 and a score outside every band is not a band of
# its own.
#
# Imported rather than restated. This tuple used to be a copy under a comment
# saying it came from `AI_PROMPT_V3`, which was a claim no test checked and one
# edit away from being false; the within-band prompt variant needs the same
# names as a schema enum, which would have made it a third copy.
BANDS: Tuple[Tuple[str, float], ...] = ai_constants.BAND_BOUNDS

# What a candidate's own jitter is worth, measured against the only substantive
# signal this project has put a number on.
#
# `reports.divergence` reports masked-minus-named on the stored 0-1 scale, and
# PT's came out at **0.072**. That is the whole finding masking exists to
# produce: what a country's identity was worth to the scorer.
#
# A candidate that returns a different score for the *same* input is spending
# some of that budget on nothing. The models answer on 0-100 and the store keeps
# 0-1, so a spread of N points is N/100 against 0.072 — which makes +/-2 points
# a quarter of the signal, and +/-0.5 points about seven percent of it. That is
# the number that decides "reproducible within tolerance", not the determinism
# gate's pass/fail: the gate answers whether a candidate is exactly reproducible,
# and this answers whether its irreproducibility is large enough to matter.
#
# Measured in this repo on 2026-08-28, when gate 2 ran PT 2019's `named` and
# `masked_nostructural` arms: 0.075 absolute over 6 diagnostic dates, against the
# 0.072 carried from a prior measurement that could not be reproduced here. The
# two land within three thousandths of each other, which is the best evidence the
# remembered figure was sound. Read `GATE2_BASELINE.json` for the current value
# rather than trusting this constant indefinitely.
#
# n=6. It is a small sample and the caveat travels with it: PT is the quietest
# country in the roster, rounds to a multiple of 5 on 84.6% of its anchors, and
# between-model disagreement on a window that ambiguous runs near 0.10 — larger
# than the divergence itself. Distinguishing the two needs a second scorer.
PT_MASKING_DIVERGENCE = 0.075


def noise_floor(score_spread_points: Optional[float]) -> Dict[str, Optional[float]]:
    """One candidate's same-input spread, in the divergence meter's units.

    Args:
        score_spread_points: `gates.determinism.score_spread`, on the model's
            0-100 scale. None when determinism was never measured — which stays
            None rather than becoming 0.0, because "not measured" and "perfectly
            stable" are opposite facts.
    """
    if score_spread_points is None:
        return {"spread_points": None, "spread_0_1": None, "share_of_divergence": None}
    spread = score_spread_points / 100.0
    return {"spread_points": score_spread_points,
            "spread_0_1": round(spread, 4),
            "share_of_divergence": round(spread / PT_MASKING_DIVERGENCE, 3)}


# The observation-only flags. Agreement on these is the sharpest test of whether
# prompt v4 survived the switch: they are explicitly recorded beside the score
# and never applied to it, so a model that quietly self-adjusts to them has
# read the prompt as instructions rather than as a schema.
FLAGS: Tuple[str, ...] = ("war_on_territory", "internal_conflict_level",
                          "emergency_rule", "sovereign_stress")


# --- the candidates ---------------------------------------------------------
# An entry is the environment it runs under plus which vendor key fills it. The
# baseline is here so `capture-baseline` and `score` share one code path and the
# incumbent cannot accidentally be measured under different rules.

CANDIDATES: Dict[str, Dict[str, Any]] = {
    "gpt-4o": {
        "arm": "scoring",
        "note": "the incumbent; captured from risk_snapshot, not re-scored",
        "env": {},
        "key_env": "OPENAI_API_KEY",
    },
    "gpt-4.1-nano": {
        "arm": "scoring",
        "note": "cheapest candidate, ~1/20th the incumbent",
        "env": {"SCORING_MODEL": "gpt-4.1-nano-2025-04-14"},
        "key_env": "OPENAI_API_KEY",
    },
    "gpt-5.6-luna": {
        "arm": "scoring",
        # Reasoning pinned to the floor, and this one earns the pin: unpinned it
        # returned 1,834 output tokens of which 1,400 were reasoning, against 283
        # pinned. Reasoning bills as output, so leaving it on prices the model
        # at roughly 1.7x and would very likely cost determinism as well — the
        # same trap MiniMax's thinking mode set in round 2.
        "env": {"SCORING_MODEL": "gpt-5.6-luna",
                "SCORING_EXTRA_BODY": '{"reasoning_effort": "none"}'},
        "key_env": "OPENAI_API_KEY",
    },
    "gpt-4.1-mini": {
        "arm": "scoring",
        "note": "beats every third-party candidate on price with strict schema intact",
        "env": {"SCORING_MODEL": "gpt-4.1-mini-2025-04-14"},
        "key_env": "OPENAI_API_KEY",
    },
    "gpt-5.4-mini": {
        "arm": "scoring",
        # Measured as already defaulting to no reasoning, unlike Luna. Pinned
        # anyway: a default is not a guarantee, and an unpinned reasoning model
        # that starts reasoning after a vendor-side change would move the cost
        # and the determinism at once, silently.
        "env": {"SCORING_MODEL": "gpt-5.4-mini-2026-03-17",
                "SCORING_EXTRA_BODY": '{"reasoning_effort": "none"}'},
        "key_env": "OPENAI_API_KEY",
    },
    "gpt-4.1": {
        "arm": "scoring",
        "note": "the conservative upgrade; same family as the incumbent",
        "env": {"SCORING_MODEL": "gpt-4.1-2025-04-14"},
        "key_env": "OPENAI_API_KEY",
    },
    # The payload arm. The scorer is deliberately left unset so `candidate_env`
    # clears SCORING_MODEL and the incumbent applies: this varies the *evidence*
    # and holds the instrument fixed, which is the mirror image of every entry
    # above it. Arm A is the existing p2 rows, read free by `capture-baseline`.
    "p3-context": {
        "arm": "payload",
        "note": "trailing context: four quarters of masked history, scorer held at gpt-4o",
        "env": {"PAYLOAD_VARIANT": "p3-context"},
        "key_env": "OPENAI_API_KEY",
    },
    # p2 again, re-scored rather than read. `capture-baseline` reads
    # `risk_snapshot`, and those rows were written when `wb_series_fetch`
    # stamped its rows with the fetch date — so every one of them was scored on
    # roughly fifteen indicators where the table held twenty-three, with the
    # information and edge ledgers empty. The stored reference and any arm run
    # after the vintage fix are no longer the same experiment.
    #
    # Pinning PAYLOAD_VARIANT to its default looks redundant and is not: it is
    # what declares this a payload arm, and it is what `candidate_env` scrubs
    # between runs. The evidence moved underneath a fixed contract, which is
    # the one kind of change this harness had no way to express.
    "p4-trend": {
        "arm": "payload",
        "note": "computed trend block: annual paths, ledger directions, theme volume",
        "env": {"PAYLOAD_VARIANT": "p4-trend"},
        "key_env": "OPENAI_API_KEY",
    },
    # The prompt arm. No payload change and no scorer change -- one paragraph
    # naming two fields the payload has carried since p1 and nothing has ever
    # read. It is the cheap half of the trend question, and if it clears the
    # criteria the computed block is optional.
    "trend-prompt": {
        "arm": "prompt",
        "note": "one paragraph naming trend_1y/trend_5y; no new evidence",
        "env": {"PROMPT_VARIANT": "trend"},
        "key_env": "OPENAI_API_KEY",
    },
    "p2-rebaseline": {
        "arm": "payload",
        "note": "p2 re-scored after the vintage fix, scorer held at gpt-4o",
        "env": {"PAYLOAD_VARIANT": "p2"},
        "key_env": "OPENAI_API_KEY",
    },
    # The elicitation arms. Same evidence as A-prime to the byte, same scorer;
    # what moves is the question. Five interventions on what the model reads
    # left US 2019 between seven and nine distinct values and pushed the
    # round-number share from 69.2% to 90.4%, so these two move what it is asked
    # to emit instead, and they do it through schema order rather than wording
    # alone.
    "within-band": {
        "arm": "prompt",
        "note": "name the band, place the score inside it, justify the placement",
        "env": {"PROMPT_VARIANT": "within-band"},
        "key_env": "OPENAI_API_KEY",
    },
    "vs-typical": {
        "arm": "prompt",
        "note": "describe this country's ordinary week, then score the departure",
        "env": {"PROMPT_VARIANT": "vs-typical"},
        "key_env": "OPENAI_API_KEY",
    },
    # The confirmation cell, and the only candidate that moves two axes on
    # purpose. `gpt-4.1` on the base prompt already reaches 18 distinct values
    # and a 5.8% round share on US 2019 -- the criterion five payload and prompt
    # arms failed -- so the open question is no longer whether the instrument
    # can discriminate but whether elicitation adds anything once the scorer
    # does. Three cells answer it: scorer alone (`gpt-4.1`, already on disk),
    # prompt alone (whichever variant above scores better), and both.
    #
    # A two-cause number is readable only because both single-cause corners
    # exist, which is what `crosses` names and what the one-axis test checks.
    #
    # Resolved to `within-band` after both single-axis arms were read, on the
    # measurements rather than on preference: it beat `vs-typical` on every
    # discrimination figure -- 8 distinct against 6, a 67.3% round share against
    # 90.4%, TR 10 against 9, rho 0.709 against 0.652, and a longest identical
    # run of 2 against 16.
    "gpt-4.1-x-elicitation": {
        "arm": "crossed",
        "crosses": ("gpt-4.1", "within-band"),
        "note": "within-band, the better elicitation variant, re-scored under gpt-4.1",
        "env": {"SCORING_MODEL": "gpt-4.1-2025-04-14",
                "PROMPT_VARIANT": "within-band"},
        "key_env": "OPENAI_API_KEY",
    },
    # The template a local model fills in. Deliberately present and deliberately
    # not runnable by accident: `SCORING_BASE_URL` here points at a port nothing
    # listens on, so a stray `smoke local` fails to connect in a second rather
    # than reaching a real endpoint and spending. `docs/scorer-acceptance.md`
    # has the worked example, including what to change and what not to.
    #
    # The rule the unrun-groq-candidate test enforces still applies: this is not
    # a candidate anybody screens. It is the shape one takes, kept next to the
    # others because a template in a document drifts from the dict it describes.
    "local-template": {
        "arm": "scoring",
        "note": "not a candidate — the shape a locally served model takes",
        "template": True,
        "env": {"SCORING_MODEL": "REPLACE-ME",
                "SCORING_BASE_URL": "http://127.0.0.1:1/v1"},
        # A local server needs *a* key because the OpenAI client insists on one;
        # it needs no *real* key. Pointing `key_env` at the OpenAI variable
        # would make a local run fail when the vendor key is absent, which is
        # exactly backwards.
        "key_env": "SCORING_LOCAL_KEY",
        "key_target": "SCORING_API_KEY",
    },
}

# Every candidate is an OpenAI model, and that is the round-2 result rather than
# a preference. MiniMax and both DeepSeek tiers were measured and are gone:
#
#   * DeepSeek serves no strict `json_schema` on either `/v1` or `/beta`. Under
#     `json_object` it returned valid output 10/10 — genuinely as good as strict
#     on the validity axis — but scored 8 distinct payloads in 10 repeats of one
#     input, spread 7 on `score_12m`.
#   * MiniMax needs `anyOf` instead of `type: [T, "null"]`, and the variant is not
#     the same instrument: on gpt-4o it moved the score and *destroyed*
#     determinism (9 samples: 52x7, 50x2, against 50x9 under the production
#     schema). Under `json_object` it failed to return JSON at all 40% of the
#     time.
#
# `gpt-4.1-mini` costs $0.0069 a snapshot against DeepSeek V4 Pro's $0.0104 at
# its *off-peak* half-rate, with strict schema and no time-of-day scheduling. The
# third-party candidates are beaten on both axes at once, so they are not left
# here to be run by accident. Their prices stay in `usage.PRICES_USD_PER_1M`,
# which is a price table rather than a run list.


class MissingKey(RuntimeError):
    """A candidate's vendor key is not in the environment. Nothing was spent."""


class UnresolvedCandidate(RuntimeError):
    """A candidate names a slot that a prior result was meant to fill."""


@contextlib.contextmanager
def candidate_env(name: str) -> Iterator[Dict[str, str]]:
    """Put one candidate's endpoint into the environment for the block.

    Restored on the way out, including variables that were unset going in, so a
    process can sweep several candidates without the second inheriting the
    first's base URL — which would silently score one model's payload at another
    model's endpoint and report it under the wrong name.

    Raises:
        MissingKey: before anything is set, so a missing key costs nothing.
    """
    spec = CANDIDATES[name]
    env = dict(spec["env"])
    # A crossed cell is registered before its sibling is known -- which variant
    # it crosses with is a result, not a preference, and writing it in ahead of
    # the measurement would be choosing the answer first. Until it is filled in
    # the candidate must refuse to run rather than export `None` and score
    # something nobody chose.
    unresolved = sorted(k for k, v in env.items() if v is None)
    if unresolved:
        raise UnresolvedCandidate(
            f"{name} has {', '.join(unresolved)} unset: it crosses an arm that "
            f"has not been read yet. Fill it in from the single-axis results.")
    if spec.get("key_target"):
        key = os.getenv(spec["key_env"])
        if not key:
            raise MissingKey(
                f"{name} needs {spec['key_env']} in backend/.env; nothing was run")
        env[spec["key_target"]] = key

    # Everything this module might set, not merely what this candidate sets: a
    # previous candidate's leftovers are exactly the contamination to prevent.
    managed = ["SCORING_MODEL", "SCORING_BASE_URL", "SCORING_API_KEY",
               "SCORING_EXTRA_BODY", "DIGEST_MODEL", "DIGEST_BASE_URL",
               "DIGEST_API_KEY", "DIGEST_EXTRA_BODY",
               # The payload axis. Missing from this list, a p3 arm would leak
               # into every arm scored after it in the same process and the
               # contamination would look like a finding.
               "PAYLOAD_VARIANT",
               # And the prompt axis, for exactly the same reason. A leaked
               # trend instruction is worse than a leaked payload variant,
               # because it changes nothing observable in the evidence -- the
               # arms would differ only in a paragraph nobody could see in the
               # result file.
               "PROMPT_VARIANT"]
    before = {k: os.environ.get(k) for k in managed}
    try:
        for k in managed:
            os.environ.pop(k, None)
        os.environ.update(env)
        yield env
    finally:
        for k, v in before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --- the pure arithmetic ----------------------------------------------------
# Split from everything that touches a network or a database so the comparison
# can be tested on hand-built pairs. `reports._pace` draws the same line.

def band(score_0_1: Optional[float]) -> Optional[str]:
    """Which of the prompt's five bands a 0-1 score lands in, or None.

    Takes the 0-1 scale because that is what the pipeline returns and what the
    database stores; the bands are written on the 0-100 scale the model answers
    on, so this is the one place the two meet.
    """
    if score_0_1 is None:
        return None
    on_100 = float(score_0_1) * 100.0
    label = BANDS[0][0]
    for name, floor in BANDS:
        if on_100 >= floor:
            label = name
    return label


def _paired(baseline: Dict[Any, Optional[float]],
            candidate: Dict[Any, Optional[float]]) -> Tuple[List[float], List[float]]:
    """The anchors both sides scored, in a stable order, Nones dropped."""
    keys = sorted(k for k in set(baseline) & set(candidate)
                  if baseline.get(k) is not None and candidate.get(k) is not None)
    return ([float(baseline[k]) for k in keys], [float(candidate[k]) for k in keys])


def _kendall_tau_b(left: List[float], right: List[float]) -> Optional[float]:
    """Kendall's tau-b, tie-corrected, over paired observations.

    Hand-rolled because ``Series.corr(method='kendall')`` delegates to SciPy,
    which is not a dependency here and is not worth becoming one for two
    coefficients — the whole of what is needed is a pair count and a tie
    correction. Tau-b rather than tau-a because the ledgers are integers on a
    0-100 grid and ties are common; tau-a would report a ceiling below 1.0 for
    two series that agree perfectly.

    ponytail: O(n^2) over the pair set. 52 anchors is 1,326 comparisons, so the
    quadratic is free; if this is ever pointed at the 2,092-snapshot pilot,
    replace it with the merge-sort inversion count rather than adding SciPy.
    """
    n = len(left)
    concordant = discordant = ties_left = ties_right = 0
    for i in range(n):
        for j in range(i + 1, n):
            da, db = left[i] - left[j], right[i] - right[j]
            if da == 0 and db == 0:
                # Tied on both sides: counted in neither correction, exactly as
                # tau-b defines it. Counting it in both would deflate the result.
                continue
            if da == 0:
                ties_left += 1
            elif db == 0:
                ties_right += 1
            elif (da > 0) == (db > 0):
                concordant += 1
            else:
                discordant += 1
    denominator = ((concordant + discordant + ties_left)
                   * (concordant + discordant + ties_right)) ** 0.5
    return None if denominator == 0 else (concordant - discordant) / denominator


def rank_correlation(baseline: Dict[Any, Optional[float]],
                     candidate: Dict[Any, Optional[float]]) -> Dict[str, Any]:
    """Spearman and Kendall over the anchors both sides scored.

    Both, because they fail differently: Spearman is moved hard by a single
    anchor that swaps from one end of the range to the other, Kendall is not,
    and a candidate that reorders one crisis week is a different finding from
    one that reorders the quiet middle.

    Spearman is Pearson over the ranks, which is its definition, so
    ``Series.rank().corr()`` computes it without SciPy — the default method is
    the only one pandas implements itself.

    Returns ``None`` for a correlation rather than a number when there are fewer
    than three pairs or when either side is constant. A constant series has no
    ranks to correlate and pandas answers NaN, which prints through a float
    format as a measurement. Same reason `reports._mean` returns None for an
    empty sample rather than 0.0.
    """
    left, right = _paired(baseline, candidate)
    out: Dict[str, Any] = {"n": len(left), "spearman": None, "kendall": None}
    if len(left) < 3 or len(set(left)) < 2 or len(set(right)) < 2:
        return out

    spearman = pd.Series(left).rank().corr(pd.Series(right).rank())
    out["spearman"] = None if pd.isna(spearman) else round(float(spearman), 4)
    kendall = _kendall_tau_b(left, right)
    out["kendall"] = None if kendall is None else round(kendall, 4)
    return out


def shift(baseline: Dict[Any, Optional[float]],
          candidate: Dict[Any, Optional[float]]) -> Dict[str, Any]:
    """Level, signed and absolute — the convention `reports.divergence` sets.

    Signed says which way the candidate leans; absolute says how far it moves.
    A candidate that scores half its weeks high and half low averages to a
    clean-looking zero under the signed mean alone, which is the failure the
    absolute pair exists to make visible.
    """
    left, right = _paired(baseline, candidate)
    deltas = [c - b for b, c in zip(left, right)]
    return {
        "n": len(deltas),
        "signed_mean": round(statistics.fmean(deltas), 4) if deltas else None,
        "abs_mean": round(statistics.fmean([abs(d) for d in deltas]), 4) if deltas else None,
        "max_abs": round(max((abs(d) for d in deltas), default=0.0), 4) if deltas else None,
    }


def band_matrix(baseline: Dict[Any, Optional[float]],
                candidate: Dict[Any, Optional[float]]) -> Dict[str, Dict[str, int]]:
    """Baseline band to candidate band, counted. Rows are the baseline's.

    The calibration meter. A candidate that is merely offset fills one diagonal
    band and the one beside it; a candidate that disagrees scatters. Empty rows
    are kept so the shape of the matrix does not change with the data — a
    5x5 that silently becomes 3x3 is a different plot every run.
    """
    labels = [name for name, _ in BANDS]
    matrix = {row: {col: 0 for col in labels} for row in labels}
    for key in sorted(set(baseline) & set(candidate)):
        row, col = band(baseline.get(key)), band(candidate.get(key))
        if row is not None and col is not None:
            matrix[row][col] += 1
    return matrix


def flag_agreement(baseline: Dict[Any, Dict[str, Any]],
                   candidate: Dict[Any, Dict[str, Any]]) -> Dict[str, Any]:
    """Per-flag agreement on the anchors both sides scored.

    Reported per flag rather than as one number because the flags are not
    equivalent: `war_on_territory` is false on every PT week in 2019 and
    agreeing about it is nearly free, while `sovereign_stress` is the one a
    model reading the prompt as instructions would start moving. A single mean
    hides which of the two happened.
    """
    out: Dict[str, Any] = {}
    keys = sorted(set(baseline) & set(candidate))
    for flag in FLAGS:
        agreed = compared = 0
        for key in keys:
            left = (baseline.get(key) or {}).get(flag)
            right = (candidate.get(key) or {}).get(flag)
            if left is None or right is None:
                continue
            compared += 1
            agreed += int(left == right)
        out[flag] = {"n": compared,
                     "agreement": round(agreed / compared, 3) if compared else None}
    return out


def _series(rows: List[Dict[str, Any]], metric: str) -> Dict[str, Optional[float]]:
    """``{as_of: value}`` for one metric, over a candidate file's rows."""
    if metric in ("llm_score", "score_3m"):
        return {r["as_of"]: r.get(metric) for r in rows}
    return {r["as_of"]: (r.get("ledger_scores") or {}).get(metric) for r in rows}


# The prompt bans them: "Use precise values (37, 62, 81) — never round to
# multiples of 5." Under a uniform distribution over integers a fifth of answers
# would land on one anyway, so 20% is the floor rather than zero. gpt-4o sits at
# 69% on US 2019 and 19% on TR 2018 — the same model, the same prompt, obeying
# the instruction where the evidence is determinate and abandoning it where it
# is not. That makes this a measure of snapping, not of taste.
_ROUND_NUMBER_STEP = 5


def series_shape(values: List[Optional[float]]) -> Dict[str, Any]:
    """How degenerate one arm's series is, on its own terms.

    Every other meter here is paired — it asks how two arms differ. These four
    ask whether a single series says anything at all, which is the question a
    flat window raises and rank correlation cannot answer: `rank_correlation`
    returns None below two distinct values, and a series with nine of them
    across fifty-two weeks is barely above that.

    Returns:
        ``distinct`` values, ``lag1_autocorr``, ``longest_run`` of identical
        consecutive scores, and ``round_share`` on the model's 0-100 scale.
        Autocorrelation is None below three points or on a constant series,
        rather than 0.0 — "not measurable" and "uncorrelated" are different
        facts, the same rule the reports follow with an em dash.
    """
    vals = [v for v in values if v is not None]
    n = len(vals)
    if not n:
        return {"n": 0, "distinct": 0, "lag1_autocorr": None,
                "longest_run": None, "round_share": None}

    mean = sum(vals) / n
    denom = sum((v - mean) ** 2 for v in vals)
    autocorr = None
    if n >= 3 and denom:
        autocorr = round(sum((vals[i] - mean) * (vals[i - 1] - mean)
                             for i in range(1, n)) / denom, 3)

    longest = run = 1
    for i in range(1, n):
        run = run + 1 if vals[i] == vals[i - 1] else 1
        longest = max(longest, run)

    rounded = sum(1 for v in vals
                  if abs(round(v * 100) - v * 100) < 1e-6
                  and round(v * 100) % _ROUND_NUMBER_STEP == 0)
    return {"n": n, "distinct": len(set(vals)), "lag1_autocorr": autocorr,
            "longest_run": longest, "round_share": round(rounded / n, 3)}


def first_move(values: List[Optional[float]], *,
               baseline_n: int = 13, delta: float = 0.05) -> Optional[int]:
    """Index of the first anchor standing `delta` above the opening baseline.

    Criterion (d) of the payload A/B and of this attempt, and until now it had
    never been code. Both previous attempts computed it by hand from the
    committed arm files and only the verdicts survived, so the one number that
    decides whether an intervention made the instrument *late* could not be
    recomputed or tested. It is here now for the same reason `series_shape` is.

    The baseline is the mean of the first `baseline_n` scored anchors -- a
    quarter of weekly anchors -- rather than the first anchor alone. A single
    opening week is one draw from a series whose own noise floor is a point or
    two, and attempt 1 recorded both readings precisely because they disagreed
    about which arm moved first.

    Returns None when nothing ever clears the line, which is a real answer about
    a flat series and not an error.
    """
    scored = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(scored) < baseline_n:
        return None
    baseline = statistics.fmean(v for _, v in scored[:baseline_n])
    return next((i for i, v in scored if v - baseline >= delta), None)


def compare_one(baseline_rows: List[Dict[str, Any]],
                candidate_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Every meter, one candidate against the baseline. Pure.

    Anchors present on only one side are counted and named rather than dropped
    quietly — `probe.compare` makes the same choice, and for the same reason:
    "not measured" and "no change" are different answers that must not print
    the same.
    """
    base_by_date = {r["as_of"]: r for r in baseline_rows}
    cand_by_date = {r["as_of"]: r for r in candidate_rows}
    only_baseline = sorted(set(base_by_date) - set(cand_by_date))
    only_candidate = sorted(set(cand_by_date) - set(base_by_date))

    metrics: Dict[str, Any] = {}
    for metric in METRICS:
        left = _series(baseline_rows, metric)
        right = _series(candidate_rows, metric)
        metrics[metric] = {**rank_correlation(left, right), **shift(left, right)}
    # Per arm, not per pair. A payload change is meant to move these; a scorer
    # change is meant not to.
    shape = {
        "baseline": series_shape(list(_series(baseline_rows, "llm_score").values())),
        "candidate": series_shape(list(_series(candidate_rows, "llm_score").values())),
    }

    return {
        "n_baseline": len(baseline_rows),
        "n_candidate": len(candidate_rows),
        "only_baseline": only_baseline,
        "only_candidate": only_candidate,
        "comparable": payload_comparability(baseline_rows, candidate_rows),
        "metrics": metrics,
        "shape": shape,
        "band_matrix": band_matrix(_series(baseline_rows, "llm_score"),
                                   _series(candidate_rows, "llm_score")),
        "flags": flag_agreement(
            {r["as_of"]: r.get("condition_flags") or {} for r in baseline_rows},
            {r["as_of"]: r.get("condition_flags") or {} for r in candidate_rows}),
        "lint": _lint_rates(baseline_rows, candidate_rows),
        "cost": cost_summary(candidate_rows),
    }


def _fingerprints(rows: List[Dict[str, Any]]) -> List[str]:
    """The distinct payload fingerprints across one arm's scored rows."""
    return sorted({r["payload_fingerprint"] for r in rows
                   if r.get("payload_fingerprint")})


def payload_comparability(baseline_rows: List[Dict[str, Any]],
                          candidate_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Whether these two arms were scored on the same evidence.

    The check that did not exist. `gpt-4.1.json` and `p2-rebaseline.json` both
    declare `PAYLOAD_VERSION: p2`; the first was scored on 08-27 with the
    information and edge ledgers resolving zero indicators, the second on 08-29
    after the vintage fix put nine more per country into every payload. Every
    number `compare_one` produced across that pair was the fix plus the
    candidate, and there was no field to notice it by.

    Reported rather than raised, and per pair rather than globally, because both
    answers are legitimate: a *payload* arm is supposed to move the fingerprint
    — that is the whole axis — while a *scoring* arm that moves it has changed
    two things at once. The renderer is where the distinction gets made; this
    function's job is to make the fact available at all.

    An arm with no fingerprints at all is `unknown`, not `comparable`. Every row
    committed before this field existed is in that state, which is the honest
    answer for them: nothing recorded what they saw.
    """
    base, cand = _fingerprints(baseline_rows), _fingerprints(candidate_rows)
    if not base or not cand:
        return {"verdict": "unknown", "baseline": base, "candidate": cand,
                "note": "one or both arms predate the payload fingerprint; "
                        "what they were scored on is not recorded"}
    if base == cand:
        return {"verdict": "same", "baseline": base, "candidate": cand}
    return {
        "verdict": "different",
        "baseline": base,
        "candidate": cand,
        "note": "these arms were scored on different evidence. Any difference "
                "below is that plus the candidate, and the two cannot be "
                "separated from these files.",
    }


def _lint_rates(baseline_rows: List[Dict[str, Any]],
                candidate_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """How often each advisory tripwire fired, either side.

    A rule that fires for one model and not the other is a threshold tuned to
    the incumbent, not a fact about the country. `util.lint` is observe-only, so
    nothing here moved a score — which is exactly why it can be read as a
    statement about the model.
    """
    def counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for row in rows:
            for finding in row.get("lint") or []:
                rule = finding.get("rule") if isinstance(finding, dict) else str(finding)
                out[rule] = out.get(rule, 0) + 1
        return out

    base, cand = counts(baseline_rows), counts(candidate_rows)
    return {rule: {"baseline": base.get(rule, 0), "candidate": cand.get(rule, 0)}
            for rule in sorted(set(base) | set(cand))}


def _model_of(rows: List[Dict[str, Any]]) -> str:
    """The model id these rows were produced by, or '' if none of them says.

    An empty answer means "we do not know which model this was", which
    `usage.is_priced` correctly treats as unpriced: a cost figure derived from a
    model nobody can name is the same fabrication as one derived from a model
    with no price.
    """
    return next((r["model_id"] for r in rows if r.get("model_id")), "")


def cost_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-snapshot cost, realised tokens, and the cache share that was measured.

    ``cache_share`` is None rather than 0.0 when no row reported cache detail.
    A provider that does not break out cached tokens and a provider whose cache
    never hit produce the same zero, and only one of them is a finding — the
    same distinction `reports._fmt` draws with its em dash.
    """
    done = [r for r in rows if r.get("status") == "complete"]
    if not done:
        return {"snapshots": 0}
    # A model with no list price is billed at the fallback rate by `usage.price`,
    # which is correct for a governor and a fabrication in a report. Tokens and
    # wall-clock are still real; the dollars are not, so they are withheld
    # rather than printed with a caveat nobody reads.
    #
    # The first row that *names* a model, not simply the first row: an early
    # anchor whose model id came back empty would otherwise withhold the cost of
    # a run that is perfectly well priced.
    priced = usage.is_priced(_model_of(done))
    spend = sum(r.get("spend_usd") or 0.0 for r in done)
    inputs = sum(r.get("input_tokens") or 0 for r in done)
    outputs = sum(r.get("output_tokens") or 0 for r in done)
    reported = [r for r in done if r.get("cached_tokens") is not None]
    cached = sum(r.get("cached_tokens") or 0 for r in reported)
    offpeak = [r["offpeak_usd"] for r in done if r.get("offpeak_usd") is not None]
    return {
        "snapshots": len(done),
        "priced": priced,
        "spend_usd": round(spend, 4) if priced else None,
        "per_snapshot_usd": round(spend / len(done), 6) if priced else None,
        "seconds_per_snapshot": (round(sum(r["seconds"] for r in done
                                           if r.get("seconds") is not None)
                                       / len(done), 2)
                                 if any(r.get("seconds") is not None for r in done)
                                 else None),
        # The comparable one. See `cache_neutral_per_snapshot`: realised spend
        # depends on what ran before it, so criterion (e) reads this instead.
        "cache_neutral_per_snapshot_usd": cache_neutral_per_snapshot(done),
        "offpeak_per_snapshot_usd": (round(sum(offpeak) / len(offpeak), 6)
                                     if offpeak else None),
        "input_tokens_per_snapshot": round(inputs / len(done), 1),
        "output_tokens_per_snapshot": round(outputs / len(done), 1),
        "cache_share": (round(cached / inputs, 3)
                        if reported and inputs else None),
        "utc_hours": sorted({r["utc_hour"] for r in done if r.get("utc_hour") is not None}),
    }


# --- what it costs to run at scale ------------------------------------------

PILOT_SNAPSHOTS = 2_092
BACKFILL_SNAPSHOTS = 25_104


def cache_neutral_per_snapshot(rows: List[Dict[str, Any]]) -> Optional[float]:
    """Per-snapshot cost with the prompt cache priced out.

    `cost_summary` reports what was actually spent, which is the right number
    for a budget and the wrong one for criterion (e). Realised spend depends on
    the provider's prompt cache, and the cache depends on *what ran just before*
    -- so an arm scored straight after another arm on the same anchors is
    flattered by an effect that has nothing to do with the arm.

    It is not hypothetical. `vs-typical` ran after `within-band` on identical
    anchors and came back with a 90.8% cache share against A-prime's 3.9%,
    reporting -36% per snapshot while sending *more* tokens than A-prime in both
    directions. On this measure it is +2%. The same artifact is why arm B is
    recorded as failing (e) at +17.0% on TR when its token counts put it at
    +14.9%, inside the line, and why arm C is recorded as -10.2% cheaper when it
    is +1.5% dearer.

    Comparable across arms because it prices the same tokens the same way every
    time, whatever the cache happened to be doing that afternoon.
    """
    scored = [r for r in rows if r.get("calls")]
    if not scored:
        return None
    model_id = _model_of(scored)
    if not usage.is_priced(model_id):
        # A locally served model has no list price, and `usage.price` would
        # return gpt-4o's. None here rather than a number, for the same reason
        # `cache_share` is None when no row reported cache detail: "not priced"
        # and "priced at zero" are opposite facts, and only one of them belongs
        # in a comparison. The tokens are still reported by `cost_summary`.
        return None
    return usage.price(
        model_id,
        sum(r["input_tokens"] for r in scored),
        sum(r["output_tokens"] for r in scored),
        0,
    ) / len(scored)


def projection(per_snapshot_usd: Optional[float]) -> Dict[str, Optional[float]]:
    """What one measured per-snapshot cost implies at pilot and backfill scale.

    Scorer-only. Digests sit on top, and saying so matters: the pilot's $130
    guard was sized against the pair, so quoting a scorer number as the whole
    cost is how a projection ends up low by a third — which is exactly what
    `score.projection`'s deleted constant did.
    """
    if per_snapshot_usd is None:
        return {"pilot_usd": None, "backfill_usd": None}
    return {"pilot_usd": round(per_snapshot_usd * PILOT_SNAPSHOTS, 2),
            "backfill_usd": round(per_snapshot_usd * BACKFILL_SNAPSHOTS, 2)}


# --- criterion 7: the disaster detector -------------------------------------

# A rank correlation over a series with three values is not a rank correlation.
# `edge_vitality` takes **two** distinct values across all 52 US 2019 anchors
# (`deferred.md` §3 -- the ledger has at most three indicators underneath it),
# and on that series gpt-4o disagrees with *itself* at rho = -0.287 across a
# payload change. Gating on it would fail the reference, which is the mistake
# the determinism gate already made once and had to be rescued from.
RHO_GATE_MIN_DISTINCT = 5

# Not zero. The gate exists to catch a candidate ranking a year backwards, not
# to adjudicate noise around the origin: `gpt-4.1-nano` sits at -0.036 on TR
# `information_capacity`, which is a coin flip on a coarse ledger rather than an
# inversion. `gpt-4.1-nano`'s friction at -0.228 against the older reference is
# the shape this is for. The reference itself clears it with room -- gpt-4o
# against A-prime is worst-gated 0.279 -- which is the test any gate has to pass
# before it is allowed to disqualify anybody.
RHO_GATE_FLOOR = -0.10


def rho_gate(baseline_rows: List[Dict[str, Any]], candidate_rows: List[Dict[str, Any]],
             *, floor: float = RHO_GATE_FLOOR,
             min_distinct: int = RHO_GATE_MIN_DISTINCT) -> Dict[str, Any]:
    """Rank agreement read as a disaster detector, not as a ranking criterion.

    Agreement with the incumbent rewards a candidate for reproducing the
    incumbent's judgement *including where it is wrong*, and the reason a
    candidate is being screened at all is that the incumbent might be. So this
    returns pass/fail on inversions and reports everything else.

    Metrics whose baseline or candidate series is too coarse to rank are
    excluded and named, because "excluded" and "agreed" are different answers --
    the same distinction `cost_summary.cache_share` draws with None.
    """
    metrics = compare_one(baseline_rows, candidate_rows)["metrics"]
    gated, excluded = {}, {}
    for name, result in metrics.items():
        rho = result.get("spearman")
        n = min(_distinct_ledger(baseline_rows, name),
                _distinct_ledger(candidate_rows, name))
        if name in ("llm_score", "score_3m") or n >= min_distinct:
            gated[name] = rho
        else:
            excluded[name] = {"spearman": rho, "distinct": n}
    failures = {k: v for k, v in gated.items() if v is not None and v < floor}
    measured = [v for v in gated.values() if v is not None]
    comparable = payload_comparability(baseline_rows, candidate_rows)
    return {
        "passed": not failures,
        "floor": floor,
        "failures": failures,
        "worst_gated": min(measured) if measured else None,
        "gated": gated,
        # A ρ against a baseline scored on different evidence is a number about
        # two things. Carried on the gate result rather than left to the caller,
        # because this gate's verdict is quoted directly into a decision.
        "comparable": comparable,
        # Reported rather than dropped: a ledger too coarse to rank is itself a
        # finding about the instrument, not a gap in the comparison.
        "excluded_as_too_coarse": excluded,
    }


def _distinct_ledger(rows: List[Dict[str, Any]], metric: str) -> int:
    """How many values `metric` actually takes across these rows."""
    if metric in ("llm_score", "score_3m"):
        values = {r.get(metric) for r in rows}
    else:
        values = {(r.get("ledger_scores") or {}).get(metric) for r in rows}
    return len(values - {None})


# --- reading and writing the result files -----------------------------------

def result_path(name: str) -> pathlib.Path:
    return RESULTS_DIR / window_slug() / f"{name}.json"


def load(name: str) -> Optional[Dict[str, Any]]:
    """One candidate's file, or None if it was never run."""
    path = result_path(name)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _hold_captured_under(path: pathlib.Path,
                         payload: Dict[str, Any]) -> Dict[str, Any]:
    """Carry an existing `captured_under` forward verbatim. Never restamp it.

    `captured_under` records the versions that produced the *rows*. A later
    write that touches anything else has, by definition, not re-scored them, so
    it has nothing to say about what did.

    `b128aad` said otherwise. It added the re-derived `gates` block to
    `gpt-4.1.json` and `gpt-4o.json` -- 481 insertions, the 52 and 53 anchor rows
    untouched -- and carried `captured_under.git_sha` from `d063fc4`/`30e07ef`,
    the trees that scored those rows on 08-27, to `b47b2b2`, the tree that added
    the gates on 08-29. In between, on 08-29 morning, the vintage fix put nine
    more indicators into every payload. So the two files that
    `docs/scorer-acceptance.md` names as the reference came to claim a post-fix
    tree for pre-fix rows, and the one field that could have shown it was the
    field the write destroyed. See `docs/pipeline-audit.md` section 3.

    New keys are allowed through: later work may record something alongside the
    stamp. Existing keys are held, and a divergence is logged rather than raised
    -- re-smoking a candidate after any commit moves `git_sha` legitimately, and
    a guard that aborts a paid gate run because the tree moved is a guard that
    gets deleted.
    """
    if not path.exists():
        return payload
    stored = (json.loads(path.read_text(encoding="utf-8"))
              .get("captured_under") or {})
    incoming = payload.get("captured_under") or {}
    if not stored:
        return payload

    held = {**incoming, **{k: v for k, v in stored.items() if v}}
    for field, was in stored.items():
        now = incoming.get(field)
        if was and now is not None and now != was:
            logger.warning(
                "[bakeoff] %s: %s is %r on the rows already stored; this write "
                "carries no rows of its own, so %r is not recorded",
                path.stem, field, was, now)
    return {**payload, "captured_under": held}


def _write(path: pathlib.Path, payload: Dict[str, Any]) -> pathlib.Path:
    """The one place an arm file is written.

    Three writers used to reach the disk independently -- `save`, `save_gates`,
    and the `smoke` branch of `main` -- and only the first had any guard on it.
    The stamp that `b128aad` overwrote went through the third. One chokepoint,
    for the same reason `upsert_indicator_series` re-dates at one instead of
    asking each fetcher to behave.
    """
    payload = _hold_captured_under(path, payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str, sort_keys=True),
                    encoding="utf-8")
    return path


def save(name: str, payload: Dict[str, Any]) -> pathlib.Path:
    """Write one candidate's file, never losing a gate result it already had.

    The carry-forward is here rather than in the caller because it used to be in
    the caller, and that is exactly how 24 of 26 committed files ended up with
    an empty `gates` block. `main()` protected the CLI's `score` path; every
    other writer -- the notebook, a test, a future tool -- built a fresh payload
    through `_wrap`, whose `gates` defaults to `{}`, and overwrote a measurement
    that cost real money with a dict that cost nothing. One guard where all
    callers route through, for the same reason `upsert_indicator_series` re-dates
    at the chokepoint instead of asking each fetcher to behave.

    A caller that genuinely means to clear the gates passes `gates: None`, which
    is distinguishable from the absent key that a rebuilt payload carries.
    """
    path = result_path(name)
    if not payload.get("gates") and path.exists():
        previous = (load(name) or {}).get("gates")
        if previous and payload.get("gates") is not None:
            payload = {**payload, "gates": previous}
            logger.info("[bakeoff] %s: carried forward the gates already on disk",
                        name)
    return _write(path, payload)


def save_gates(name: str, gates: Dict[str, Any]) -> List[pathlib.Path]:
    """Record one candidate's gate result in every window it has a file in.

    The gates are a property of the *candidate*, not of the window: `smoke` runs
    against `_SMOKE_EVIDENCE`, a canned payload with no country and no anchor in
    it. Storing them in a window-scoped file therefore made them look
    window-scoped, and smoking a candidate under `US-2019` left the `TR-2018`
    file saying the gate had never been run -- indistinguishable, on disk, from
    a candidate that had never been smoked at all.
    """
    written = []
    for window in sorted({p.parent.name for p in RESULTS_DIR.glob(f"*/{name}.json")}
                         | {window_slug()}):
        path = RESULTS_DIR / window / f"{name}.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            payload = _wrap(name, CANDIDATES.get(name, {}).get("arm", "scoring"), [])
        payload["gates"] = gates
        written.append(_write(path, payload))
    return written


# --- the hard gates ---------------------------------------------------------
# Both run before any real spend, and both report failure rather than routing
# around it. A candidate that needs `json_object` plus local validation and a
# retry is not the same instrument, and a candidate that ignores `seed` costs
# the byte-for-byte rebuild `rebuild_snapshot` exists to perform.

# A payload with the shape of a real one and none of its cost: masked language,
# plausible magnitudes, two articles. Magnitudes are kept realistic because a
# schema that holds on trivial input and fails on a real number is a schema that
# was never tested — and `evidence_coverage` and the ledger scores are exactly
# where a model improvises.
_SMOKE_EVIDENCE = {
    "structural": {"gdp_growth_pct": 2.1, "cpi_inflation_pct": 0.9,
                   "unemployment_pct": 6.5, "gov_debt_pct_gdp": 117.2},
    "vintages": {"weo_edition": "2019-04"},
}
_SMOKE_ARTICLES = [
    {"id": "a1", "source": "a national daily", "published_at": "2019-06-03",
     "title": "Central bank holds policy rate for a third meeting",
     "digest": {"what_happened": "The central bank held its policy rate, citing "
                                 "balanced risks and easing headline inflation.",
                "actors": "the central bank, the rate-setting committee",
                "numbers": "0.0%, third consecutive meeting, 7-2 vote",
                "transmission": "borrowing costs, credit growth",
                "directly_about_country": True, "stage1_severity": 25}},
    {"id": "a2", "source": "a news agency", "published_at": "2019-06-05",
     "title": "Governing party loses majority in regional vote",
     "digest": {"what_happened": "The governing party lost its majority in a "
                                 "regional election and coalition talks began.",
                "actors": "the governing party, the main opposition party",
                "numbers": "41 seats, 38%, 12 days",
                "transmission": "policy continuity, fiscal plans",
                "directly_about_country": True, "stage1_severity": 45}},
]


# The other two bands. A noise floor measured on one Moderate payload is a noise
# floor for Moderate, and the models do not behave the same across the range:
# `gpt-4.1-nano` swings 20 points on a calm payload and 5 on a stressed one, so
# smoking only the middle would have reported it four times steadier than it is.
#
# Three payloads rather than three real anchors, for the same reason
# `_SMOKE_EVIDENCE` exists: an anchor costs a database, a harvest and a digest
# pass, and none of that is what the gate measures. What matters is that the
# three land in different bands, which `BAND_BOUNDS` decides.
_SMOKE_BANDS: Dict[str, Tuple[Dict[str, Any], List[Dict[str, Any]]]] = {}

_CALM_EVIDENCE = {
    "structural": {"gdp_growth_pct": 2.4, "cpi_inflation_pct": 1.4,
                   "unemployment_pct": 3.8, "gov_debt_pct_gdp": 41.2},
    "vintages": {"weo_edition": "2019-04"},
}
_CALM_ARTICLES = [
    {"id": "a1", "source": "a national daily", "published_at": "2019-06-03",
     "title": "Budget surplus widens on stronger receipts",
     "digest": {"what_happened": "The finance ministry reported a wider budget "
                                 "surplus after receipts beat forecasts.",
                "actors": "the finance ministry, the audit office",
                "numbers": "1.2% of GDP, third consecutive quarter",
                "transmission": "fiscal space, issuance plans",
                "directly_about_country": True, "stage1_severity": 10}},
    {"id": "a2", "source": "a news agency", "published_at": "2019-06-05",
     "title": "Regulator approves cross-border rail concession",
     "digest": {"what_happened": "The transport regulator approved a rail "
                                 "concession after a routine consultation.",
                "actors": "the transport regulator, two bidding consortia",
                "numbers": "30-year term, four bidders",
                "transmission": "infrastructure investment",
                "directly_about_country": True, "stage1_severity": 8}},
]

_STRESSED_EVIDENCE = {
    "structural": {"gdp_growth_pct": -4.8, "cpi_inflation_pct": 61.3,
                   "unemployment_pct": 14.9, "gov_debt_pct_gdp": 152.6},
    "vintages": {"weo_edition": "2019-04"},
}
_STRESSED_ARTICLES = [
    {"id": "a1", "source": "a national daily", "published_at": "2019-06-03",
     "title": "Currency falls a further fifth as reserves are drawn down",
     "digest": {"what_happened": "The currency fell sharply for a second week "
                                 "while the central bank sold reserves to slow "
                                 "the decline.",
                "actors": "the central bank, foreign creditors",
                "numbers": "-21% in two weeks, reserves down 34%",
                "transmission": "import costs, external debt service",
                "directly_about_country": True, "stage1_severity": 88}},
    {"id": "a2", "source": "a news agency", "published_at": "2019-06-05",
     "title": "Emergency powers extended as protests spread to third city",
     "digest": {"what_happened": "The government extended emergency powers by "
                                 "decree after protests spread and several "
                                 "journalists were detained.",
                "actors": "the government, the interior ministry, protest "
                          "organisers",
                "numbers": "90-day extension, 11 detained, 3 cities",
                "transmission": "rule of law, press freedom, investment climate",
                "directly_about_country": True, "stage1_severity": 92}},
]

_SMOKE_BANDS = {
    "calm": (_CALM_EVIDENCE, _CALM_ARTICLES),
    "moderate": (_SMOKE_EVIDENCE, _SMOKE_ARTICLES),
    "stressed": (_STRESSED_EVIDENCE, _STRESSED_ARTICLES),
}


def smoke_prompt(band: str = "moderate") -> str:
    """The real prompt, on canned evidence. Not a toy schema and not a toy prompt.

    A candidate that satisfies a three-field schema says nothing about one that
    has to satisfy `RISK_SCHEMA_V3` — ten required fields, two nested arrays and
    `additionalProperties: false` at every level. So the gate runs the thing
    that will actually be sent.

    Args:
        band: which of `_SMOKE_BANDS` to render. `moderate` is the original
            single payload, kept as the default so every caller that predates
            the other two keeps its meaning.
    """
    from backend.llm import langchain_llm

    evidence, articles = _SMOKE_BANDS[band]
    prompt = ai_constants.AI_PROMPT_V3.format(
        country="the country",
        as_of_date="2019-06-03",
        evidence_json=json.dumps(evidence, ensure_ascii=False),
        articles_json=json.dumps(articles, ensure_ascii=False),
        full_text_block="(no full-text articles supplied)",
    )
    # Including the rule blocks the variant appends, resolved by the same
    # function the scoring call uses. Without this the gate rendered the base
    # template under a prompt variant and reported that the candidate could hold
    # a schema the run would never send it -- harmless while a variant only
    # added a paragraph, and actively wrong once one changes the schema.
    rules, _ = langchain_llm._prompt_rules_and_version(evidence)
    return prompt + rules


# Reported but not gated. Everything outside this set — every score, every
# ledger, every flag, `evidence_coverage` and `news_article_scores` — must match
# byte-for-byte across repeats or the candidate fails.
#
# The split exists because the incumbent forced it. Measured over six repeats of
# one prompt at temperature 0 and seed 42, gpt-4o returns *identical* values for
# `score_12m`, `score_3m`, `ledger_scores`, `condition_flags`, `evidence_coverage`
# and `news_article_scores` — and varies these two. Gating on the whole payload
# therefore fails **gpt-4o itself**, and a gate the reference cannot pass
# disqualifies every candidate for a defect the reference shares. That is how a
# bake-off ends with no candidates and no finding.
#
#   `bullet_summary`     — free prose, reworded on essentially every repeat
#                          (6 distinct of 6). Displayed, never scored: nothing
#                          ranks on it, no ledger derives from it, `compare` never
#                          reads it.
#   `subscore_evidence`  — *which* evidence item is cited for a ledger, not what
#                          the ledger scored. Observed alternating between `a1`
#                          and `structural` for `information_capacity` while the
#                          ledger's own value never moved (2 distinct of 6).
#
# So the reproducibility claim is intact where it is actually made — the stored
# row and the manifest hashing what the model read. Prose and citation drift are
# still reported, as `exact_match_rate`, because a candidate that churns them far
# harder than the incumbent is worth seeing even though it is not disqualifying.
#   `band_placement`     — the elicitation arms' own prose, and prose on the same
#   `typical_week`         terms as `bullet_summary`: reworded between repeats
#                          while every number holds. Measured over three repeats
#                          under `vs-typical`, gpt-4o returned identical
#                          `score_12m`, `score_3m`, `evidence_coverage`,
#                          `delta_vs_typical`, `ledger_scores` and
#                          `condition_flags`, and two distinct `typical_week`
#                          paragraphs. Gating on those words would fail the
#                          variant for the defect the incumbent already has.
#
# `band` and `delta_vs_typical` are deliberately NOT here. They are the decisions
# the variants exist to force, not descriptions of one, and a variant whose band
# wanders between repeats while its score does not is a finding rather than
# noise.
_UNGATED_FIELDS: Tuple[str, ...] = ("bullet_summary", "subscore_evidence",
                                    "band_placement", "typical_week")


def _scored_only(payload: Dict[str, Any]) -> str:
    """The answer minus what is reported-but-not-gated, canonicalised."""
    return json.dumps({k: v for k, v in payload.items() if k not in _UNGATED_FIELDS},
                      sort_keys=True, default=str)


def smoke_schema() -> Dict[str, Any]:
    """The schema the run would actually send, variant included.

    The gate's whole claim is that it exercises the real contract rather than a
    toy one, and a hardcoded `RISK_SCHEMA_V3` quietly stopped being that the
    moment a prompt variant started asking for extra fields.
    """
    from backend.llm import langchain_llm

    return langchain_llm._SCHEMA_BY_PROMPT_VARIANT.get(
        provenance.prompt_variant(), ai_constants.RISK_SCHEMA_V3)


# --- what a grammar backend will not enforce --------------------------------

# Constraints a JSON-Schema-to-grammar compiler cannot express. A context-free
# grammar decides the *shape* of the token stream; it cannot count, compare or
# range-check, so every keyword below is silently dropped by llama.cpp's GBNF
# converter and by the guided-decoding backends vLLM ships (outlines, xgrammar).
# The request succeeds, the output parses, and the constraint was never applied.
#
# This is not a hypothesis about grammars in general. It is the reason
# `_validate_locally` runs on the `json_object` route *and* would need to run on
# a `guided_json` one: "the endpoint constrained the output" and "the output
# satisfies the schema" are different claims, and only the second is the
# contract. OpenAI's strict mode has the same hole -- it rejects a schema
# carrying some of these rather than enforcing them -- which is why the
# production wrapper is not evidence either.
_UNENFORCEABLE_BY_GRAMMAR = (
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern",
    "minItems", "maxItems", "uniqueItems",
    "minProperties", "maxProperties",
)


def grammar_risks(schema: Dict[str, Any], path: str = "$") -> List[str]:
    """Every constraint in `schema` a grammar-constrained endpoint will ignore.

    Read it before pointing the harness at a local model, and read the result as
    a list of things something *other than the endpoint* has to be responsible
    for -- not as a reason to change the schema.

    Two things are already true and are worth not rediscovering. First, this is
    not a local-model problem: LangChain forwards `minimum`, `maximum` and
    `maxLength` to OpenAI verbatim under `strict: true`, and they are not part
    of the enforced subset, so **production has the same hole**. Second, the run
    is already defended against it -- every score reaches storage through
    `langchain_llm._from_100`, which clamps to 0-1 and whose docstring says
    exactly why. So a candidate emitting `score_12m: 250` produces a stored 1.0
    rather than a 2.5.

    The gate is therefore *stricter* than the run: `_validate_locally` rejects
    what `_from_100` would quietly clamp. That is the right direction -- a model
    that answers 250 has misunderstood the scale, and a bake-off should say so
    rather than clamp it into looking fine -- but it is a difference, and a
    candidate failing the schema gate on a bound alone deserves the distinction
    noted rather than being read as "cannot hold the schema".

    The union types are reported separately and are a different worry. Rewriting
    the four `["integer", "null"]` nodes as `anyOf` -- mechanically equivalent
    JSON Schema -- destroyed determinism on `gpt-4o` (50x9 became 52x7, 50x2).
    So how a backend chooses to compile a union is not a detail, and two
    backends compiling it differently is a reason to re-measure determinism
    rather than to assume it transfers.
    """
    found: List[str] = []
    if not isinstance(schema, dict):
        return found
    for key in _UNENFORCEABLE_BY_GRAMMAR:
        if key in schema:
            found.append(f"{path}: {key}={schema[key]!r} is not enforced by a grammar")
    if isinstance(schema.get("type"), list):
        found.append(f"{path}: union type {schema['type']} compiles differently "
                     f"per backend; re-measure determinism, do not assume it")
    for name, child in (schema.get("properties") or {}).items():
        found.extend(grammar_risks(child, f"{path}.{name}"))
    if isinstance(schema.get("items"), dict):
        found.extend(grammar_risks(schema["items"], f"{path}[]"))
    for keyword in ("anyOf", "oneOf", "allOf"):
        for i, child in enumerate(schema.get(keyword) or []):
            found.extend(grammar_risks(child, f"{path}.{keyword}[{i}]"))
    return found


def _validate_locally(payload: Dict[str, Any], schema: Dict[str, Any]) -> None:
    """Raise if `payload` does not satisfy `schema`. The non-strict route's gate.

    An endpoint that cannot compile our schema into a grammar can still be asked
    for JSON and checked here. That is a *different instrument* -- nothing
    stopped the model emitting an invalid answer, we merely noticed -- so the
    verdict it produces is recorded under a different name.
    """
    import jsonschema

    jsonschema.validate(payload, schema)


def _answer_via_json_object(chat, prompt, schema) -> Dict[str, Any]:
    """One answer from an endpoint with no strict mode, validated here instead.

    `json_object` is the widest-supported structured-output mode: llama.cpp,
    vLLM and every third-party OpenAI-compatible server serve some form of it,
    and none of them serves OpenAI's `strict` flag. Round 2 measured DeepSeek
    and MiniMax exactly this way and the code was never committed, so the next
    endpoint without strict mode had to have it written again.
    """
    from langchain_core.messages import SystemMessage

    text = chat.invoke([SystemMessage(content=prompt)]).content
    if isinstance(text, list):  # some servers return content parts
        text = "".join(part.get("text", "") for part in text
                       if isinstance(part, dict))
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"no JSON object in the response: {text[:200]!r}")
    payload = json.loads(text[start:end + 1])
    _validate_locally(payload, schema)
    return payload


def smoke(name: str, repeats: int = 3,
          bands: Optional[Tuple[str, ...]] = None) -> Dict[str, Any]:
    """Schema, then determinism across three bands. No database.

    Args:
        repeats: samples per band. 3 is enough to see a drifter; 10 is what the
            published noise-floor matrix used.
        bands: which of `_SMOKE_BANDS` to run. Defaults to all three, because a
            noise floor measured on one payload is a noise floor for that band
            and the candidates do not behave alike across the range.

    Returns:
        ``{schema, determinism, cost}``. ``schema.passed`` False means the
        candidate is out — reported, not worked around. ``schema.route`` says
        *how* it held: `strict` is the production contract, `json_object` means
        the endpoint has no strict mode and the schema was enforced here
        instead, which is a weaker fact wearing the same word.
    """
    from langchain_core.messages import SystemMessage

    spec = CANDIDATES[name]
    bands = bands or tuple(_SMOKE_BANDS)
    schema = None
    out: Dict[str, Any] = {"candidate": name, "arm": spec["arm"],
                           "note": spec.get("note", "")}
    by_band: Dict[str, Dict[str, Any]] = {}

    with candidate_env(name) as env:
        from backend.util.pilot import score as pilot_score

        out["endpoint"] = {k: v for k, v in env.items() if not k.endswith("API_KEY")}
        # Read here, inside the environment. `_wrap` runs after this block has
        # exited and would stamp the process default -- which is how
        # `backend/bakeoff/US-2019/p3-context.json` came to record
        # `PROMPT_VERSION: v4.0-masked-production` while every row inside it says
        # v4.1-trailing-context.
        out["captured_under"] = pilot_score.versions()
        schema = smoke_schema()
        api_key = os.getenv("OPENAI_API_KEY") or ""
        route, error, sample = None, None, None
        started = time.time()

        with usage.meter(budget_usd=BAKEOFF_BUDGET_USD) as meter:
            for band in bands:
                prompt = smoke_prompt(band)
                answers: List[str] = []
                try:
                    if route in (None, "strict"):
                        chat = ai_client.build_chat(api_key).with_structured_output(
                            schema=schema, strict=True)
                        for _ in range(repeats):
                            result = chat.invoke([SystemMessage(content=prompt)])
                            answers.append(json.dumps(result, sort_keys=True,
                                                      default=str))
                        route = "strict"
                    else:
                        raise RuntimeError("strict mode already ruled out")
                except Exception as strict_exc:  # noqa: BLE001
                    # An endpoint without strict mode is not the same verdict as
                    # a model that cannot hold the schema, and the original gate
                    # could not tell them apart: both arrived as one broad
                    # `except` and both read FAIL. Try the wider route once, and
                    # say which one answered.
                    answers = []
                    try:
                        chat = ai_client.build_chat(api_key).bind(
                            response_format={"type": "json_object"})
                        for _ in range(repeats):
                            answers.append(json.dumps(
                                _answer_via_json_object(chat, prompt, schema),
                                sort_keys=True, default=str))
                        route = "json_object"
                        if error is None:
                            error = (f"strict mode unavailable, fell back: "
                                     f"{type(strict_exc).__name__}: {strict_exc}")
                    except Exception as loose_exc:  # noqa: BLE001
                        # Both routes gone. Every way this can fail — a 400 from
                        # a provider that serves neither mode, a validation error
                        # from one that serves it badly, a transport error — is
                        # the same verdict: this candidate cannot hold the schema
                        # on the endpoint we would ship.
                        route = route or "none"
                        error = (f"{type(strict_exc).__name__}: {strict_exc} "
                                 f"| json_object: {type(loose_exc).__name__}: "
                                 f"{loose_exc}")
                        by_band[band] = _band_result([])
                        break
                if sample is None and answers:
                    sample = json.loads(answers[0])
                by_band[band] = _band_result(answers)

        out["schema"] = {"passed": bool(sample) and route in ("strict", "json_object"),
                         "route": route, "error": error, "sample": sample}
        out["cost"] = {"calls": meter.calls, "spend_usd": round(meter.spend_usd, 6),
                       "input_tokens": meter.input_tokens,
                       "output_tokens": meter.output_tokens,
                       "cached_tokens": meter.cached_tokens,
                       "seconds": round(time.time() - started, 2),
                       # A model with no entry in `usage.PRICES_USD_PER_1M` is
                       # billed at the fallback rate, which is a real number
                       # standing in for one nobody measured. Say so here rather
                       # than let a local endpoint report a dollar figure.
                       "priced": usage.is_priced(ai_client.scoring_model())}

    out["determinism"] = _roll_up_bands(by_band)
    return out


def _moved_fields(parsed: List[Dict[str, Any]]) -> Dict[str, List[Any]]:
    """The gated fields that took more than one value across the repeats.

    Keyed by a dotted path so a ledger is named rather than the whole
    `ledger_scores` object: "the model wobbled on `edge_vitality`" and "the
    model returned a different risk score" are answers a reader needs kept
    apart, and one of them is a reason to stop.
    """
    moved: Dict[str, List[Any]] = {}
    for key in sorted({k for p in parsed for k in p} - set(_UNGATED_FIELDS)):
        values = [p.get(key) for p in parsed]
        if all(isinstance(v, dict) for v in values):
            for sub in sorted({k for v in values for k in v}):
                seen = [v.get(sub) for v in values]
                if len(set(map(repr, seen))) > 1:
                    moved[f"{key}.{sub}"] = sorted(set(seen), key=repr)
        elif len({json.dumps(v, sort_keys=True, default=str) for v in values}) > 1:
            moved[key] = values
    return moved


def _band_result(answers: List[str]) -> Dict[str, Any]:
    """One band's repeats, with the per-sample scores kept.

    The scores are stored rather than summarised because §10 asks a canary to
    record *what* moved, and `score_spread` cannot answer it. The published
    three-anchor matrix reported a worst spread of 20 points for `gpt-4.1-nano`
    and the draw behind it — 30, 35, 38, 38, 40, 30, 50, 40, 30, 40 — survived
    only as a sentence in a document.
    """
    if len(answers) < 2:
        return {"repeats": len(answers), "exact_match_rate": None,
                "scored_match_rate": None, "score_spread": None, "scores": [],
                "note": "not measured: the schema gate failed first"}
    parsed = [json.loads(a) for a in answers]
    identical = sum(1 for a in answers[1:] if a == answers[0])
    scored = [_scored_only(p) for p in parsed]
    scored_identical = sum(1 for s in scored[1:] if s == scored[0])
    scores = [p.get("score_12m") for p in parsed]
    numeric = [s for s in scores if isinstance(s, (int, float))]
    return {
        "repeats": len(answers),
        "exact_match_rate": round(identical / (len(answers) - 1), 3),
        # The rate that decides it. See `_UNGATED_FIELDS`.
        "scored_match_rate": round(scored_identical / (len(answers) - 1), 3),
        "scores": scores,
        "distinct_scored": len(set(scored)),
        # Which gated fields actually moved. "The scorer changed" and "the
        # scorer drifted by one point on one ledger" are different findings and
        # `deferred.md` §10 asks the canary to tell them apart; a match rate
        # cannot. Measured on the first run of this: gpt-4o holds `score_12m`
        # at 12 across ten calm repeats and still returns two distinct scored
        # payloads, so without this the divergence is visible and anonymous.
        "moved_fields": _moved_fields(parsed),
        # The number that survives a model reformatting its prose. Two runs can
        # differ in `bullet_summary` and agree perfectly on every score, which is
        # a far weaker failure than two runs that disagree about the risk.
        "score_spread": round(max(numeric) - min(numeric), 4) if numeric else None,
    }


def _roll_up_bands(by_band: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """The three bands as one verdict, worst-band-wins, with the bands kept.

    Worst rather than mean: a candidate steady on two payloads and wild on the
    third is wild, and averaging it to "mostly fine" is how `gpt-4.1-nano`'s
    20-point calm-payload swing would disappear behind two 5-point ones.
    """
    if not by_band:
        return {"repeats": 0, "exact_match_rate": None, "scored_match_rate": None,
                "score_spread": None, "scores": [], "by_band": {},
                "note": "not measured: the schema gate failed first"}
    measured = [b for b in by_band.values() if b.get("scored_match_rate") is not None]
    if not measured:
        worst = next(iter(by_band.values()))
        return {**worst, "by_band": by_band}
    spreads = [b["score_spread"] for b in measured if b["score_spread"] is not None]
    return {
        "repeats": max(b["repeats"] for b in measured),
        "bands": len(measured),
        "exact_match_rate": min(b["exact_match_rate"] for b in measured),
        "scored_match_rate": min(b["scored_match_rate"] for b in measured),
        "score_spread": max(spreads) if spreads else None,
        "scores": [s for b in measured for s in b["scores"]],
        "by_band": by_band,
    }


# --- capturing the incumbent ------------------------------------------------

def capture_baseline() -> Dict[str, Any]:
    """The gate-2 gpt-4o rows, read out of `risk_snapshot`. No model calls.

    The baseline is produced by the real gate-2 run through `pilot.run score`,
    which writes the production series. This only reads it back into the same
    file shape every candidate uses, so `compare` has one reader and the
    notebook has one format — and so the incumbent is not re-scored, which would
    make it a fourth candidate rather than the reference.
    """
    from backend.data_upsert import data_push

    with data_push._transaction() as cur:
        cur.execute("""
            SELECT as_of, score, score_3m, ledger_scores, condition_flags, lint,
                   model_id, prompt_version, input_manifest
              FROM risk_snapshot
             WHERE country_iso2 = %s AND scoring_mode = 'masked'
               AND as_of BETWEEN %s AND %s
             ORDER BY as_of
        """, (COUNTRY, SINCE, UNTIL))
        fetched = cur.fetchall()

    rows = []
    for (as_of, score, score_3m, ledgers, flags, lint,
         model_id, prompt_version, manifest) in fetched:
        rows.append({
            "as_of": as_of.isoformat(),
            "status": "complete",
            "llm_score": float(score) if score is not None else None,
            "score_3m": float(score_3m) if score_3m is not None else None,
            # Unwrapped, not copied. `risk_snapshot.ledger_scores` is a JSONB
            # holding *two* things — `{ledger_scores: {...}, subscore_evidence:
            # {...}}` — while the candidate arm returns the four scores flat,
            # straight off `llm_output`. Copying the column whole nests them one
            # level too deep, and then every ledger lookup misses.
            #
            # It failed silently, which is why it is called out here: `_paired`
            # drops a None rather than raising, so the comparison printed `n=0`
            # and an em dash for all four ledgers and looked like a metric nobody
            # had populated instead of a bug. The composite still matched, so the
            # report read as working. `.get("ledger_scores", ledgers)` rather than
            # a bare index so a future flat column does not start returning empty.
            "ledger_scores": (ledgers or {}).get("ledger_scores", ledgers) or {},
            "condition_flags": flags or {},
            "lint": lint or [],
            "model_id": model_id,
            "prompt_version": prompt_version,
            # Cost is not recoverable from `risk_snapshot` — it lives in the
            # ledger, per (country, as_of, mode). Left absent rather than zero:
            # a baseline reporting $0.00 a snapshot would make every candidate
            # look infinitely worse on the one axis they were chosen for.
            "spend_usd": None,
            "articles": len((manifest or {}).get("articles") or []),
        })
    # Stamped from the rows themselves, not from `MODEL_NAME`. The literal would
    # say `gpt-4o-2024-08-06` no matter what actually wrote those rows — including
    # when `SCORING_MODEL` is set in the reader's environment, which is exactly the
    # case a bake-off creates. The rows know who scored them; ask them.
    #
    # More than one distinct id is not an error to smooth over. A reference
    # assembled from two scorers is not a reference, and recording the list is how
    # that becomes visible instead of being averaged into a plausible single name.
    scored_by = sorted({r["model_id"] for r in rows if r["model_id"]})
    return _wrap("gpt-4o", "scoring", rows,
                 endpoint={"SCORING_MODEL": scored_by[0] if len(scored_by) == 1
                           else (scored_by or ai_client.scoring_model())})


def _wrap(name: str, arm: str, rows: List[Dict[str, Any]],
          endpoint: Optional[Dict[str, Any]] = None,
          gates: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One candidate's file, stamped with what produced it.

    The stamp is the load-bearing part, for the same reason `reports.summary`
    says so: two score series are not comparable unless they were measured under
    the same masking, prompt and payload versions, and a file that does not say
    which it used is a number without units. `versions()` now carries the model
    too, which is the whole point of this exercise.
    """
    from backend.util.pilot import score as pilot_score

    return {
        "candidate": name,
        "arm": arm,
        "note": CANDIDATES.get(name, {}).get("note", ""),
        "country": COUNTRY,
        "since": SINCE.isoformat(),
        "until": UNTIL.isoformat(),
        "captured_under": pilot_score.versions(),
        "endpoint": endpoint or {},
        "gates": gates or {},
        "rows": rows,
        "cost": cost_summary(rows),
    }


# --- scoring a candidate ----------------------------------------------------

def score_anchors(name: str, budget_usd: float = BAKEOFF_BUDGET_USD,
                  limit: Optional[int] = None) -> Dict[str, Any]:
    """Run the 52 anchors through one candidate. Writes no snapshot row anywhere.

    Deliberately not `pilot.score.score_one`. That writes a `run_ledger` row
    keyed (job_type, country_iso2, as_of, variant) and would overwrite gate 2's
    ledger on its own primary key, and `risk_snapshot.scoring_mode` admits only
    two values, so there is no third variant to write into without a schema
    change. This calls the live path with ``upsert=False`` — the same switch the
    diagnostic arms already use — so the candidate is scored by production code
    and lands in a file.

    The digest cache is shared on purpose. It is keyed on the digest model, so
    every *scoring* candidate reads the identical digests gpt-4o read, which is
    what isolates the scorer as the only variable; a *digest* candidate keys
    differently and misses cleanly. Neither can serve one model's output under
    another model's name — which was true of the rewrite cache until this branch
    fixed it.
    """
    from backend.data_upsert import store
    from backend.news_fetching import snapshot_select
    from backend.util import pipeline
    from backend.util.pilot import score as pilot_score

    spec = CANDIDATES[name]
    dates = pilot_score.anchors(SINCE, UNTIL)
    if limit:
        dates = dates[:limit]
    country_name = config.country_name(COUNTRY)
    rows: List[Dict[str, Any]] = []
    spent = 0.0

    with candidate_env(name) as env:
        endpoint = {k: v for k, v in env.items() if not k.endswith("API_KEY")}
        logger.info("[bakeoff] %s over %d anchors at %s", name, len(dates),
                    endpoint or "the OpenAI default")
        api_key = os.getenv("OPENAI_API_KEY") or ""
        if not api_key:
            raise MissingKey(
                "OPENAI_API_KEY is still required: the masking passes stay on "
                "gpt-4o-mini whatever the candidate is")

        for as_of in dates:
            items = snapshot_select.select(COUNTRY, as_of)
            if not items:
                # A real answer for a thin week, not an error — `score_one` makes
                # the same call. Recorded so the comparison can say the anchor
                # was reached and had nothing in it, which is different from an
                # anchor that failed and different again from one never run.
                rows.append({"as_of": as_of.isoformat(), "status": "empty",
                             "llm_score": None, "articles": 0})
                continue

            started = time.time()
            hour = datetime.datetime.now(datetime.timezone.utc).hour
            with usage.meter(budget_usd=budget_usd, already_spent_usd=spent) as meter:
                try:
                    out, manifest = pipeline._process_country(
                        country_name, COUNTRY, [], as_of=as_of, items=items,
                        scoring_mode="masked",
                        upsert=False,
                        digest_content_cache=store)
                    # A returned dict is not a scored anchor. The pipeline
                    # degrades an API failure into an empty result rather than
                    # raising -- correct for the daily run, where one country
                    # must not end the pass -- so an exhausted API key produced
                    # 47 rows marked `complete` with `llm_score: null`,
                    # `error: null` and `calls: 0`, and the arm reported itself
                    # finished. An arm that scored nothing must not look like an
                    # arm that scored zero.
                    if out.get("score") is None:
                        status = "unscored"
                        error = "the pipeline returned no score; see the run log"
                    else:
                        status, error = "complete", None
                except Exception as exc:  # noqa: BLE001
                    logger.exception("[bakeoff] %s %s failed", name, as_of)
                    out, manifest, status = {}, {}, "failed"
                    error = f"{type(exc).__name__}: {exc}"

            spent += meter.spend_usd
            model_id = out.get("model_id") or ""
            rows.append({
                "as_of": as_of.isoformat(),
                "status": status,
                "error": error,
                "llm_score": out.get("score"),
                "score_3m": out.get("score_3m"),
                # Recorded because a criterion needed it and it was not here.
                # `docs/payload-ab.md` attempt 2 pre-registered (d) as "share of
                # bullet_summary outputs referencing direction" -- a diagnostic
                # for whether a block was read at all, which is the thing p3
                # could not tell -- and the arm rows carried every number and
                # not one word of prose, so the criterion was unmeasurable on
                # the run it was written for. Pre-registering against a field
                # the harness does not store is the same class of mistake as
                # writing a value nothing reads.
                "bullet_summary": out.get("bullet_summary"),
                # What the anchor was actually scored on, as opposed to which
                # contract named it. Two arms whose rows carry different
                # fingerprints saw different evidence, and `compare_one` refuses
                # to put them in the same table. Without this the reference and
                # the candidate both said `PAYLOAD_VERSION: p2` across the
                # vintage fix and nothing could tell them apart.
                "payload_fingerprint": ((manifest.get("payload_health") or {})
                                        .get("indicators") or {}).get("fingerprint"),
                "ledger_scores": out.get("ledger_scores") or {},
                "condition_flags": out.get("condition_flags") or {},
                "lint": manifest.get("lint") or [],
                "model_id": model_id,
                # Which prompt actually rendered. Under p3 this moves to
                # v4.1-trailing-context because the rule is appended only when
                # the payload carries the block — so the row records whether the
                # model was told how to read it, not merely that it was sent.
                "prompt_version": out.get("prompt_version"),
                "spend_usd": round(meter.spend_usd, 6),
                "offpeak_usd": usage.offpeak_price(
                    model_id, meter.input_tokens, meter.output_tokens,
                    meter.cached_tokens),
                "input_tokens": meter.input_tokens,
                "output_tokens": meter.output_tokens,
                # None, not 0, when nothing was metered at all: "did not say"
                # and "nothing hit" are different answers.
                "cached_tokens": meter.cached_tokens if meter.calls else None,
                "calls": meter.calls,
                "seconds": round(time.time() - started, 2),
                "utc_hour": hour,
                "articles": len(items),
                # Empty except on the two elicitation arms, where it holds the
                # decision the variant exists to force -- the band and its
                # placement, or the baseline and the departure from it. Without
                # it a variant that quietly ignored the instruction and a
                # variant that followed it and gained nothing look identical in
                # the result file, which is the distinction the whole arm is
                # for.
                "elicitation": out.get("elicitation") or {},
            })
            logger.info("[bakeoff] %s %s %s score=%s $%.4f (running $%.2f)",
                        name, as_of, status, out.get("score"),
                        meter.spend_usd, spent)

            if spent > budget_usd:
                logger.error("[bakeoff] budget $%.2f passed at %s; stopping with "
                             "%d anchor(s) scored", budget_usd, as_of, len(rows))
                break

        payload = _wrap(name, spec["arm"], rows, endpoint=endpoint)
    return payload


# --- reading it back --------------------------------------------------------

def compare_all() -> Dict[str, Any]:
    """Every candidate file against the baseline file. Reads nothing else.

    Raises:
        FileNotFoundError: the baseline has not been captured. Named rather
            than tolerated, because a comparison with no reference is three
            candidates agreeing with each other about a question nobody asked.
    """
    baseline = load("gpt-4o")
    if baseline is None:
        raise FileNotFoundError(
            f"{result_path('gpt-4o')} does not exist. Run gate 2 first "
            f"(`python -m backend.util.pilot.run score --country PT "
            f"--since 2019-01-01 --until 2019-12-31 --approved`), then "
            f"`python -m backend.util.tools.bakeoff capture-baseline`.")

    out: Dict[str, Any] = {"baseline": baseline, "candidates": {}, "missing": []}
    for name, spec in CANDIDATES.items():
        # `local-template` is the shape a local candidate takes, not one anybody
        # screens. Listing it under "not run" every time trains the reader to
        # skip that line, which is the line that says a real candidate is
        # missing.
        if name == "gpt-4o" or spec.get("template"):
            continue
        found = load(name)
        if found is None:
            out["missing"].append(name)
            continue
        out["candidates"][name] = {
            "file": found,
            "comparison": compare_one(baseline["rows"], found["rows"]),
        }
    return out


def render(result: Optional[Dict[str, Any]] = None) -> None:
    """The comparison, printed. The bake-off's deliverable in a terminal.

    Ordered the way the decision is actually made and not the way it is asked
    about: the gates first, because a candidate that cannot hold the schema is
    out whatever it costs; rank correlation second, because a reordering cannot
    be recalibrated away; level and bands third; cost last. A cost table read
    before the gates is how a cheap model that fails both of them gets adopted.
    """
    result = result or compare_all()
    baseline = result["baseline"]
    fmt = reports._fmt

    print("\n=== 0. the reference ===")
    print(f"  {baseline['candidate']} on {baseline['country']} "
          f"{baseline['since']}..{baseline['until']}, "
          f"{len(baseline['rows'])} anchor(s)")
    for field in ("SCORING_MODEL", "DIGEST_MODEL", "SEED", "PROMPT_VERSION",
                  "PAYLOAD_VERSION"):
        print(f"    {field:<16} {baseline['captured_under'].get(field)}")
    if result["missing"]:
        print(f"  not run: {', '.join(result['missing'])}")

    for name, entry in result["candidates"].items():
        found, cmp = entry["file"], entry["comparison"]
        gates = found.get("gates") or {}
        print(f"\n=== {name} — {found.get('note', '')} ===")

        print("  1. gates  (a failure here ends it; the numbers below are context)")
        schema = (gates.get("schema") or {})
        determinism = (gates.get("determinism") or {})
        route = schema.get("route")
        print(f"     strict schema  {_verdict(schema.get('passed'))}"
              f"  via {route or '—'}"
              f"{'  ' + str(schema.get('error'))[:70] if schema.get('error') else ''}")
        if route == "json_object":
            # Said out loud rather than left in a field: this candidate did not
            # hold the production contract. It produced valid JSON and we
            # checked it here, which is a different instrument.
            print("                    (no strict mode on this endpoint; the "
                  "schema was enforced locally)")
        rate = determinism.get("scored_match_rate")
        print(f"     determinism    {_verdict(rate == 1.0 if rate is not None else None)}"
              f"  scored-match={fmt(rate)}  whole-payload="
              f"{fmt(determinism.get('exact_match_rate'))}  worst spread="
              f"{fmt(determinism.get('score_spread'))}"
              f"  over {determinism.get('bands') or 0} band(s)")
        for band, result in (determinism.get("by_band") or {}).items():
            print(f"       {band:<10} spread={fmt(result.get('score_spread'))}"
                  f"  scores={result.get('scores')}")

        print("  2. rank correlation  (the meter: reordering cannot be recalibrated)")
        print(f"     {'metric':<22} {'n':>4} {'spearman':>9} {'kendall':>9} "
              f"{'signed':>9} {'|shift|':>9} {'max|d|':>8}")
        for metric in METRICS:
            row = cmp["metrics"][metric]
            print(f"     {metric:<22} {row['n']:>4} {fmt(row['spearman']):>9} "
                  f"{fmt(row['kendall']):>9} {fmt(row['signed_mean']):>9} "
                  f"{fmt(row['abs_mean']):>9} {fmt(row['max_abs']):>8}")

        floor = noise_floor(determinism.get("score_spread"))
        if floor["share_of_divergence"] is not None:
            print(f"     noise floor: same input varies {floor['spread_points']:g} pt "
                  f"= {floor['spread_0_1']} on the stored scale = "
                  f"{floor['share_of_divergence']:.0%} of PT's {PT_MASKING_DIVERGENCE} "
                  f"masking divergence")
            print("       (a correlation cannot be read as disagreement below this; "
                  "it is the candidate arguing with itself)")
        else:
            print("     noise floor: not measured")

        sh = cmp.get("shape") or {}
        if sh:
            print("  2b. series shape  (per arm; a payload change should move "
                  "these, a scorer change should not)")
            print(f"     {'arm':<10} {'distinct':>9} {'lag1 ac':>9} "
                  f"{'longest run':>12} {'round share':>12}")
            for arm in ("baseline", "candidate"):
                row = sh.get(arm) or {}
                print(f"     {arm:<10} {row.get('distinct', '—'):>9} "
                      f"{fmt(row.get('lag1_autocorr')):>9} "
                      f"{row.get('longest_run') or '—':>12} "
                      f"{fmt(row.get('round_share')):>12}")

        print("  3. band migration  (rows = baseline, columns = candidate; "
              "diagonal is offset, scatter is disagreement)")
        labels = [b[0] for b in BANDS]
        print("     " + " " * 14 + "".join(f"{c[:9]:>10}" for c in labels))
        for row_label in labels:
            counts = cmp["band_matrix"][row_label]
            print(f"     {row_label:<14}" + "".join(f"{counts[c]:>10}" for c in labels))

        print("  4. observation-only flags  (agreement; these never touch a score)")
        for flag, row in cmp["flags"].items():
            print(f"     {flag:<26} n={row['n']:>3}  agreement={fmt(row['agreement'])}")

        print("  5. lint tripwires  (advisory; a rule firing on one side is a "
              "threshold tuned to the incumbent)")
        if not cmp["lint"]:
            print("     none fired on either side")
        for rule, row in cmp["lint"].items():
            print(f"     {rule:<36} baseline={row['baseline']:>3}  "
                  f"candidate={row['candidate']:>3}")

        cost = cmp["cost"]
        print("  6. cost")
        if not cost.get("snapshots"):
            print("     nothing completed")
        else:
            at_scale = projection(cost["per_snapshot_usd"])
            print(f"     ${cost['per_snapshot_usd']:.6f}/snapshot over "
                  f"{cost['snapshots']} complete, ${cost['spend_usd']:.4f} total")
            if cost.get("offpeak_per_snapshot_usd") is not None:
                off = projection(cost["offpeak_per_snapshot_usd"])
                print(f"     ${cost['offpeak_per_snapshot_usd']:.6f}/snapshot "
                      f"off-peak (pilot ${off['pilot_usd']}, "
                      f"backfill ${off['backfill_usd']})")
            print(f"     tokens/snapshot in={cost['input_tokens_per_snapshot']} "
                  f"out={cost['output_tokens_per_snapshot']}  "
                  f"cache share={fmt(cost['cache_share'])}")
            print(f"     scorer-only at scale: pilot ${at_scale['pilot_usd']}, "
                  f"48-country backfill ${at_scale['backfill_usd']} "
                  f"(digests on top)")
            print(f"     ran in UTC hour(s) {cost['utc_hours']}; DeepSeek peak is "
                  f"{sorted(usage.DEEPSEEK_PEAK_HOURS_UTC)}")

        if cmp["only_baseline"] or cmp["only_candidate"]:
            print(f"  7. unmatched anchors: {len(cmp['only_baseline'])} baseline-only, "
                  f"{len(cmp['only_candidate'])} candidate-only")


def _verdict(passed: Optional[bool]) -> str:
    """PASS / FAIL / an em dash for not measured. Never a blank."""
    return "—    " if passed is None else ("PASS " if passed else "FAIL ")


# --- the CLI ----------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("smoke", help="the two hard gates, before any real spend")
    p.add_argument("candidate", choices=sorted(CANDIDATES))
    p.add_argument("--repeats", type=int, default=3,
                   help="determinism repeats per band; 3 is enough to see a "
                        "drifter, 10 is what the published noise floor used")
    p.add_argument("--bands", default=",".join(_SMOKE_BANDS),
                   help="comma-separated subset of calm,moderate,stressed. All "
                        "three by default -- a noise floor measured on one "
                        "payload is a noise floor for that band. Total calls is "
                        "repeats x bands, so the default is 9")

    sub.add_parser("capture-baseline",
                   help="read gate 2's gpt-4o rows out of risk_snapshot")

    p = sub.add_parser("score", help="run the 52 anchors through one candidate")
    p.add_argument("candidate", choices=sorted(n for n in CANDIDATES if n != "gpt-4o"))
    p.add_argument("--budget", type=float, default=BAKEOFF_BUDGET_USD)
    p.add_argument("--limit", type=int,
                   help="stop after N anchors; for a cheap first look")

    sub.add_parser("compare", help="every candidate file against the baseline")

    # Global rather than per-subcommand, because every subcommand has to agree
    # about which window it is working on — a capture in one window and a score
    # in another would compare two country-years and say nothing about either.
    for name in ("smoke", "capture-baseline", "score", "compare"):
        sp = sub.choices[name]
        sp.add_argument("--country", default=COUNTRY,
                        help=f"ISO2 of the window to work on (default {COUNTRY})")
        sp.add_argument("--since", default=SINCE.isoformat())
        sp.add_argument("--until", default=UNTIL.isoformat())

    args = parser.parse_args()
    set_window(args.country,
               datetime.date.fromisoformat(args.since),
               datetime.date.fromisoformat(args.until))
    logger.info("[bakeoff] window %s (%s..%s) -> %s",
                window_slug(), SINCE, UNTIL, RESULTS_DIR / window_slug())

    if args.command == "smoke":
        bands = tuple(b.strip() for b in args.bands.split(",") if b.strip())
        unknown = [b for b in bands if b not in _SMOKE_BANDS]
        if unknown:
            parser.error(f"unknown band(s) {unknown}; "
                         f"choose from {sorted(_SMOKE_BANDS)}")
        result = smoke(args.candidate, repeats=args.repeats, bands=bands)
        # Merged into the candidate's file rather than printed and lost. The
        # notebook reads the gates from there, and a gate result that lives only
        # in a terminal is the failure this repo has already made twice.
        #
        # Into *every* window the candidate has a file in, not just the current
        # one: the gate is measured on a canned payload with no country in it, so
        # filing it under one window was what left 24 of 26 files claiming the
        # gate had never been run.
        gates = {k: result[k] for k in ("schema", "determinism", "cost")}
        paths = save_gates(args.candidate, gates)
        for path in paths:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["endpoint"] = (result.get("endpoint")
                                   or payload.get("endpoint") or {})
            # Right for a file this run creates, and refused by `_write` for one
            # that already carries a stamp — this branch is the writer that
            # restamped the two reference arms in `b128aad`.
            if result.get("captured_under"):
                payload["captured_under"] = result["captured_under"]
            _write(path, payload)
        print(json.dumps(result, indent=2, default=str)[:4000])
        print("\nwrote " + ", ".join(str(p) for p in paths))
        return

    if args.command == "capture-baseline":
        payload = capture_baseline()
        if not payload["rows"]:
            print(f"risk_snapshot holds no masked {COUNTRY} "
                  f"{SINCE}..{UNTIL} rows. The reference has not been scored — "
                  f"capture nothing rather than an empty baseline.")
            return
        print(f"wrote {save('gpt-4o', payload)} — {len(payload['rows'])} anchor(s)")
        return

    if args.command == "score":
        payload = score_anchors(args.candidate, budget_usd=args.budget,
                                limit=args.limit)
        # The gates already measured for this candidate survive because `save`
        # carries them forward, not because this branch remembers to. That used
        # to live here, which is why every writer that was not this branch lost
        # them.
        print(f"wrote {save(args.candidate, payload)}")
        render()
        return

    render()


if __name__ == "__main__":
    main()
