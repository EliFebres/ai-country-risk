"""newsapi.ai (Event Registry) harvester — bodies outside the Guardian's beat.

The Guardian hands back full text and is free, which is why it is the spine.
What it cannot do is cover a country it does not cover: Portugal, Turkey and
Korea reach the corpus as *foreign* news, a few stories a month, and the NYT
tier beside it is headline-and-two-sentences by design. Event Registry indexes
~150,000 publishers with the article body in the search response and an archive
back to 2014, which is the only route to bodies outside one newspaper's beat.

This is the first **metered** source in the harvest. Guardian costs calls and
NYT costs patience; this one costs money per search, so the shape of the module
is organised around spend rather than around politeness:

* every response's `req-tokens` header is read and accumulated, so the run
  reports what it was billed rather than what it predicted;
* `config.NEWSAPI_TOKEN_BUDGET` is enforced from inside the fetch, not checked
  at the end, because a budget checked at the end is not a budget;
* a page cap bounds the worst case before the first call is made.

One query per country-year, not six per theme
---------------------------------------------
The Guardian issues one search per theme because its `q` syntax is a keyword
match and the themes are what make the retrieved pool resemble the live one.
Doing that here would cost six times as much — ~300 tokens a country-year
against ~50 — which is the difference between the 48-country archive being
affordable and not.

So this module follows the **NYT** shape instead: one query per window, themes
derived afterwards by `core.classify_themes` from the text itself. It is
deliberately **not** in `THEME_QUERYING`. The retrieval is a concept query
rather than a keyword one — Event Registry entity-links "Portugal" the country
away from Portugal the football club — so the pool it returns is a superset of
what six keyword themes would have found, filtered down locally for free.

What that trades away is the *guarantee* that each theme is represented, and
that guarantee is real: `core.select_with_theme_floor` reserves two of twenty
slots per theme so a quiet ledger is not crowded out by a loud one. Whether a
broad query fills those floors is a measurement, not an assumption, and it is
the pre-registered failure line for this shape — if a theme lands under its
floor on more than a quarter of anchors, the fix is a targeted top-up search
for the short themes only, not a return to six queries.

Archive access is opt-out, not opt-in
-------------------------------------
Worth stating because every account of this API says the opposite. Their SDK
takes `allowUseOfArchive=True` **by default**; passing False makes it send
`forceMaxDataTimeWindow=31`, clamping the search to the last month. There is no
"enable the archive" parameter to set. Every call this module makes is
archival, so the requirement is negative: `forceMaxDataTimeWindow` must never
appear in a payload, and a test asserts it does not.

Bodies are checked for length, which is new here
------------------------------------------------
`adapters.guardian` stores whatever arrives — `if item.get("text")` — so a
one-character body is recorded there as `recovered`. That is fine for a source
that returns whole articles and dangerous for one aggregating 150,000
publishers, many of which syndicate stubs. A body under
`config.NEWSAPI_MIN_BODY_CHARS` is not called a body: its text goes to the
abstract and the row stays `pending` so the Wayback drain can try for a real
one. Nothing is discarded and nothing is mislabelled.
"""

import datetime
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from backend.util import http
from backend.data_upsert import store
from backend.util import config
from backend.news_fetching import core

logger = logging.getLogger(__name__)

SOURCE_SYSTEM = "newsapi_ai"
_ENDPOINT = "https://eventregistry.org/api/v1/article/getArticles"

# What the service says each request cost and what is left. `req-tokens` is the
# only honest source for the spend — the published price list gives "5
# tokens/searched year" and says nothing about how a sub-year window is billed,
# which is exactly the question the harvest shape turns on.
_TOKEN_HEADER = "req-tokens"
_LIMIT_HEADER = "x-ratelimit-limit"
_REMAINING_HEADER = "x-ratelimit-remaining"

# Running spend for this process. `measured_calls` counts the responses that
# actually carried `req-tokens`, so a run can say whether its total is a
# measurement or arithmetic — the same distinction `guardian.quota()` draws with
# `limit_is_measured`, and for the same reason: a number that might be either is
# the kind that quietly misinforms the next estimate.
_SPEND: Dict[str, Any] = {
    "tokens": 0.0, "calls": 0, "measured_calls": 0, "limit": None, "remaining": None}


