"""Unwrap Google News RSS links to the publisher's real article URL.

Google News RSS returns opaque ``news.google.com/rss/articles/CBMi...`` links.
Those are useless downstream: the scraper cannot extract from them, the
denylist cannot match a publisher host, and storing them means every article
on the dashboard points at Google rather than the source. This module resolves
them, preferring Google's own batchexecute endpoint and falling back to
meta-refresh / first external anchor.

Resolution is best-effort by contract: any failure returns the original URL
(logged with a traceback) rather than raising, because a single unresolvable
link must not drop an article from the run.
"""

import json
import logging
import requests

from typing import Optional
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs

from backend.util.http import BROWSER_UA as _UA

logger = logging.getLogger(__name__)


def resolve_google_news_url(
    gnews_url: str,
    session: Optional[requests.Session] = None,
    timeout: float = 8.0,
) -> str:
    """Resolve a Google News RSS link to the publisher's raw article URL.

    Strategy, in order:
      1) If the link already carries ``?url=<publisher>``, use that.
      2) Parse the article page and POST to the DotsSplashUi batchexecute
         endpoint to retrieve the final URL.
      3) Fall back to ``<meta http-equiv="refresh">``, then the first external
         ``<a href>``.

    Args:
        gnews_url: the Google News link (a non-Google URL is returned as-is).
        session: session to reuse across calls.
        timeout: per-request timeout in seconds.

    Returns:
        The publisher URL, or ``gnews_url`` unchanged if every strategy failed.
        Safe to call at scale; never raises.
    """
    try:
        parsed = urlparse(gnews_url)
        if "news.google.com" not in parsed.netloc:
            # Already a raw publisher URL
            return gnews_url

        # Case 1: sometimes Google includes the direct URL as a query param
        qs = parse_qs(parsed.query)
        if "url" in qs and qs["url"]:
            return qs["url"][0]

        s = session or requests.Session()
        r = s.get(gnews_url, headers={"user-agent": _UA}, timeout=timeout)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Case 2: use the hidden data-p payload to call batchexecute
        cwiz = soup.select_one("c-wiz[data-p]")
        if cwiz and cwiz.has_attr("data-p"):
            data_p = cwiz["data-p"]
            # Normalize the weird prefix into valid JSON then build f.req payload
            obj = json.loads(data_p.replace('%.@.', '["garturlreq",'))
            payload = {
                "f.req": json.dumps([[["Fbv4je", json.dumps(obj[:-6] + obj[-2:]), "null", "generic"]]])
            }
            resp2 = s.post(
                "https://news.google.com/_/DotsSplashUi/data/batchexecute",
                headers={
                    "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "user-agent": _UA,
                },
                data=payload,
                timeout=timeout,
            )
            txt = resp2.text.lstrip(")]}'\n")
            array_string = json.loads(txt)[0][2]
            final_url = json.loads(array_string)[1]
            if isinstance(final_url, str) and final_url.startswith("http"):
                return final_url

        # Case 3a: meta refresh fallback
        meta = soup.find("meta", attrs={"http-equiv": "refresh"})
        if meta:
            content = (meta.get("content") or "")
            parts = content.split("url=", 1)
            if len(parts) == 2 and parts[1].strip().startswith("http"):
                return parts[1].strip()

        # Case 3b: first external anchor that isn't to news.google.com
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("http") and "news.google.com" not in href:
                return href

    except Exception:
        # Documented contract: on any error return the original URL instead of
        # exploding — but leave a trace so resolver breakage is visible in logs.
        logger.warning("Could not resolve %s; returning original", gnews_url, exc_info=True)

    return gnews_url
