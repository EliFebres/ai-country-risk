"""New York Times archive harvester — the degraded tier, and the deep one.

Structurally unlike the other two. The Guardian and GDELT are asked a question
per country per theme; the NYT archive endpoint takes a year and a month and
returns *everything the paper published that month*, for the whole world, in one
call. Ten years is about 120 calls total.

That inverts the work. There is no query to filter by, so the filtering happens
here, against ``masking.gazetteer`` — the same list of names, demonyms, cities
and institutions the masked run uses to hide a country is the list used to find
one. Keeping the two in one place is the point: a country the gazetteer cannot
find is a country it also cannot mask, and both failures should surface from the
same fix.

Everything lands ``tier='abstract-only'`` and ``body_status='degraded-title-only'``.
The NYT does not return body text and its pages are paywalled, so what is stored
is the headline and the abstract, and the row says so rather than pretending to
be a Guardian row with a missing body. That tier is a real evidence level, not a
defect — an abstract is what the paper itself wrote about the story — and it is
what carries the months before ``GDELT_START`` where the corpus is otherwise
Guardian-only.

The status is deliberately **not** ``'pending'``. Pending means "a body is
coming", and none is: a Wayback fetch of a paywalled NYT page returns the
paywall, not the article. Queueing these would have put roughly 200,000 URLs
into the recovery drain — weeks of polite waiting at one request a second, for
nothing. ``degraded-title-only`` is what the store already calls "no body, use
the title and abstract", which is exactly the truth here.

Themes come from the classifier rather than from a query, because there is no
query. ``store.article_row`` already does that for any item arriving without a
``_theme``.
"""

import datetime
import logging
import os
import time
from typing import Dict, List, Optional, Tuple

import requests

from backend.util import http
from backend.utils.history import config, store
from backend.utils.masking import gazetteer
from backend.utils.news_fetching import article_ranking, core

logger = logging.getLogger(__name__)

SOURCE_SYSTEM = "nyt"
_ENDPOINT = "https://api.nytimes.com/svc/archive/v1/{year}/{month}.json"

# Keyword types worth reading as "this article is about that place". The archive
# tags most foreign coverage with a `glocations` entry, which catches stories
# whose headline names only a city or a person.
_PLACE_KEYWORDS = frozenset({"glocations", "subject", "organizations"})

# Desks that do not carry country-risk news. A denylist rather than an allowlist
# because a quarter of archive rows have no desk at all — mostly older and wire
# copy — and an allowlist would silently throw all of it away.
#
# This matters more here than for the other two sources. The Guardian and GDELT
# are asked a themed question; the archive hands over the entire paper, sport
# and recipes included. In one measured month (2018-08) the desks below were
# 25% of everything that matched a roster country.
_SKIP_DESKS = frozenset({
    "Sports", "Culture", "Weekend", "Society", "Style", "Dining", "Food",
    "Travel", "Arts", "Books", "Movies", "Theater", "Television", "Obits",
    "RealEstate", "Automobiles", "Games", "Well", "Home", "Fashion",
    "Magazine", "TStyle", "Escapes", "Living", "Vows", "Insider", "Smarter Living",
})


def _api_key() -> str:
    """Read ``NYT_API_KEY`` at call time, never cached at import.

    Same contract as ``GUARDIAN_API_KEY``: the process loads one ``.env`` at
    startup and every module reads env when it needs it.
    """
    key = os.getenv("NYT_API_KEY")
    if not key:
        raise RuntimeError(
            "NYT_API_KEY is not set. A free key comes from "
            "https://developer.nytimes.com/ (create an app, enable the Archive "
            "API) — put it in backend/.env"
        )
    return key


