import re
import json
import requests

from bs4 import BeautifulSoup
from urllib.parse import urljoin
from typing import Optional, Tuple, List


# --------------------------- HTTP / Config ---------------------------

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
)

_REMOVALS = {"script", "style", "noscript", "iframe", "svg", "footer", "header", "nav", "aside", "form"}

# Generic, publisher-agnostic image meta keys
_META_IMAGE_KEYS = [
    ("property", "og:image"),
    ("property", "og:image:secure_url"),
    ("property", "og:image:url"),
    ("name", "twitter:image"),
    ("name", "twitter:image:src"),
    ("itemprop", "image"),
    ("name", "parsely-image"),  # common analytics key used by many sites
]

_IMG_ATTR_CANDIDATES = [
    "src", "data-src", "data-original", "data-lazy-src", "data-image", "data-thumb"
]

# --------------------------- URL helpers ---------------------------

def _absolutize(candidate: str, base: str) -> Optional[str]:
    """Make candidate URL absolute against base, return http(s) URL or None."""
    if not candidate:
        return None
    u = urljoin(base, candidate)
    if u.startswith(("http://", "https://")):
        return u
    return None

def _best_from_srcset(srcset: str, base: str) -> Optional[str]:
    """Choose the largest candidate from an HTML srcset string."""
    try:
        parts = [p.strip() for p in srcset.split(",") if p.strip()]
        scored = []
        for p in parts:
            bits = p.split()
            if not bits:
                continue
            url = _absolutize(bits[0], base)
            if not url:
                continue
            w = 0
            if len(bits) > 1 and bits[1].endswith("w"):
                try:
                    w = int(bits[1][:-1])
                except Exception:
                    w = 0
            scored.append((w, url))
        scored.sort(reverse=True)
        return scored[0][1] if scored else None
    except Exception:
        return None

# --------------------------- Image extraction ---------------------------

def _collect_meta_images(soup: BeautifulSoup, base_url: str) -> List[str]:
    """
    Gather image candidates from:
      - OpenGraph/Twitter/itemprop/parsely meta tags
      - <link rel="image_src">
      - JSON-LD (image/thumbnailUrl in Article/NewsArticle or any graph)
    """
    out: List[str] = []

    # OpenGraph / Twitter / itemprop / parsely / og:image:url
    for attr, key in _META_IMAGE_KEYS:
        for tag in soup.find_all("meta", attrs={attr: key}):
            content = (tag.get("content") or "").strip()
            u = _absolutize(content, base_url)
            if u:
                out.append(u)

    # link rel="image_src"
    for link in soup.find_all("link", rel=lambda v: v and "image_src" in v):
        href = (link.get("href") or "").strip()
        u = _absolutize(href, base_url)
        if u:
            out.append(u)

    # JSON-LD (prefer Article/NewsArticle, but accept any object with image/thumbnailUrl)
    def push_image(val):
        if isinstance(val, str):
            u = _absolutize(val, base_url)
            if u:
                out.append(u)
        elif isinstance(val, dict):
            u = _absolutize(
                val.get("url") or val.get("contentUrl") or val.get("thumbnailUrl") or "",
                base_url,
            )
            if u:
                out.append(u)
        elif isinstance(val, list):
            for v in val:
                push_image(v)

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = script.string or ""
            if not raw.strip():
                continue
            data = json.loads(raw)
        except Exception:
            continue

        nodes = data if isinstance(data, list) else [data]
        for obj in list(nodes):  # copy so we can extend safely
            if not isinstance(obj, dict):
                continue

            # Handle @graph containers as well
            graph = obj.get("@graph")
            if isinstance(graph, list):
                nodes.extend([n for n in graph if isinstance(n, dict)])

            # Pull from Article-like or generic nodes
            if obj.get("@type") in ("Article", "NewsArticle") or any(
                k in obj for k in ("image", "thumbnailUrl")
            ):
                img_field = obj.get("image") or obj.get("thumbnailUrl")
                if img_field:
                    push_image(img_field)

    # Dedup preserve order
    seen = set()
    uniq = []
    for u in out:
        if u not in seen:
            uniq.append(u)
            seen.add(u)
    return uniq

