"""
Live prices (Financial Modeling Prep) for the bottom-bar "Prices" pane.

This module backs every FMP-sourced asset in the Prices daemon: equity indices,
the relabeled MSCI ETF proxies, crypto, commodities, and the US Treasury yields
(the Bonds rows). FMP has no non-US sovereign-yield feed, so the Bonds pane
tracks US tenors only.

Responsibilities, all designed to keep API hits minimal:
  • ``fetch_live_quotes`` — ONE batched call per tick returns the current price
    and 1D % change for every requested symbol (FMP's ``batch-quote`` endpoint
    accepts a comma-separated ``symbols`` list of mixed symbol types).
  • ``fetch_reference_closes`` — called at most once per day, reads each symbol's
    daily history and extracts the quarter-start and year-start closing prices
    used to compute the 1Q and YTD changes in-process on every tick.
  • ``fetch_treasury_yields`` — called at most once per day, returns US Treasury
    par yields (px + 1D/1Q/YTD point changes) from one treasury-rates call.

It mirrors the resilience conventions of ``fmp_calendar_fetch``:
  • ``tenacity`` retry with exponential backoff on transient HTTP/network errors;
  • graceful degradation — any failure logs a warning and returns an empty
    result (per symbol where possible) so the daemon loop never aborts.
"""

import os
import logging
from datetime import datetime, timezone, date
from typing import Any, Dict, List, Optional

import requests
from requests.exceptions import HTTPError, Timeout, ConnectionError, RequestException
from tenacity import (
    retry,
    wait_exponential_jitter,
    stop_after_attempt,
    retry_if_exception,
)

import backend.utils.constants as constants

logger = logging.getLogger(__name__)

# Transient statuses worth retrying (mirrors fmp_calendar_fetch._RETRYABLE_STATUS).
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_DEFAULT_HEADERS = {
    "User-Agent": "AI-Country-Risk/1.0 (+https://github.com/EliFebres/AI-Country-Risk-Dashboard)"
}
_TIMEOUT = 20  # seconds


def _is_retryable_exc(exc: BaseException) -> bool:
    """Retry on network/transient HTTP conditions only."""
    if isinstance(exc, (Timeout, ConnectionError)):
        return True
    if isinstance(exc, HTTPError):
        resp = getattr(exc, "response", None)
        return getattr(resp, "status_code", None) in _RETRYABLE_STATUS
    if isinstance(exc, RequestException):
        resp = getattr(exc, "response", None)
        return getattr(resp, "status_code", None) in _RETRYABLE_STATUS
    return False


def _to_float_or_none(v: Any) -> Optional[float]:
    """Coerce FMP's numeric-ish fields (sometimes strings, '+1.2%', or None)."""
    if v is None or v == "":
        return None
    if isinstance(v, str):
        v = v.strip().rstrip("%")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@retry(
    wait=wait_exponential_jitter(initial=1, max=30),
    stop=stop_after_attempt(5),
    retry=retry_if_exception(_is_retryable_exc),
    reraise=True,
)
def _fmp_get(url: str, params: Dict[str, str]) -> requests.Response:
    """Single GET to an FMP endpoint, retrying transient errors."""
    resp = requests.get(url, params=params, headers=_DEFAULT_HEADERS, timeout=_TIMEOUT)
    if resp.status_code in _RETRYABLE_STATUS:
        # Raise so tenacity retries; non-transient statuses fall through to the caller.
        resp.raise_for_status()
    return resp