def spend() -> Dict[str, Any]:
    """What the API has billed this process so far.

    ``tokens_are_measured`` is False when no response carried ``req-tokens``, in
    which case ``tokens`` is the published-price arithmetic and should be read
    as an assertion.
    """
    return {**_SPEND,
            "budget": config.NEWSAPI_TOKEN_BUDGET,
            "tokens_are_measured": bool(_SPEND["measured_calls"])}


class TokenBudgetExhausted(RuntimeError):
    """The run reached ``config.NEWSAPI_TOKEN_BUDGET``. A stop, not a failure."""


class QuotaExhausted(RuntimeError):
    """The *account's* token allowance is gone — Event Registry answers 401.

    A scheduling fact rather than an error: the run checkpoints what it
    finished, says how much more it needs, and exits cleanly.
    """


# Event Registry answers 401 for a spent allowance *and* for a key it does not
# know, and the two want opposite handling: one is a scheduling fact to
# checkpoint and resume from, the other is a configuration error that will
# never resolve on its own. Only the body tells them apart, so it is read.
#
# Measured against the live endpoint on 2026-08-28, an unrecognised key returns
# a bare-text 401 (no JSON, no billing headers) reading "The apiKey that was
# provided is not recognized as a valid key for a user."
_BAD_KEY_MARKERS = ("not recognized as a valid key", "not a valid key",
                    "invalid api key", "unable to execute the request")


class ArchiveUnavailable(RuntimeError):
    """The account cannot reach past the recent window, so a backfill is futile.

    Measured against a live 5K-plan key on 2026-08-28: every request dated
    older than ~30 days returned ``totalResults: 0`` while the same query over
    the last three days returned 26,224 — and **each of those empty archive
    requests still billed a token**. Nothing in the response says the archive
    was refused. There is no error, no warning, and no flag; the window is
    silently clamped and the result is a clean, empty, paid answer.

    That combination is the expensive one. A 48-country decade harvest would
    have spent ~480 tokens writing nothing and checkpointed all 480 windows
    `done`, so the next run would skip them and the corpus would record a
    completed backfill that never happened.
    """


class BadKey(RuntimeError):
    """The key is not one the service knows. Never a quota stop.

    Split from `QuotaExhausted` because conflating them is expensive in a very
    specific way: a bad key would checkpoint every window `status='failed',
    note='quota exhausted'`, the harvest would return 0 like any ordinary wall,
    and the log would say "re-run to resume" about a run that can never
    succeed. A key that does not work must fail loudly on the first call.
    """


def _api_key() -> str:
    """Read ``NEWSAPI_AI_API_KEY`` at call time, never cached at import.

    Same contract as ``GUARDIAN_API_KEY`` beside it: the process loads one
    ``.env`` at startup and every module reads env when it needs it.
    """
    key = os.getenv("NEWSAPI_AI_API_KEY")
    if not key:
        raise RuntimeError(
            "NEWSAPI_AI_API_KEY is not set. A key comes with a plan at "
            "https://newsapi.ai/plans — put it in backend/.env"
        )
    return key


# ---------------------------------------------------------------------------
# Queries and windows
# ---------------------------------------------------------------------------

def concept_uri(country_name: str) -> str:
    """Event Registry's concept URI for a country.

    Their concept namespace is Wikipedia article URLs, so this is derivable
    rather than something to look up — which matters, because a lookup would be
    a billed call per country to learn a string that never changes.

    ponytail: convention, not a resolution. If a roster country's Wikipedia
    title ever disagrees with its `constants.COUNTRY_ROSTER` name the query
    returns nothing rather than the wrong thing, and `window_items` says so
    loudly. The upgrade path is a hand-maintained override dict, added the day
    a country actually needs one.
    """
    return "http://en.wikipedia.org/wiki/" + country_name.strip().replace(" ", "_")


