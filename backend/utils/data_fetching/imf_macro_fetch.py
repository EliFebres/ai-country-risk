"""
IMF higher-frequency macro refresh (new IMF Data API, SDMX 2.1).

The World Bank indicators in the main panel are annual and lagged 1–2 years, so a
country mid-shock shows a badly stale headline (e.g. Argentina inflation reading
the 2024 annual average of ~220% when the current monthly y/y print is ~32%).
A few of those indicators exist at monthly/quarterly frequency from the IMF; this
module fetches the **freshest observation** for each one configured in
``constants.IMF_RECENT_INDICATORS`` so the rest of the pipeline can store it in
``recent_indicator`` and let the front-end prefer it over the annual value.

Source: the current IMF Data API (SDMX 2.1) at ``constants.IMF_DATA_ENDPOINT``.
The legacy IFS host (``dataservices.imf.org``) was retired. The country dimension
is ISO-3 (e.g. ``ARG``); for the CPI dataset the IMF already publishes a
pre-computed year-over-year percent change, so no manual y/y math is required.

Every network/parse failure degrades gracefully to "no value" (``None`` / omitted
key) so a single country's gap or an IMF outage never aborts the surrounding run
(mirrors ``political_corruption_fetch`` and ``fetch_metrics`` returning empty).
"""

import calendar
import logging
import datetime as _dt
from typing import Optional, Tuple, Dict, Any
from xml.etree import ElementTree as ET

import requests

from backend.utils import constants

logger = logging.getLogger(__name__)

# Polite identification; the IMF API serves anonymous GETs.
_HEADERS = {"User-Agent": "AI-Country-Risk-Dashboard/1.0 (+imf-macro-refresh)"}
_TIMEOUT = 40  # seconds


def _localname(tag: str) -> str:
    """Strip any XML namespace from an element tag (``{ns}Obs`` -> ``Obs``)."""
    return tag.rsplit("}", 1)[-1]


def _end_of_month(year: int, month: int) -> _dt.date:
    """Last calendar day of ``month`` in ``year``."""
    return _dt.date(year, month, calendar.monthrange(year, month)[1])


def _period_to_date(time_period: str) -> Optional[_dt.date]:
    """Convert an SDMX ``TIME_PERIOD`` to an end-of-period calendar date.

    Handles the three frequencies the IMF returns:
      * monthly   ``'2026-M03'`` -> 2026-03-31 (last day of the month)
      * quarterly ``'2026-Q1'``  -> 2026-03-31 (last day of the quarter)
      * annual    ``'2026'``     -> 2026-12-31

    Returns ``None`` for anything unrecognized.
    """
    tp = (time_period or "").strip()
    try:
        if "-M" in tp:
            y_str, m_str = tp.split("-M")
            y, m = int(y_str), int(m_str)
            if not 1 <= m <= 12:
                return None
            return _end_of_month(y, m)
        if "-Q" in tp:
            y_str, q_str = tp.split("-Q")
            y, q = int(y_str), int(q_str)
            if not 1 <= q <= 4:
                return None
            return _end_of_month(y, q * 3)
        if tp.isdigit():
            return _dt.date(int(tp), 12, 31)
    except Exception:  # noqa: BLE001 - any malformed period degrades to None
        return None
    return None


def _fetch_latest_obs(dataflow: str, key: str, *, start_period: str) -> Optional[Tuple[str, float]]:
    """Return ``(time_period, value)`` of the latest non-null observation, or ``None``.

    Queries one SDMX series (``dataflow``/``key``) from ``start_period`` onward and
    scans every ``<Obs>`` for the chronologically-latest numeric value. SDMX period
    strings are zero-padded (``2026-M03``), so a plain string ``max`` orders them
    correctly within a single frequency.
    """
    url = f"{constants.IMF_DATA_ENDPOINT}/{dataflow}/{key}"
    try:
        resp = requests.get(url, headers=_HEADERS, params={"startPeriod": start_period}, timeout=_TIMEOUT)
        if resp.status_code != 200 or not resp.text:
            logger.warning("IMF %s/%s -> HTTP %s (no data)", dataflow, key, resp.status_code)
            return None
        root = ET.fromstring(resp.text)
    except Exception as e:  # noqa: BLE001 - network/parse errors degrade to None
        logger.warning("IMF fetch failed for %s/%s: %s", dataflow, key, e)
        return None

    best: Optional[Tuple[str, float]] = None
    for el in root.iter():
        if _localname(el.tag) != "Obs":
            continue
        tp = el.get("TIME_PERIOD")
        raw = el.get("OBS_VALUE")
        if not tp or raw in (None, "", "NaN"):
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if best is None or tp > best[0]:
            best = (tp, val)
    return best


def fetch_recent_indicators(iso3: str) -> Dict[str, Dict[str, Any]]:
    """Freshest sub-annual observation per configured indicator for one country.

    Args:
        iso3: ISO-3 country code (the IMF country-dimension key, e.g. ``'ARG'``).

    Returns:
        ``{indicator_display_name: {value, period (date), freq, unit, source}}``.
        Indicators with no usable IMF observation are simply omitted, so the
        result may be empty (callers should treat that as "no refresh available").
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not iso3:
        return out

    # A short window is enough — we only keep the single latest print. Two full
    # years back guarantees at least one observation even for laggy reporters.
    start_period = str(_dt.date.today().year - 2)

    for name, spec in constants.IMF_RECENT_INDICATORS.items():
        key = spec["key"].format(iso3=iso3)
        latest = _fetch_latest_obs(spec["dataflow"], key, start_period=start_period)
        if latest is None:
            continue
        time_period, value = latest
        period = _period_to_date(time_period)
        if period is None:
            continue
        out[name] = {
            "value": round(value, 2),
            "period": period,
            "freq": spec.get("freq", "M"),
            "unit": spec.get("unit"),
            "source": "IMF",
        }
    return out


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    logging.basicConfig(level=logging.INFO)
    for code in ("ARG", "NGA", "PAK", "USA", "DEU"):
        print(code, fetch_recent_indicators(code))
