"""Scoring and selection of a country's articles — pure logic, no network.

Two rounds of ranking happen per country. Before the LLM runs,
``score_relevance`` cheaply filters out the sport and entertainment that a bare
country query drags in, so tokens aren't spent having the model reject them.
After the LLM runs, ``select_top_ids`` picks the 3 articles that reach the
dashboard, preferring one representative per distinct topic so the Top-3 covers
three stories rather than three write-ups of the same story.

Everything here is a pure function over dicts, which is why it is the most
heavily tested module in the backend.
"""

import logging
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Tuple

from backend.util.dates import parse_date_for_sort

logger = logging.getLogger(__name__)

# HIGH relevance keywords, grouped by the ledger they speak to. The grouping is
# documentation only — the scorer treats them as one flat list — but it is what
# keeps this list and `article_enrichment._QUERY_THEMES` from drifting apart. An
# article a theme query retrieved but this list does not recognise scores at the
# 0.3 base and loses every tiebreak, which would leave the per-theme floor and
# the open fill disagreeing about what matters.
#
# ponytail: naive substring matching, so 'tax' also matches 'taxi' and 'audit'
# matches 'auditorium'. Upgrade to word-boundary regex if false positives ever
# show up in practice; for a pre-LLM triage heuristic they cost one wasted slot.
_HIGH_KEYWORDS = [
    # order / government
    'government', 'ministry', 'parliament', 'president', 'prime minister',
    'election', 'cabinet', 'policy', 'central bank', 'interest rate',
    'monetary policy', 'inflation', 'gdp',
    # friction — what is taken, and how well it converts
    'tax', 'customs', 'permit', 'bureaucracy', 'corruption', 'court',
    'budget', 'fiscal', 'trade',
    # information — can the country's own instruments be trusted
    'judiciary', 'press freedom', 'journalist', 'censorship', 'audit',
    'statistics',
    # edge — is the system still learning. 'university' is deliberately NOT here
    # despite being in the edge query: it retrieves well but scores badly, and
    # promoted a university orienteering championship over real evidence in
    # testing. The query is ANDed with the country name; this list is not.
    'startup', 'entrepreneur', 'emigration', 'brain drain',
    # security
    'military', 'defense', 'conflict', 'sanctions', 'war', 'coup', 'security'
]

# MEDIUM relevance keywords
_MEDIUM_KEYWORDS = [
    'economy', 'economic', 'finance', 'currency', 'debt', 'growth',
    'minister', 'official', 'regulation', 'law', 'reform'
]

# LOW relevance (noise - entertainment/sports)
_NOISE_KEYWORDS = [
    'sport', 'football', 'soccer', 'basketball', 'tennis', 'cricket',
    'music', 'entertainment', 'celebrity', 'festival', 'award',
    'movie', 'film', 'actor', 'singer', 'concert'
]

# Ceiling for an article that names the country only in its body, never its
# title. Above the 0.3 relevance threshold, so nothing is discarded, and below
# the weakest score a genuinely-about-the-country story reaches in practice, so
# incidental mentions sort underneath them.
_BODY_MENTION_CAP = 0.55

# How many articles reach the dashboard per country. The DB enforces this too
# (risk_snapshot_article.rank has a BETWEEN 1 AND 3 constraint).
TOP_N = 3


