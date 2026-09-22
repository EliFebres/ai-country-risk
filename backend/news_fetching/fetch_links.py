import re
import html
import httpx
import asyncio
import feedparser
import trafilatura
import datetime as dt
import logging

from typing import List, Dict
from urllib.parse import urlencode, quote_plus, urlparse

from backend.news_fetching.url_resolver import resolve_google_news_url
from backend.news_fetching.source_filter import is_blocked_url


# Quiet noisy warnings from trafilatura
logging.getLogger("trafilatura").setLevel(logging.ERROR)
logging.getLogger("trafilatura.core").setLevel(logging.ERROR)

UA = "Mozilla/5.0 (compatible; ai-country-risk/1.0)"


def _gnews_url(query: str, lang: str = "en", country: str = "US") -> str:
    """Build a properly encoded Google News RSS search URL."""
    base = "https://news.google.com/rss/search"
    hl = f"{lang}-{country}"
    ceid = f"{country}:{lang}"
    params = {"q": query, "hl": hl, "gl": country, "ceid": ceid}
    return f"{base}?{urlencode(params, quote_via=quote_plus)}"


def _strip_html(s: str) -> str:
    """Remove all HTML (including <a> links) and unescape entities."""
    if not s:
        return ""
    s = re.sub(r"<a[^>]*>.*?</a>", "", s, flags=re.S | re.I)  # drop anchors
    s = re.sub(r"<[^>]+>", "", s)                              # drop remaining tags
    s = html.unescape(s)                                       # unescape entities
    s = re.sub(r"\s+", " ", s).strip()                         # collapse whitespace
    return s


def _clip_words(s: str, max_words: int) -> str:
    """Return the first max_words of s (by whitespace)."""
    if not s or max_words <= 0:
        return ""
    parts = s.split()
    if len(parts) <= max_words:
        return s.strip()
    return " ".join(parts[:max_words]).strip()


# --- The article's own date ------------------------------------------------
#
# The feed's date is not the article's date. Google News re-lists republished
# pieces and dates them to the day they were re-listed, so a "30-day window"
# built on the feed date quietly admits material that is years old. Where the
# page states its own publication date, that date wins; where it does not, the
# feed's date is all there is and the disagreement cannot arise.
#
# The patterns are ordered by how load-bearing the publisher treats them:
# `article:published_time` is the Open Graph field news sites fill deliberately,
# JSON-LD `datePublished` is the schema.org equivalent, and a bare `<time
# datetime=...>` is the weakest because it is often a "last updated" stamp.

_PAGE_DATE_PATTERNS = (
    re.compile(
        r'<meta[^>]+(?:property|name)=["\'](?:og:)?article:published_time["\'][^>]+'
        r'content=["\']([^"\']+)["\']',
        re.I,
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)='
        r'["\'](?:og:)?article:published_time["\']',
        re.I,
    ),
    re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.I),
    re.compile(
        r'<meta[^>]+(?:property|name)=["\'](?:pubdate|publishdate|publication_date)["\']'
        r'[^>]+content=["\']([^"\']+)["\']',
        re.I,
    ),
    re.compile(r'<time[^>]+datetime=["\']([^"\']+)["\']', re.I),
)


def _page_published_at(html_text: str) -> str | None:
    """Return the publication date the article's own page states, or None.

    Args:
        html_text: The raw HTML of the article page.

    Returns:
        An ISO8601 UTC string, or None if the page states no usable date.
    """
    if not html_text:
        return None
    head = html_text[:20000]  # every one of these lives in <head> or early JSON-LD
    for pattern in _PAGE_DATE_PATTERNS:
        m = pattern.search(head)
        if not m:
            continue
        raw = (m.group(1) or "").strip()
        if not raw:
            continue
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        # A page claiming to be from the future is a template, not a date.
        if parsed > dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1):
            continue
        return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return None


async def _fetch_text_async(
    url: str, client: httpx.AsyncClient, max_chars: int = 3000
) -> tuple[str, str | None]:
    try:
        # If somehow still a Google News link, resolve it here too
        if "news.google.com" in urlparse(url).netloc:
            try:
                url = resolve_google_news_url(url)
            except Exception:
                pass

        r = await client.get(url, timeout=15)
        r.raise_for_status()
        # Provide URL context to trafilatura for better extraction heuristics
        text = trafilatura.extract(r.text, url=str(r.url)) or ""
        return text[:max_chars], _page_published_at(r.text)
    except Exception:
        return "", None


def _fetch_text_sync(
    url: str, client: httpx.Client, max_chars: int = 3000
) -> tuple[str, str | None]:
    try:
        if "news.google.com" in urlparse(url).netloc:
            try:
                url = resolve_google_news_url(url)
            except Exception:
                pass

        r = client.get(url, timeout=15)
        r.raise_for_status()
        text = trafilatura.extract(r.text, url=str(r.url)) or ""
        return text[:max_chars], _page_published_at(r.text)
    except Exception:
        return "", None


