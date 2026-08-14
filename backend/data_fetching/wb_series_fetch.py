"""World Bank annual series for the friction framework, into ``indicator_series``.

The nine indicators the dashboard has always shown arrive through the parquet
panel (``country_data_fetch`` -> ``wb_panel_wide``). The friction framework needs
another eight, and they deliberately do **not** go into that panel.

The reason is mechanical, not stylistic: ``backfill_missing_panels`` skips any
country that already has a partition, so adding columns to ``INDICATORS`` would
leave every already-backfilled country silently missing them — the payload would
build, the model would score, and nobody would see the gap. Writing to
``indicator_series`` instead means a new indicator appears for every country on
the next refresh with no partition rewrite and no migration.

The fetching itself reuses ``fetch_metrics.wb_series`` rather than reimplementing
it: that function already carries the retry policy the World Bank needs (it
sporadically returns spurious 400s under load, which is why 400 is in its
retryable set) and already degrades a missing (country, indicator) pair to an
empty series instead of raising.
"""

import datetime as _dt
import logging
from typing import Any, Dict, List, Mapping, Optional

import pandas as pd

from backend.util import constants
from backend.data_fetching import fetch_metrics
from backend.data_upsert import data_push

logger = logging.getLogger(__name__)

# How far back to pull. Ten years covers the 5-year deltas the payload reports
# with room for a gappy reporter, without dragging a full history into a table
# whose job is recent evidence.
_START_YEAR_LOOKBACK = 12


def _rows_for_series(
    series: pd.Series,
    *,
    iso2: str,
    code: str,
    as_of: _dt.date,
    source: str,
) -> List[Dict[str, Any]]:
    """Turn one World Bank annual series into ``indicator_series`` rows.

    Args:
        series: year-indexed values, as returned by ``fetch_metrics.wb_series``.
        iso2: ISO-2 country code to store against.
        code: the registry id to store under.
        as_of: the date we learned these values — the fetch date.
        source: provenance label for the stored rows.

    Returns:
        One row dict per non-null observation. Empty for an empty series.
    """
    rows: List[Dict[str, Any]] = []
    for year, value in series.dropna().items():
        try:
            period = str(int(year))
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        rows.append({
            "country_iso2": iso2,
            "indicator_code": code,
            "freq": "A",
            "period": period,
            "value": numeric,
            "as_of": as_of,
            "source": source,
        })
    return rows


def fetch_country_series(
    iso2: str,
    iso3: str,
    *,
    codes: Optional[Mapping[str, Any]] = None,
    as_of: Optional[_dt.date] = None,
) -> List[Dict[str, Any]]:
    """Fetch one country's registry-listed World Bank series as storable rows.

    Args:
        iso2: ISO-2 code, used as the storage key.
        iso3: ISO-3 code, used for the World Bank query.
        codes: registry ids to fetch. Defaults to ``constants.WB_SERIES_CODES``.
        as_of: the date to stamp values with. Defaults to today — this is a
            fetch, so a clock read is honest here, unlike in the pure layers.

    Returns:
        Rows ready for ``data_push.upsert_indicator_series``. Indicators with no
        World Bank data for this country contribute nothing, which is a real
        absence (no short-term external debt reporting, no national broad money
        inside the euro area) and not an error.
    """
    stamp = as_of or _dt.date.today()
    wanted = tuple(codes) if codes is not None else constants.WB_SERIES_CODES
    start = stamp.year - _START_YEAR_LOOKBACK

    rows: List[Dict[str, Any]] = []
    for code in wanted:
        spec = constants.INDICATOR_REGISTRY.get(code)
        if not spec:
            logger.warning("[wb-series] %s is not in INDICATOR_REGISTRY; skipping", code)
            continue
        try:
            series = fetch_metrics.wb_series(iso3, code, start=start)
        except Exception:
            # wb_series already degrades its own failures to an empty series, so
            # reaching here means something structural. One indicator must not
            # cost the country the other seven.
            logger.exception("[wb-series] %s/%s failed", iso2, code)
            continue
        if series.empty:
            logger.debug("[wb-series] %s/%s has no data", iso2, code)
            continue
        rows.extend(_rows_for_series(
            series, iso2=iso2, code=code, as_of=stamp, source=str(spec["source"]),
        ))
    return rows


def refresh_wb_series() -> None:
    """Refresh every rostered country's World Bank series into ``indicator_series``.

    One country's failure is logged and skipped; the rest of the roster still
    refreshes. Safe to re-run — the upsert is keyed on
    (country, indicator, freq, period).
    """
    stamp = _dt.date.today()
    refreshed = 0
    for country in constants.COUNTRY_ROSTER:
        iso2, iso3 = country["iso2"], country["iso3"]
        try:
            rows = fetch_country_series(iso2, iso3, as_of=stamp)
            if rows:
                data_push.upsert_indicator_series(rows)
                refreshed += 1
                logger.info("[wb-series] %s: %d observations", iso2, len(rows))
        except Exception:
            logger.exception("[wb-series] %s ERROR", iso2)
    logger.info("[wb-series] refreshed %d/%d countries", refreshed, len(constants.COUNTRY_ROSTER))


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    logging.basicConfig(level=logging.INFO)
    for _iso2, _iso3 in (("PT", "PRT"), ("US", "USA"), ("BR", "BRA")):
        _rows = fetch_country_series(_iso2, _iso3)
        _codes = sorted({r["indicator_code"] for r in _rows})
        print(f"{_iso2}: {len(_rows)} rows across {len(_codes)} indicators: {_codes}")
