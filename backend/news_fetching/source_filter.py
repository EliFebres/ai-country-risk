"""
News-source denylist.

Articles from blocked publishers must never reach the AI or the website, so we
filter them out at fetch time. The denylist lives in a plain-text file
(``blocked_sources.txt``) the user can edit — one domain or URL per line — and
this module loads it once per process (``functools.lru_cache``) and exposes
``is_blocked_url`` for the fetch pipeline.

Matching is host-based and subdomain-inclusive: an entry of ``whalesbook.com``
blocks ``www.whalesbook.com``, ``m.whalesbook.com``, etc. A leading ``www.`` is
ignored and matching is case-insensitive. Full URLs and bare domains are both
accepted in the file. Degrades gracefully (blocks nothing) if the file is
missing or unreadable.
"""

import logging
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

BLOCKLIST_PATH = Path(__file__).with_name("blocked_sources.txt")


def _normalize_host(value: str) -> str:
    """Reduce a denylist line (URL or bare domain) to a comparable host.

    Drops scheme, path, port, and a leading ``www.``; lowercases. Returns ``""``
    for blanks and ``#`` comments so callers can discard them.
    """
    v = (value or "").strip().lower()
    if not v or v.startswith("#"):
        return ""
    if "://" in v:
        v = urlparse(v).netloc
    else:
        v = v.split("/", 1)[0]  # strip any path on a bare-domain entry
    v = v.split(":", 1)[0]       # strip port
    if v.startswith("www."):
        v = v[4:]
    return v


@lru_cache(maxsize=1)
def load_blocked_domains() -> frozenset[str]:
    """Load and normalize the denylist once per process.

    Returns an empty set (block nothing) if the file is absent or unreadable.
    """
    try:
        lines = BLOCKLIST_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return frozenset()
    except Exception as e:  # noqa: BLE001 - never let config IO break the pipeline
        logger.warning("Could not read blocked_sources.txt: %s", e)
        return frozenset()

    domains = {_normalize_host(line) for line in lines}
    domains.discard("")
    if domains:
        logger.info("Loaded %d blocked news source(s).", len(domains))
    return frozenset(domains)


def _host_of(url: str) -> str:
    """Extract a normalized host (no port, no leading ``www.``) from a URL."""
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return ""
    host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def is_blocked_url(url: str | None) -> bool:
    """True if ``url``'s host matches a denylist entry (subdomain-inclusive)."""
    if not url:
        return False
    blocked = load_blocked_domains()
    if not blocked:
        return False

    host = _host_of(url)
    if not host:
        return False

    # Match the full host or any parent domain, so a denylisted registrable
    # domain also blocks its subdomains (sub.whalesbook.com -> whalesbook.com).
    parts = host.split(".")
    for i in range(len(parts) - 1):
        if ".".join(parts[i:]) in blocked:
            return True
    return False
