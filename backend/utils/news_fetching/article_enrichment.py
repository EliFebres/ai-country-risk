"""Gathering a country's articles and filling in what the feed leaves out.

Google News RSS gives titles, redirect links, and a one-line blurb. The risk
prompt and the dashboard need more than that, so this module does the network
work in three stages:

  1. ``fetch_relevant_news`` — one query per ledger the prompt scores, plus a
     broad catch-all, de-duplicated and scored. One query reliably misses whole
     categories of news, and a query set that does not match the ledgers misses
     whole ledgers.
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

from backend.utils.news_fetching import article_ranking, core, fetch_links
from backend.utils.news_fetching.advanced_scraper import crawlbase_token
from backend.utils.news_fetching.advanced_scraper import scrape_one as crawlbase_scrape_one
from backend.utils.news_fetching.simple_scraper import get_article_assets
from backend.utils.news_fetching.source_filter import is_blocked_url
from backend.utils.news_fetching.url_resolver import resolve_google_news_url

logger = logging.getLogger(__name__)

# Articles scoring below this are treated as off-topic noise.
_RELEVANCE_THRESHOLD = 0.3

# Body text stored per article; also the cap requested from the feed expander.
# Shared with the historical harvesters, so both paths hold the same size of
# evidence per article.
_MAX_CONTENT_CHARS = core.MAX_BODY_CHARS

# The query themes, the theme-floor selection, and the two dedupe keys now live
# in `news_fetching.core`, because the historical harvesters need exactly the
# same behavior and a second copy would be a silent disagreement about what
# "the 20 articles" means. These aliases keep this module's own vocabulary.
_QUERY_THEMES = core.THEME_QUERIES
_headline_key = core.headline_key
_by_relevance = core.by_relevance
_select_with_theme_floor = core.select_with_theme_floor

# 6 x 10 fetches the same ~60 raw items the old 4 x 15 did: six themes at no
# extra scrape cost, and the same <=20 articles reach the paid digest stage.
_PER_QUERY_RESULTS = 10

# Slots each theme is guaranteed in the returned list. 6 x 2 = 12 of 20; the
# remaining 8 fill by relevance.
_PER_THEME_FLOOR = 2


def fetch_relevant_news(country_name: str, max_articles: int = 20) -> List[Dict]:
    """Gather and rank recent news for one country.

    Runs one Google News query per theme in :data:`_QUERY_THEMES` — one for each
    ledger the prompt scores, plus a broad catch-all — because a single query
    reliably misses whole categories. Results are de-duplicated by publisher URL,
    scored by ``article_ranking.score_relevance``, and selected with a per-theme
    floor so no one theme can take the whole budget.

    Args:
        country_name: country to search for.
        max_articles: cap on the returned list.

    Returns:
        Up to ``max_articles`` items, most relevant first, each carrying the
        ``_theme`` that found it. If fewer than 3 clear the relevance bar, the
        bar is dropped and the best of the raw pool are used instead: the
        snapshot needs 3 articles, and thin coverage is better than an empty pane.
    """
    all_items: List[Dict] = []
    seen_urls = set()
    seen_headlines = set()

    for theme, template in _QUERY_THEMES.items():
        items = fetch_links.gnews_rss(
            query=template.format(c=country_name),
            max_results=_PER_QUERY_RESULTS,
            extract_chars=_MAX_CONTENT_CHARS,
            summary_words=240,
        )

        for item in items:
            # `core.dedupe_key` keys on the resolved publisher URL, not the
            # Google wrapper: two queries returning the same story get different
            # wrapper links, and a duplicate costs a stage-1 digest call and
            # inflates its own topic_group in the model's clustering. gnews_rss
            # resolves this for every entry already, so it is free here.
            url = core.dedupe_key(item)
            headline = _headline_key(item.get("title"))
            if not url or url in seen_urls or (headline and headline in seen_headlines):
                continue
            seen_urls.add(url)
            if headline:
                seen_headlines.add(headline)
            # Which query found it is the better evidence, so it stays primary;
            # `ensure_theme` is the content-classifier fallback and is a no-op
            # here. It runs anyway so the live path and the historical one tag
            # an untagged item by the same rule rather than two.
            item["_theme"] = theme
            all_items.append(core.ensure_theme(item))

    for item in all_items:
        item["relevance_score"] = article_ranking.score_relevance(item, country_name)

    filtered = [it for it in all_items if it.get("relevance_score", 0) >= _RELEVANCE_THRESHOLD]

    # Thin coverage: the floor is meaningless when there is nothing to ration, so
    # drop the bar and take the best of the raw pool, as before.
    if len(filtered) < article_ranking.TOP_N:
        logger.info("[%s] Only %d high-relevance items (>=%.1f). Relaxing threshold to ensure 3.",
                    country_name, len(filtered), _RELEVANCE_THRESHOLD)
        return _by_relevance(all_items)[:max(max_articles, article_ranking.TOP_N)]

    selected = _select_with_theme_floor(filtered, max_articles, _PER_THEME_FLOOR)
    logger.info("[%s] %d/%d articles kept, themes: %s", country_name, len(selected),
                len(all_items),
                ", ".join(f"{t}={sum(1 for i in selected if i.get('_theme') == t)}"
                          for t in _QUERY_THEMES))
    return selected


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
