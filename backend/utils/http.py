"""Shared HTTP plumbing for the API-fetch modules.

Three fetch modules (World Bank, FMP calendar, FMP prices) carried identical
copies of the transient-error predicate and the tenacity retry decorator; this
module is their single source. The *set* of retryable statuses stays a
parameter on purpose: the World Bank API sporadically throws spurious 400s
under load and is retried on 400, while FMP is not — merging the sets would
change retry behavior.

Also holds the two shared User-Agent strings: the project UA sent to data
APIs, and the browser UA the news scrapers need (some publishers serve
different HTML to non-browser agents).
"""

from typing import AbstractSet, Any, Callable, Dict

import requests
from requests.exceptions import Timeout, ConnectionError, RequestException
from tenacity import (
    retry,
    wait_exponential_jitter,
    stop_after_attempt,
    retry_if_exception,
)

# Polite identification for data APIs (World Bank, FMP).
PROJECT_UA = "AI-Country-Risk/1.0 (+https://github.com/EliFebres/AI-Country-Risk-Dashboard)"

# Realistic browser UA for news scraping; some endpoints return different
# (or no) HTML to non-browser agents.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
)

# Statuses FMP endpoints are retried on (no 400 — an FMP 400 is a real error).
FMP_RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})

_REQUEST_TIMEOUT = 20  # seconds


def _is_retryable_exc(exc: BaseException, retryable_status: AbstractSet[int]) -> bool:
    """True for network errors and HTTP errors whose status is transient.

    Args:
        exc: the exception raised by ``requests``.
        retryable_status: statuses considered transient for this API.
    """
    if isinstance(exc, (Timeout, ConnectionError)):
        return True
    if isinstance(exc, RequestException):
        resp = getattr(exc, "response", None)
        return getattr(resp, "status_code", None) in retryable_status
    return False


def retry_transient(retryable_status: AbstractSet[int]) -> Callable:
    """Tenacity decorator: exponential jitter 1–30s, 5 attempts, transient-only.

    Args:
        retryable_status: statuses considered transient for this API.
    """
    return retry(
        wait=wait_exponential_jitter(initial=1, max=30),
        stop=stop_after_attempt(5),
        retry=retry_if_exception(lambda exc: _is_retryable_exc(exc, retryable_status)),
        reraise=True,
    )


@retry_transient(FMP_RETRYABLE_STATUS)
def fmp_get(url: str, params: Dict[str, Any]) -> requests.Response:
    """Single GET to an FMP endpoint, retrying transient errors.

    Raises on retryable statuses so tenacity retries; non-transient statuses
    fall through for the caller to handle.
    """
    resp = requests.get(url, params=params, headers={"User-Agent": PROJECT_UA},
                        timeout=_REQUEST_TIMEOUT)
    if resp.status_code in FMP_RETRYABLE_STATUS:
        resp.raise_for_status()
    return resp
