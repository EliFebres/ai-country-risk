import os
import sys
import logging
import pathlib
import requests

from typing import List, Dict, Tuple
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

# --- Resolve project root so "backend/" is importable ------------------------
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Single .env load for the whole ETL process (modules read env at call time).
load_dotenv(PROJECT_ROOT / "backend" / ".env")
load_dotenv()  # also pick up a repo-root/cwd .env, without overriding

# --- Internal Imports -------------------------------------------
from backend.utils import constants
from backend.utils import data_retrieval
from backend.utils.dates import utc_minute_iso
from backend.utils.ai import langchain_llm
from backend.utils.ai import calendar_ranker
from backend.utils.ai import alerts_ranker
from backend.utils.data_upsert import data_push
from backend.utils.news_fetching import fetch_links
from backend.utils.data_fetching import fetch_metrics
from backend.utils.data_fetching import country_data_fetch
from backend.utils.data_fetching import fmp_calendar_fetch
from backend.utils.data_fetching import imf_macro_fetch
from backend.utils.news_fetching.url_resolver import resolve_google_news_url
from backend.utils.news_fetching.simple_scraper import get_article_assets
from backend.utils.news_fetching.source_filter import is_blocked_url
from backend.utils.news_fetching.advanced_scraper import scrape_one as crawlbase_scrape_one

# --- Paths ------------------------------------------------------------------
BACKEND_DIR    = pathlib.Path(__file__).resolve().parent
PROCESSED_DATA = BACKEND_DIR / "data" / "wb_panel_wide"

logger = logging.getLogger("main")


# --- Helpers ----------------------------------------------------------------
def _crawlbase_token() -> str:
    # Prefer JS token, then standard token
    return os.getenv("CRAWLBASE_JS_TOKEN") or os.getenv("CRAWLBASE_TOKEN") or ""

def _has_country_partition(root: pathlib.Path, iso2: str) -> bool:
    """
    Return True if a partition dir like country_code=XX exists and has at least one .parquet file.
    """
    part_dir = root / f"country_code={iso2}"
    if not part_dir.is_dir():
        return False
    try:
        return any(p.suffix == ".parquet" for p in part_dir.glob("*.parquet"))
    except Exception:
        return False

def _parse_date_for_sort(date_str: str | None):
    """Parse publication date for sorting. Returns datetime(1970-01-01) for invalid/missing dates."""
    if not date_str:
        return datetime(1970, 1, 1)
    try:
        # Try ISO (allow trailing Z)
        return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
    except Exception:
        pass
    try:
        # Try date-only
        return datetime.strptime(date_str[:10], "%Y-%m-%d")
    except Exception:
        return datetime(1970, 1, 1)

def _score_article_relevance(article: Dict, country_name: str) -> float:
    """
    Score article relevance (0-1) based on title/summary content.
    Higher = more relevant to geopolitical risk.
    """
    title = (article.get("title") or "").lower()
    summary = (article.get("summary") or article.get("snippet") or "").lower()
    text = f"{title} {summary}"
    country_lower = country_name.lower()

    # Must mention country (very small base if not, to allow backfill as absolute last resort)
    if country_lower not in text:
        return 0.1

    score = 0.3  # Base score for mentioning country

    # HIGH relevance keywords (government/policy/economy/security)
    high_keywords = [
        'government', 'ministry', 'parliament', 'president', 'prime minister',
        'central bank', 'interest rate', 'monetary policy', 'inflation', 'gdp',
        'election', 'cabinet', 'policy', 'budget', 'fiscal', 'trade',
        'military', 'defense', 'conflict', 'sanctions', 'war', 'coup', 'security'
    ]

    # MEDIUM relevance keywords
    medium_keywords = [
        'economy', 'economic', 'finance', 'currency', 'debt', 'growth',
        'minister', 'official', 'regulation', 'law', 'reform'
    ]

    # LOW relevance (noise - entertainment/sports)
    noise_keywords = [
        'sport', 'football', 'soccer', 'basketball', 'tennis', 'cricket',
        'music', 'entertainment', 'celebrity', 'festival', 'award',
        'movie', 'film', 'actor', 'singer', 'concert'
    ]

    high_count = sum(1 for kw in high_keywords if kw in text)
    medium_count = sum(1 for kw in medium_keywords if kw in text)
    noise_count = sum(1 for kw in noise_keywords if kw in text)

    score += min(high_count * 0.15, 0.5)     # Up to +0.5 for high keywords
    score += min(medium_count * 0.08, 0.2)   # Up to +0.2 for medium keywords
    score -= noise_count * 0.2               # Penalty for noise

    # Bonus for high keywords in the title
    if any(kw in title for kw in high_keywords):
        score += 0.15

    return max(0.0, min(1.0, score))

