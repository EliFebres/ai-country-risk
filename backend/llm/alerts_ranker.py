"""
Global news-alert ranking for the front-end "AI Alerts" table.

The per-country pipeline already picks each country's Top-3 articles. This module
pools those across all countries and asks the LLM to rank them **relative to each
other** by importance to the GLOBAL economy, tagging each with one fixed topic
(``ALERT_TOPICS``) and one severity label (``ALERT_SEVERITIES``). Only the global
top-N are returned for storage in the ``news_alert`` table.

Mirrors the structured-output conventions of ``calendar_ranker.rank_calendar_events``:
``ChatOpenAI(...).with_structured_output(schema, strict=True)`` driven by the
``ALERTS_RANK_PROMPT`` / ``ALERTS_RANK_SCHEMA`` defined in ``ai/constants.py``.

Graceful degradation by design: missing key, network, or schema errors log a
warning and return ``[]`` so the surrounding run is never blocked.
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from langchain_core.messages import SystemMessage

import backend.util.constants as constants
import backend.llm.constants as ai_constants
from backend.util import dates
from backend.llm import client as ai_client

logger = logging.getLogger(__name__)

# Cap how much summary text reaches the prompt (keeps the single call bounded).
_SUMMARY_CHARS = 300


def _compact(articles: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Reduce pooled articles to the minimal fields the model needs to rank them."""
    out: List[Dict[str, str]] = []
    for a in articles:
        rid = a.get("_rank_id")
        if not rid:
            continue
        summary = (a.get("summary") or "").strip()
        out.append({
            "id": rid,
            "country": (a.get("country_name") or a.get("country_iso2") or "").strip(),
            "source": (a.get("source") or "").strip(),
            "published_at": dates.date_prefix(a.get("published_at")),
            "title": (a.get("title") or "").strip(),
            "summary": summary[:_SUMMARY_CHARS],
        })
    return out


def rank_global_alerts(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rank pooled country articles by importance to the global economy.

    The whole pool is ranked in a SINGLE LLM call so the comparison is truly
    global (every article scored against every other). Each article is tagged
    with one ``ALERT_TOPICS`` topic and one ``ALERT_SEVERITIES`` severity.

    Args:
        articles: pooled article dicts, each carrying at least ``country_iso2`` /
            ``country_name`` plus ``url`` / ``title`` / ``source`` /
            ``published_at`` / ``summary`` / ``image``.

    Returns:
        The global top-``ALERTS_TOP_N`` alerts, sorted by importance (desc),
        each enriched with ``topic`` / ``severity`` / ``importance`` /
        ``rationale`` / ``global_rank`` and the originating ``country_iso2`` /
        ``country_name``. Empty list if there is nothing to score, the key is
        missing, or the call failed.
    """
    if not articles:
        return []

    top_n = constants.ALERTS_TOP_N

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not set; skipping global alert ranking.")
        return []

    # Mint pool-stable ids (per-country a1..aN collide across countries).
    pool = [a for a in articles if isinstance(a, dict) and (a.get("url") or "").strip()]
    if not pool:
        return []
    for i, a in enumerate(pool, start=1):
        a["_rank_id"] = f"g{i}"
    by_id = {a["_rank_id"]: a for a in pool}

    compact = _compact(pool)
    if not compact:
        return []

    structured_llm = ai_client.build_chat(api_key).with_structured_output(
        schema=ai_constants.ALERTS_RANK_SCHEMA, strict=True
    )

    today = datetime.now(timezone.utc).date().isoformat()
    prompt = ai_constants.ALERTS_RANK_PROMPT.format(
        today=today,
        articles_json=json.dumps(compact, ensure_ascii=False),
    )

    try:
        data = structured_llm.invoke([SystemMessage(content=prompt)])
    except Exception as exc:  # noqa: BLE001 - graceful degradation by design
        logger.warning("Global alert ranking failed (%d articles): %s", len(compact), exc)
        return []

    alerts = (data or {}).get("alerts") if isinstance(data, dict) else None
    if not isinstance(alerts, list):
        logger.warning("Global alert ranking returned no 'alerts' array: %s", str(data)[:200])
        return []

    enriched: List[Dict[str, Any]] = []
    for r in alerts:
        if not isinstance(r, dict):
            continue
        src = by_id.get(r.get("id"))
        if not src:
            continue
        importance = ai_client.parse_importance(r.get("importance"))
        if importance is None:
            continue
        topic = (r.get("topic") or "").strip()
        severity = (r.get("severity") or "").strip()
        if topic not in ai_constants.ALERT_TOPICS or severity not in ai_constants.ALERT_SEVERITIES:
            continue
        enriched.append({
            "country_iso2": src.get("country_iso2"),
            "country_name": src.get("country_name"),
            "url":          src.get("url"),
            "title":        src.get("title"),
            "source":       src.get("source"),
            "published_at": src.get("published_at"),
            "summary":      src.get("summary"),
            "image":        src.get("image"),
            "topic":        topic,
            "severity":     severity,
            "importance":   importance,
            "rationale":    (r.get("rationale") or "").strip(),
        })

    enriched.sort(key=lambda a: a["importance"], reverse=True)
    top = enriched[:top_n]
    for rank, a in enumerate(top, start=1):
        a["global_rank"] = rank

    logger.info("AI-ranked %d/%d pooled articles; kept top %d.", len(enriched), len(pool), len(top))
    return top
