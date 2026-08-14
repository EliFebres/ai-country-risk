"""Guardian Content API harvester — the one source that needs no recovery.

The Guardian hands back ``bodyText`` in the search response itself, so an
article arrives complete: no Wayback lookup, no live refetch, no leakage risk.
That makes this the spine of the historical corpus and the yardstick the other
two sources are measured against — where Guardian coverage is thin, the recovery
curve is what has to carry the week.

What this module is really solving is quota. The free developer tier is a daily
call budget, and the naive shape of the harvest — a call per country per month
per theme — spends about 3,600 of them, which is over a week of harvesting for
five countries. So windows start at a **year** with the API's maximum page size,
and only the windows that overflow get subdivided into quarters and then months.
The US triggers that; Portugal does not. Results are ordered by date rather than
relevance, because relevance ordering plus a page cap silently truncates the
tail of a window and nothing anywhere reports it.

The six per-theme queries are kept rather than collapsed into one OR'd query.
Collapsing would be cheaper and would break the point: the historical corpus has
to be retrieved the same way the live run retrieves, or the per-theme floor in
snapshot assembly is filling slots from a differently-shaped pool.

Running out of quota mid-harvest is a normal outcome, not a failure. The run
checkpoints what it finished, says how many more days it needs, and exits 0.
"""

import datetime
import logging
import os
import time
from typing import Dict, Iterator, List, Optional, Tuple

import requests

from backend.util import http
from backend.data_upsert import store
from backend.util import config
from backend.news_fetching import core

logger = logging.getLogger(__name__)

SOURCE_SYSTEM = "guardian"
_ENDPOINT = "https://content.guardianapis.com/search"

# Guardian reports the remaining daily allowance on every response. Read rather
# than assumed: the documented free-tier number has changed more than once, and
# a harvest that plans against a stale constant plans wrong.
_QUOTA_HEADER = "X-RateLimit-Remaining-Day"
_LIMIT_HEADER = "X-RateLimit-Limit-Day"


class QuotaExhausted(RuntimeError):
    """The daily call budget ran out. Not an error — a scheduling fact.

    Carries the tier's own daily limit when the response reported one, so the
    driver can say how many more days the harvest needs rather than guessing.
    """

    def __init__(self, message: str, daily_limit: Optional[int] = None):
        super().__init__(message)
        self.daily_limit = daily_limit


def _api_key() -> str:
    """Read ``GUARDIAN_API_KEY`` at call time, never cached at import.

    Same contract as ``DATABASE_URL`` and ``OPENAI_API_KEY``: the process loads
    one ``.env`` at startup and every module reads env when it needs it.
    """
    key = os.getenv("GUARDIAN_API_KEY")
    if not key:
        raise RuntimeError(
            "GUARDIAN_API_KEY is not set. A free developer key comes from "
            "https://open-platform.theguardian.com/access/ — put it in backend/.env"
        )
    return key


# ---------------------------------------------------------------------------
# Queries and windows
# ---------------------------------------------------------------------------

def guardian_query(theme: str, country_name: str) -> str:
    """Translate one live theme query into Guardian's ``q`` syntax.

    Google News takes ``"Brazil" (tax OR customs)`` as an implicit AND; the
    Guardian wants it spelled out. Derived from ``core.THEME_QUERIES`` rather
    than retyped, so a term added to the live retrieval reaches the historical
    one automatically — the two must ask for the same news or the corpora are
    not comparable.
    """
    query = core.THEME_QUERIES[theme].format(c=country_name)
    return query.replace('" (', '" AND (', 1)


