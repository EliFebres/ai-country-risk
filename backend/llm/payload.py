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

__all__ = ["build_economics_block", "LEDGER_QUESTIONS"]


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