def months(start: datetime.date, end: datetime.date) -> List[Tuple[int, int]]:
    """Every ``(year, month)`` the archive endpoint has to be asked for.

    Not a date range like the other adapters' windows: this endpoint is
    addressed by year and month, so those are its actual arguments.
    """
    out: List[Tuple[int, int]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        out.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def month_bounds(year: int, month: int) -> Tuple[datetime.date, datetime.date]:
    """First and last day of a month — the checkpoint's window."""
    first = datetime.date(year, month, 1)
    nxt = datetime.date(year + month // 12, month % 12 + 1, 1)
    return first, nxt - datetime.timedelta(days=1)


@http.retry_transient(frozenset({429, 500, 502, 503, 504}),
                      initial=config.NYT_REQUEST_INTERVAL_SECONDS,
                      max_wait=120)
def _get(year: int, month: int) -> requests.Response:
    """One archive call, retrying transient errors.

    The backoff starts at the tier's own interval rather than at one second: a
    retry quicker than five-a-minute is a retry that cannot be answered.
    A month of the paper is a large response, hence the long read timeout.
    """
    resp = requests.get(_ENDPOINT.format(year=year, month=month),
                        params={"api-key": _api_key()},
                        headers={"User-Agent": http.PROJECT_UA}, timeout=120)
    if resp.status_code in (429, 500, 502, 503, 504):
        resp.raise_for_status()
    return resp


def _docs(year: int, month: int) -> List[Dict]:
    """Every document the paper published that month.

    Degrades to an empty list rather than raising on a malformed response: one
    unreadable month is a thin month, not a reason to lose the other 119.
    """
    resp = _get(year, month)
    if resp.status_code != 200:
        logger.warning("[nyt] %04d-%02d returned %s", year, month, resp.status_code)
        return []
    try:
        return (resp.json().get("response") or {}).get("docs") or []
    except ValueError:
        logger.warning("[nyt] %04d-%02d returned non-JSON (%d bytes)",
                       year, month, len(resp.content))
        return []


def carries_risk_news(doc: Dict) -> bool:
    """Is this document from a desk that reports the kind of news being scored?

    A document with no desk is kept: absent is not the same as excluded, and a
    quarter of the archive predates the field.
    """
    return (doc.get("news_desk") or "").strip() not in _SKIP_DESKS


def searchable_text(doc: Dict) -> str:
    """Everything about a document worth matching a country against.

    Headline, abstract, lead paragraph and the place-ish keywords together.
    Deliberately not the body — there is none — so an article that mentions a
    country only in passing further down is not collected, which is the right
    bias for a source with no relevance score of its own.
    """
    headline = (doc.get("headline") or {}).get("main") or ""
    keywords = " ".join(
        str(kw.get("value") or "")
        for kw in (doc.get("keywords") or [])
        if isinstance(kw, dict) and kw.get("name") in _PLACE_KEYWORDS
    )
    return " ".join(filter(None, (
        headline, doc.get("abstract") or "", doc.get("lead_paragraph") or "", keywords)))


def to_item(doc: Dict) -> Optional[Dict]:
    """Turn one archive document into a canonical article item.

    No body and no theme: the NYT returns neither. The abstract becomes the
    snippet, and ``store.article_row`` classifies themes from the text it has.

    Returns None for a document with no URL, no headline or no date — all three
    are needed to place an article in a snapshot window and rank it there.
    """
    url = (doc.get("web_url") or "").strip()
    published = doc.get("pub_date")
    headline = (doc.get("headline") or {}).get("main") or ""
    if not url or not published or not headline:
        return None
    return core.normalize_item(
        title=headline,
        link=url,
        published=published,
        source="The New York Times",
        snippet=doc.get("abstract") or doc.get("lead_paragraph") or "",
    )


def harvest_month(year: int, month: int, roster: List[str]) -> Dict[str, int]:
    """Fetch one month once and split it across every country that wants it.

    One call serves the whole roster, which is the entire economics of this
    source. Fetching per country would be five times the calls for exactly the
    same bytes.

    Returns:
        Rows written per country ISO2.
    """
    docs = [d for d in _docs(year, month) if carries_risk_news(d)]
    written: Dict[str, int] = {}

    for iso2 in roster:
        country = config.country_name(iso2)
        items = []
        for doc in docs:
            if not gazetteer.mentions(searchable_text(doc), iso2):
                continue
            item = to_item(doc)
            if item:
                item["relevance_score"] = article_ranking.score_relevance(item, country)
                items.append(item)

        # Keep the most relevant, by the live scorer, and say what went. The
        # tail is articles no snapshot could have selected; dropping them
        # silently would read afterwards as a complete harvest.
        kept = core.by_relevance(items)[:config.NYT_MAX_PER_COUNTRY_MONTH]
        if len(items) > len(kept):
            logger.info("[nyt] %04d-%02d %s: kept %d of %d matches (cap %d)",
                        year, month, iso2, len(kept), len(items),
                        config.NYT_MAX_PER_COUNTRY_MONTH)

        rows = []
        for item in kept:
            try:
                rows.append(store.article_row(
                    item, country_iso2=iso2, source_system=SOURCE_SYSTEM,
                    body_status="degraded-title-only", tier="abstract-only"))
            except ValueError as exc:
                # One malformed document must not cost the month its others.
                logger.debug("[nyt] skipped a document: %s", exc)
        store.upsert_articles(rows)
        written[iso2] = len(rows)

    return written


def harvest(roster: Optional[List[str]] = None, since: Optional[str] = None) -> int:
    """Harvest the archive from ``PILOT_START`` to today.

    Resumable per (country, month). A month is only fetched when at least one
    roster country still needs it, and each country is checkpointed separately,
    so adding a country later re-fetches only what that country is missing.

    A month that fails is checkpointed as failed for every country and the
    harvest continues — only 'done' windows are skipped on resume, so it is
    retried next run rather than becoming a silent hole.

    Returns:
        Rows written this run.
    """
    roster = roster or config.PILOT_ROSTER
    start = datetime.date.fromisoformat(since or config.PILOT_START)
    end = datetime.date.today()

    done = {iso2: store.completed_windows(SOURCE_SYSTEM, iso2) for iso2 in roster}
    todo = []
    for year, month in months(start, end):
        first, last = month_bounds(year, month)
        wanted = [iso2 for iso2 in roster if first not in done[iso2]]
        if wanted:
            todo.append((year, month, first, last, wanted))

    logger.info("[nyt] %d month(s) to fetch, ~%d minutes at %.0fs each. One call "
                "covers every country in the roster.",
                len(todo),
                round(len(todo) * config.NYT_REQUEST_INTERVAL_SECONDS / 60),
                config.NYT_REQUEST_INTERVAL_SECONDS)

    written = failed = 0
    for year, month, first, last, wanted in todo:
        time.sleep(config.NYT_REQUEST_INTERVAL_SECONDS)
        try:
            counts = harvest_month(year, month, wanted)
        except Exception:  # noqa: BLE001
            logger.exception("[nyt] %04d-%02d failed; continuing", year, month)
            for iso2 in wanted:
                store.write_checkpoint(SOURCE_SYSTEM, iso2, first, last,
                                       status="failed", note="request error")
            failed += 1
            continue
        for iso2, n in counts.items():
            store.write_checkpoint(SOURCE_SYSTEM, iso2, first, last, items_written=n)
            written += n
        logger.info("[nyt] %04d-%02d: %s", year, month,
                    ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    logger.info("[nyt] done: %d rows (abstract-only tier), %d month(s) failed",
                written, failed)
    return written