def _rank_ids_by(
    ids: List[str],
    items_by_id: Dict[str, Dict],
    impact_map: Dict[str, float],
) -> List[str]:
    """
    Rank a list of article IDs by:
      1) impact DESC
      2) published recency DESC
      3) precomputed relevance_score DESC (if present)
    """
    def key_fn(aid: str) -> Tuple[float, datetime, float]:
        it = items_by_id.get(aid, {})
        impact = float(impact_map.get(aid, 0.0))
        dt = _parse_date_for_sort(it.get("published"))
        rel = float(it.get("relevance_score", 0.0))
        return (impact, dt, rel)
    return sorted(ids, key=key_fn, reverse=True)

def _fetch_relevant_news(country_name: str, max_articles: int = 20) -> List[Dict]:
    """
    Fetch news via 4 queries:
      - Broad catch-all (country only)
      - Government/Political
      - Economic/Central Bank
      - Security/Military
    Score by relevance and return up to max_articles. If the filtered set is < 3,
    relax the threshold and fill from the broader pool to ensure >=3 when possible.
    """
    queries = [
        # NEW: Broad catch-all to maximize recall; noise is filtered by scoring
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
            extract_chars=24000,
            summary_words=240,
        )

        for item in items:
            url = item.get("link", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_items.append(item)

    # Score each article
    for item in all_items:
        item["relevance_score"] = _score_article_relevance(item, country_name)

    # High-quality filter first
    filtered = [it for it in all_items if it.get("relevance_score", 0) >= 0.3]
    filtered.sort(key=lambda x: (x.get("relevance_score", 0.0), _parse_date_for_sort(x.get("published"))), reverse=True)

    # If we have very few, relax threshold to ensure >=3 (if possible)
    if len(filtered) < 3:
        logger.info("[%s] Only %d high-relevance items (>=0.3). Relaxing threshold to ensure 3.", country_name, len(filtered))
        relaxed = sorted(
            all_items,
            key=lambda x: (x.get("relevance_score", 0.0), _parse_date_for_sort(x.get("published"))),
            reverse=True,
        )
        # Keep top 'max_articles', but ensure at least 3 if available
        filtered = relaxed[:max(max_articles, 3)]

    return filtered[:max_articles]

def ensure_missing_country_panels(root: pathlib.Path, indicators: dict) -> None:
    """
    Make sure every country in constants.COUNTRY_ROSTER has a partition under root.
    Only (re)build and write partitions that are missing or empty.
    """
    root.mkdir(parents=True, exist_ok=True)

    roster = constants.COUNTRY_ROSTER
    iso3_by_iso2 = constants.ISO3_BY_ISO2

    missing = []
    for country in roster:
        iso2 = str(country["iso2"]).strip()
        if not iso2:
            continue
        if not _has_country_partition(root, iso2):
            missing.append(iso2)

    if not missing:
        logger.info("All %d countries already have parquet partitions in %s.", len(roster), root)
        return

    logger.info("Backfilling %d missing panels → %s", len(missing), missing)
    for iso2 in missing:
        try:
            panel = fetch_metrics.build_country_panel(iso2, indicators)

            # Merge non-WB indicators (e.g. Political Corruption Index from OWID)
            panel = country_data_fetch.merge_extra_indicators(panel, iso2, iso3_by_iso2)

            if panel is None or panel.empty:
                logger.info("[%s] No rows for selected indicators — skipping write.", iso2)
                continue

            country_data_fetch.ingest_panel_wide(panel, iso2, root)
            logger.info("[%s] Wrote panel with %d years × %d indicators.", iso2, panel.shape[0], panel.shape[1])
        except Exception:
            # One country's failed backfill must not block the others.
            logger.exception("[%s] ERROR while backfilling panel", iso2)

# --- Top-3 selection ---------------------------------------------------------
def ensure_top_three(
    items_by_id: Dict[str, Dict],
    imp_map: Dict[str, float],
    topic_map: Dict[str, str] | None,
    iso2: str,
) -> List[str]:
    """Pick 3 article ids, preferring one representative per AI topic group.

    With >=3 distinct topics, the best article (by impact/recency/relevance)
    of each of the top 3 topics is chosen; with fewer topics the remainder is
    backfilled from the impact ranking; with no topic map at all the plain
    impact ranking decides. ``imp_map`` gets missing ids filled with 0.0 in
    place so later lookups stay stable. ``iso2`` is only used for log lines.
    """
    if not items_by_id:
        return []

    all_ids = list(items_by_id.keys())

    # If we have some impact scores, fill missing ones with 0.0 so ranking is stable
    if imp_map:
        for aid in all_ids:
            imp_map.setdefault(aid, 0.0)

    # Prefer topic representatives ONLY if we have >=3 distinct topics
    if topic_map:
        topics = defaultdict(list)
        for aid, tg in topic_map.items():
            if aid in items_by_id:  # ensure exists
                topics[tg].append(aid)

        topic_reps: List[Tuple[str, float, str]] = []
        for tg, ids in topics.items():
            # Best in topic by (impact, recency, relevance)
            best = _rank_ids_by(ids, items_by_id, imp_map)[0] if ids else None
            if best:
                topic_reps.append((best, imp_map.get(best, 0.0), tg))

        topic_reps.sort(key=lambda t: t[1], reverse=True)
        distinct_topic_count = len(topics)

        if distinct_topic_count >= 3:
            top_ids = [aid for aid, _, _ in topic_reps[:3]]
            logger.info("[%s] AI identified %d topics (used 1/article).", iso2, distinct_topic_count)
            return top_ids

        # If topics <=2, still use the best representative(s) then fill to 3
        chosen = [aid for aid, _, _ in topic_reps[:3]]
        remaining = [aid for aid in all_ids if aid not in chosen]
        ranked_remaining = _rank_ids_by(remaining, items_by_id, imp_map)
        needed = 3 - len(chosen)
        chosen += ranked_remaining[:max(0, needed)]
        logger.info("[%s] Only %d topic(s). Backfilled to 3 with best remaining.", iso2, distinct_topic_count)
        return chosen[:3]

    # No topic map at all → fall back to global ranking by impact/recency/relevance
    ranked = _rank_ids_by(all_ids, items_by_id, imp_map)
    return ranked[:3]


def _impact_topic_maps(llm_output: Dict) -> Tuple[Dict[str, float], Dict[str, str]]:
    """Extract per-article impact and topic-group maps from the LLM output."""
    try:
        article_scores = llm_output.get("news_article_scores") or []
        imp_map: Dict[str, float] = {}
        topic_map: Dict[str, str] = {}  # article_id -> topic_group

        for e in article_scores:
            if not isinstance(e, dict):
                continue
            aid = e.get("id", "")
            if not aid:
                continue
            try:
                imp_map[aid] = float(e.get("impact", 0.0))
            except (ValueError, TypeError):
                imp_map[aid] = 0.0
            topic_map[aid] = e.get("topic_group", "unknown")
        return imp_map, topic_map
    except Exception:
        return {}, {}


def _select_top_ids(
    items_by_id: Dict[str, Dict],
    imp_map: Dict[str, float],
    topic_map: Dict[str, str],
    iso2: str,
) -> List[str]:
    """Top-3 ids via topic clustering when the LLM scored impacts, else via
    the pre-LLM relevance+recency ranking."""
    if imp_map:
        return ensure_top_three(items_by_id, imp_map, topic_map or {}, iso2)

    # No impact from LLM (edge), fall back to relevance+recency from fetch stage
    ranked_ids = sorted(
        items_by_id.keys(),
        key=lambda iid: (
            items_by_id[iid].get("relevance_score", 0.0),
            _parse_date_for_sort(items_by_id[iid].get("published")),
        ),
        reverse=True,
    )
    logger.info("[%s] No LLM impacts. Used relevance+recency fallback.", iso2)
    return ranked_ids[:3]


# --- Per-country pipeline steps ----------------------------------------------
def _resolve_and_enrich(items: List[Dict], iso2: str) -> List[Dict]:
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
                    it["content"] = full_text[:24000]
                if need_image and thumb:
                    it["image"] = thumb

    return items


def _enrich_top_images(top_ids: List[str], items_by_id: Dict[str, Dict]) -> None:
    """Fill missing Top-3 images (and absent publish dates) via Crawlbase.

    Costs an API credit per article, so it runs only for the 3 chosen articles
    and only when the simple scraper found no image. No-op without a token."""
    cb_token = _crawlbase_token()
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


def _build_top_articles(
    top_ids: List[str],
    items_by_id: Dict[str, Dict],
    imp_map: Dict[str, float],
) -> List[Dict]:
    """Assemble the ranked Top-3 payload rows written to risk_snapshot_article."""
    top_articles = []
    for r, iid in enumerate(top_ids, start=1):
        it = items_by_id.get(iid, {})
        try:
            impact = float(imp_map.get(iid, 0.0))
        except Exception:
            impact = None

        top_articles.append({
            "rank": r,
            "id": it.get("id"),
            "url": it.get("link") or "",
            "title": it.get("title") or "",
            "source": it.get("source") or "",
            "published_at": it.get("published") or None,
            "impact": float(impact) if impact is not None else None,
            "summary": it.get("summary") or it.get("snippet") or "",
            "image": it.get("image"),
        })
    return top_articles


def _process_country(country_name: str, iso2: str, global_alert_pool: List[Dict]) -> None:
    """Run the full pipeline for one country: macro payload → news → LLM score
    → Top-3 selection/enrichment → DB upsert. Appends the country's Top-3 to
    ``global_alert_pool`` for the post-loop global alert ranking."""
    # 1) Macro payload (pretty, JSON-serializable). ALL_INDICATORS adds
    #    the merged non-WB indicators (Political Corruption Index) so they
    #    reach both the LLM payload and the DB upsert.
    payload = data_retrieval.prepare_llm_payload_pretty(
        country_iso=iso2,
        indicators=constants.ALL_INDICATORS,
        since=2015,
        lookback=10,
        deltas=(1, 5),
    )

    # 2) Fetch relevant news using multi-query strategy with relevance filtering (+ BROAD query)
    items = _fetch_relevant_news(country_name or iso2, max_articles=20)

    if items:
        avg_rel = sum(it.get("relevance_score", 0) for it in items) / len(items)
        logger.info("[%s] Fetched %d articles (avg relevance: %.2f)", iso2, len(items), avg_rel)

    items = _resolve_and_enrich(items, iso2)

    # Assign stable ids ("a1","a2",...)
    for i, it in enumerate(items, start=1):
        it["id"] = f"a{i}"

    # 3) LLM scoring
    llm_output = langchain_llm.country_llm_score(
        country_display=country_name,
        payload=payload,
        articles=items,
    )

    # 4) Rank and select Top-3 using AI's TOPIC CLUSTERING, with guaranteed length=3
    imp_map, topic_map = _impact_topic_maps(llm_output)
    items_by_id = {it.get("id"): it for it in items if isinstance(it, dict) and it.get("id")}
    top_ids = _select_top_ids(items_by_id, imp_map, topic_map, iso2)

    # 5) Enrich ONLY the Top-3 with missing images using the advanced scraper
    _enrich_top_images(top_ids, items_by_id)

    # 6) Build Top-3 payload AFTER enrichment
    top_articles = _build_top_articles(top_ids, items_by_id, imp_map)

    # 6b) Add this country's Top-3 to the global alert pool (ranked after the loop)
    for a in top_articles:
        global_alert_pool.append({**a, "country_iso2": iso2, "country_name": country_name})

    # 7) Upsert to DB
    data_push.upsert_snapshot(
        {**payload, "llm_output": llm_output, "top_articles": top_articles},
        country_name=country_name
    )

    logger.info("[%s] score=%s", iso2, llm_output.get("score"))
    logger.info("article_url: %s", [a["url"] for a in top_articles])
    logger.info("img_url: %s", [a["image"] for a in top_articles])


# --- Run phases --------------------------------------------------------------
def _refresh_calendar() -> None:
    """Fetch the FMP economic calendar, AI-rank the next-14-day subset, and
    upsert. The AI ranking is guarded separately so its failure never blocks
    the upsert of the raw events."""
    events = fmp_calendar_fetch.fetch_economic_calendar()
    if not events:
        logger.info("[econ-calendar] no events fetched (skipping upsert)")
        return

    # AI-rank the next-14-day subset by importance to investors (US-tilted).
    cutoff = datetime.now(timezone.utc) + timedelta(days=constants.CAL_RANK_HORIZON_DAYS)
    subset = [ev for ev in events if ev["event_time"] <= cutoff]
    for i, ev in enumerate(subset, start=1):
        ev["_rank_id"] = f"e{i}"
    try:
        scores = calendar_ranker.rank_calendar_events(subset)
        scored_at = datetime.now(timezone.utc)
        for ev in subset:
            s = scores.get(ev.get("_rank_id"))
            if s:
                ev["ai_importance"] = s.get("importance")
                ev["ai_rationale"]  = s.get("rationale")
                ev["ai_scored_at"]  = scored_at
        logger.info("[econ-calendar] AI-ranked %d/%d next-14d events", len(scores), len(subset))
    except Exception:
        logger.exception("[econ-calendar] ranking ERROR")

    data_push.upsert_economic_events(events)
    logger.info("[econ-calendar] upserted %d events", len(events))


def _refresh_imf_indicators() -> None:
    """Refresh fast-moving indicators (Inflation) from the IMF at monthly
    frequency into `recent_indicator`. World Bank values are annual and lag
    1–2 years; the front-end prefers this fresher value and falls back to the
    WB annual one when a country has no IMF observation. Guarded per-country
    so an IMF gap or outage never blocks the risk loop."""
    refreshed = 0
    for c in constants.COUNTRY_ROSTER:
        try:
            recent = imf_macro_fetch.fetch_recent_indicators(c["iso3"])
            if recent:
                data_push.upsert_recent_indicators(c["iso2"], recent)
                refreshed += 1
        except Exception:
            logger.exception("[imf-refresh] %s ERROR", c["iso2"])
    logger.info("[imf-refresh] refreshed %d/%d countries", refreshed, len(constants.COUNTRY_ROSTER))


def _push_global_alerts(global_alert_pool: List[Dict]) -> None:
    """Rank the pooled Top-3 articles by importance to the global economy and
    persist the top-N to ``news_alert``."""
    ranked_alerts = alerts_ranker.rank_global_alerts(global_alert_pool)
    if ranked_alerts:
        data_push.upsert_news_alerts(
            ranked_alerts, as_of=datetime.now(timezone.utc).date()
        )
        logger.info("[alerts] ranked %d/%d pooled articles, stored %d",
                    len(ranked_alerts), len(global_alert_pool), len(ranked_alerts))
    else:
        logger.info("[alerts] no alerts ranked from %d pooled articles (skipping upsert)", len(global_alert_pool))


# --- Main -------------------------------------------------------------------
def main() -> None:
    """Daily ETL: backfill panels, refresh calendar + IMF indicators, score
    every rostered country, then rank and store the global news alerts.

    Every phase and every country is wrapped in its own resilience boundary:
    a failure is logged with a full traceback and the run moves on, so one
    bad country or one flaky upstream never aborts the whole day."""
    logger.info("=== AI Country Risk run started at %s UTC ===", utc_minute_iso(datetime.now(timezone.utc)))

    # 0) Ensure/Backfill panels per country (incremental, idempotent)
    ensure_missing_country_panels(root=PROCESSED_DATA, indicators=constants.INDICATORS)

    # 0b) Economic calendar (FMP) for the front-end Econ Calendar pane.
    try:
        _refresh_calendar()
    except Exception:
        logger.exception("[econ-calendar] ERROR")

    # 0c) Fresher-than-annual indicator values from the IMF.
    if constants.IMF_RECENT_INDICATORS:
        _refresh_imf_indicators()

    # 1-7) Per-country: payload → news → LLM score → Top-3 → DB.
    global_alert_pool: List[Dict] = []
    for c in constants.COUNTRY_ROSTER:
        country_name, iso2 = c["name"], c["iso2"]
        try:
            _process_country(country_name, iso2, global_alert_pool)
        except Exception:
            # Resilience boundary: one country's failure must not kill the run.
            logger.exception("[%s] ERROR", iso2)

    # 8) Global news alerts from the pooled Top-3s.
    try:
        _push_global_alerts(global_alert_pool)
    except Exception:
        logger.exception("[alerts] ERROR")

    logger.info("=== Run finished at %s UTC ===", utc_minute_iso(datetime.now(timezone.utc)))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [etl] %(levelname)s %(message)s",
    )
    main()
