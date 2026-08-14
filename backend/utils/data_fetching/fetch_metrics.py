"""World Bank indicator fetching.

Pulls the annual macro/governance series that form the backbone of each
country's panel. The World Bank API is the pipeline's least reliable
upstream — it sporadically returns spurious 400s under load and simply has no
data for many (country, indicator) pairs — so every failure mode short of a
programming error degrades to an empty series rather than an exception: a
country missing one indicator still gets a panel, and the run continues.
"""

import logging
import requests
import pandas as pd

from typing import List, Dict, Mapping, Optional
from requests.exceptions import HTTPError, RequestException

import backend.util.constants as constants
from backend.util import http


# ---------------------------- Helpers --------------------------------- #
# WB sporadically throws spurious 400s under load, so unlike FMP we retry 400.
_RETRYABLE_STATUS = frozenset({400, 429, 500, 502, 503, 504})
_DEFAULT_HEADERS = {"User-Agent": http.PROJECT_UA}
_TIMEOUT = 20  # seconds


def _empty_series(indicator: str) -> pd.Series:
    """The 'no data' return shape for wb_series."""
    return pd.Series(dtype="float64", name=indicator)


def _require_non_empty_str(value: object, param: str) -> None:
    """Raise if ``value`` is not a non-blank string.

    Raises:
        TypeError: if ``value`` is not a str.
        ValueError: if it is blank.
    """
    if not isinstance(value, str):
        raise TypeError(f"`{param}` must be a str, got {type(value).__name__}: {value!r}")
    if not value.strip():
        raise ValueError(f"`{param}` must be a non-empty str, got {value!r}")


def _validate_year_range(start: Optional[int], end: Optional[int]) -> None:
    """Raise if the optional year bounds are not ints or are inverted.

    Raises:
        TypeError: if a supplied bound is not an int.
        ValueError: if ``start`` is later than ``end``.
    """
    for name, value in (("start", start), ("end", end)):
        if value is not None and not isinstance(value, int):
            raise TypeError(f"`{name}` must be an int or None, got {type(value).__name__}: {value!r}")
    if start is not None and end is not None and start > end:
        raise ValueError(f"`start` year must be <= `end` year, got start={start}, end={end}")


# ----------------------------- Fetch one series ----------------------------- #
@http.retry_transient(_RETRYABLE_STATUS)
def _wb_request(
    url: str,
    params: Dict[str, str],
    session: Optional[requests.Session],
) -> requests.Response:
    """GET one World Bank URL, retrying transient failures.

    Args:
        url: fully-formatted endpoint for one country/indicator pair.
        params: query string (format, paging, optional date range).
        session: connection-pooling session to reuse, or None for a bare GET.

    Returns:
        The response. Transient statuses raise (so tenacity retries); other
        error statuses are returned for the caller to interpret.
    """
    req = session or requests
    resp = req.get(url, params=params, headers=_DEFAULT_HEADERS, timeout=_TIMEOUT)
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
    """Fetch one World Bank indicator time series for one country.

    Args:
        code: ISO-2 country code (case-insensitive; whitespace tolerated).
        indicator: World Bank series code, e.g. ``'FP.CPI.TOTL.ZG'``.
        start: earliest year to request (inclusive).
        end: latest year to request (inclusive).
        session: session to reuse across indicators for the same country.

    Returns:
        Values indexed by ascending year, named after ``indicator``. Empty
        when the country has no data for this series — a normal outcome, not
        an error: network failures, 400/404, and unparseable payloads all
        degrade to empty so one missing indicator never fails a panel.

    Raises:
        TypeError: if ``code`` or ``indicator`` is not a string, or a year
            bound is not an int.
        ValueError: if either is blank, the year range is inverted, or
            ``WB_ENDPOINT`` is misconfigured with query parameters.
    """
    _require_non_empty_str(code, "code")
    _require_non_empty_str(indicator, "indicator")
    _validate_year_range(start, end)
    if session is not None and not isinstance(session, requests.Session):
        raise TypeError(
            f"`session` must be a requests.Session or None, got {type(session).__name__}"
        )

    norm_code = code.strip().upper()

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
    """Assemble several World Bank indicators for one country into one table.

    Args:
        code: ISO-2 country code.
        indicators: panel column name -> World Bank series code, e.g.
            ``{"INFLATION": "FP.CPI.TOTL.ZG"}``.
        start: earliest year to request (inclusive).
        end: latest year to request (inclusive).

    Returns:
        Year-indexed DataFrame with one column per requested indicator,
        outer-joined so a country with partial coverage still gets a panel.
        Columns with no data are present but all-NaN.

    Raises:
        TypeError: if ``code`` is not a string or ``indicators`` is not a mapping.
        ValueError: if ``code`` is blank, ``indicators`` is empty or contains
            blank names/codes, or the year range is inverted.
    """
    _require_non_empty_str(code, "code")
    if not isinstance(indicators, Mapping):
        raise TypeError(f"`indicators` must be a mapping, got {type(indicators).__name__}")
    if not indicators:
        raise ValueError("`indicators` mapping must not be empty")
    bad_names = [k for k in indicators if not (isinstance(k, str) and k.strip())]
    if bad_names:
        raise ValueError(f"all indicator names must be non-empty str, got {bad_names!r}")
    bad_codes = [v for v in indicators.values() if not (isinstance(v, str) and v.strip())]
    if bad_codes:
        raise ValueError(f"all World-Bank codes must be non-empty str, got {bad_codes!r}")
    _validate_year_range(start, end)

    frames: List[pd.Series] = []
    # Reuse one session per country to avoid excess handshakes
    with requests.Session() as sess:
        sess.headers.update(_DEFAULT_HEADERS)
        for col, ind_code in indicators.items():
            try:
                s = wb_series(code, ind_code, start=start, end=end, session=sess)
                s.name = col
            except Exception as e:  # noqa: BLE001 - one bad indicator must not fail the panel
                logging.warning("WB error for %s/%s: %s (skipping)", code, ind_code, e)
                s = pd.Series(dtype="float64", name=col)

            frames.append(s)

    if not frames:
        return pd.DataFrame()

    panel = pd.concat(frames, axis=1, sort=True)  # outer-join on year
    try:
        panel = panel.astype("float64")
    except (TypeError, ValueError):
        # Mixed dtypes across indicators: leave as-is rather than lose the panel.
        pass
    return panel
