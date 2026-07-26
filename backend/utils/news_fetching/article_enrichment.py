"""Gathering a country's articles and filling in what the feed leaves out.

Google News RSS gives titles, redirect links, and a one-line blurb. The risk
prompt and the dashboard need more than that, so this module does the network
work in three stages:

  1. ``fetch_relevant_news`` — four queries per country (a broad catch-all plus
     government, economic, and security angles), de-duplicated and scored.
     One query reliably misses whole categories of news.
  2. ``resolve_and_enrich`` — unwrap the Google redirect links to real
     publishers, drop denylisted sources, then a single GET per article to
     recover a summary, body text, and thumbnail.
  3. ``enrich_top_images`` — a last resort for the chosen Top-3 only, using
     Crawlbase (JS rendering) on articles still missing an image. It costs an
     API credit per call, hence the narrow scope.

Every stage degrades rather than raises: a country with thin coverage still
produces a snapshot.
"""

import logging
from typing import Dict, List

import requests

from backend.utils.dates import parse_date_for_sort
from backend.utils.news_fetching import article_ranking, fetch_links
from backend.utils.news_fetching.advanced_scraper import crawlbase_token
from backend.utils.news_fetching.advanced_scraper import scrape_one as crawlbase_scrape_one
from backend.utils.news_fetching.simple_scraper import get_article_assets
from backend.utils.news_fetching.source_filter import is_blocked_url
from backend.utils.news_fetching.url_resolver import resolve_google_news_url

logger = logging.getLogger(__name__)

# Articles scoring below this are treated as off-topic noise.
_RELEVANCE_THRESHOLD = 0.3

# Body text stored per article; also the cap requested from the feed expander.
_MAX_CONTENT_CHARS = 24000


def fetch_relevant_news(country_name: str, max_articles: int = 20) -> List[Dict]:
    """Gather and rank recent news for one country.

    Runs four Google News queries — a broad catch-all plus government,
    economic, and security angles — because a single query reliably misses
    whole categories. Results are de-duplicated by URL and scored by
    ``article_ranking.score_relevance``.

    Args:
        country_name: country to search for.
        max_articles: cap on the returned list.

    Returns:
        Up to ``max_articles`` items, most relevant first. If fewer than 3
        clear the relevance bar, the bar is dropped and the best of the raw
        pool are used instead: the snapshot needs 3 articles, and thin
        coverage is better than an empty pane.
    """
    queries = [
        # Broad catch-all to maximize recall; noise is filtered by scoring
        f'"{country_name}"',

        # Government/Political
        f'"{country_name}" (government OR president OR prime minister OR parliament OR election OR cabinet OR coup OR protest)',

        # Economic/Central Bank
        f'"{country_name}" (central bank OR interest rate OR inflation OR GDP OR currency OR monetary policy OR IMF OR World Bank)',

        # Security/Military
        f'"{country_name}" (military OR defense OR conflict OR war OR attack OR sanctions OR security OR terrorism)',
    ]

    all_items: List[Dict] = []
    seen_urls = set()

    for query in queries:
        items = fetch_links.gnews_rss(
            query=query,
            max_results=15,           # up to ~60 raw before de-dupe
            extract_chars=_MAX_CONTENT_CHARS,
            summary_words=240,
        )

        for item in items:
            url = item.get("link", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_items.append(item)

    # Score each article
    for item in all_items:
        item["relevance_score"] = article_ranking.score_relevance(item, country_name)

    # High-quality filter first
    filtered = [it for it in all_items if it.get("relevance_score", 0) >= _RELEVANCE_THRESHOLD]
    filtered.sort(key=lambda x: (x.get("relevance_score", 0.0), parse_date_for_sort(x.get("published"))), reverse=True)

    # If we have very few, relax threshold to ensure >=3 (if possible)
    if len(filtered) < article_ranking.TOP_N:
        logger.info("[%s] Only %d high-relevance items (>=%.1f). Relaxing threshold to ensure 3.",
                    country_name, len(filtered), _RELEVANCE_THRESHOLD)
        relaxed = sorted(
            all_items,
            key=lambda x: (x.get("relevance_score", 0.0), parse_date_for_sort(x.get("published"))),
            reverse=True,
        )
        # Keep top 'max_articles', but ensure at least 3 if available
        filtered = relaxed[:max(max_articles, article_ranking.TOP_N)]

    return filtered[:max_articles]


def resolve_and_enrich(items: List[Dict], iso2: str) -> List[Dict]:
    """Resolve Google News wrappers, drop denylisted sources, and fill missing
    summaries/images with the simple (single-GET) scraper. Mutates the item
    dicts in place; returns the filtered list."""
    with requests.Session() as sess:
        # a) Replace news.google.com wrappers with publisher URLs
        for it in items:
            link = it.get("link")
            if isinstance(link, str) and "news.google.com" in link:
                it["link"] = resolve_google_news_url(link, session=sess)

        # a2) Defense-in-depth: drop denylisted sources now that links are
        #     resolved, in case a wrapper couldn't be resolved earlier.
        before = len(items)
        items = [it for it in items if not is_blocked_url(it.get("link"))]
        removed = before - len(items)
        if removed:
            logger.info("[%s] Blocked %d article(s) from denylisted sources.", iso2, removed)

        # b) Ensure summary/content and thumbnail (simple scraper, single GET)
        for it in items:
            link = it.get("link")
            if not isinstance(link, str) or not link.startswith("http"):
                continue

            cur_sum = (it.get("summary") or "").strip()
            source  = (it.get("source")  or "").strip()
            need_summary = (not cur_sum) or (len(cur_sum.split()) < 8) or (cur_sum.lower() == source.lower())
            need_image = not it.get("image")

            if need_summary or need_image:
                thumb, summary, full_text = get_article_assets(link, session=sess, max_words=160)
                if need_summary and summary:
                    it["summary"] = summary
                if full_text:
                    it["content"] = full_text[:_MAX_CONTENT_CHARS]
                if need_image and thumb:
                    it["image"] = thumb

    return items


def enrich_top_images(top_ids: List[str], items_by_id: Dict[str, Dict]) -> None:
    """Fill missing Top-3 images (and absent publish dates) via Crawlbase.

    Costs an API credit per article, so it runs only for the 3 chosen articles
    and only when the simple scraper found no image. No-op without a token."""
    cb_token = crawlbase_token()
    if not cb_token:
        return
    for iid in top_ids:
        it = items_by_id.get(iid)
        if not it:
            continue
        if it.get("image"):  # only if image is missing
            continue
        link = it.get("link") or ""
        if not isinstance(link, str) or not link.startswith("http"):
            continue

        rec = crawlbase_scrape_one(link, cb_token)
        if rec.get("error") or rec.get("skipped"):
            continue
        # Fill image if Crawlbase found one
        if rec.get("image_url"):
            it["image"] = rec["image_url"]
        # Backfill published if missing
        if (not it.get("published")) and rec.get("published_at"):
            it["published"] = rec["published_at"]
