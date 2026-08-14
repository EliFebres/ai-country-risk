"""Google News RSS search plus article-body extraction.

``gnews_rss`` is the pipeline's news source: it queries Google News RSS,
unwraps the redirect links to real publisher URLs, drops denylisted
publishers, and fetches each article's main text with trafilatura so the risk
prompt sees real content rather than a one-line RSS blurb.

Body fetches run concurrently (httpx + asyncio) because a country's ~15
articles are otherwise a long serial wait; the synchronous variants exist for
the case where a caller is already inside an event loop, where
``asyncio.run`` would fail.
"""

import re
import html
import httpx
import asyncio
import feedparser
import datetime as dt

from typing import List, Dict
from urllib.parse import urlencode, quote_plus, urlparse

from backend.utils.news_fetching import core
from backend.utils.news_fetching.url_resolver import resolve_google_news_url
from backend.utils.news_fetching.source_filter import is_blocked_url

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
    return " ".join(s.split())                                 # collapse whitespace


def _clip_words(s: str, max_words: int) -> str:
    """Return the first max_words of s (by whitespace)."""
    if not s or max_words <= 0:
        return ""
    parts = s.split()
    if len(parts) <= max_words:
        return s.strip()
    return " ".join(parts[:max_words]).strip()


async def _fetch_text_async(url: str, client: httpx.AsyncClient, max_chars: int = 3000) -> str:
    """Fetch and extract one article's main text (empty string on any failure).

    Args:
        url: publisher URL, or a Google News link still needing resolution.
        client: shared async client.
        max_chars: truncation cap on the extracted text.

    Returns:
        The extracted body, truncated, or "" if the fetch or extraction failed
        — a missing body is normal (paywalls, JS-only pages) and must not stop
        the surrounding batch.
    """
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
        return core.extract_body(r.text, url=str(r.url))[:max_chars]
    except Exception:
        return ""


def _fetch_text_sync(url: str, client: httpx.Client, max_chars: int = 3000) -> str:
    """Synchronous twin of ``_fetch_text_async``, same contract.

    Used only when the caller is already inside an event loop.
    """
    try:
        if "news.google.com" in urlparse(url).netloc:
            try:
                url = resolve_google_news_url(url)
            except Exception:
                pass

        r = client.get(url, timeout=15)
        r.raise_for_status()
        return core.extract_body(r.text, url=str(r.url))[:max_chars]
    except Exception:
        return ""


async def _expand_items_async(entries: List[Dict], max_articles: int, max_chars: int) -> List[Dict]:
    """Add ``text``/``word_count`` to the first ``max_articles`` entries.

    Fetches all bodies concurrently. Entries beyond ``max_articles`` are passed
    through untouched; a per-article failure yields an empty body, never an
    exception.
    """
    urls = [core.dedupe_key(e) for e in entries[:max_articles] if core.dedupe_key(e)]
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": UA}) as client:
        texts = await asyncio.gather(
            *(_fetch_text_async(u, client, max_chars) for u in urls),
            return_exceptions=True
        )
    out = []
    for e, t in zip(entries[:max_articles], texts):
        text = "" if isinstance(t, Exception) else (t or "")
        e2 = dict(e)
        e2["text"] = text
        e2["word_count"] = len(text.split())
        out.append(e2)
    return out + entries[max_articles:]


def _expand_items_sync(entries: List[Dict], max_articles: int, max_chars: int) -> List[Dict]:
    """Synchronous twin of ``_expand_items_async``, same contract."""
    urls = [core.dedupe_key(e) for e in entries[:max_articles] if core.dedupe_key(e)]
    with httpx.Client(follow_redirects=True, headers={"User-Agent": UA}) as client:
        texts = [_fetch_text_sync(u, client, max_chars) for u in urls]
    out = []
    for e, text in zip(entries[:max_articles], texts):
        e2 = dict(e)
        e2["text"] = text or ""
        e2["word_count"] = len((text or "").split())
        out.append(e2)
    return out + entries[max_articles:]


def gnews_rss(
    query: str,
    *,
    max_results: int = 10,
    extract_chars: int = 3000,
    summary_words: int = 240,
) -> List[Dict]:
    """
    Return Google News RSS items (English/US feed, max 30 days old), each
    expanded with the article's extracted main text and a plain-text summary.

    Items are ``core.normalize_item`` dicts — the same canonical shape every
    historical adapter emits — carrying:
      - 'title':          str
      - 'link':           str (original Google News link)
      - 'publisher_link': str (resolved publisher URL)
      - 'published':      ISO8601 str or None
      - 'source':         str (publisher name if available)
      - 'snippet':        str (PLAIN TEXT, links removed)
      - '_theme':         None here; the caller tags it with the query that found it
      - 'snippet_html':   str (original RSS summary with HTML)
      - 'text', 'word_count'              (extracted body, may be empty)
      - 'summary', 'summary_word_count'   (first summary_words of text/snippet)
    """
    url = _gnews_url(query)
    feed = feedparser.parse(url)

    # Discard items older than 30 days (or with no publish date at all).
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)

    items: List[Dict] = []
    for e in feed.entries:
        # Parse published time (UTC-aware)
        published_dt = None
        if getattr(e, "published_parsed", None):
            published_dt = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)

        # Age filter
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

        items.append(core.normalize_item(
            title=getattr(e, "title", "") or "",
            link=raw_link,                     # keep original for reference
            publisher_link=publisher_link,     # use this for fetching content
            published=published_dt.isoformat().replace("+00:00", "Z") if published_dt else None,
            source=source_title,
            snippet=plain_summary,
            snippet_html=raw_summary,
        ))

        # Stop once we have enough recent items
        if len(items) >= max_results:
            break

    # Expand with article body text (limit to number of kept items)
    if items:
        try:
            _ = asyncio.get_running_loop()  # raises RuntimeError if none
            # If we're already in an event loop, use sync fallback to avoid nested loop issues
            items = _expand_items_sync(items, max_articles=len(items), max_chars=extract_chars)
        except RuntimeError:
            items = asyncio.run(_expand_items_async(items, max_articles=len(items), max_chars=extract_chars))

    # Build longer plain-text summaries
    if items:
        for e in items:
            base = e.get("text") or e.get("snippet") or ""
            summary = _clip_words(base, summary_words)
            e["summary"] = summary
            e["summary_word_count"] = len(summary.split())

    return items
