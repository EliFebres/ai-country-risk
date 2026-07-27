"""The daily run's phases — the only place fetch, AI, and storage meet.

Every other module under ``utils/`` owns exactly one concern: ``data_fetching``
talks to upstream APIs, ``news_fetching`` gathers and ranks articles, ``ai``
calls the model, ``data_upsert`` writes Postgres. Dependencies between them
stay one-way (only ``ai.digest_engine`` reaches down into ``data_upsert``,
for its digest cache), which is what keeps a change in one from destabilizing
the rest.

The work of a run, though, is inherently cross-cutting: "fetch the calendar,
have the model rank it, store the result" touches three of those domains. That
orchestration lives here, at the top of the dependency graph, so the layers
below stay one-way. ``main.py`` calls these four functions in order and does
nothing else.

Each phase owns its own resilience boundary: a failure is logged with a full
traceback and the run continues, so one flaky upstream or one bad country
costs a single phase rather than the whole day.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from backend.utils import constants, data_retrieval, provenance
from backend.utils.ai import alerts_ranker, calendar_ranker, digest_engine, langchain_llm
from backend.utils.ai import client as ai_client
from backend.utils.data_fetching import fmp_calendar_fetch, imf_macro_fetch
from backend.utils.data_upsert import data_push
from backend.utils.news_fetching import article_enrichment, article_ranking

logger = logging.getLogger(__name__)

# Macro payload window handed to the LLM.
_PAYLOAD_SINCE_YEAR = 2015
_PAYLOAD_LOOKBACK_YEARS = 10
_PAYLOAD_DELTA_HORIZONS = (1, 5)

# Articles fetched per country before Top-3 selection.
_MAX_ARTICLES_PER_COUNTRY = 20


def refresh_calendar() -> None:
    """Fetch the FMP economic calendar, AI-rank the near term, and upsert.

    The AI ranking is guarded separately from the upsert: a ranking failure
    still leaves the raw events stored (unranked), which the front-end renders
    fine. Any other failure is logged and swallowed so the country loop runs.
    """
    try:
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
    except Exception:
        logger.exception("[econ-calendar] ERROR")


def refresh_imf_indicators() -> None:
    """Refresh fast-moving indicators (Inflation) from the IMF into
    ``recent_indicator``.

    World Bank values are annual and lag 1-2 years; the front-end prefers this
    fresher monthly value and falls back to the WB annual one when a country
    has no IMF observation. Guarded per-country so an IMF gap or outage never
    blocks the risk loop. No-op when no indicators are configured.
    """
    if not constants.IMF_RECENT_INDICATORS:
        return

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
        since=_PAYLOAD_SINCE_YEAR,
        lookback=_PAYLOAD_LOOKBACK_YEARS,
        deltas=_PAYLOAD_DELTA_HORIZONS,
    )

    # 2) Fetch relevant news using multi-query strategy with relevance filtering
    items = article_enrichment.fetch_relevant_news(
        country_name or iso2, max_articles=_MAX_ARTICLES_PER_COUNTRY
    )

    if items:
        avg_rel = sum(it.get("relevance_score", 0) for it in items) / len(items)
        logger.info("[%s] Fetched %d articles (avg relevance: %.2f)", iso2, len(items), avg_rel)

    items = article_enrichment.resolve_and_enrich(items, iso2)

    # Assign stable ids ("a1","a2",...)
    for i, it in enumerate(items, start=1):
        it["id"] = f"a{i}"

    # 2b) Stage 1: digest every article's full text with the cheap model,
    #     keyed on the same as_of the snapshot upsert will use, then pick
    #     which articles the scorer reads in full.
    as_of = data_push.payload_as_of(payload)
    items = digest_engine.digest_articles(
        items, country_display=country_name, iso2=iso2, as_of=as_of
    )
    fulltext_ids = digest_engine.select_fulltext_ids(items)
    logger.info("[%s] full-text ids: %s", iso2, fulltext_ids)

    # 3) LLM scoring. `as_of` is the snapshot's own date, not today's: it
    #    anchors the prompt and the policy layer's sanctions lookup on the same
    #    day the row is keyed on. `macro_facts` lets policy read the measured
    #    CPI instead of the model's opinion of it.
    llm_output = langchain_llm.country_llm_score(
        country_display=country_name,
        payload=payload,
        articles=items,
        as_of=as_of,
        macro_facts=data_retrieval.macro_latest_facts(payload),
        fulltext_ids=fulltext_ids,
    )

    # 3b) Provenance: hash what the model actually saw, so this row can be
    #     reproduced (or found to be irreproducible) later. Provenance is
    #     metadata, not the product — a bug in assembling it must never cost the
    #     country its score, so it degrades to NULL and the snapshot still writes.
    try:
        input_manifest = provenance.build_input_manifest(
            items=items,
            prompt_entries=langchain_llm.prompt_entries(items),
            fulltext_ids=fulltext_ids,
            payload=payload,
            model_id=llm_output.get("model_id"),
            prompt_version=llm_output.get("prompt_version"),
            policy_version=llm_output.get("policy_version"),
            seed=ai_client.SEED,
        )
    except Exception:
        logger.exception("[%s] provenance manifest failed; writing the snapshot without it", iso2)
        input_manifest = None

    # 4) Rank and select Top-3 using AI's TOPIC CLUSTERING, with guaranteed length=3
    imp_map, topic_map = article_ranking.impact_topic_maps(llm_output)
    items_by_id = {it.get("id"): it for it in items if isinstance(it, dict) and it.get("id")}
    top_ids = article_ranking.select_top_ids(items_by_id, imp_map, topic_map, iso2)

    # 5) Enrich ONLY the Top-3 with missing images using the advanced scraper
    article_enrichment.enrich_top_images(top_ids, items_by_id)

    # 6) Build Top-3 payload AFTER enrichment
    top_articles = article_ranking.build_top_articles(top_ids, items_by_id, imp_map)

    # 6b) Add this country's Top-3 to the global alert pool (ranked after the loop)
    for a in top_articles:
        global_alert_pool.append({**a, "country_iso2": iso2, "country_name": country_name})

    # 7) Upsert to DB
    data_push.upsert_snapshot(
        {**payload, "llm_output": llm_output, "top_articles": top_articles,
         "input_manifest": input_manifest},
        country_name=country_name
    )

    logger.info("[%s] score=%s", iso2, llm_output.get("score"))
    logger.info("article_url: %s", [a["url"] for a in top_articles])
    logger.info("img_url: %s", [a["image"] for a in top_articles])


def process_all_countries() -> List[Dict]:
    """Score every country in the roster and pool their Top-3 articles.

    Returns:
        The pooled Top-3 articles across all countries, each tagged with its
        originating country, ready for ``publish_global_alerts``.

    One country's failure is logged with a traceback and skipped; the rest of
    the roster still gets scored and stored.
    """
    global_alert_pool: List[Dict] = []
    for c in constants.COUNTRY_ROSTER:
        country_name, iso2 = c["name"], c["iso2"]
        try:
            _process_country(country_name, iso2, global_alert_pool)
        except Exception:
            # Resilience boundary: one country's failure must not kill the run.
            logger.exception("[%s] ERROR", iso2)
    return global_alert_pool


def publish_global_alerts(global_alert_pool: List[Dict]) -> None:
    """Rank the pooled Top-3 articles by importance to the global economy and
    persist the top-N to ``news_alert``.

    Runs last and is fully guarded: the per-country snapshots are already
    written by this point, so a failure here must not undo them.
    """
    try:
        ranked_alerts = alerts_ranker.rank_global_alerts(global_alert_pool)
        if ranked_alerts:
            data_push.upsert_news_alerts(
                ranked_alerts, as_of=datetime.now(timezone.utc).date()
            )
            logger.info("[alerts] ranked %d/%d pooled articles, stored %d",
                        len(ranked_alerts), len(global_alert_pool), len(ranked_alerts))
        else:
            logger.info("[alerts] no alerts ranked from %d pooled articles (skipping upsert)", len(global_alert_pool))
    except Exception:
        logger.exception("[alerts] ERROR")
