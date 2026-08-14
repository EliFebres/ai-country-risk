"""
Economic calendar (Financial Modeling Prep).

The front-end "Econ Calendar" pane shows the next few days of major global
economic decisions/releases (rate decisions, CPI, GDP, …). This module pulls
that feed from FMP's economic-calendar endpoint and normalizes it into the
shape ``data_push.upsert_economic_events`` writes to Postgres.

It mirrors the resilience conventions of the rest of the pipeline:
  • ``tenacity`` retry with exponential backoff on transient HTTP/network errors
    (same retryable statuses as ``fetch_metrics``);
  • graceful degradation — any failure (missing key, network, bad JSON) logs a
    warning and returns ``[]`` so the surrounding run never aborts (mirrors
    ``political_corruption_fetch._download_owid_df``).

Filtering (both driven by ``constants``):
  • keep only ``High``/``Medium`` impact events (``FMP_CALENDAR_KEEP_IMPACTS``);
  • keep only countries in the curated allowlist (``FMP_CALENDAR_COUNTRIES``),
    which also supplies the display name.

FMP timestamps are UTC; we return timezone-aware UTC datetimes.
"""

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import backend.util.constants as constants
from backend.util import http

logger = logging.getLogger(__name__)


def _to_float_or_none(v: Any) -> Optional[float]:
    """Coerce FMP's numeric-ish fields (often strings or None) to float or None."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_fmp_datetime(s: Any) -> Optional[datetime]:
    """Parse an FMP ``date`` ('YYYY-MM-DD HH:MM:SS', UTC) to an aware UTC datetime."""
    if not isinstance(s, str) or not s.strip():
        return None
    text = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # Last resort: ISO 8601 (allow trailing Z).
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _normalize_event(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Filter + normalize one raw FMP event. Returns None if it should be dropped."""
    if not isinstance(raw, dict):
        return None

    impact = (raw.get("impact") or "").strip().title()
    if impact not in constants.FMP_CALENDAR_KEEP_IMPACTS:
        return None

    code = (raw.get("country") or "").strip().upper()
    country_name = constants.FMP_CALENDAR_COUNTRIES.get(code)
    if not country_name:
        return None

    event_time = _parse_fmp_datetime(raw.get("date"))
    if event_time is None:
        return None

    event = (raw.get("event") or "").strip()
    if not event:
        return None

    return {
        "event_time": event_time,
        "country_code": code,
        "country_name": country_name,
        "event": event,
        "importance": constants.FMP_IMPACT_TO_CODE[impact],
        "currency": (raw.get("currency") or "").strip() or None,
        "previous": _to_float_or_none(raw.get("previous")),
        "estimate": _to_float_or_none(raw.get("estimate")),
        "actual": _to_float_or_none(raw.get("actual")),
    }


def fetch_economic_calendar() -> List[Dict[str, Any]]:
    """Fetch upcoming major economic events from FMP.

    Pulls a rolling window from today through today +
    ``constants.FMP_CALENDAR_DAYS_AHEAD`` days (UTC), keeps only
    High/Medium-impact events in the curated country allowlist, and
    returns normalized, de-duplicated event dicts ready for
    ``data_push.upsert_economic_events``.

    Returns an empty list on any failure (missing key, network, parse) so the
    caller's run is never interrupted.
    """
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        logger.warning("FMP_API_KEY is not set; skipping economic-calendar fetch.")
        return []

    now = datetime.now(timezone.utc)
    params = {
        "from": now.strftime("%Y-%m-%d"),
        "to": (now + timedelta(days=constants.FMP_CALENDAR_DAYS_AHEAD)).strftime("%Y-%m-%d"),
        "apikey": api_key,
    }

    try:
        resp = http.fmp_get(constants.FMP_ECON_CALENDAR_ENDPOINT, params)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001 - graceful degradation by design
        logger.warning("Could not fetch FMP economic calendar: %s", e)
        return []

    if not isinstance(data, list):
        logger.warning("Unexpected FMP economic-calendar payload type: %s", type(data).__name__)
        return []

    out: List[Dict[str, Any]] = []
    seen: set = set()
    for raw in data:
        norm = _normalize_event(raw)
        if norm is None:
            continue
        key = (norm["event_time"], norm["country_code"], norm["event"])
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)

    logger.info("Fetched %d economic-calendar events from FMP (after filtering).", len(out))
    return out