async def _expand_items_async(entries: List[Dict], max_articles: int, max_chars: int) -> List[Dict]:
    urls = [
        (e.get("publisher_link") or e.get("link"))
        for e in entries[:max_articles]
        if (e.get("publisher_link") or e.get("link"))
    ]
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": UA}) as client:
        texts = await asyncio.gather(
            *(_fetch_text_async(u, client, max_chars) for u in urls),
            return_exceptions=True
        )
    out = []
    for e, t in zip(entries[:max_articles], texts):
        text, page_date = ("", None) if isinstance(t, Exception) else t
        e2 = dict(e)
        e2["text"] = text or ""
        e2["word_count"] = len((text or "").split())
        e2["page_published_at"] = page_date
        out.append(e2)
    return out + entries[max_articles:]


def _expand_items_sync(entries: List[Dict], max_articles: int, max_chars: int) -> List[Dict]:
    urls = [
        (e.get("publisher_link") or e.get("link"))
        for e in entries[:max_articles]
        if (e.get("publisher_link") or e.get("link"))
    ]
    with httpx.Client(follow_redirects=True, headers={"User-Agent": UA}) as client:
        texts = [_fetch_text_sync(u, client, max_chars) for u in urls]
    out = []
    for e, (text, page_date) in zip(entries[:max_articles], texts):
        e2 = dict(e)
        e2["text"] = text or ""
        e2["word_count"] = len((text or "").split())
        e2["page_published_at"] = page_date
        out.append(e2)
    return out + entries[max_articles:]


def gnews_rss(
    query: str,
    *,
    max_results: int = 10,
    expand: bool = True,
    extract_chars: int = 3000,
    lang: str = "en",
    country: str = "US",
    build_summary: bool = True,
    summary_words: int = 240,
    max_age_days: int | None = 30,   # limit by age (None = no filter)
) -> List[Dict]:
    """
    Return Google News RSS items. If expand=True, also fetch and extract each article's main text.

    Each item contains:
      - 'title':          str
      - 'link':           str (original Google News link)
      - 'publisher_link': str (resolved publisher URL)
      - 'published':      ISO8601 str or None
      - 'source':         str (publisher name if available)
      - 'snippet':        str (PLAIN TEXT, links removed)
      - 'snippet_html':   str (original RSS summary with HTML)
      - ['text','word_count'] present when expand=True and extraction succeeds
      - ['summary','summary_word_count'] present when build_summary=True
      - 'page_published_at': ISO8601 str or None — the date the article's own
        page states, which is not the feed's date for a republished piece
      - 'stale_republication': bool — the page's own date falls outside the
        window even though the feed's date did not

    Args:
        max_age_days: If set, discard items older than this many days (items
                      without a publish date are discarded).
    """
    url = _gnews_url(query, lang=lang, country=country)
    feed = feedparser.parse(url)

    cutoff = None
    if max_age_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)

    items: List[Dict] = []
    for e in feed.entries:
        # Parse published time (UTC-aware)
        published_dt = None
        if getattr(e, "published_parsed", None):
            published_dt = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)

        # Age filter
        if cutoff is not None:
            if (published_dt is None) or (published_dt < cutoff):
                continue

        raw_summary = getattr(e, "summary", "") or ""
        plain_summary = _strip_html(raw_summary)

        source_title = ""
        src = getattr(e, "source", None)
        if src and hasattr(src, "title"):
            source_title = getattr(src, "title", "") or ""
        elif isinstance(src, str):
            source_title = src

        raw_link = getattr(e, "link", "") or ""
        try:
            publisher_link = resolve_google_news_url(raw_link)
        except Exception:
            publisher_link = raw_link

        # Drop denylisted publishers up front — never fetched, scored, or stored.
        if is_blocked_url(publisher_link) or is_blocked_url(raw_link):
            continue

        items.append({
            "title": getattr(e, "title", "") or "",
            "link": raw_link,                     # keep original for reference
            "publisher_link": publisher_link,     # use this for fetching content
            "published": published_dt.isoformat().replace("+00:00", "Z") if published_dt else None,
            "source": source_title,
            "snippet": plain_summary,
            "snippet_html": raw_summary,
        })

        # Stop once we have enough recent items
        if len(items) >= max_results:
            break

    # Optionally expand with article body text (limit to number of kept items)
    if expand and items:
        try:
            _ = asyncio.get_running_loop()  # raises RuntimeError if none
            # If we're already in an event loop, use sync fallback to avoid nested loop issues
            items = _expand_items_sync(items, max_articles=len(items), max_chars=extract_chars)
        except RuntimeError:
            items = asyncio.run(_expand_items_async(items, max_articles=len(items), max_chars=extract_chars))

    # Build longer plain-text summaries
    if build_summary and items:
        for e in items:
            base = e.get("text") or e.get("snippet") or ""
            summary = _clip_words(base, summary_words)
            e["summary"] = summary
            e["summary_word_count"] = len(summary.split())

    # Re-apply the window to the article's own date, now that expansion has
    # found it. The feed dates a republished piece to the day it was re-listed,
    # so this is the only point at which a years-old article can be caught.
    # Items are marked rather than dropped: the caller counts them, because a
    # silent drop makes a retrieval problem look like thin coverage.
    if max_age_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)
        for e in items:
            page_date = e.get("page_published_at")
            e["stale_republication"] = False
            if not page_date:
                continue
            try:
                parsed = dt.datetime.fromisoformat(page_date.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed < cutoff:
                e["stale_republication"] = True

    return items
