"""Asking a model to name the country it was not told.

The masked run's entire claim is that the scorer judged a country it could not
identify. This measures whether that is true, on the same bundles the scorer
saw, with the cheap model.

What the number means needs stating before it is read, because the obvious
reading is wrong. A low guess rate does not prove masking works; it proves
masking worked *on this corpus*. And a high guess rate is not automatically a
failure — the United States is going to be identified nearly every time, from
the size of the numbers, the institutions, the sheer volume of coverage, and
there is no gazetteer that fixes that. That is why it is in the roster: it
calibrates the ceiling. The meter to read is the *spread* between the US and
the rest, not any single country's rate.

The probe never sees the named bundle, so it cannot be scored against its own
answer key by accident.
"""

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

from backend.utils.ai import client as ai_client
from backend.utils.history import config

logger = logging.getLogger(__name__)

_PROBE_SCHEMA = {
    # Same omission as the rewrite schema had, and worse here: the probe fails
    # *open*, recording a failed call as "no guess", so a nameless schema would
    # have reported perfect masking on every bundle it never actually read.
    "title": "CountryGuess",
    "type": "object",
    "properties": {
        "country": {
            "type": "string",
            "description": "ISO 3166-1 alpha-2 code of the most likely country, "
                           "or 'ZZ' if there is genuinely no way to tell.",
        },
        "confidence": {
            "type": "number",
            "description": "0.0 to 1.0.",
        },
        "alternatives": {
            "type": "array",
            "description": "The three most likely countries with probabilities "
                           "summing to at most 1.0, most likely first. The "
                           "first entry must match `country`.",
            "items": {
                "type": "object",
                "properties": {
                    "country": {"type": "string"},
                    "probability": {"type": "number"},
                },
                "required": ["country", "probability"],
                "additionalProperties": False,
            },
        },
        "insufficient_information": {
            "type": "boolean",
            "description": "True when the bundle carries nothing country-specific "
                           "and the answer is a prior rather than an inference.",
        },
        "evidence": {
            "type": "string",
            "description": "The specific detail that gave it away, or why it is untellable.",
        },
    },
    "required": ["country", "confidence", "alternatives",
                 "insufficient_information", "evidence"],
    "additionalProperties": False,
}

# Asking for a distribution rather than an answer, and offering a way out.
#
# The old prompt said "guess even when unsure" and allowed only a single code.
# Both push the same way: a model that must name one country will name the one
# its prior favours, and on this roster that is the United States. The meter then
# reports the model's prior as an identifiability rate, and the two are
# indistinguishable in the output — a masked US bundle and an empty bundle both
# come back "US, 0.85".
#
# `alternatives` makes the prior visible: a bundle identified on evidence
# concentrates probability, a bundle answered from prior spreads it.
# `insufficient_information` lets the model say so outright. Neither is a fix for
# masking; both are what make the number readable, and they are why the control
# arm below exists.
_PROBE_PROMPT = """\
The news summaries below have had country names, demonyms, currencies, cities \
and institutions removed. Identify which country they are about.

Give your three most likely candidates with probabilities. Concentrate the \
probability only as far as the evidence warrants: if two countries fit equally \
well, say so with two similar probabilities rather than picking one.

Set `insufficient_information` to true when the text carries nothing \
country-specific and your answer is really a guess from base rates — that is a \
more useful answer than a confident one you cannot support, and it is not \
penalised. Use 'ZZ' as the country in that case if no candidate stands out.

Say what gave it away: the specific number, institution, event or phrasing. If \
you are inferring from base rates, say that instead.

{bundle}
"""

# How much of each article the probe reads. It is measuring identifiability of
# the *evidence*, so it gets what the scorer got, capped so a single long
# article cannot dominate the bundle.
_PER_ARTICLE_CHARS = 1200