def _add_months(day: datetime.date, months: int) -> datetime.date:
    """``day`` shifted by whole months, clamped to the 1st (windows start there)."""
    total = (day.year * 12 + day.month - 1) + months
    return datetime.date(total // 12, total % 12 + 1, 1)


def _chunks(start: datetime.date, end: datetime.date, months: int
            ) -> List[Tuple[datetime.date, datetime.date]]:
    """Split ``[start, end]`` into consecutive windows of ``months`` months."""
    out: List[Tuple[datetime.date, datetime.date]] = []
    cursor = start
    while cursor <= end:
        nxt = min(_add_months(cursor, months) - datetime.timedelta(days=1), end)
        out.append((cursor, nxt))
        cursor = nxt + datetime.timedelta(days=1)
    return out


def _span_months(start: datetime.date, end: datetime.date) -> int:
    """Roughly how many months a window covers."""
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def subdivide(start: datetime.date, end: datetime.date
              ) -> List[Tuple[datetime.date, datetime.date]]:
    """Break an overflowing window down one level: year → quarters → months.

    Returns an empty list for a window already a month wide — there is nowhere
    further to go, and the caller pages it as far as it can and logs what it had
    to leave. A silent truncation would read as "we covered that month".
    """
    months = _span_months(start, end)
    if months > 3:
        return _chunks(start, end, 3)
    if months > 1:
        return _chunks(start, end, 1)
    return []


def year_windows(start: datetime.date, end: datetime.date
                 ) -> List[Tuple[datetime.date, datetime.date]]:
    """The top-level harvest windows, one per calendar year in range.

    Calendar years rather than rolling twelve-month spans so a resumed harvest
    always asks for the same windows as the run before it, which is what makes
    the checkpoints mean anything.
    """
    out: List[Tuple[datetime.date, datetime.date]] = []
    for year in range(start.year, end.year + 1):
        out.append((max(datetime.date(year, 1, 1), start),
                    min(datetime.date(year, 12, 31), end)))
    return out


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------

@http.retry_transient(frozenset({429, 500, 502, 503, 504}))
def _get(params: Dict[str, object]) -> requests.Response:
    """One Content API call, retrying transient errors and 429s."""
    resp = requests.get(_ENDPOINT, params=params,
                        headers={"User-Agent": http.PROJECT_UA}, timeout=30)
    if resp.status_code in (429, 500, 502, 503, 504):
        resp.raise_for_status()
    return resp


def _int_or_none(value: Optional[str]) -> Optional[int]:
    """Parse a rate-limit header, tolerating an absent or malformed one."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _page(query: str, start: datetime.date, end: datetime.date, page: int) -> Dict:
    """Fetch one page of one window.

    ``order-by=newest`` is deliberate: date order is stable across pages, so a
    window that has to stop early stops at a known boundary. Relevance order
    would drop an arbitrary slice of the window with nothing to show for it.
    """
    resp = _get({
        "q": query,
        "from-date": start.isoformat(),
        "to-date": end.isoformat(),
        "show-fields": "bodyText",
        "page-size": config.GUARDIAN_PAGE_SIZE,
        "order-by": "newest",
        "page": page,
        "api-key": _api_key(),
    })
    limit = _int_or_none(resp.headers.get(_LIMIT_HEADER))
    remaining = _int_or_none(resp.headers.get(_QUOTA_HEADER))
    if remaining is not None and remaining <= 0:
        raise QuotaExhausted(f"daily Guardian call budget spent (limit {limit})", limit)
    if resp.status_code == 429:
        raise QuotaExhausted("Guardian returned 429 after backoff", limit)
    resp.raise_for_status()
    body = resp.json().get("response") or {}
    if body.get("status") != "ok":
        raise RuntimeError(f"Guardian API said {body.get('status')!r}: {body.get('message')}")
    return body


def to_item(result: Dict, theme: str) -> Optional[Dict]:
    """Turn one Content API result into a canonical article item.

    Returns None for a result with no URL or no publication date — both are
    required to place an article in a snapshot window, and neither is worth
    failing a whole window over.
    """
    url = (result.get("webUrl") or "").strip()
    published = result.get("webPublicationDate")
    if not url or not published:
        return None
    body = ((result.get("fields") or {}).get("bodyText") or "")[:core.MAX_BODY_CHARS]
    return core.normalize_item(
        title=result.get("webTitle") or "",
        link=url,
        published=published,
        source="The Guardian",
        text=body,
        theme=theme,
    )


def _window_items(query: str, theme: str, start: datetime.date,
                  end: datetime.date, calls: List[int]) -> Iterator[Dict]:
    """Every article in one window, subdividing rather than truncating.

    ``calls`` is a one-element list used as a counter, so the driver can report
    real call spend against its own pre-flight estimate instead of the estimate
    twice.
    """
    time.sleep(config.REQUEST_INTERVAL_SECONDS)
    calls[0] += 1
    first = _page(query, start, end, 1)
    pages = int(first.get("pages") or 1)

    if pages > config.GUARDIAN_SUBDIVIDE_ABOVE_PAGES:
        children = subdivide(start, end)
        if children:
            logger.info("  %s %s..%s: %s pages, splitting into %d",
                        theme, start, end, pages, len(children))
            for child_start, child_end in children:
                yield from _window_items(query, theme, child_start, child_end, calls)
            return
        logger.warning("  %s %s..%s: %d pages and already one month wide — "
                       "taking the first %d pages, %d left behind",
                       theme, start, end, pages,
                       config.GUARDIAN_SUBDIVIDE_ABOVE_PAGES,
                       pages - config.GUARDIAN_SUBDIVIDE_ABOVE_PAGES)
        pages = config.GUARDIAN_SUBDIVIDE_ABOVE_PAGES

    for result in first.get("results") or []:
        item = to_item(result, theme)
        if item:
            yield item

    for page in range(2, pages + 1):
        time.sleep(config.REQUEST_INTERVAL_SECONDS)
        calls[0] += 1
        for result in _page(query, start, end, page).get("results") or []:
            item = to_item(result, theme)
            if item:
                yield item


# ---------------------------------------------------------------------------
# The harvest
# ---------------------------------------------------------------------------

def harvest_window(iso2: str, country_name: str, start: datetime.date,
                   end: datetime.date, calls: List[int]) -> int:
    """Harvest one country-year across all six themes, de-duplicated.

    First-seen-wins across themes with ``broad`` queried last, exactly as the
    live run does it, so a story a specific theme also found keeps the specific
    tag and the per-theme floor has something to ration.
    """
    seen: Dict[str, Dict] = {}
    for theme in core.THEME_QUERIES:
        query = guardian_query(theme, country_name)
        for item in _window_items(query, theme, start, end, calls):
            seen.setdefault(core.dedupe_key(item), item)

    rows = [store.article_row(item, country_iso2=iso2, source_system=SOURCE_SYSTEM,
                              body_status="recovered", body_vintage="api-native")
            for item in seen.values() if item.get("text")]
    # An article the API returned without a body is a paywalled or withdrawn
    # piece; it goes in as a stub for Wayback to try, not as a silent loss.
    rows += [store.article_row(item, country_iso2=iso2, source_system=SOURCE_SYSTEM,
                               body_status="pending")
             for item in seen.values() if not item.get("text")]
    store.upsert_articles(rows)
    return len(rows)


def harvest(roster: Optional[List[str]] = None,
            since: Optional[str] = None) -> int:
    """Harvest every pilot country from ``PILOT_START`` to today.

    Resumable: a completed (country, year) is checkpointed and skipped on a
    re-run. Quota exhaustion checkpoints what finished, reports how many more
    days the harvest needs, and returns normally — the caller exits 0.

    Returns:
        Rows written this run.
    """
    roster = roster or config.PILOT_ROSTER
    start = datetime.date.fromisoformat(since or config.PILOT_START)
    end = datetime.date.today()
    windows = year_windows(start, end)

    todo = [(iso2, w) for iso2 in roster for w in windows
            if w[0] not in store.completed_windows(SOURCE_SYSTEM, iso2)]
    estimate = len(todo) * len(core.THEME_QUERIES)
    logger.info("[guardian] %d country-years x %d themes = ~%d calls before any "
                "subdivision (%d country-years already done)",
                len(todo), len(core.THEME_QUERIES), estimate,
                len(roster) * len(windows) - len(todo))

    calls = [0]
    written = 0
    for done, (iso2, (window_start, window_end)) in enumerate(todo):
        name = config.country_name(iso2)
        try:
            n = harvest_window(iso2, name, window_start, window_end, calls)
        except QuotaExhausted as exc:
            left = len(todo) - done
            per_day = exc.daily_limit or calls[0] or 1
            logger.warning(
                "[guardian] %s; stopping cleanly after %d calls. %d of %d "
                "country-years left, ~%d calls — roughly %d more day(s). "
                "Re-run to resume where this stopped.",
                exc, calls[0], left, len(todo), left * len(core.THEME_QUERIES),
                -(-left * len(core.THEME_QUERIES) // per_day))
            return written
        except Exception:  # noqa: BLE001
            # Checkpointed as failed rather than left unwritten, so the next run
            # retries it — only 'done' windows are skipped. A single 503 on the
            # eighth of twelve windows must not throw away the seven behind it,
            # which is exactly what an uncaught one did on 2026-08-03.
            logger.exception("[guardian] %s %s failed; continuing",
                             iso2, window_start.year)
            store.write_checkpoint(SOURCE_SYSTEM, iso2, window_start, window_end,
                                   status="failed", note="request error")
            continue
        store.write_checkpoint(SOURCE_SYSTEM, iso2, window_start, window_end,
                               items_written=n)
        written += n
        logger.info("[guardian] %s %s: %d rows (%d calls so far)",
                    iso2, window_start.year, n, calls[0])

    logger.info("[guardian] done: %d rows in %d calls", written, calls[0])
    return written
