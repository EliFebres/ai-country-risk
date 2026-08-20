"""Which scorer, measured against a fixed reference rather than against a price list.

A cheaper model is not a cheaper instrument. The pilot's whole claim is that
every row in a ten-year series was produced by one scorer under one prompt, so
changing the scorer is not a procurement decision that happens to touch the
code — it is an instrument change, and the only honest way to make one is to
re-run a fixed set of anchors through both and look at what moved.

The fixed set is gate 2: PT, 2019, weekly Mondays, 52 anchors. Small enough to
cost a few dollars, long enough that a rank correlation means something, and
already the dry run this repo has been pointing at.

**Rank correlation is the meter.** A constant level offset is survivable — the
calibration anchors in the prompt can be moved and the whole series shifts with
them. Reordering is not: it means the candidate disagrees about which weeks were
risky, and no amount of recalibration fixes disagreement about the ordering. So
Spearman and Kendall are reported first and the mean shift second, which is the
opposite of the order anybody asks the question in.

Nothing here writes `risk_snapshot` and nothing writes a `run_ledger` row. The
scoring arm calls the live path with `upsert=False`, which is the same switch
the two diagnostic arms already use, so a candidate cannot overwrite the
baseline it is being compared against.

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

import pandas as pd  # noqa: E402

from backend.llm import client as ai_client  # noqa: E402
from backend.llm import constants as ai_constants  # noqa: E402
from backend.llm import usage  # noqa: E402
from backend.util import config  # noqa: E402
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
COUNTRY = "PT"
SINCE = datetime.date(2019, 1, 1)
UNTIL = datetime.date(2019, 12, 31)

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

# The prompt's own bands, from `ai_constants.AI_PROMPT_V3`. Lower bound
# inclusive; the tails are open because the model may return 0-4 or 99-100 and a
# score outside every band is not a band of its own.
BANDS: Tuple[Tuple[str, float], ...] = (
    ("Low", 0.0), ("Low-Moderate", 20.0), ("Moderate", 40.0),
    ("High", 75.0), ("Extreme", 90.0),
)

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
    "minimax-m3": {
        "arm": "scoring",
        "note": "primary candidate, <=512K tier",
        "env": {"SCORING_MODEL": "MiniMax-M3",
                "SCORING_BASE_URL": "https://api.minimax.io/v1"},
        "key_env": "MINIMAX_API_KEY",
        "key_target": "SCORING_API_KEY",
    },
    "deepseek-v4-pro": {
        "arm": "scoring",
        "note": "alternative; thinking pinned off, reasoning tokens bill as output",
        "env": {"SCORING_MODEL": "deepseek-v4-pro",
                "SCORING_BASE_URL": "https://api.deepseek.com/v1",
                "SCORING_EXTRA_BODY": '{"thinking": {"type": "disabled"}}'},
        "key_env": "DEEPSEEK_API_KEY",
        "key_target": "SCORING_API_KEY",
    },
    "deepseek-v4-flash": {
        "arm": "digest",
        "note": "stage-1 candidate; thinking pinned off",
        "env": {"DIGEST_MODEL": "deepseek-v4-flash",
                "DIGEST_BASE_URL": "https://api.deepseek.com/v1",
                "DIGEST_EXTRA_BODY": '{"thinking": {"type": "disabled"}}'},
        "key_env": "DEEPSEEK_API_KEY",
        "key_target": "DIGEST_API_KEY",
    },
    "gpt-oss-120b": {
        "arm": "digest",
        "note": "stage-1 candidate, US-hosted, no residency question",
        "env": {"DIGEST_MODEL": "openai/gpt-oss-120b",
                "DIGEST_BASE_URL": "https://api.groq.com/openai/v1"},
        "key_env": "GROQ_API_KEY",
        "key_target": "DIGEST_API_KEY",
    },
}


class MissingKey(RuntimeError):
    """A candidate's vendor key is not in the environment. Nothing was spent."""


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
               "DIGEST_API_KEY", "DIGEST_EXTRA_BODY"]
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

    return {
        "n_baseline": len(baseline_rows),
        "n_candidate": len(candidate_rows),
        "only_baseline": only_baseline,
        "only_candidate": only_candidate,
        "metrics": metrics,
        "band_matrix": band_matrix(_series(baseline_rows, "llm_score"),
                                   _series(candidate_rows, "llm_score")),
        "flags": flag_agreement(
            {r["as_of"]: r.get("condition_flags") or {} for r in baseline_rows},
            {r["as_of"]: r.get("condition_flags") or {} for r in candidate_rows}),
        "lint": _lint_rates(baseline_rows, candidate_rows),
        "cost": cost_summary(candidate_rows),
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
    spend = sum(r.get("spend_usd") or 0.0 for r in done)
    inputs = sum(r.get("input_tokens") or 0 for r in done)
    outputs = sum(r.get("output_tokens") or 0 for r in done)
    reported = [r for r in done if r.get("cached_tokens") is not None]
    cached = sum(r.get("cached_tokens") or 0 for r in reported)
    offpeak = [r["offpeak_usd"] for r in done if r.get("offpeak_usd") is not None]
    return {
        "snapshots": len(done),
        "spend_usd": round(spend, 4),
        "per_snapshot_usd": round(spend / len(done), 6),
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


# --- reading and writing the result files -----------------------------------

def result_path(name: str) -> pathlib.Path:
    return RESULTS_DIR / f"{name}.json"


def load(name: str) -> Optional[Dict[str, Any]]:
    """One candidate's file, or None if it was never run."""
    path = result_path(name)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save(name: str, payload: Dict[str, Any]) -> pathlib.Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = result_path(name)
    path.write_text(json.dumps(payload, indent=2, default=str, sort_keys=True),
                    encoding="utf-8")
    return path


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


def smoke_prompt() -> str:
    """The real prompt, on canned evidence. Not a toy schema and not a toy prompt.

    A candidate that satisfies a three-field schema says nothing about one that
    has to satisfy `RISK_SCHEMA_V3` — ten required fields, two nested arrays and
    `additionalProperties: false` at every level. So the gate runs the thing
    that will actually be sent.
    """
    return ai_constants.AI_PROMPT_V3.format(
        country="the country",
        as_of_date="2019-06-03",
        evidence_json=json.dumps(_SMOKE_EVIDENCE, ensure_ascii=False),
        articles_json=json.dumps(_SMOKE_ARTICLES, ensure_ascii=False),
        full_text_block="(no full-text articles supplied)",
    )


def smoke(name: str, repeats: int = 3) -> Dict[str, Any]:
    """Strict schema, then determinism. Four calls, cents, no database.

    Returns:
        ``{schema, determinism, cost}``. ``schema.passed`` False means the
        candidate is out — reported, not worked around.
    """
    from langchain_core.messages import SystemMessage

    spec = CANDIDATES[name]
    prompt = smoke_prompt()
    out: Dict[str, Any] = {"candidate": name, "arm": spec["arm"],
                           "note": spec.get("note", "")}

    with candidate_env(name) as env:
        out["endpoint"] = {k: v for k, v in env.items() if not k.endswith("API_KEY")}
        api_key = os.getenv("OPENAI_API_KEY") or ""
        answers: List[str] = []
        with usage.meter(budget_usd=BAKEOFF_BUDGET_USD) as meter:
            try:
                chat = ai_client.build_chat(api_key).with_structured_output(
                    schema=ai_constants.RISK_SCHEMA_V3, strict=True)
                for _ in range(repeats):
                    result = chat.invoke([SystemMessage(content=prompt)])
                    answers.append(json.dumps(result, sort_keys=True, default=str))
                out["schema"] = {"passed": True, "error": None,
                                 "sample": json.loads(answers[0])}
            except Exception as exc:  # noqa: BLE001
                # Deliberately broad. Every way this can fail — a 400 from a
                # provider that does not serve strict json_schema, a validation
                # error from one that serves it badly, a transport error — is
                # the same verdict: this candidate cannot hold the schema on the
                # endpoint we would ship. Which one it was goes in `error`.
                out["schema"] = {"passed": False, "error": f"{type(exc).__name__}: {exc}",
                                 "sample": None}

        out["cost"] = {"calls": meter.calls, "spend_usd": round(meter.spend_usd, 6),
                       "input_tokens": meter.input_tokens,
                       "output_tokens": meter.output_tokens,
                       "cached_tokens": meter.cached_tokens}

    if len(answers) < 2:
        out["determinism"] = {"repeats": len(answers), "exact_match_rate": None,
                              "score_spread": None,
                              "note": "not measured: the schema gate failed first"}
        return out

    identical = sum(1 for a in answers[1:] if a == answers[0])
    scores = [json.loads(a).get("score_12m") for a in answers]
    numeric = [s for s in scores if isinstance(s, (int, float))]
    out["determinism"] = {
        "repeats": len(answers),
        "exact_match_rate": round(identical / (len(answers) - 1), 3),
        "scores": scores,
        # The number that survives a model reformatting its prose. Two runs can
        # differ in `bullet_summary` and agree perfectly on every score, which is
        # a far weaker failure than two runs that disagree about the risk.
        "score_spread": round(max(numeric) - min(numeric), 4) if numeric else None,
    }
    return out


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
            "ledger_scores": ledgers or {},
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
    return _wrap("gpt-4o", "scoring", rows,
                 endpoint={"SCORING_MODEL": ai_client.MODEL_NAME})


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
                "ledger_scores": out.get("ledger_scores") or {},
                "condition_flags": out.get("condition_flags") or {},
                "lint": manifest.get("lint") or [],
                "model_id": model_id,
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
    for name in CANDIDATES:
        if name == "gpt-4o":
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
    for field in ("SCORING_MODEL", "DIGEST_MODEL", "SEED", "PROMPT_VERSION"):
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
        print(f"     strict schema  {_verdict(schema.get('passed'))}"
              f"{'  ' + str(schema.get('error'))[:70] if schema.get('error') else ''}")
        rate = determinism.get("exact_match_rate")
        print(f"     determinism    {_verdict(rate == 1.0 if rate is not None else None)}"
              f"  exact-match={fmt(rate)}  score spread="
              f"{fmt(determinism.get('score_spread'))}")

        print("  2. rank correlation  (the meter: reordering cannot be recalibrated)")
        print(f"     {'metric':<22} {'n':>4} {'spearman':>9} {'kendall':>9} "
              f"{'signed':>9} {'|shift|':>9} {'max|d|':>8}")
        for metric in METRICS:
            row = cmp["metrics"][metric]
            print(f"     {metric:<22} {row['n']:>4} {fmt(row['spearman']):>9} "
                  f"{fmt(row['kendall']):>9} {fmt(row['signed_mean']):>9} "
                  f"{fmt(row['abs_mean']):>9} {fmt(row['max_abs']):>8}")

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
                   help="determinism repeats; 3 is enough to see a drifter")

    sub.add_parser("capture-baseline",
                   help="read gate 2's gpt-4o rows out of risk_snapshot")

    p = sub.add_parser("score", help="run the 52 anchors through one candidate")
    p.add_argument("candidate", choices=sorted(n for n in CANDIDATES if n != "gpt-4o"))
    p.add_argument("--budget", type=float, default=BAKEOFF_BUDGET_USD)
    p.add_argument("--limit", type=int,
                   help="stop after N anchors; for a cheap first look")

    sub.add_parser("compare", help="every candidate file against the baseline")

    args = parser.parse_args()

    if args.command == "smoke":
        result = smoke(args.candidate, repeats=args.repeats)
        # Merged into the candidate's file rather than printed and lost. The
        # notebook reads the gates from there, and a gate result that lives only
        # in a terminal is the failure this repo has already made twice.
        existing = load(args.candidate) or _wrap(
            args.candidate, CANDIDATES[args.candidate]["arm"], [])
        existing["gates"] = {k: result[k] for k in ("schema", "determinism", "cost")}
        existing["endpoint"] = result.get("endpoint") or existing.get("endpoint") or {}
        print(json.dumps(result, indent=2, default=str)[:4000])
        print(f"\nwrote {save(args.candidate, existing)}")
        return

    if args.command == "capture-baseline":
        payload = capture_baseline()
        if not payload["rows"]:
            print("risk_snapshot holds no masked PT 2019 rows. Gate 2 has not "
                  "run — capture nothing rather than an empty baseline.")
            return
        print(f"wrote {save('gpt-4o', payload)} — {len(payload['rows'])} anchor(s)")
        return

    if args.command == "score":
        payload = score_anchors(args.candidate, budget_usd=args.budget,
                                limit=args.limit)
        # Keep any gates already measured for this candidate: the smoke run and
        # the scoring run are two commands writing one file, and losing the
        # gates on the second would leave a cost table with nothing above it.
        existing = load(args.candidate) or {}
        if existing.get("gates"):
            payload["gates"] = existing["gates"]
        print(f"wrote {save(args.candidate, payload)}")
        render()
        return

    render()


if __name__ == "__main__":
    main()