# The instrument's own version, on the same derived-not-maintained principle as
# `rewrite.SWEEP_VERSION`.
#
# A probe result is only comparable to another taken with the same instrument.
# Asking for a top-3 distribution and offering `insufficient_information`
# changes what the model reports about identical text — that is the point of the
# change — so a stored result from before it is not a baseline for one after it,
# and a key that could not tell them apart would silently overwrite one with the
# other. Which is the same failure `SWEEP_VERSION` exists to prevent, one layer
# up.
PROBE_VERSION = hashlib.sha256(
    (_PROBE_PROMPT + json.dumps(_PROBE_SCHEMA, sort_keys=True)).encode("utf-8")
).hexdigest()[:8]


def bundle_text(items: List[Dict[str, Any]],
                fulltext_ids: Optional[List[str]] = None) -> str:
    """Exactly what the scoring prompt carries, as one block for the probe.

    This has to mirror the prompt or the meter is measuring the wrong thing, and
    the first version did not. It read ``item["text"]`` for all twenty articles,
    but the scorer never sees twenty bodies: ``prompt_entries`` hands it a title
    and a *digest* per article, and full text for only the two or three ids in
    ``fulltext_ids``. Probing the raw bodies measures a bundle strictly more
    identifiable than the one that gets sent, and would have reported the
    instrument as leakier than it is — forever, and in the direction that looks
    like diligence.

    So the entries come from ``prompt_entries`` rather than from a second
    reading of the items. URLs are excluded there already, which matters: a path
    like "/2018/aug/13/turkey-lira-crisis" would hand over the answer, and it is
    never sent.

    Args:
        items: the masked articles, after stage-1 digesting.
        fulltext_ids: the ids whose full text the prompt carries. Omitted, no
            article contributes a body — the conservative reading, and the right
            one when the caller does not know.
    """
    # Imported here rather than at module scope: `langchain_llm` imports this
    # module's neighbours, and a top-level import closes the cycle.
    from backend.utils.ai import langchain_llm

    chosen = set(fulltext_ids or ())
    parts = []
    for i, entry in enumerate(langchain_llm.prompt_entries(items), start=1):
        digest = entry.get("digest")
        body = json.dumps(digest, ensure_ascii=False) if isinstance(digest, dict) else (
            entry.get("summary") or "")
        if entry.get("id") in chosen:
            item = next((it for it in items if it.get("id") == entry.get("id")), {})
            body = f"{body}\n{item.get('text') or ''}"
        parts.append(f"[{i}] {entry.get('title') or ''}\n{body[:_PER_ARTICLE_CHARS]}".strip())
    return "\n\n".join(parts)


