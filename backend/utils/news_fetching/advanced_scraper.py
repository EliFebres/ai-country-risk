"""Crawlbase-backed metadata scraper for the pages the simple scraper can't read.

Some publishers render their article (and its OpenGraph image) only after
JavaScript runs, so ``simple_scraper``'s single GET comes back without a
thumbnail. This module fetches through Crawlbase, which renders the page
first. It costs an API credit per call, so ``main`` uses it sparingly: only
for a country's Top-3 articles, and only when no image was found otherwise.

Requests honor robots.txt (parsed once per host, cached for the process), and
every failure is returned as an ``error``/``skipped`` marker in the result
dict rather than raised — one unscrapable article must not interrupt the run.
"""

import json

import requests
import tldextract

from bs4 import BeautifulSoup
from urllib import robotparser
from urllib.parse import urlparse
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple

# -------------------- Tuned constants (faster + safer) -------------------- #
API_BASE = "https://api.crawlbase.com"

# Use a connect/read tuple for tighter control (vs single long timeout).
TIMEOUT_CONNECT_SECS = 5
TIMEOUT_READ_SECS = 20
TIMEOUT_TUPLE: Tuple[int, int] = (TIMEOUT_CONNECT_SECS, TIMEOUT_READ_SECS)

# Crawlbase waits — most publishers expose OG/Twitter tags without long JS idle time.
PAGE_WAIT_MS = 1000   # was 2000
AJAX_WAIT_MS = 300    # was 2000

# Global UA
DEFAULT_UA = "NewsMetaScraper/1.0 (AI Country Risk) Python"