def _first_content_image(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    """Fallback: traverse likely containers and pick a meaningful <img>."""
    containers = []
    art = soup.find("article")
    if art:
        containers.append(art)
    main = soup.find("main") or soup.find(attrs={"role": "main"})
    if main:
        containers.append(main)
    if not containers:
        containers = [soup]

    for c in containers:
        imgs = c.find_all("img")
        for img in imgs:
            # srcset gives multiple sizes—pick biggest
            srcset = img.get("srcset")
            if srcset:
                u = _best_from_srcset(srcset, base_url)
                if u:
                    return u
            # else check common lazy attrs and src
            for attr in _IMG_ATTR_CANDIDATES:
                val = (img.get(attr) or "").strip()
                if not val:
                    continue
                u = _absolutize(val, base_url)
                if u and not any(t in u.lower() for t in ("/pixel", "1x1", "spacer.gif")):
                    return u
    return None

def extract_thumbnail_from_html(html: str, base_url: str) -> str:
    """Public helper if you already have HTML. Returns best thumbnail URL or ''."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        metas = _collect_meta_images(soup, base_url)
        if metas:
            return metas[0]
        u2 = _first_content_image(soup, base_url)
        return u2 or ""
    except Exception:
        return ""

# --------------------------- Text extraction & summary ---------------------------

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()

def _best_container(soup: BeautifulSoup) -> Optional[BeautifulSoup]:
    """
    Heuristic: prefer <article>; else role=main/<main>; else largest <div> by <p> text length.
    """
    candidates = []

    # 1) <article>
    articles = soup.find_all("article")
    if articles:
        candidates.extend(articles)

    # 2) role=main or <main>
    main = soup.find(attrs={"role": "main"}) or soup.find("main")
    if main:
        candidates.append(main)

    # 3) class/id hints
    hints = ["article", "content", "story", "post", "entry", "body", "read", "main", "text"]
    for tag in soup.find_all(True):
        cid = (tag.get("class") or []) + [tag.get("id") or ""]
        if any(h in " ".join(map(str, cid)).lower() for h in hints):
            candidates.append(tag)

    # Deduplicate while preserving order
    seen = set()
    uniq = []
    for c in candidates:
        if id(c) not in seen:
            uniq.append(c)
            seen.add(id(c))
    if not uniq:
        return None

    def score(node) -> int:
        ps = node.find_all("p")
        return sum(len(_clean(p.get_text(" ", strip=True))) for p in ps)

    return max(uniq, key=score)

def extract_main_text_from_html(html: str) -> str:
    """Extract the main article text from HTML using lightweight heuristics."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        # Strip obvious non-content
        for tag in list(_REMOVALS):
            for n in soup.find_all(tag):
                n.decompose()
        container = _best_container(soup) or soup
        paragraphs = [
            _clean(p.get_text(" ", strip=True))
            for p in container.find_all("p")
        ]
        paragraphs = [p for p in paragraphs if len(p) > 40]  # drop very short junk
        text = _clean(" ".join(paragraphs))
        return text
    except Exception:
        return ""

def summarize_lead(text: str, max_words: int = 160) -> str:
    """
    Simple lead summary: take sentences until we hit ~max_words.
    """
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out, words = [], 0
    for s in sentences:
        w = len(s.split())
        if w == 0:
            continue
        if words + w > max_words and out:
            break
        out.append(s)
        words += w
        if words >= max_words:
            break
    return _clean(" ".join(out))[:2000]  # safety cap

# --------------------------- One-request high-level API ---------------------------

def get_article_assets(
    url: str,
    session: Optional[requests.Session] = None,
    timeout: float = 10.0,
    max_words: int = 160,
) -> Tuple[str, str, str]:
    """
    Fetch the URL exactly once and return:
        (thumbnail_url, summary, full_text)

    - Ensures a single HTTP GET (no double-pinging).
    - Thumbnail derived from OG/Twitter/JSON-LD, with <img> fallback.
    - Summary is a lead-like extract up to ~max_words.
    - Returns empty strings on failure.

    Args:
        url: Article URL to fetch.
        session: Optional requests.Session to reuse connections.
        timeout: Per-request timeout in seconds.
        max_words: Target summary length.

    Returns:
        (thumbnail_url, summary, full_text)
    """
    try:
        s = session or requests.Session()
        r = s.get(url, headers={"user-agent": _UA}, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        base_url = r.url  # after redirects
        html = r.text

        # Parse once for thumbnail
        soup = BeautifulSoup(html, "html.parser")
        metas = _collect_meta_images(soup, base_url)
        thumb = metas[0] if metas else (_first_content_image(soup, base_url) or "")

        # Extract text (separate flow so we don't mutate soup used for image logic)
        full_text = extract_main_text_from_html(html)
        summary = summarize_lead(full_text, max_words=max_words) if full_text else ""

        return thumb, summary, full_text
    except Exception:
        return "", "", ""

# --------------------------- Backwards-compatible helpers ---------------------------

def extract_thumbnail(
    url: str,
    session: Optional[requests.Session] = None,
    timeout: float = 10.0,
) -> str:
    """
    Backwards-compatible: fetches and returns only the thumbnail URL.
    NOTE: This performs its own GET. Prefer `get_article_assets()` to avoid multiple requests.
    """
    try:
        s = session or requests.Session()
        r = s.get(url, headers={"user-agent": _UA}, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return extract_thumbnail_from_html(r.text, r.url)
    except Exception:
        return ""

def extract_and_summarize(
    url: str,
    session: Optional[requests.Session] = None,
    timeout: float = 10.0,
    max_words: int = 160,
) -> Tuple[str, str]:
    """
    Backwards-compatible: fetches and returns (summary, full_text).
    NOTE: This performs its own GET. Prefer `get_article_assets()` to avoid multiple requests.
    """
    try:
        s = session or requests.Session()
        r = s.get(url, headers={"user-agent": _UA}, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        text = extract_main_text_from_html(r.text)
        if not text:
            return "", ""
        summary = summarize_lead(text, max_words=max_words)
        return summary, text
    except Exception:
        return "", ""