def probe(items: List[Dict[str, Any]], api_key: str,
          model_chat: Optional[Any] = None,
          fulltext_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Ask which country a masked bundle is about.

    Returns:
        ``{'country', 'confidence', 'alternatives', 'insufficient_information',
        'evidence'}``. A failed call returns 'ZZ' at confidence 0.0 with the
        error as evidence — the opposite of the leakage scan's fail-closed, and
        deliberately: this is a measurement, not a gate, and a failed
        measurement must not be recorded as a successful identification.
    """
    if not items:
        return _no_guess("empty bundle")
    try:
        chat = model_chat or ai_client.build_digest_chat(api_key)
        result = chat.with_structured_output(
            schema=_PROBE_SCHEMA, strict=True).invoke(
                _PROBE_PROMPT.format(bundle=bundle_text(items, fulltext_ids)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("probe failed (%s); recorded as no-guess", exc)
        return _no_guess(f"probe failed: {exc}")
    if not isinstance(result, dict):
        return _no_guess("probe returned no object")

    alternatives = []
    for entry in result.get("alternatives") or []:
        if not isinstance(entry, dict):
            continue
        try:
            alternatives.append({
                "country": str(entry.get("country") or "ZZ").upper()[:2],
                "probability": float(entry.get("probability") or 0.0),
            })
        except (TypeError, ValueError):
            continue
    return {
        "country": str(result.get("country") or "ZZ").upper()[:2],
        "confidence": float(result.get("confidence") or 0.0),
        "alternatives": alternatives[:3],
        "insufficient_information": bool(result.get("insufficient_information")),
        "evidence": str(result.get("evidence") or ""),
    }


def _no_guess(evidence: str) -> Dict[str, Any]:
    """The shape a probe that did not happen returns.

    ``insufficient_information`` is True here for the same reason the whole
    function fails open: an unanswered probe is not evidence that masking
    worked, and anything averaging over these must be able to exclude them.
    """
    return {"country": "ZZ", "confidence": 0.0, "alternatives": [],
            "insufficient_information": True, "evidence": evidence}


# --- the control arm --------------------------------------------------------
# What the probe answers when there is genuinely nothing to answer from.
#
# Every identifiability number is meaningless without this. A probe forced to
# name a country will name the one its prior favours, and on a roster containing
# the United States that is the United States — so "US identified at 0.85" and
# "the model always says US" produce identical output. The only way to tell them
# apart is to hand it a bundle with no country in it and see what it says.
#
# These are written rather than derived from real articles on purpose: a real
# bundle stripped by a rule is stripped only of what the rule knew about, which
# is the assumption under test. Numbers are kept, and kept plausible, because a
# bundle with no numbers is not the same instrument — magnitudes are exactly what
# the probe cites when it identifies the US from the size of a stimulus package.
_NULL_DIGESTS = (
    {"what_happened": "The central bank held its policy rate for a third "
                      "consecutive meeting, citing balanced risks.",
     "actors": "the central bank, the rate-setting committee",
     "numbers": "3.25%, third consecutive meeting, 7-2 vote",
     "transmission": "borrowing costs, credit growth"},
    {"what_happened": "Headline inflation eased for the fourth month while core "
                      "inflation proved stickier than expected.",
     "actors": "the national statistics office",
     "numbers": "2.8%, 3.4%, fourth month, 0.2 percentage points",
     "transmission": "real incomes, wage bargaining"},
    {"what_happened": "The governing party lost its majority in a regional "
                      "election, and coalition talks began the following week.",
     "actors": "the governing party, the main opposition party",
     "numbers": "41 seats, 38%, 12 days",
     "transmission": "policy continuity, fiscal plans"},
    {"what_happened": "A large domestic bank reported a rise in non-performing "
                      "loans concentrated in commercial property.",
     "actors": "a large domestic bank, the financial regulator",
     "numbers": "4.1% of the book, 1.2bn, up from 2.9%",
     "transmission": "credit supply, capital ratios"},
    {"what_happened": "Industrial production contracted for a second quarter as "
                      "export orders weakened.",
     "actors": "manufacturers, the trade ministry",
     "numbers": "-1.4%, second consecutive quarter, 62% of output",
     "transmission": "employment, current account"},
    {"what_happened": "The finance ministry announced a fiscal package funded by "
                      "additional borrowing, and the debt agency raised its "
                      "issuance target.",
     "actors": "the finance ministry, the debt management agency",
     "numbers": "0.8% of GDP, 14bn, three years",
     "transmission": "yields, the fiscal deficit"},
)


def null_bundle(size: int = 20) -> List[Dict[str, Any]]:
    """A bundle with the shape of real masked evidence and no country in it.

    The control arm's input. Cycled to ``size`` so it matches the article count
    of a real snapshot: a six-article bundle and a twenty-article one are not the
    same test, because volume is itself a signal the probe uses.
    """
    out = []
    for i in range(size):
        digest = dict(_NULL_DIGESTS[i % len(_NULL_DIGESTS)])
        digest["stage1_severity"] = 30 + (i % 5) * 10
        digest["directly_about_country"] = True
        out.append({
            "id": f"a{i + 1}",
            "title": digest["what_happened"][:70],
            "digest": digest,
            "stage1_severity": digest["stage1_severity"],
            "published": "2020-06-0{}T00:00:00Z".format(i % 9 + 1),
            "source": "a news agency",
        })
    return out


def distribution(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """How often each country was named, against how often it could have been.

    The prior, made visible. If the guesses concentrate on one country far beyond
    its share of the bundles probed, the meter is reporting the model's base
    rates rather than the corpus's leakiness — and every identifiability rate
    needs deflating by that before it means anything.

    Args:
        results: dicts with ``country_iso2`` (the truth) and ``guess``.
    """
    guessed: Dict[str, int] = {}
    truth: Dict[str, int] = {}
    insufficient = 0
    for row in results:
        guess = row["guess"]
        guessed[guess.get("country") or "ZZ"] = guessed.get(guess.get("country") or "ZZ", 0) + 1
        truth[row["country_iso2"]] = truth.get(row["country_iso2"], 0) + 1
        insufficient += bool(guess.get("insufficient_information"))
    n = len(results) or 1
    return {
        "guessed": dict(sorted(guessed.items(), key=lambda kv: -kv[1])),
        "truth": dict(sorted(truth.items(), key=lambda kv: -kv[1])),
        # Positive means a country was named more often than it appeared.
        "over_representation": {
            iso2: round(count / n - truth.get(iso2, 0) / n, 3)
            for iso2, count in sorted(guessed.items(), key=lambda kv: -kv[1])
        },
        "insufficient_information": insufficient,
        "n": len(results),
    }


def compare(baseline: List[Dict[str, Any]],
            current: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One masking behaviour's probe results against another's, bundle by bundle.

    The reason ``probe_result`` exists. The sweep fix of 2026-08-03 could not be
    measured against the run that motivated it, because that run's results lived
    in a commit message — so "did it work" was answerable only by re-reading
    prose. Joined on ``(country_iso2, as_of)``, which is the bundle; the masking
    versions are what differ between the two sides and so cannot be in the join.

    Args:
        baseline, current: rows as ``data_push.read_probe_results`` returns them.

    Returns:
        One row per bundle present in *either* side, sorted country then anchor.
        ``was``/``now`` are None where a side has no row: a bundle that only one
        run covered is a real fact about the comparison and must not be silently
        dropped, which is how twenty bundles became six traces in the first
        place.
    """
    def index(rows: List[Dict[str, Any]]) -> Dict[tuple, Dict[str, Any]]:
        return {(r["country_iso2"], r["as_of"]): r for r in rows}

    old, new = index(baseline), index(current)
    out = []
    for key in sorted(set(old) | set(new)):
        was, now = old.get(key), new.get(key)
        out.append({
            "country_iso2": key[0],
            "as_of": key[1],
            "was_guess": (was or {}).get("guess"),
            "was_confidence": (was or {}).get("confidence"),
            "was_identified": (was or {}).get("identified"),
            "now_guess": (now or {}).get("guess"),
            "now_confidence": (now or {}).get("confidence"),
            "now_identified": (now or {}).get("identified"),
            "now_evidence": (now or {}).get("evidence"),
            # None when either side is missing: "not measured" and "no change"
            # are different answers and must not print the same.
            "fixed": (None if was is None or now is None
                      else bool(was["identified"]) and not now["identified"]),
            "regressed": (None if was is None or now is None
                          else not was["identified"] and bool(now["identified"])),
        })
    return out


def classify(country_iso2: str, guess: Dict[str, Any],
             confident_at: float = 0.5) -> str:
    """Which of four things a probe result actually is.

    A meter with two buckets — hit and miss — misreads this corpus in both
    directions. Counting only correct hits understates how much signal a bundle
    carries: PT on a quiet week came back "GB at 0.70", which is masking holding
    and the text still being legible enough to place confidently in Western
    Europe. Counting confidence alone overstates it: the same result is *wrong*,
    and a divergence meter read through it would be crediting the mask with less
    than it achieved.

    Returns:
        ``identified``    — named the right country, confidently.
        ``wrong``         — named a country confidently, and the wrong one. The
                            bundle leaked *something*; it did not leak identity.
        ``uncertain``     — a low-confidence guess, right or wrong.
        ``no_guess``      — declined, or said `insufficient_information`.
    """
    confidence = float(guess.get("confidence") or 0.0)
    country = (guess.get("country") or "ZZ").upper()
    if country in ("", "ZZ") or guess.get("insufficient_information"):
        return "no_guess"
    if confidence < confident_at:
        return "uncertain"
    return "identified" if country == country_iso2.upper() else "wrong"


def source_mix_correlation(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Does the probe's success track the outlet rather than the evidence?

    The cheap check with a real answer attached. The corpus is two outlets with
    very different footprints — the Guardian is British and covers Portugal
    thinly, the NYT archive is overwhelmingly American — and a model that has
    read both can recognise house style. If bundles with a higher NYT share are
    more identifiable, part of the identifiability number is outlet
    fingerprinting rather than evidence leakage, and the divergence meter must
    not be read through it without that said out loud.

    It is a caveat generator, not a correction: no threshold here changes a
    score, and a correlation is not proof of mechanism.

    Args:
        results: dicts with ``country_iso2``, ``guess``, and ``sources``
            (``{"guardian": n, "nyt": n}`` for that bundle).
    """
    buckets: Dict[str, List[float]] = {}
    for row in results:
        sources = row.get("sources") or {}
        total = sum(sources.values()) or 1
        nyt_share = sources.get("nyt", 0) / total
        outcome = classify(row["country_iso2"], row["guess"])
        buckets.setdefault(outcome, []).append(nyt_share)

    summary = {
        outcome: {"n": len(shares),
                  "mean_nyt_share": round(sum(shares) / len(shares), 3)}
        for outcome, shares in sorted(buckets.items())
    }
    placed = [s for o in ("identified", "wrong") for s in buckets.get(o, [])]
    silent = buckets.get("no_guess", []) + buckets.get("uncertain", [])
    gap = None
    if placed and silent:
        gap = round(sum(placed) / len(placed) - sum(silent) / len(silent), 3)
    return {
        "by_outcome": summary,
        # Positive means bundles the probe placed at all were NYT-heavier than
        # the ones it could not — the shape that would suggest fingerprinting.
        "nyt_share_gap": gap,
    }


def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Guess rates per country, and the spread that is the actual meter.

    Args:
        results: dicts with ``country_iso2`` (the truth) and ``guess`` (a
            :func:`probe` result).

    Returns:
        Per-country hit rates plus ``spread``, the gap between the most and
        least identifiable country. The US is expected at the top; if every
        country sits up there with it, masking is not working, and if the
        spread is wide then masking works everywhere except where coverage
        volume gives it away.
    """
    per: Dict[str, Dict[str, Any]] = {}
    for row in results:
        truth = row["country_iso2"]
        stats = per.setdefault(truth, {"n": 0, "hits": 0, "confidence": 0.0,
                                       "identified": 0, "wrong": 0,
                                       "uncertain": 0, "no_guess": 0})
        stats["n"] += 1
        stats["hits"] += int(row["guess"].get("country") == truth)
        stats["confidence"] += float(row["guess"].get("confidence") or 0.0)
        stats[classify(truth, row["guess"])] += 1

    for stats in per.values():
        stats["rate"] = stats["hits"] / stats["n"] if stats["n"] else 0.0
        stats["mean_confidence"] = stats["confidence"] / stats["n"] if stats["n"] else 0.0
        # Confidently placed *somewhere*, right or wrong. The bundle carried
        # enough to commit to an answer, which is a different fact from whether
        # the answer was correct — and the one masking is actually judged on.
        stats["placed_rate"] = ((stats["identified"] + stats["wrong"]) / stats["n"]
                                if stats["n"] else 0.0)
        del stats["confidence"]

    rates = [s["rate"] for s in per.values()]
    return {
        "per_country": per,
        "spread": (max(rates) - min(rates)) if rates else 0.0,
        "ceiling": max(rates) if rates else 0.0,
        "totals": {
            outcome: sum(s[outcome] for s in per.values())
            for outcome in ("identified", "wrong", "uncertain", "no_guess")
        },
        "roster": list(config.PILOT_ROSTER),
    }