# -------------------- Time helper -------------------- #
def now_utc_z() -> str:
    """ISO 8601 UTC with trailing 'Z'."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# -------------------- robots.txt compliance (with timeouts & caching) -------------------- #
_robots_cache: Dict[str, robotparser.RobotFileParser] = {}
_ROBOTS_TIMEOUT: Tuple[int, int] = (3, 3)  # (connect, read)

def _fetch_robots_txt(base: str) -> Optional[str]:
    """
    Fetch /robots.txt with explicit short timeouts.
    Returns the content (str) or None on failure.
    """
    try:
        resp = requests.get(
            f"{base}/robots.txt",
            headers={"User-Agent": DEFAULT_UA, "Accept-Encoding": "gzip"},
            timeout=_ROBOTS_TIMEOUT,
        )
        if resp.status_code >= 400:
            return None
        return resp.text or ""
    except Exception:
        return None

def robots_allowed(url: str, user_agent: str = DEFAULT_UA) -> bool:
    """
    Parse and cache robots.txt for the host, then check can_fetch.
    Returns False if robots can't be fetched or parsed (conservative).
    """
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    host = parsed.netloc
    base = f"{scheme}://{host}"

    rp = _robots_cache.get(base)
    if rp is None:
        txt = _fetch_robots_txt(base)
        if txt is None:
            return False  # conservative: can't fetch robots
        rp = robotparser.RobotFileParser()
        try:
            # Use .parse() so we control the fetch timeout above
            rp.parse(txt.splitlines())
            _robots_cache[base] = rp
        except Exception:
            return False

    try:
        return rp.can_fetch(user_agent, url)
    except Exception:
        return False


# -------------------- Crawlbase fetch -------------------- #
def crawlbase_fetch(url: str, token: str) -> Dict[str, Any]:
    """
    Hit Crawlbase with format=json to receive HTML body plus metadata.
    Tuned to avoid excessive page waits and with explicit timeouts.
    """
    params = {
        "token": token,
        "url": url,
        "format": "json",     # returns JSON envelope with 'body', 'original_status', etc.
        "device": "desktop",
        "page_wait": PAGE_WAIT_MS,
        "ajax_wait": AJAX_WAIT_MS,
    }
    r = requests.get(
        API_BASE,
        params=params,
        headers={"Accept-Encoding": "gzip", "User-Agent": DEFAULT_UA},
        timeout=TIMEOUT_TUPLE,   # (connect, read)
    )
    r.raise_for_status()
    return r.json()


# -------------------- HTML parsing helpers -------------------- #
def _first_meta(soup: BeautifulSoup, *names: str) -> Optional[str]:
    """Content of the first ``<meta>`` matching any of ``names`` (by property or name)."""
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None

def _parse_json_ld(soup: BeautifulSoup) -> Dict[str, Any]:
    """
    Parse the first Article/NewsArticle JSON-LD block we can find.
    """
    out: Dict[str, Any] = {}
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            raw = script.string or ""
            if not raw.strip():
                continue
            data = json.loads(raw)
        except Exception:
            continue

        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            typ = obj.get("@type") or obj.get("type")
            if isinstance(typ, list):
                typ = next((t for t in typ if isinstance(t, str)), None)
            if str(typ).lower() in {"article", "newsarticle"} or obj.get("headline") or obj.get("datePublished"):
                out.setdefault("headline", obj.get("headline"))
                out.setdefault("datePublished", obj.get("datePublished") or obj.get("dateCreated"))
                # image can be str/list/dict
                img = obj.get("image")
                if isinstance(img, str):
                    out.setdefault("image", img)
                elif isinstance(img, list) and img:
                    out.setdefault("image", img[0])
                elif isinstance(img, dict) and img.get("url"):
                    out.setdefault("image", img.get("url"))
                if obj.get("description"):
                    out.setdefault("description", obj["description"])
                pub = obj.get("publisher")
                if isinstance(pub, dict) and pub.get("name"):
                    out.setdefault("source", pub.get("name"))
                return out
    return out

def extract_metadata(html: str, url: str) -> Dict[str, Any]:
    """Pull title/description/image/publish-date out of a rendered page.

    Tries OpenGraph and Twitter cards first, then JSON-LD (usually the better
    source for dates and images), then a per-domain nudge where those fall
    short.

    Returns:
        ``{title, description, image_url, published_at, source_domain}``, any
        value of which may be None if the page didn't expose it.
    """
    soup = BeautifulSoup(html, "html.parser")
    ext = tldextract.extract(url)
    domain = ".".join([p for p in [ext.domain, ext.suffix] if p])

    # Generic OG/Twitter
    title = _first_meta(soup, "og:title", "twitter:title") or (soup.title.string.strip() if soup.title else None)
    description = _first_meta(soup, "og:description", "twitter:description")
    image = _first_meta(soup, "og:image", "twitter:image", "twitter:image:src")
    published = _first_meta(soup, "article:published_time", "og:pubdate", "publish_date", "date")

    # JSON-LD fallback (often better for date/image)
    ld = _parse_json_ld(soup)
    title = title or ld.get("headline")
    description = description or ld.get("description")
    image = image or ld.get("image")
    published = published or ld.get("datePublished")

    # Domain nudge: Reuters exposes the publish date in a <time> tag.
    if domain == "reuters.com":
        time_tag = soup.find("time", attrs={"datetime": True})
        if time_tag and time_tag.get("datetime"):
            published = published or time_tag["datetime"].strip()

    return {
        "title": title,
        "description": description,
        "image_url": image,
        "published_at": published,
        "source_domain": domain,
    }


# -------------------- Orchestrator -------------------- #
def scrape_one(url: str, token: str) -> Dict[str, Any]:
    """Fetch one URL via Crawlbase (single attempt) and extract its metadata.

    Args:
        url: article URL to render and scrape.
        token: Crawlbase API token.

    Returns:
        On success, the ``extract_metadata`` fields plus ``url``,
        ``fetched_at``, ``original_status``, ``html_bytes``. On failure, a dict
        carrying ``skipped``/``reason`` (robots disallow) or ``error``. Never
        raises — callers check for those keys and move on.
    """
    if not robots_allowed(url):
        return {
            "url": url,
            "skipped": True,
            "reason": "robots_disallow",
            "fetched_at": now_utc_z(),
        }

    try:
        cb = crawlbase_fetch(url, token)
        original_status = cb.get("original_status")
        body = cb.get("body") or ""

        if original_status is None or int(original_status) >= 400:
            if original_status is not None and 400 <= int(original_status) < 500:
                return {
                    "url": url,
                    "fetched_at": now_utc_z(),
                    "original_status": original_status,
                    "error": f"origin_4xx:{original_status}",
                }
            # 5xx or missing body/status: surface as a generic failure below.
            raise RuntimeError(f"Upstream status/body invalid: {original_status}, bytes={len(body)}")

        meta = extract_metadata(body, url)
        return {
            "url": url,
            "fetched_at": now_utc_z(),
            "original_status": original_status,
            "html_bytes": len(body),
            **meta,
        }

    except Exception as e:
        return {
            "url": url,
            "error": f"failed_after_1_attempts: {e}",
            "fetched_at": now_utc_z(),
        }
