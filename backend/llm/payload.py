"""
The economics block: what the scorer reads about the country's numbers.

Organised **by ledger**, so the model reads friction's indicators together, then
order's, then information's, then edge's. That is not presentation. The score is
four ledger judgements plus a composite, and an indicator list in registry order
asks the model to do the sorting itself on every call.

Every value carries its trajectory, not just its level. 7.2% inflation on the
way down from 20% is a different country from 7.2% on the way up from 2%, and a
level alone cannot tell them apart. The last five annual observations travel
with the number, with the direction stated in words and computed in code.

Every value also carries when it became knowable, from `util.vintage` and never
from the clock, along with the scheme that produced that date and how stale it
is against the series' own cadence.

**An indicator with no observation is omitted, and named in the census.** It is
never a zero and never a padded null: a zero reads as reassurance, and a null
reads as a measurement. Absence is absence, and the place to say so is the
census, loudly, not the payload, quietly.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

from backend.util import constants, trends, vintage

__all__ = [
    "build_economics_block",
    "build_scoring_payload",
    "count_tokens",
    "LEDGER_QUESTIONS",
]


# What each ledger is asking, put in front of the model with the numbers rather
# than left to the scoring prompt. The indicators only mean something against
# the question they are answering.
LEDGER_QUESTIONS: Dict[str, str] = {
    "friction": "What is taken, and how well does it convert?",
    "order": "How much doubt is there about the load-bearing rules?",
    "information": "Can the country's own instruments be trusted?",
    "edge": "Is the system learning?",
}


def _resolve_one(
    code: str,
    spec: Dict[str, Any],
    panel: Dict[str, Dict[str, Any]],
    curated: Dict[str, Dict[str, Any]],
    today: dt.date,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve one registry code. Returns ``(entry, drop_reason)``."""
    panel_col = spec.get("panel_col")

    if panel_col:
        got = panel.get(panel_col)
        if not got:
            return None, "unmapped" if panel_col not in panel else "no row"
        series = got.get("series") or {}
        latest_year = got.get("latest_year")
        value = got.get("latest")
        published = None
    else:
        got = curated.get(code)
        if not got:
            return None, "no row"
        series = got.get("series") or {}
        latest_year = got.get("period")
        value = got.get("value")
        published = got.get("as_of")

    if value is None:
        return None, "no row"

    try:
        as_of, scheme = vintage.as_of_for(
            spec["source"], latest_year, freq=spec.get("freq", "A"), published=published
        )
    except (KeyError, ValueError) as e:
        return None, f"undatable ({e.__class__.__name__})"

    cadence = vintage.CADENCE_DAYS.get(spec.get("freq", "A"), 365)
    stale = vintage.staleness_days(as_of, today)

    entry = {
        "code": code,
        "label": spec["label"],
        "unit": spec["unit"],
        "source": spec["source"],
        "value": value,
        "period": latest_year,
        "as_of": as_of.isoformat(),
        "as_of_scheme": scheme,
        "staleness_days": stale,
        # Stated rather than left to be worked out: "older than this series'
        # own rhythm" is the judgement the freshness instruction needs.
        "stale_for_its_cadence": stale > cadence,
        **trends.describe(series, latest_year=latest_year if isinstance(latest_year, int) else None),
    }
    return entry, None


def build_economics_block(
    panel: Dict[str, Dict[str, Any]],
    curated: Optional[Dict[str, Dict[str, Any]]] = None,
    *,
    today: Optional[dt.date] = None,
) -> Dict[str, Any]:
    """Assemble the by-ledger economics block, and say what did not resolve.

    Args:
        panel: ``{panel_col: {"latest", "latest_year", "series"}}`` from the
            World Bank parquet panel.
        curated: ``{registry_code: {"value", "period", "as_of", "series"}}``
            from `backend/data/curated.csv`.
        today: For staleness; defaults to today.

    Returns:
        ``{"ledgers": {...}, "resolution": {...}}``. `resolution` is what the
        census reads: expected against resolved, per ledger and per source, with
        every dropped code named and the reason given.
    """
    curated = curated or {}
    today = today or dt.date.today()

    ledgers: Dict[str, Dict[str, Any]] = {
        name: {"question": LEDGER_QUESTIONS[name], "indicators": []}
        for name in constants.LEDGERS
    }
    dropped: List[Dict[str, str]] = []
    by_source: Dict[str, Dict[str, int]] = {}
    schemes: Dict[str, int] = {}

    for code, spec in constants.INDICATOR_REGISTRY.items():
        ledger = spec["ledger"]
        source = spec["source"]
        slot = by_source.setdefault(source, {"expected": 0, "resolved": 0})
        slot["expected"] += 1

        entry, reason = _resolve_one(code, spec, panel, curated, today)
        if entry is None:
            dropped.append({
                "code": code, "ledger": ledger, "source": source,
                "label": spec["label"], "reason": reason,
            })
            continue

        ledgers[ledger]["indicators"].append(entry)
        slot["resolved"] += 1
        schemes[entry["as_of_scheme"]] = schemes.get(entry["as_of_scheme"], 0) + 1

    expected_by_ledger = {name: 0 for name in constants.LEDGERS}
    for spec in constants.INDICATOR_REGISTRY.values():
        expected_by_ledger[spec["ledger"]] += 1

    resolved_by_ledger = {
        name: len(block["indicators"]) for name, block in ledgers.items()
    }

    # A ledger that resolved nothing is stated in the payload as well as in the
    # census. The model is told the evidence is absent rather than being left to
    # infer it from an empty list.
    for name, block in ledgers.items():
        if not block["indicators"]:
            block["note"] = (
                "No indicator resolved for this ledger. Nothing is known about it "
                "from the numbers this run; judge it from the articles alone, and "
                "do not read the absence as a good result."
            )

    return {
        "ledgers": ledgers,
        "resolution": {
            "expected_by_ledger": expected_by_ledger,
            "resolved_by_ledger": resolved_by_ledger,
            "empty_ledgers": [n for n, c in resolved_by_ledger.items() if c == 0],
            "by_source": by_source,
            "dropped": dropped,
            "as_of_schemes": schemes,
        },
    }


