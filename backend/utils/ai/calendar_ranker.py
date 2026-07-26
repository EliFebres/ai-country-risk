"""
AI importance ranking for the economic calendar (US-tilted).

Given upcoming economic-calendar events, ask the LLM to score each event's
importance to investors as a single blended value (0-1) that weighs US-market
relevance slightly above the rest of the world — the dashboard's audience is
primarily US-based — plus a one-line rationale.

Mirrors the structured-output conventions of ``langchain_llm.country_llm_score``:
``ChatOpenAI(...).with_structured_output(schema, strict=True)`` driven by the
``CAL_RANK_PROMPT`` / ``CAL_RANK_SCHEMA`` defined in ``ai/constants.py``.

Graceful degradation by design: missing key, network, or schema errors log a
warning and return ``{}`` so the surrounding calendar upsert is never blocked.
A single failed batch is skipped without losing the others.
"""

import os
import logging
from datetime import datetime, date, timezone, timedelta
from typing import Any, Dict, List, Optional

from langchain_core.messages import SystemMessage

import backend.utils.constants as constants
import backend.utils.ai.constants as ai_constants
from backend.utils import dates
from backend.utils.ai import client as ai_client

logger = logging.getLogger(__name__)

# Events per LLM call — bounds prompt/output size on busy weeks.
_BATCH_SIZE = 80


def _event_date(event_time: Any) -> Optional[date]:
    """Best-effort calendar date for week bucketing (accepts datetime or string)."""
    if isinstance(event_time, datetime):
        return event_time.date()
    if isinstance(event_time, str) and event_time[:10]:
        try:
            return date.fromisoformat(event_time[:10])
        except ValueError:
            return None
    return None


def _compact(events: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Reduce events to the minimal fields the model needs to rank them."""
    out: List[Dict[str, str]] = []
    for e in events:
        rid = e.get("_rank_id")
        if not rid:
            continue
        out.append({
            "id": rid,
            "date": dates.date_prefix(e.get("event_time")),
            "country": (e.get("country_name") or "").strip(),
            "event": (e.get("event") or "").strip(),
            "fmp_importance": (e.get("importance") or "").strip(),
        })
    return out


def _rank_batch(structured_llm, today: str, period: str, batch: List[Dict[str, str]]) -> Dict[str, Dict[str, Any]]:
    """Rank a single batch; returns {} on any error (logged)."""
    import json

    prompt = ai_constants.CAL_RANK_PROMPT.format(
        today=today,
        period=period,
        events_json=json.dumps(batch, ensure_ascii=False),
    )
    try:
        data = structured_llm.invoke([SystemMessage(content=prompt)])
    except Exception as exc:  # noqa: BLE001 - graceful degradation by design
        logger.warning("Calendar ranking batch failed (%d events): %s", len(batch), exc)
        return {}

    rankings = (data or {}).get("rankings") if isinstance(data, dict) else None
    if not isinstance(rankings, list):
        logger.warning("Calendar ranking returned no 'rankings' array: %s", str(data)[:200])
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    for r in rankings:
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        if not rid:
            continue
        importance = ai_client.parse_importance(r.get("importance"))
        if importance is None:
            continue
        out[rid] = {
            "importance": importance,
            "rationale": (r.get("rationale") or "").strip(),
        }
    return out


def rank_calendar_events(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Score each event's investor importance (US-tilted) with a one-line rationale.

    Events are bucketed into weeks of ``constants.CAL_RANK_WEEK_DAYS`` days and
    each week is ranked **relative to itself**, so a quiet week still uses the
    full 0-1 range instead of being flattened by a busier adjacent week.

    Args:
        events: event dicts, each carrying an assigned ``_rank_id`` plus
            ``event_time`` / ``country_name`` / ``event`` / ``importance``.

    Returns:
        ``{ _rank_id: {"importance": float 0..1, "rationale": str} }``.
        Empty dict if there is nothing to score, the key is missing, or every
        batch failed.
    """
    if not events:
        return {}

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not set; skipping calendar importance ranking.")
        return {}

    week_days = constants.CAL_RANK_WEEK_DAYS

    structured_llm = ai_client.build_chat(api_key).with_structured_output(
        schema=ai_constants.CAL_RANK_SCHEMA, strict=True
    )

    today = datetime.now(timezone.utc).date()

    # Bucket events into weeks measured from today (events without a parseable
    # date, or already in the past, land in week 0).
    buckets: Dict[int, List[Dict[str, Any]]] = {}
    for e in events:
        d = _event_date(e.get("event_time"))
        wk = max(0, (d - today).days // week_days) if d else 0
        buckets.setdefault(wk, []).append(e)

    scores: Dict[str, Dict[str, Any]] = {}
    total = 0
    # Rank each week independently so it sets its own high→low range.
    for wk in sorted(buckets):
        compact = _compact(buckets[wk])
        if not compact:
            continue
        total += len(compact)
        start_date = today + timedelta(days=wk * week_days)
        end_date = today + timedelta(days=(wk + 1) * week_days - 1)
        period = f"{start_date.isoformat()} to {end_date.isoformat()}"
        for start in range(0, len(compact), _BATCH_SIZE):
            batch = compact[start:start + _BATCH_SIZE]
            scores.update(_rank_batch(structured_llm, today.isoformat(), period, batch))

    logger.info("AI-ranked %d/%d calendar events across %d week(s).", len(scores), total, len(buckets))
    return scores