def fetch_live_quotes(source_symbols: List[str]) -> Dict[str, Dict[str, Optional[float]]]:
    """Fetch current price + 1D % change for ``source_symbols`` in one batch call.

    Returns ``{source_symbol: {"px": float|None, "chg_1d": float|None}}`` for the
    symbols FMP returned. Missing symbols are simply absent. Returns ``{}`` on any
    failure (missing key, network, parse) so the caller's tick is never interrupted.
    """
    if not source_symbols:
        return {}

    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        logger.warning("FMP_API_KEY is not set; skipping live-quote fetch.")
        return {}

    try:
        resp = _fmp_get(
            constants.FMP_QUOTE_ENDPOINT,
            {"symbols": ",".join(source_symbols), "apikey": api_key},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001 - graceful degradation by design
        logger.warning("Could not fetch FMP live quotes: %s", e)
        return {}

    if not isinstance(data, list):
        logger.warning("Unexpected FMP quote payload type: %s", type(data).__name__)
        return {}

    out: Dict[str, Dict[str, Optional[float]]] = {}
    for raw in data:
        if not isinstance(raw, dict):
            continue
        sym = raw.get("symbol")
        if not sym:
            continue
        out[sym] = {
            "px": _to_float_or_none(raw.get("price")),
            # Stable quote uses `changePercentage`; tolerate the legacy `changesPercentage` too.
            "chg_1d": _to_float_or_none(
                raw.get("changePercentage", raw.get("changesPercentage"))
            ),
        }

    logger.info("Fetched %d/%d FMP live quotes.", len(out), len(source_symbols))
    return out


def _quarter_start(d: date) -> date:
    """First calendar day of the quarter containing ``d``."""
    q_first_month = ((d.month - 1) // 3) * 3 + 1
    return date(d.year, q_first_month, 1)


def fetch_treasury_yields(
    assets: List[Dict[str, Any]], now_utc: Optional[datetime] = None
) -> Dict[str, Dict[str, Optional[float]]]:
    """Fetch US Treasury par yields for the given bond assets in one call.

    ``assets`` are PRICE_ASSETS bond entries whose ``source_symbol`` is an FMP
    treasury-rates tenor field (e.g. ``year2``/``year10``/``year30``). The
    endpoint returns a daily history with every tenor as a column, so a single
    from/to call yields px plus the 1D/1Q/YTD POINT changes for all tenors.

    Returns ``{internal_symbol: {"px", "chg", "q", "ytd"}}`` (points) for the
    tenors that resolved. Returns ``{}`` on any failure (missing key, network,
    parse) so the daemon's daily refresh is never interrupted.
    """
    if not assets:
        return {}

    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        logger.warning("FMP_API_KEY is not set; skipping treasury-yield fetch.")
        return {}

    now = now_utc or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date()
    year_start = date(today.year, 1, 1)
    quarter_start = _quarter_start(today)

    params = {
        "from": year_start.strftime("%Y-%m-%d"),
        "to": today.strftime("%Y-%m-%d"),
        "apikey": api_key,
    }
    try:
        resp = _fmp_get(constants.FMP_TREASURY_ENDPOINT, params)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001 - graceful degradation by design
        logger.warning("Could not fetch FMP treasury rates: %s", e)
        return {}

    if not isinstance(data, list) or not data:
        logger.warning("Unexpected FMP treasury-rates payload.")
        return {}

    # Build an ascending date index so quarter-/year-start lookups are simple.
    rows: List[Dict[str, Any]] = []
    for r in data:
        if not isinstance(r, dict):
            continue
        ds = r.get("date")
        if not ds:
            continue
        try:
            d = date.fromisoformat(str(ds)[:10])
        except ValueError:
            continue
        rows.append({"date": d, "row": r})
    rows.sort(key=lambda x: x["date"])
    if not rows:
        return {}

    out: Dict[str, Dict[str, Optional[float]]] = {}
    for a in assets:
        internal = a.get("symbol")
        field = a.get("source_symbol")
        if not internal or not field:
            continue
        # Series of (date, value) for this tenor, dropping gaps.
        series = [
            (x["date"], _to_float_or_none(x["row"].get(field)))
            for x in rows
            if _to_float_or_none(x["row"].get(field)) is not None
        ]
        if not series:
            continue
        px = series[-1][1]
        prev = series[-2][1] if len(series) >= 2 else None
        ref_q = next((v for d, v in series if d >= quarter_start), None)
        ref_ytd = next((v for d, v in series if d >= year_start), None)
        out[internal] = {
            "px": round(px, 3),
            "chg": round(px - prev, 3) if prev is not None else None,
            "q": round(px - ref_q, 3) if ref_q is not None else None,
            "ytd": round(px - ref_ytd, 3) if ref_ytd is not None else None,
        }

    logger.info("Fetched FMP treasury yields for %d/%d tenors.", len(out), len(assets))
    return out


def _first_close_on_or_after(
    series: List[Dict[str, Any]], cutoff: date
) -> Optional[Dict[str, Any]]:
    """First ``{date, close}`` in an ascending series whose date >= ``cutoff``."""
    for row in series:
        if row["date"] >= cutoff:
            return row
    return None


def _fetch_history(symbol: str, api_key: str, year_start: date, today: date) -> List[Dict[str, Any]]:
    """Return an ascending ``[{date, close}]`` daily series for ``symbol``."""
    params = {
        "symbol": symbol,
        "from": year_start.strftime("%Y-%m-%d"),
        "to": today.strftime("%Y-%m-%d"),
        "apikey": api_key,
    }
    resp = _fmp_get(constants.FMP_HISTORICAL_ENDPOINT, params)
    resp.raise_for_status()
    data = resp.json()

    # Stable returns a bare list of {date, close, ...} (newest first); the legacy
    # path wraps it as {"symbol": ..., "historical": [...]}. Handle both, then
    # normalize to ascending [{date, close}].
    raw_rows = data.get("historical") if isinstance(data, dict) else data
    if not isinstance(raw_rows, list):
        return []

    rows: List[Dict[str, Any]] = []
    for r in raw_rows:
        if not isinstance(r, dict):
            continue
        ds = r.get("date")
        close = _to_float_or_none(r.get("close"))
        if not ds or close is None:
            continue
        try:
            d = date.fromisoformat(str(ds)[:10])
        except ValueError:
            continue
        rows.append({"date": d, "close": close})

    rows.sort(key=lambda x: x["date"])
    return rows


def fetch_reference_closes(
    source_symbols: List[str], now_utc: Optional[datetime] = None
) -> Dict[str, Dict[str, Any]]:
    """Read quarter-start and year-start reference closes for each symbol.

    Called at most once per day. Returns
    ``{source_symbol: {"ref_q", "ref_q_date", "ref_ytd", "ref_ytd_date"}}`` for
    the symbols that resolved. Each symbol is fetched independently and a failure
    on one never drops the others. Returns ``{}`` if the API key is missing.
    """
    if not source_symbols:
        return {}

    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        logger.warning("FMP_API_KEY is not set; skipping reference-close fetch.")
        return {}

    now = now_utc or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date()
    year_start = date(today.year, 1, 1)
    quarter_start = _quarter_start(today)

    out: Dict[str, Dict[str, Any]] = {}
    for sym in source_symbols:
        try:
            series = _fetch_history(sym, api_key, year_start, today)
        except Exception as e:  # noqa: BLE001 - degrade per-symbol
            logger.warning("Could not fetch FMP history for %s: %s", sym, e)
            continue
        if not series:
            continue

        ytd_row = _first_close_on_or_after(series, year_start)
        q_row = _first_close_on_or_after(series, quarter_start)
        if ytd_row is None and q_row is None:
            continue

        out[sym] = {
            "ref_ytd": ytd_row["close"] if ytd_row else None,
            "ref_ytd_date": ytd_row["date"] if ytd_row else None,
            "ref_q": q_row["close"] if q_row else None,
            "ref_q_date": q_row["date"] if q_row else None,
        }

    logger.info("Fetched reference closes for %d/%d FMP symbols.", len(out), len(source_symbols))
    return out
