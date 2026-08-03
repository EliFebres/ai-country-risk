"""GDELT DOC 2.0 harvester — breadth without bodies. **Dormant: see below.**

GDELT indexes an order of magnitude more of the world's news than any single
publisher's API, which is what makes a country like Turkey or Korea harvestable
at all: the Guardian covers them, but it covers them as foreign news, a few
stories a month. GDELT sees the wires.

What it does not return is text. Every row lands ``body_status='pending'`` and
step 4 goes back over the queue with the Wayback Machine. That is the trade —
breadth now, recovery later, and the recovery curve is what says how much of
that breadth actually survived.

The DOC API over HTTP, not BigQuery: no key, no credentials, no new dependency,
and a month-windowed query is cheap enough that there is no reason to reach for
the warehouse.

Why this source is not in the pilot
-----------------------------------
The plan above assumed the only discipline was politeness. It is not. Measured
against the live endpoint on 2026-08-03, twelve calls varying both spacing and
query form:

    30s apart — 1 of 6 answered
    20s apart — 1 of 4
    10s apart — 2 of 4

The failure is not the request interval and not the query syntax: quoted and
unquoted multi-word phrases each both succeeded and failed across repetitions.
The signature is a per-IP budget — the first call after a multi-minute idle is
answered and everything after it 429s, whatever the spacing. The body returned
with the 429 is GDELT's generic error page, which is why it reads like a rate
limit however you provoke it.

At that throughput the 3,500-call pilot harvest is roughly twelve days of wall
clock with most windows failing. Guardian and NYT already put ~91k articles in
the store across the roster, and what GDELT would add is bodyless stubs that
each cost a Wayback fetch on top.

So the module stays, tested and working, and nothing calls it. The upgrade path
is the one GDELT's own error page points at: the web-ngrams dataset, or a bulk
route rather than a query-per-window one. Revisit at 48-country scale, where the
breadth would actually be worth the engineering.
"""

import datetime
import logging
import time
from typing import Dict, List, Optional, Tuple

import requests

from backend.utils import http
from backend.utils.history import config, store
from backend.utils.news_fetching import core

logger = logging.getLogger(__name__)

SOURCE_SYSTEM = "gdelt"
_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"

# English only, matching the live feed's `hl=en-US` decision. Not a claim that
# English coverage is better — a claim that the historical corpus has to be the
# same language as the corpus the scorer was calibrated on.
_LANG_FILTER = "sourcelang:english"


def gdelt_query(theme: str, country_name: str) -> str:
    """Translate one live theme query into DOC-API syntax.

    Derived from ``core.THEME_QUERIES`` for the same reason the Guardian's is:
    the historical path has to ask for the same news the live path asks for, or
    the two corpora are not comparable and neither is the series built on them.
    """
    query = core.THEME_QUERIES[theme].format(c=country_name)
    return f"{query} {_LANG_FILTER}"


def month_windows(start: datetime.date, end: datetime.date
                  ) -> List[Tuple[datetime.date, datetime.date]]:
    """One window per calendar month in range, oldest first."""
    out: List[Tuple[datetime.date, datetime.date]] = []
    cursor = datetime.date(start.year, start.month, 1)
    while cursor <= end:
        nxt = datetime.date(cursor.year + cursor.month // 12,
                            cursor.month % 12 + 1, 1)
        out.append((max(cursor, start), min(nxt - datetime.timedelta(days=1), end)))
        cursor = nxt
    return out


@http.retry_transient(frozenset({429, 500, 502, 503, 504}),
                      initial=config.GDELT_REQUEST_INTERVAL_SECONDS + 1,
                      max_wait=60)
def _get(params: Dict[str, object]) -> requests.Response:
    """One DOC-API call, retrying transient errors.

    The backoff starts above GDELT's stated five-second floor. The default
    one-second first retry is *below* it, which turns any single 429 into five
    guaranteed failures — every retry is itself too fast to be answered.
    """
    resp = requests.get(_ENDPOINT, params=params,
                        headers={"User-Agent": http.PROJECT_UA}, timeout=60)
    if resp.status_code in (429, 500, 502, 503, 504):
        resp.raise_for_status()
    return resp


def _stamp(day: datetime.date, end_of_day: bool = False) -> str:
    """GDELT's YYYYMMDDHHMMSS window bound."""
    return day.strftime("%Y%m%d") + ("235959" if end_of_day else "000000")


def _articles(query: str, start: datetime.date, end: datetime.date) -> List[Dict]:
    """The raw ``artlist`` records for one window.

    Degrades to an empty list rather than raising: GDELT answers a query with no
    matches with an empty body, sometimes with HTML, sometimes with a JSON error
    object, and none of those are worth failing a 3,500-call harvest over.
    """
    resp = _get({
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": config.GDELT_MAX_RECORDS,
        "sort": "datedesc",
        "startdatetime": _stamp(start),
        "enddatetime": _stamp(end, end_of_day=True),
    })
    if resp.status_code != 200:
        logger.warning("[gdelt] %s..%s returned %s", start, end, resp.status_code)
        return []
    try:
        return resp.json().get("articles") or []
    except ValueError:
        logger.warning("[gdelt] %s..%s returned non-JSON (%d bytes)",
                       start, end, len(resp.content))
        return []


