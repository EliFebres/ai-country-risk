# backend/utils/data_fetching/fetch_metrics.py
import logging
import requests
import pandas as pd

from typing import List, Dict, Mapping, Optional
from requests.exceptions import HTTPError, RequestException

import backend.utils.constants as constants
from backend.utils import http


# ---------------------------- Helpers --------------------------------- #
# WB sporadically throws spurious 400s under load, so unlike FMP we retry 400.
_RETRYABLE_STATUS = frozenset({400, 429, 500, 502, 503, 504})
_DEFAULT_HEADERS = {"User-Agent": http.PROJECT_UA}


def _empty_series(indicator: str) -> pd.Series:
    """The 'no data' return shape for wb_series."""
    return pd.Series(dtype="float64", name=indicator)


# ----------------------------- Fetch one series ----------------------------- #
@http.retry_transient(_RETRYABLE_STATUS)
def _wb_request(
    url: str,
    params: Dict[str, str],
    session: Optional[requests.Session],
) -> requests.Response:
    req = session or requests
    # Merge a UA header in a non-destructive way
    try:
        resp = req.get(url, params=params, headers=_DEFAULT_HEADERS, timeout=20)
    except RequestException as e:
        # Let tenacity decide if we retry
        raise e
    # If transient: raise so tenacity retries; if non-transient: we handle in caller.
    if resp.status_code in _RETRYABLE_STATUS:
        try:
            resp.raise_for_status()
        except HTTPError as e:
            e.response = resp  # ensure status is visible in retry predicate
            raise e
    return resp


def wb_series(
    code: str,
    indicator: str,
    *,
    start: Optional[int] = None,
    end:   Optional[int] = None,
    session: Optional[requests.Session] = None,
) -> pd.Series:
    """
    Fetch a World Bank indicator time series for a single country.

    Returns:
        pandas.Series of values indexed by ascending year; empty on 'no data'.

    Robustness:
      - Retries only on transient statuses (429/5xx) and network errors.
      - Treats 200 with empty rows, 400/404 as 'no data' (empty), not as an error.
    """
    # Input validation
    assert isinstance(code, str) and code.strip(),  "`code` must be non-empty str"
    assert isinstance(indicator, str) and indicator.strip(), "`indicator` must be non-empty str"
    if start is not None:
        assert isinstance(start, int), "`start` must be int"
    if end is not None:
        assert isinstance(end, int),   "`end` must be int"
    if start is not None and end is not None:
        assert start <= end, "`start` year must be ≤ `end` year"
    if session is not None:
        assert isinstance(session, requests.Session), "`session` must be requests.Session"

    norm_code = (code or "").strip().upper()

    if "?" in constants.WB_ENDPOINT:
        raise ValueError("WB_ENDPOINT should not include query parameters")

    url = constants.WB_ENDPOINT.format(code=norm_code, ind=indicator)
    params: Dict[str, str] = {"format": "json", "per_page": "1000"}
    if start is not None and end is not None:
        params["date"] = f"{start}:{end}"

    # Perform request with retry-on-transient
    try:
        resp = _wb_request(url, params, session)
    except RequestException as e:
        # If the exception was already filtered as non-retryable, we land here.
        logging.warning("WB network error for %s/%s: %s (skipping)", norm_code, indicator, e)
        return _empty_series(indicator)

    # Handle non-transient statuses gracefully (e.g., 400/404 → no data)
    if resp.status_code >= 400:
        if resp.status_code in (400, 404):
            logging.warning("WB %s for %s/%s (treating as empty)", resp.status_code, norm_code, indicator)
            return _empty_series(indicator)
        # Anything else 4xx that slipped through
        try:
            resp.raise_for_status()
        except HTTPError as e:
            logging.warning("WB HTTP %s for %s/%s: %s (skipping)", resp.status_code, norm_code, indicator, e)
            return _empty_series(indicator)

    # Parse payload
    try:
        payload = resp.json()
    except ValueError:
        logging.warning("WB invalid JSON for %s/%s (treating as empty)", norm_code, indicator)
        return _empty_series(indicator)

    if not isinstance(payload, list) or len(payload) < 2:
        logging.warning("WB unexpected payload for %s/%s: %s (treating as empty)", norm_code, indicator, payload)
        return _empty_series(indicator)

    rows = payload[1] or []  # WB returns [meta, rows]; rows can be None

    # Build (year, value) pairs in WB default order (desc by year)
    series_pairs = []
    for item in rows:
        try:
            year = int(item.get("date"))
        except (TypeError, ValueError):
            continue
        val = item.get("value")
        series_pairs.append((year, float(val) if val is not None else None))

    # Local year filtering when only one bound supplied
    if start is not None and end is None:
        series_pairs = [(y, v) for y, v in series_pairs if y >= start]
    elif end is not None and start is None:
        series_pairs = [(y, v) for y, v in series_pairs if y <= end]

    if not series_pairs:
        return _empty_series(indicator)
    years = [y for (y, _) in series_pairs]
    vals  = [v for (_, v) in series_pairs]
    return pd.Series(vals, index=years, name=indicator).sort_index()


# --------------------------- Multi-indicator panel --------------------------- #
def build_country_panel(
    code: str,
    indicators: Mapping[str, str],
    *,
    start: Optional[int] = None,
    end:   Optional[int] = None,
) -> pd.DataFrame:
    """
    Assemble multiple World Bank indicators for one country into a year-indexed table.
    More resilient: reuses a single Session and tolerates missing indicators without failing the panel.
    """
    assert isinstance(code, str) and code.strip(), "`code` must be non-empty str"
    assert indicators, "`indicators` mapping must not be empty"
    assert all(isinstance(k, str) and k.strip() for k in indicators.keys()), \
        "all indicator names must be non-empty str"
    assert all(isinstance(v, str) and v.strip() for v in indicators.values()), \
        "all World-Bank codes must be non-empty str"
    if start is not None and end is not None:
        assert start <= end, "`start` year must be ≤ `end` year"

    frames: List[pd.Series] = []
    # Reuse one session per country to avoid excess handshakes
    with requests.Session() as sess:
        sess.headers.update(_DEFAULT_HEADERS)
        for col, ind_code in indicators.items():
            try:
                s = wb_series(code, ind_code, start=start, end=end, session=sess)
                s.name = col
            except RequestException as e:
                logging.warning("WB network error for %s/%s: %s (skipping)", code, ind_code, e)
                s = pd.Series(dtype="float64", name=col)
            except Exception as e:
                logging.warning("WB error for %s/%s: %s (skipping)", code, ind_code, e)
                s = pd.Series(dtype="float64", name=col)

            frames.append(s)

    if not frames:
        return pd.DataFrame()

    panel = pd.concat(frames, axis=1, sort=True)  # outer-join on year
    try:
        panel = panel.astype("float64")
    except Exception:
        pass
    return panel