# --- What the scorer actually receives --------------------------------------


def build_scoring_payload(
    iso2: str,
    country_name: str,
    *,
    structural: Optional[Dict[str, Any]],
    economics: Dict[str, Any],
    pool_report: Dict[str, Any],
    gate: Dict[str, Any],
    coverage: Dict[str, Any],
    full_text_k: int,
    body_cap_chars: int,
) -> Dict[str, Any]:
    """Assemble the payload, in the order the model reads it.

    The order is deliberate and is the argument of the whole session:

    1. **The structural facts**, first, so everything after is read against them.
       A 7% policy rate means something different in a country that sets its own
       rate than in one that imports Frankfurt's.
    2. **The economics block by ledger**, each value with its trajectory in
       words, its vintage and its staleness.
    3. **The per-theme article counts**, with the note that a zero means no
       relevant coverage was found — not that nothing happened.
    4. **The digests** of every admitted article.
    5. **The top-k full texts**, each labelled with how much of it was read.
    6. **The computed coverage components**, so the model can see how much it
       was given without being asked to assess that itself.

    Every article carries an id (``a1``, ``a2``, ...) so the returned
    per-article scores can be matched back, and the validator can notice one
    that was never answered.
    """
    selected = gate["selected"]

    articles = []
    for i, a in enumerate(selected, start=1):
        a["id"] = f"a{i}"
        entry = {
            "id": a["id"],
            "publisher": a.get("source"),
            "published": (a.get("page_published_at") or a.get("published") or "")[:10],
            "title": a.get("title"),
            "themes": a.get("themes", []),
            "ledgers": (a.get("relevance") or {}).get("ledgers", []),
            "body_status": a.get("body_status", "title-only"),
        }
        digest = a.get("digest")
        if digest:
            entry["digest"] = {
                "what_happened": digest.get("what_happened"),
                "institutions": digest.get("institutions", []),
                "direction": digest.get("direction"),
                "numbers": digest.get("numbers", []),
            }
        articles.append(entry)

    full_texts = []
    for a in selected[:full_text_k]:
        body = (a.get("text") or "")[:body_cap_chars]
        if not body.strip():
            continue
        full_texts.append({
            "id": a["id"],
            "title": a.get("title"),
            "publisher": a.get("source"),
            "body_status": a.get("body_status"),
            "chars_read": len(body),
            "chars_original": a.get("body_chars_original"),
            "text": body,
        })

    theme_counts = {
        theme: gate["per_theme"].get(theme, 0)
        for theme in pool_report.get("per_theme", {})
    }

    return {
        "country": {"iso2": iso2, "name": country_name},
        "structural_facts": structural or {
            "note": "No structural facts are on file for this country. Do not "
                    "assume a default monetary regime or income level."
        },
        "economics_by_ledger": economics["ledgers"],
        "article_coverage": {
            "note": "Counts are of articles that passed the relevance gate. A "
                    "zero means no relevant coverage was found for that theme "
                    "this week — it does not mean nothing happened, and it is "
                    "not a good sign.",
            "selected_per_theme": theme_counts,
            "selected_per_ledger": gate["per_ledger"],
            "candidates_fetched": pool_report.get("fetched", 0),
            "passed_the_gate": gate["counts"]["eligible"],
            "selected": gate["counts"]["selected"],
            "budget": gate["counts"]["budget"],
        },
        "articles": articles,
        "full_texts": full_texts,
        "evidence_coverage_components": {
            "note": "Computed from what you were sent. Do not return a coverage "
                    "figure of your own.",
            **coverage,
        },
    }


# --- How big the thing is ---------------------------------------------------

def count_tokens(text: str, model: str = "gpt-4o-2024-08-06") -> Dict[str, Any]:
    """Return a token count and how it was arrived at.

    Falls back to a characters-over-four estimate if `tiktoken` is not
    installed, and says which it used — an estimate reported as a measurement is
    how a self-hosting decision gets made on the wrong number.
    """
    try:
        import tiktoken

        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("o200k_base")
        return {"tokens": len(enc.encode(text)), "method": f"tiktoken/{enc.name}"}
    except Exception:
        return {"tokens": round(len(text) / 4), "method": "estimate (chars/4)"}