def to_item(record: Dict, theme: str) -> Optional[Dict]:
    """Turn one ``artlist`` record into a canonical article item.

    No body: GDELT returns URL and metadata only, which is the whole reason
    step 4 exists. Returns None for a record with no URL or no timestamp.
    """
    url = (record.get("url") or "").strip()
    seendate = record.get("seendate") or ""
    if not url or len(seendate) < 8:
        return None
    # 'YYYYMMDDTHHMMSSZ' — GDELT's own compact stamp, which nothing downstream
    # parses, so it becomes ISO here at the source boundary.
    published = (f"{seendate[0:4]}-{seendate[4:6]}-{seendate[6:8]}T"
                 f"{seendate[9:11] or '00'}:{seendate[11:13] or '00'}:"
                 f"{seendate[13:15] or '00'}Z")
    return core.normalize_item(
        title=record.get("title") or "",
        link=url,
        published=published,
        source=record.get("domain") or "",
        theme=theme,
    )


def harvest_window(iso2: str, country_name: str, start: datetime.date,
                   end: datetime.date) -> Tuple[int, int]:
    """Harvest one country-month across all six themes.

    Returns:
        ``(rows_written, overlap)`` — how many stories went in, and how many of
        them a previous source had already found. The upsert collapses both
        copies onto one row, so the overlap is invisible afterwards; counting it
        here is the only way a harvest can say what it actually added.
    """
    seen: Dict[str, Dict] = {}
    for theme in core.THEME_QUERIES:
        time.sleep(config.GDELT_REQUEST_INTERVAL_SECONDS)
        for record in _articles(gdelt_query(theme, country_name), start, end):
            item = to_item(record, theme)
            if item:
                seen.setdefault(core.dedupe_key(item), item)

    overlap = len(store.existing_urls(list(seen)))
    rows = []
    for item in seen.values():
        try:
            rows.append(store.article_row(item, country_iso2=iso2,
                                          source_system=SOURCE_SYSTEM,
                                          body_status="pending"))
        except ValueError as exc:
            # One malformed record must not cost the window its other 249.
            logger.debug("[gdelt] skipped a record: %s", exc)
    store.upsert_articles(rows)
    return len(rows), overlap


def harvest(roster: Optional[List[str]] = None, since: Optional[str] = None) -> int:
    """Harvest every pilot country from ``GDELT_START`` to today.

    Resumable per (country, month). No quota to manage — the only discipline is
    politeness, one request every five seconds, which is what makes this five
    hours rather than a minute.

    A window that fails is logged and checkpointed as failed, and the harvest
    moves on. GDELT is one flaky free service answering thousands of queries;
    losing five hours of progress to its worst minute would be the machine's
    fault, not GDELT's.

    Returns:
        Rows written this run.
    """
    roster = roster or config.PILOT_ROSTER
    start = datetime.date.fromisoformat(since or config.GDELT_START)
    end = datetime.date.today()
    windows = month_windows(start, end)

    todo = [(iso2, w) for iso2 in roster for w in windows
            if w[0] not in store.completed_windows(SOURCE_SYSTEM, iso2)]
    calls = len(todo) * len(core.THEME_QUERIES)
    logger.info("[gdelt] %d country-months x %d themes = %d calls, ~%d minutes at "
                "%.1fs each (%d country-months already done)",
                len(todo), len(core.THEME_QUERIES), calls,
                round(calls * config.GDELT_REQUEST_INTERVAL_SECONDS / 60),
                config.GDELT_REQUEST_INTERVAL_SECONDS,
                len(roster) * len(windows) - len(todo))

    written = overlapped = failed = 0
    for iso2, (window_start, window_end) in todo:
        name = config.country_name(iso2)
        try:
            n, overlap = harvest_window(iso2, name, window_start, window_end)
        except Exception:  # noqa: BLE001
            # Checkpointed as failed rather than left unwritten, so the next run
            # retries it (only 'done' windows are skipped) and the report can
            # say which months are thin because GDELT would not answer.
            logger.exception("[gdelt] %s %s failed; continuing", iso2, window_start)
            store.write_checkpoint(SOURCE_SYSTEM, iso2, window_start, window_end,
                                   status="failed", note="request error")
            failed += 1
            continue
        store.write_checkpoint(SOURCE_SYSTEM, iso2, window_start, window_end,
                               items_written=n, note=f"overlap={overlap}")
        written += n
        overlapped += overlap

    logger.info("[gdelt] done: %d rows, %d of them already known to another "
                "source (%.0f%% overlap), %d window(s) failed", written, overlapped,
                100.0 * overlapped / written if written else 0.0, failed)
    return written