def score_relevance(article: Dict, country_name: str) -> float:
    """Score how likely an article is to be about this country's risk (0-1).

    A cheap keyword heuristic that runs before the LLM sees anything: Google
    News returns plenty of sport and entertainment for a bare country query,
    and paying for tokens to have the model reject those is wasteful.

    Args:
        article: item with ``title`` and ``summary``/``snippet``.
        country_name: the country the query was for.

    Returns:
        0-1. Articles that never name the country floor at 0.1 rather than 0,
        so they remain available as last-resort backfill when a country has
        almost no coverage.
    """
    title = (article.get("title") or "").lower()
    summary = (article.get("summary") or article.get("snippet") or "").lower()
    text = f"{title} {summary}"
    country_lower = country_name.lower()

    # Must mention country (very small base if not, to allow backfill as absolute last resort)
    if country_lower not in text:
        return 0.1

    score = 0.3  # Base score for mentioning country

    high_count = sum(1 for kw in _HIGH_KEYWORDS if kw in text)
    medium_count = sum(1 for kw in _MEDIUM_KEYWORDS if kw in text)
    noise_count = sum(1 for kw in _NOISE_KEYWORDS if kw in text)

    score += min(high_count * 0.15, 0.5)     # Up to +0.5 for high keywords
    score += min(medium_count * 0.08, 0.2)   # Up to +0.2 for medium keywords
    score -= noise_count * 0.2               # Penalty for noise

    # Bonus for high keywords in the title
    if any(kw in title for kw in _HIGH_KEYWORDS):
        score += 0.15

    # A country named only in the body is usually the venue, not the subject.
    # The keyword counts measure "is this risk-relevant news", not "is this news
    # about THIS country", so a dense policy story that merely happens in the
    # country outranks the country's own news: five US Federal Reserve stories
    # scored a perfect 1.00 for Portugal because the ECB forum meets in Sintra.
    # Capping below the weakest titled story restores the ordering without
    # discarding anything — these stay available as backfill for a country whose
    # own coverage is thin, which is the same reason the 0.1 floor exists.
    if country_lower not in title:
        score = min(score, _BODY_MENTION_CAP)

    return max(0.0, min(1.0, score))


def rank_ids_by(
    ids: List[str],
    items_by_id: Dict[str, Dict],
    impact_map: Dict[str, float],
) -> List[str]:
    """Rank article ids by impact, then recency, then pre-LLM relevance.

    Args:
        ids: article ids to order.
        items_by_id: id -> article dict (for date and relevance).
        impact_map: id -> LLM impact score; missing ids count as 0.0.

    Returns:
        The ids, most significant first.
    """
    def key_fn(aid: str) -> Tuple[float, datetime, float]:
        """Sort key: (impact, recency, relevance), all descending via reverse."""
        it = items_by_id.get(aid, {})
        impact = float(impact_map.get(aid, 0.0))
        dt = parse_date_for_sort(it.get("published"))
        rel = float(it.get("relevance_score", 0.0))
        return (impact, dt, rel)
    return sorted(ids, key=key_fn, reverse=True)


def impact_topic_maps(llm_output: Dict) -> Tuple[Dict[str, float], Dict[str, str]]:
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
            best = rank_ids_by(ids, items_by_id, imp_map)[0] if ids else None
            if best:
                topic_reps.append((best, imp_map.get(best, 0.0), tg))

        topic_reps.sort(key=lambda t: t[1], reverse=True)
        distinct_topic_count = len(topics)

        if distinct_topic_count >= TOP_N:
            top_ids = [aid for aid, _, _ in topic_reps[:TOP_N]]
            logger.info("[%s] AI identified %d topics (used 1/article).", iso2, distinct_topic_count)
            return top_ids

        # If topics <=2, still use the best representative(s) then fill to 3
        chosen = [aid for aid, _, _ in topic_reps[:TOP_N]]
        remaining = [aid for aid in all_ids if aid not in chosen]
        ranked_remaining = rank_ids_by(remaining, items_by_id, imp_map)
        needed = TOP_N - len(chosen)
        chosen += ranked_remaining[:max(0, needed)]
        logger.info("[%s] Only %d topic(s). Backfilled to 3 with best remaining.", iso2, distinct_topic_count)
        return chosen[:TOP_N]

    # No topic map at all → fall back to global ranking by impact/recency/relevance
    ranked = rank_ids_by(all_ids, items_by_id, imp_map)
    return ranked[:TOP_N]


def select_top_ids(
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
            parse_date_for_sort(items_by_id[iid].get("published")),
        ),
        reverse=True,
    )
    logger.info("[%s] No LLM impacts. Used relevance+recency fallback.", iso2)
    return ranked_ids[:TOP_N]


def build_top_articles(
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