def _add_months(day: datetime.date, months: int) -> datetime.date:
    """``day`` advanced by whole months, clamped to the first of the month."""
    total = (day.year * 12 + day.month - 1) + months
    return datetime.date(total // 12, total % 12 + 1, 1)


def windows(start: datetime.date, end: datetime.date, granularity: str = "year"
            ) -> List[Tuple[datetime.date, datetime.date]]:
    """Harvest windows over ``[start, end]``, by calendar year or calendar month.

    Calendar boundaries rather than rolling spans, for the reason
    ``guardian.year_windows`` gives: a resumed harvest must ask for the same
    windows as the run before it or its checkpoints mean nothing.

    Both granularities live here rather than one being borrowed from the
    Guardian adapter because the *choice between them* is this module's own
    problem. Archive tokens scale with the span searched, so window width is a
    price, and pricing this source is not something to couple to another
    adapter's date arithmetic.

    Args:
        start: first day to cover.
        end: last day to cover, inclusive.
        granularity: ``"year"`` or ``"month"``.

    Returns:
        Windows in chronological order, clamped to ``[start, end]``.
    """
    if granularity not in ("year", "month"):
        raise ValueError(f"granularity must be 'year' or 'month', got {granularity!r}")
    out: List[Tuple[datetime.date, datetime.date]] = []
    if granularity == "year":
        for year in range(start.year, end.year + 1):
            out.append((max(datetime.date(year, 1, 1), start),
                        min(datetime.date(year, 12, 31), end)))
        return out
    cursor = datetime.date(start.year, start.month, 1)
    while cursor <= end:
        nxt = _add_months(cursor, 1)
        out.append((max(cursor, start), min(nxt - datetime.timedelta(days=1), end)))
        cursor = nxt
    return out


def asserted_tokens(start: datetime.date, end: datetime.date) -> int:
    """The published price of one search over ``[start, end]``.

    Their plans page prices an archive search at 5 tokens per *searched year*
    and gives the worked example "2015 to 2017 … 15 tokens (3 years of
    content)". It says nothing about a window narrower than a year, so this
    charges a full year for one — the conservative reading, and the one that
    makes a budget check safe when the header is missing.

    Only used when a response carried no ``req-tokens``. The header is the
    number that decides anything.
    """
    return (end.year - start.year + 1) * config.NEWSAPI_TOKENS_PER_ARCHIVE_YEAR


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------

def _int_or_none(value: Optional[str]) -> Optional[int]:
    """Parse a whole-number header, tolerating an absent or malformed one."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Optional[str]) -> Optional[float]:
    """Parse a fractional header, tolerating an absent or malformed one.

    `req-tokens` arrives as ``"1.000"``, not ``"1"`` — measured against the live
    endpoint on 2026-08-28. Parsing it with :func:`_int_or_none` returns None on
    every response, so the meter silently falls back to the price list and the
    run reports arithmetic while believing it reported a measurement. Which is
    the exact failure `tokens_are_measured` exists to make visible, arriving
    through the one path that would not have tripped it.
    """
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _read_spend_headers(resp: requests.Response, fallback: float) -> float:
    """Fold one response's billing headers into ``_SPEND`` and return its cost.

    Read from *every* response, refusals included — a refused archive search is
    still capable of having been billed, and the 401 that ends a run is the one
    response whose `x-ratelimit-remaining` anybody wants to read.

    Args:
        resp: the response, successful or not.
        fallback: what to charge if the response carried no ``req-tokens``.
    """
    billed = _float_or_none(resp.headers.get(_TOKEN_HEADER))
    limit = _int_or_none(resp.headers.get(_LIMIT_HEADER))
    remaining = _int_or_none(resp.headers.get(_REMAINING_HEADER))
    cost = billed if billed is not None else fallback
    _SPEND.update(
        tokens=(_SPEND["tokens"] or 0) + cost,
        calls=(_SPEND["calls"] or 0) + 1,
        measured_calls=(_SPEND["measured_calls"] or 0) + int(billed is not None),
        limit=limit if limit is not None else _SPEND["limit"],
        remaining=remaining if remaining is not None else _SPEND["remaining"],
    )
    return cost


@http.retry_transient(frozenset({429, 500, 502, 503, 504}))
def _post(payload: Dict[str, object], fallback: float) -> requests.Response:
    """One search, retrying transient errors but never a refusal.

    **401 is not an auth failure here.** Event Registry answers 401 when the
    account's token allowance is spent, which is the same status a bad key gets.
    Either way retrying is wrong: a bad key will not become good and a spent
    allowance will not refill within a backoff, and every attempt is a billable
    search. `QuotaExhausted` is not a `RequestException`, so
    `http._is_retryable_exc` refuses it and tenacity reraises on the first
    attempt — the discipline `guardian._get` uses on its own wall, for the same
    reason: the retries would be spent against the budget that just ran out.
    """
    resp = requests.post(_ENDPOINT, json=payload,
                         headers={"User-Agent": http.PROJECT_UA}, timeout=60)
    _read_spend_headers(resp, fallback)
    if resp.status_code == 401:
        said = (resp.text or "")[:300]
        if any(marker in said.lower() for marker in _BAD_KEY_MARKERS):
            raise BadKey(
                f"newsapi.ai does not recognise NEWSAPI_AI_API_KEY. This is a "
                f"configuration error, not a quota stop — check the key at "
                f"https://newsapi.ai/dashboard and that the plan is active. "
                f"The service said: {said}")
        raise QuotaExhausted(
            f"newsapi.ai refused with 401 — token allowance spent: {said}")
    if resp.status_code in (429, 500, 502, 503, 504):
        resp.raise_for_status()
    return resp


def _payload(country_name: str, start: datetime.date, end: datetime.date,
             page: int) -> Dict[str, object]:
    """The request body for one page of one window.

    ``articlesSortBy="date"`` for the reason ``guardian._page`` gives: date
    order is stable across pages, so a window that stops early stops at a known
    boundary rather than dropping an arbitrary slice. Note what that does *not*
    fix — under a page cap, a year-wide window sorted by date returns the newest
    N and leaves the start of the year unread. That is a property of year-wide
    windows, not of the sort, and it is why `granularity="month"` exists.

    There is deliberately no ``forceMaxDataTimeWindow`` key. Its presence would
    clamp the search to the last 31 days, which is the whole archive gone.
    """
    return {
        "action": "getArticles",
        "conceptUri": concept_uri(country_name),
        "dateStart": start.isoformat(),
        "dateEnd": end.isoformat(),
        "articlesPage": page,
        "articlesCount": config.NEWSAPI_PAGE_SIZE,
        "articlesSortBy": "date",
        "resultType": "articles",
        # -1 is "the whole body"; the field is a truncation length, so anything
        # else silently caps every article at that many characters.
        "articleBodyLen": -1,
        "includeArticleBody": True,
        # Needed for the source-composition measurement: a Portuguese regional
        # paper is a masking risk in a way the Guardian is not, and
        # `source.location` is what says which is which. Costs nothing extra.
        "includeSourceLocation": True,
        "isDuplicateFilter": "skipDuplicates",
        "dataType": ["news"],
        "apiKey": _api_key(),
    }


def _page(country_name: str, start: datetime.date, end: datetime.date,
          page: int, spent: List[float]) -> Dict:
    """Fetch one page, charging it against the run's token budget first.

    The budget is checked *before* the call, against what the call is expected
    to cost. Checking afterwards would let the run overshoot by exactly the call
    that broke it, which on a source billing 5 tokens a page is the difference
    between a cap and a suggestion.
    """
    expected = asserted_tokens(start, end)
    if spent[0] + expected > config.NEWSAPI_TOKEN_BUDGET:
        raise TokenBudgetExhausted(
            f"{spent[0]} tokens spent; the next search would cost about "
            f"{expected} against a budget of {config.NEWSAPI_TOKEN_BUDGET}")

    before = _SPEND["tokens"] or 0
    try:
        resp = _post(_payload(country_name, start, end, page), expected)
    except requests.HTTPError as exc:
        # The backstop. A 429 that survived five spaced attempts is the service
        # telling us to stop, and stopping is resumable where a string of false
        # `request error` checkpoints is not — the lesson `guardian._page`
        # records from 2026-08-15.
        if getattr(exc.response, "status_code", None) == 429:
            raise QuotaExhausted("newsapi.ai returned 429 after backoff") from exc
        raise
    finally:
        spent[0] += (_SPEND["tokens"] or 0) - before

    resp.raise_for_status()
    body = resp.json()
    # Their errors arrive inside a 200 as often as not.
    if body.get("error"):
        raise RuntimeError(f"newsapi.ai said {body['error']!r}")
    return (body.get("articles") or {})


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def to_item(article: Dict) -> Optional[Dict]:
    """Turn one Event Registry article into a canonical item.

    Returns None for an article with no URL or no publication date — both are
    required to place it in a snapshot window, and neither is worth failing a
    whole window over.

    ``dateTimePub`` is the publisher's own stamp and ``dateTime`` is when the
    index saw it; the first is what the no-future invariant means by "published"
    and the second is only a fallback. Both are ISO-8601 UTC. The ``Z`` is
    appended when absent rather than defaulted away: a naive string reaches
    Postgres to be read in the session's timezone, which is a backfill that
    re-dates itself by the deploy region's offset.
    """
    url = (article.get("url") or "").strip()
    published = article.get("dateTimePub") or article.get("dateTime")
    if not url or not published:
        return None
    published = str(published)
    if not published.endswith("Z") and "+" not in published[10:]:
        published += "Z"

    source = article.get("source") or {}
    body = (article.get("body") or "")[:core.MAX_BODY_CHARS]
    return core.normalize_item(
        title=article.get("title") or "",
        link=url,
        published=published,
        source=str(source.get("title") or source.get("uri") or ""),
        text=body,
        # No theme: this adapter does not query per theme, so the theme is
        # whatever `store.article_row` classifies out of the text. Same as NYT.
        #
        # Carried as `extra`, which `normalize_item` passes through and
        # `store.article_row` ignores: the store has no column for either, but
        # the source-composition measurement cannot be made without them. A
        # regional Portuguese paper writing "the government" is a masking risk
        # the Guardian doing the same is not, and `source.location` is the only
        # field that says which one this is.
        source_uri=str(source.get("uri") or ""),
        source_location=(source.get("location") or {}),
    )


def classify_body(item: Dict) -> Tuple[str, Optional[str], Dict]:
    """Decide honestly what kind of body, if any, this item arrived with.

    Three outcomes, and the middle one is the reason this function exists:

    * at least ``config.NEWSAPI_MIN_BODY_CHARS`` — a body. ``recovered`` with
      vintage ``api-native``, which is the only vintage `snapshot_select`
      trusts unconditionally, and which is only honest because the text came
      inside the search response rather than from a later refetch.
    * shorter than that — **not** a body. The text moves to the snippet, so it
      is stored as an abstract rather than as evidence, and the row goes to the
      Wayback queue as ``pending`` in case a real body can be recovered. A
      400-character syndication stub read as full text would poison every
      digest built on it, silently, and the count would still say `recovered`.
    * nothing at all — ``pending``, same as the Guardian's bodyless rows.

    Returns:
        ``(body_status, body_vintage, item)`` with ``item`` adjusted so that
        what reaches the store matches what the status claims.
    """
    text = item.get("text") or ""
    if len(text) >= config.NEWSAPI_MIN_BODY_CHARS:
        return "recovered", "api-native", item
    if text:
        # Demoted, not discarded: the store writes `snippet` to `abstract`.
        return "pending", None, {**item, "text": "", "snippet": item.get("snippet") or text}
    return "pending", None, item


def window_items(country_name: str, start: datetime.date, end: datetime.date,
                 spent: List[float], max_pages: Optional[int] = None) -> List[Dict]:
    """Every article for one window, up to the page cap.

    ``max_pages`` is per *window*, not per country-year, and that is the whole
    economics of this source. Measured 2026-08-28: an archival search costs a
    flat 5 tokens per calendar year it touches, however narrow the window — a
    five-day range in 2025 bills the same 5 tokens as the whole of 2025. Nothing
    is prorated. So twelve monthly windows cost 60 tokens a page where one
    year-wide window costs 5, and the cap has to move with the granularity or
    the monthly shape is twelve times the price for no reason.

    Truncation is announced rather than inferred. A window that hits the cap has
    left articles behind, and a silent stop reads afterwards as "that is all
    there was" — the failure `guardian._window_items` names when its own
    subdivision runs out of room.
    """
    cap = max_pages if max_pages is not None else config.NEWSAPI_MAX_PAGES_PER_WINDOW
    seen: Dict[str, Dict] = {}
    total: Optional[int] = None
    for page in range(1, cap + 1):
        time.sleep(config.REQUEST_INTERVAL_SECONDS)
        block = _page(country_name, start, end, page, spent)
        results = block.get("results") or []
        if total is None:
            total = block.get("totalResults")
        for result in results:
            item = to_item(result)
            if item:
                seen.setdefault(core.dedupe_key(item), item)
        if page >= (block.get("pages") or 1) or not results:
            break
    else:
        if total and total > len(seen):
            logger.warning("  %s %s..%s: %s articles indexed, capped at %d page(s), "
                           "%s left behind", country_name, start, end, total,
                           cap, total - len(seen))

    if total == 0 or (total is None and not seen):
        # Almost always the concept URI rather than a quiet year: an unresolvable
        # concept matches nothing and returns a clean, empty, billed answer.
        logger.warning("  %s %s..%s: no articles. Check the concept URI %s "
                       "resolves — an unknown concept returns zero, not an error.",
                       country_name, start, end, concept_uri(country_name))
    return list(seen.values())


def harvest_window(iso2: str, country_name: str, start: datetime.date,
                   end: datetime.date, spent: List[float]) -> int:
    """Fetch one window and write it. Returns rows written."""
    rows = []
    for item in window_items(country_name, start, end, spent):
        body_status, vintage, adjusted = classify_body(item)
        try:
            rows.append(store.article_row(
                adjusted, country_iso2=iso2, source_system=SOURCE_SYSTEM,
                body_status=body_status, body_vintage=vintage))
        except ValueError:
            # One malformed record must not cost the window — the discipline
            # `nyt.harvest_month` and `gdelt.harvest_window` both keep.
            logger.warning("  skipping unusable article: %s",
                           (adjusted.get("link") or "")[:120])
    store.upsert_articles(rows)
    return len(rows)


# ---------------------------------------------------------------------------
# The harvest
# ---------------------------------------------------------------------------

# How far back the un-entitled window reaches. Their own no-archive clamp is
# `forceMaxDataTimeWindow=31`, so 31 days is the boundary being tested for.
_RECENT_HORIZON_DAYS = 31

# Memoised across a run: the probe costs a token and the answer cannot change
# mid-harvest. None until asked.
_ARCHIVE_VERDICT: Optional[str] = None


def diagnose_empty_window(country_name: str, spent: List[float]) -> str:
    """Why did an archival window come back empty? One probe, three answers.

    Called only when an archival window returns nothing, so an account that
    works never pays for it. It asks the *same concept* over the last three
    days, where every account can reach, and reads the difference:

    * ``"clamped"`` — recent returns articles, the archive returns none. The
      account has no archive entitlement. Stop: every further window bills a
      token to write nothing.
    * ``"query"`` — recent returns nothing either, so the concept URI is wrong
      and the archive is not implicated. Deliberately **not** treated as a
      clamp: a false "archive unavailable" would stop a harvest that a corrected
      URI would have completed.
    * ``"quiet"`` — never returned here, but named because it is the third
      possibility the caller must not rule out on one window. A genuinely empty
      country-month is why this asks rather than assumes.

    The probe's token is charged to ``spent`` like any other, so the run's
    budget and its ledger agree about what it cost.
    """
    global _ARCHIVE_VERDICT
    if _ARCHIVE_VERDICT is not None:
        return _ARCHIVE_VERDICT
    today = datetime.date.today()
    block = _page(country_name, today - datetime.timedelta(days=3), today, 1, spent)
    _ARCHIVE_VERDICT = "clamped" if (block.get("totalResults") or 0) else "query"
    return _ARCHIVE_VERDICT


def harvest(roster: Optional[List[str]] = None, since: Optional[str] = None,
            until: Optional[str] = None, granularity: str = "year") -> int:
    """Harvest every outstanding window for every country in the roster.

    Args:
        roster: ISO2 codes. Defaults to :data:`config.PILOT_ROSTER`.
        since: ISO date overriding :data:`config.HARVEST_FLOOR`.
        until: ISO date bounding the top. Defaults to today. Unlike the other
            two adapters this one is billed, so a run has to be able to say
            where it stops as well as where it starts.
        granularity: ``"year"`` or ``"month"`` windows. Month windows cost more
            searches and give even temporal coverage by construction; year
            windows are cheaper and let the page cap decide which part of the
            year is read. Which is right is a measurement, not a preference.

    Returns:
        Rows written this run.
    """
    roster = roster or config.PILOT_ROSTER
    start = datetime.date.fromisoformat(since or config.HARVEST_FLOOR)
    end = datetime.date.fromisoformat(until) if until else datetime.date.today()
    all_windows = windows(start, end, granularity)

    # One query per country, not one per (country, window) — the fix
    # `guardian.harvest` records: the comprehension form ran `completed_windows`
    # on every pair, which is 528 round trips to answer 48 questions.
    done = {iso2: store.completed_windows(SOURCE_SYSTEM, iso2) for iso2 in roster}
    todo = [(iso2, w) for iso2 in roster for w in all_windows
            if w[0] not in done[iso2]]

    # The convergence signal, for the reason the other two adapters carry one:
    # a finite job that has finished must say so, or a tail of the log cannot
    # tell it from one that is stuck.
    if not todo:
        logger.info("[newsapi_ai] nothing to harvest — roster complete through %s "
                    "(%d country/ies x %d window(s), all checkpointed done)",
                    end.isoformat(), len(roster), len(all_windows))
        return 0

    floor = sum(asserted_tokens(s, e) for _, (s, e) in todo)
    logger.info("[newsapi_ai] %d window(s) at %s granularity, %d done. Budget %d "
                "tokens; one page each would cost ~%d (asserted from the price "
                "list, not measured — pages multiply it).",
                len(todo), granularity,
                len(roster) * len(all_windows) - len(todo),
                config.NEWSAPI_TOKEN_BUDGET, floor)

    spent = [0.0]
    written = 0
    recent_floor = datetime.date.today() - datetime.timedelta(days=_RECENT_HORIZON_DAYS)
    for done_n, (iso2, (window_start, window_end)) in enumerate(todo):
        name = config.country_name(iso2)
        started, before = time.monotonic(), spent[0]
        logger.info("[newsapi_ai] %d/%d %s %s..%s starting (%.1f tokens spent)",
                    done_n + 1, len(todo), iso2, window_start, window_end, spent[0])
        try:
            n = harvest_window(iso2, name, window_start, window_end, spent)
            if (n == 0 and window_end < recent_floor
                    and diagnose_empty_window(name, spent) == "clamped"):
                raise ArchiveUnavailable(
                    f"{window_start}..{window_end} returned nothing while the "
                    f"last three days return articles for the same concept. The "
                    f"account cannot reach the archive — every further window "
                    f"would bill a token to write nothing, and checkpoint `done` "
                    f"over a backfill that never happened. Enable archive access "
                    f"at https://newsapi.ai/dashboard before re-running.")
        except (BadKey, ArchiveUnavailable):
            # Deliberately not caught by the handler below. A key the service
            # rejects fails every window identically, so swallowing it would
            # write one useless checkpoint per country-year and report a
            # misconfiguration as a harvest that found nothing. `done` on an
            # empty archival window is worse still: it is not retried.
            raise
        except (TokenBudgetExhausted, QuotaExhausted) as exc:
            # The wall gets a row of its own, so the ledger says why a run
            # stopped where it did rather than staying silent about it.
            # `failed` because only `done` is skipped on resume, so this window
            # is retried next run.
            store.write_checkpoint(SOURCE_SYSTEM, iso2, window_start, window_end,
                                   status="failed", note="quota exhausted",
                                   seconds=time.monotonic() - started,
                                   calls=spent[0] - before)
            logger.warning("[newsapi_ai] %s", exc)
            logger.warning("[newsapi_ai] stopped at %d/%d, %.1f tokens spent (%s). "
                           "Re-run to resume.", done_n, len(todo), spent[0],
                           "measured" if spend()["tokens_are_measured"]
                           else "asserted; no billing header seen")
            return written
        except Exception:  # noqa: BLE001
            logger.exception("[newsapi_ai] %d/%d %s %s..%s failed, continuing",
                             done_n + 1, len(todo), iso2, window_start, window_end)
            store.write_checkpoint(SOURCE_SYSTEM, iso2, window_start, window_end,
                                   status="failed", note="request error",
                                   seconds=time.monotonic() - started,
                                   calls=spent[0] - before)
            continue

        # Per-window cost, not the running total: the ledger is read back
        # country-year by country-year to price the next roster, and a
        # cumulative number there would make every later window look worse than
        # the one before it.
        store.write_checkpoint(SOURCE_SYSTEM, iso2, window_start, window_end,
                               items_written=n, seconds=time.monotonic() - started,
                               calls=spent[0] - before,
                               note=f"tokens={spent[0] - before:.1f}")
        written += n
        logger.info("[newsapi_ai] %d/%d %s %s..%s: %d rows, %.1f tokens (%.1f total)",
                    done_n + 1, len(todo), iso2, window_start, window_end, n,
                    spent[0] - before, spent[0])

    logger.info("[newsapi_ai] done: %d rows, %.1f tokens (%s)", written, spent[0],
                "measured" if spend()["tokens_are_measured"] else "asserted")
    return written
