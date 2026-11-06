"""
The data half of the score: every indicator as a risk percentile across the roster.

A value like inflation needs no model judgement. It becomes a number by ranking
the country against the other roster countries: 0 = the lowest risk in the
roster, 100 = the highest. The ranking is deterministic, so this half of the
score cannot move between two calls on the same week.

Which way is riskier comes from the registry's `risk` field (`higher`, `lower`
or `target`), not from here. The roster table is built once per run from the
local parquet panel and the curated CSV, the same sources the payload reads, so
the value a country is scored on is the value the model was shown.

A missing indicator is left out of the ledger mean and counted. It is never
filled in: an absent number is absent, not average.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

from backend.data_fetching import curated_loader, data_retrieval
from backend.llm import constants as ai_constants
from backend.util import constants

logger = logging.getLogger(__name__)

__all__ = ["risk_value", "roster_table", "percentile_of", "ledger_data_scores"]

_PRECISION = 6

for _code, _spec in constants.INDICATOR_REGISTRY.items():
    if _spec.get("risk") not in ("higher", "lower", "target"):
        raise RuntimeError(
            f"INDICATOR_REGISTRY[{_code!r}] has no risk direction; the data score "
            f"cannot rank a value without knowing which way is riskier"
        )


def risk_value(code: str, value: float) -> float:
    """Transform a raw value so that a larger number always means more risk."""
    direction = constants.INDICATOR_REGISTRY[code]["risk"]
    x = float(value)
    if direction == "higher":
        return x
    if direction == "lower":
        return -x
    return abs(x - ai_constants.SCORING["inflation_target"])


def roster_table(
    *,
    roster: Optional[List[str]] = None,
    since: int = 2000,
    lookback: int = 10,
) -> Dict[str, Any]:
    """Every roster country's latest value for every registry code.

    Reads the parquet panel partition per country (`data_retrieval.panel_values`,
    the same call the payload makes) and the curated CSV once. "Latest" is the
    most recent year with a value, as `payload._resolve_one` resolves it.

    Returns:
        ``{"values": {code: {iso2: value}}, "periods": {code: {iso2: year}},
        "n_by_code": {code: int}, "missing_countries": [...], "sha256": str,
        "built_at": iso}``. A country whose partition cannot be read is skipped
        and named rather than failing the run.
    """
    iso2s = list(roster) if roster else [c["iso2"] for c in constants.COUNTRY_ROSTER]
    curated = curated_loader.load_curated()
    values: Dict[str, Dict[str, float]] = {code: {} for code in constants.INDICATOR_REGISTRY}
    periods: Dict[str, Dict[str, int]] = {code: {} for code in constants.INDICATOR_REGISTRY}
    missing: List[str] = []

    for iso2 in iso2s:
        try:
            panel = data_retrieval.panel_values(iso2, since=since, lookback=lookback)
        except Exception as exc:  # a missing partition is a fact about the panel
            logger.warning("roster table: no panel for %s (%s)", iso2, exc)
            missing.append(iso2)
            panel = {}
        own_curated = curated.get(iso2, {})
        for code, spec in constants.INDICATOR_REGISTRY.items():
            panel_col = spec.get("panel_col")
            if panel_col:
                got = panel.get(panel_col)
                if not got or got.get("latest") is None:
                    continue
                values[code][iso2] = float(got["latest"])
                periods[code][iso2] = int(got["latest_year"])
            else:
                got = own_curated.get(code)
                if not got or got.get("value") is None:
                    continue
                values[code][iso2] = float(got["value"])
                periods[code][iso2] = int(got["period"])

    digest = hashlib.sha256(
        json.dumps(values, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "values": values,
        "periods": periods,
        "n_by_code": {code: len(v) for code, v in values.items()},
        "missing_countries": missing,
        "sha256": digest,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }


def percentile_of(code: str, value: float, iso2: str, table: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Rank one value against every *other* roster country's value for `code`.

    The country itself is left out of the comparison set, so a frozen payload's
    value and a later-refreshed panel value rank the same way. Ties take the
    mid-rank. With `m` others: ``100 × (below + 0.5 × ties) / m``, so the
    lowest-risk value in the roster is 0 and the highest is 100.

    Returns None when fewer than `SCORING["min_roster_n"]` other countries
    have a value: a percentile among three is not a percentile.
    """
    others = [
        round(risk_value(code, v), _PRECISION)
        for other, v in (table.get("values", {}).get(code) or {}).items()
        if other != iso2
    ]
    m = len(others)
    if m < ai_constants.SCORING["min_roster_n"]:
        return None
    r = round(risk_value(code, value), _PRECISION)
    below = sum(1 for o in others if o < r)
    ties = sum(1 for o in others if o == r)
    return {
        "value": float(value),
        "risk_value": r,
        "percentile": round(100.0 * (below + 0.5 * ties) / m, 2),
        "n_others": m,
        "below": below,
        "ties": ties,
    }


def ledger_data_scores(
    ledgers: Dict[str, Any],
    iso2: str,
    table: Dict[str, Any],
) -> Dict[str, Any]:
    """The data score for each ledger from a payload's economics block.

    Args:
        ledgers: ``economics["ledgers"]`` in production or the frozen payload's
            ``payload["economics_by_ledger"]``; both are
            ``{ledger: {"indicators": [{"code", "value", ...}]}}``.
        iso2: The country being scored.
        table: From :func:`roster_table`.

    Returns:
        ``{"percentiles": {code: {..., "ledger"}}, "data_scores": {ledger:
        float|None}, "n_by_ledger": {ledger: int}, "roster_sha256": str}``.
        The ledger score is the mean of the available percentiles, or None when
        none resolved.
    """
    percentiles: Dict[str, Dict[str, Any]] = {}
    data_scores: Dict[str, Optional[float]] = {}
    n_by_ledger: Dict[str, int] = {}

    for ledger in constants.LEDGERS:
        block = ledgers.get(ledger) or {}
        pcts: List[float] = []
        for entry in block.get("indicators") or []:
            code = entry.get("code")
            value = entry.get("value")
            if code not in constants.INDICATOR_REGISTRY or value is None:
                continue
            got = percentile_of(code, value, iso2, table)
            if got is None:
                percentiles[code] = {"ledger": ledger, "value": float(value),
                                     "percentile": None, "reason": "roster too thin"}
                continue
            percentiles[code] = {"ledger": ledger, **got}
            pcts.append(got["percentile"])
        n_by_ledger[ledger] = len(pcts)
        data_scores[ledger] = round(sum(pcts) / len(pcts), 2) if pcts else None

    return {
        "percentiles": percentiles,
        "data_scores": data_scores,
        "n_by_ledger": n_by_ledger,
        "roster_sha256": table.get("sha256"),
    }
