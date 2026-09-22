import json
import requests

from typing import Optional
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs


# Keep a realistic UA; some endpoints return different HTML otherwise
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
)


def resolve_google_news_url(
    gnews_url: str,
    session: Optional[requests.Session] = None,
    timeout: float = 8.0,
) -> str:
    """
    Resolve a Google News RSS link like:
      https://news.google.com/rss/articles/CBMi...
    to the publisher's *raw* article URL.

    Strategy (in order):
      1) If the link already has ?url=<publisher>, return it.
      2) Parse the article page and POST to the DotsSplashUi batchexecute
         endpoint to retrieve the final URL.
      3) Fallbacks: <meta http-equiv="refresh">, or first external <a href>.
      4) If all else fails, return the original gnews_url.

    Safe to call at scale; returns the original if resolution fails.
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
        # On any error, just return the original URL instead of exploding
        pass

    return gnews_url
