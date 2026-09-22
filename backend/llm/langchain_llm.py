# backend/llm/langchain_llm.py
import os
import json
import logging
from datetime import datetime, date
from functools import lru_cache
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from backend.util import env
env.load()

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage

import backend.llm.constants as ai_constants

logger = logging.getLogger(__name__)

# -------------------------
# Optional diagnostic metric (does not affect score)
# -------------------------
def _recency_weight(days_old: int) -> float:
    if days_old <= 14: return 1.0
    if days_old <= 60: return 0.60
    return 0.30

def _compute_news_flow(articles_min: List[Dict], impact_by_id: Dict[str, float]) -> float:
    """Recency-weighted mean + small corroboration boost if >=2 severe (>=0.85) events within 30 days.
    This is purely diagnostic; it does not alter the model's score.
    """
    num = den = 0.0
    today = datetime.utcnow().date()
    severe_recent = 0

    for it in articles_min:
        _id = it.get("id")
        imp = impact_by_id.get(_id)
        if imp is None:
            continue
        published_at = (it.get("published_at") or "")[:10]
        try:
            age = (today - datetime.fromisoformat(published_at).date()).days
        except Exception:
            age = 9999
        w = _recency_weight(age)
        num += w * float(imp)
        den += w
        if imp >= 0.85 and age <= 30:
            severe_recent += 1

    news = (num / den) if den > 0 else 0.10
    if severe_recent >= 2:
        news = min(news * 1.10, 1.0)
    return float(max(0.05, min(news, 0.95)))

# -------------------------
# Helpers for prompt I/O
# -------------------------
def _articles_to_json(articles: List[Dict]) -> str:
    """Normalize article fields used in the prompt.

    Every selected article reaches the model. The slice that used to be here
    took the first ten, while the rest of the pipeline fetched, scraped and
    ranked twenty — so half the budget was paid for and never read, and articles
    eleven to twenty entered Top-3 selection with an impact of zero because the
    model had never been shown them.
    """
    norm = []
    for i, it in enumerate(articles):
        norm.append({
            "id": f"a{i+1}",
            "source": (it.get("source") or "").strip(),
            "published_at": (it.get("published") or "")[:10],
            "title": (it.get("title") or "").strip(),
            "summary": (it.get("summary") or it.get("text") or it.get("snippet") or "").strip(),
        })
    return json.dumps(norm, ensure_ascii=False)

def _articles_min_list(articles_json_str: str) -> List[Dict]:
    raw = json.loads(articles_json_str) if articles_json_str else []
    return [
        {
            "id": it.get("id"),
            "title": it.get("title", ""),
            "summary": it.get("summary", ""),
            "source": it.get("source", ""),
            "published_at": it.get("published_at", "")
        }
        for it in raw
    ]

# -------------------------
# Legal-investability gate (YAML-driven)
# -------------------------
try:
    import yaml  # PyYAML
except Exception:  # pragma: no cover
    yaml = None  # graceful degrade: gate will be inert if PyYAML missing

LEGAL_RULES_PATH = Path(__file__).with_name("legal_restrictions.yaml")

@lru_cache(maxsize=1)
def _load_legal_rules_index() -> Dict[str, Dict]:
    """Load YAML and return a dict index by iso2 OR code."""
    if yaml is None:
        logger.warning("PyYAML not installed; legal gate disabled.")
        return {}
    try:
        with open(LEGAL_RULES_PATH, "r", encoding="utf-8") as f:
            y = yaml.safe_load(f) or {}
        entries = y.get("entries") or []
        idx: Dict[str, Dict] = {}
        for e in entries:
            key = (e.get("iso2") or e.get("code") or "").upper()
            if key:
                idx[key] = e
        return idx
    except Exception as exc:
        logger.warning("Failed to load legal_restrictions.yaml: %s", exc)
        return {}

def _parse_iso_date(s: Optional[str]) -> date:
    if not s:
        return date.min
    try:
        return datetime.fromisoformat(s[:10]).date()
    except Exception:
        return date.min

def _extract_iso2_and_asof(
    country_display: str,
    payload: Dict
) -> Tuple[Optional[str], date]:
    """
    Best-effort extraction of iso2 and as_of date from payload/callsite.
    Falls back to today's date if as_of not present.
    """
    iso2 = None
    # common keys used across pipelines
    for k in ("iso2", "country_iso2", "country_code", "countryCode", "country", "country_meta"):
        v = payload.get(k)
        if isinstance(v, str) and len(v) == 2:
            iso2 = v.upper()
            break
        if isinstance(v, dict):
            cc = v.get("iso2") or v.get("code")
            if isinstance(cc, str) and len(cc) == 2:
                iso2 = cc.upper()
                break

    # as_of date
    as_of_raw = (
        payload.get("as_of")
        or payload.get("score_as_of")
        or payload.get("date")
        or payload.get("scoring_date")
    )
    as_of = _parse_iso_date(as_of_raw) if isinstance(as_of_raw, str) else date.today()

    # final fallback: no iso2 found; we could try mapping country_display -> iso2,
    # but to avoid brittle heuristics we simply return None (gate won't fire).
    return iso2, as_of

def _legal_gate_decision(iso2: Optional[str], as_of: date) -> Optional[Dict]:
    """
    Returns a dict with gate info if the 1.0 override should fire, else None.
    """
    if not iso2:
        return None
    rules = _load_legal_rules_index()
    entry = rules.get(iso2.upper())
    if not entry:
        return None

    trigger = (entry.get("trigger") or {}).get("set_score_1_0") is True
    if not trigger:
        return None

    eff = _parse_iso_date(entry.get("effective_from"))
    if as_of >= eff:
        return {
            "name": entry.get("name") or iso2,
            "rule": entry.get("rule") or "Sanctions investability prohibition",
            "sources": entry.get("sources") or []
        }
    return None

# -------------------------
# Main entry — model score with optional legal override
# -------------------------
def country_llm_score(
    *,
    country_display: str,
    payload: Dict,
    articles: List[Dict],
    llm: Optional["ChatOpenAI"] = None,
    model: str = "gpt-4o",   # any model supporting structured outputs
    temperature: float = 0.0,
    seed: int = 42,
    api_key: Optional[str] = None,
    short_circuit_if_gate: bool = False,   # leave False to keep your current behavior
) -> Dict[str, object]:
    """
    Returns:
      {
        "score": float|None,        # final score (after legal gate override)
        "bullet_summary": str,
        "subscores": {...},         # model diagnostics only
        "news_article_scores": [...],  # includes topic_group
        "news_flow": float,         # diagnostic only
      }
    """
    assert isinstance(payload, dict) and payload, "`payload` must be a non-empty dict"
    assert isinstance(articles, list), "`articles` must be a list"
    assert isinstance(country_display, str) and country_display.strip(), "`country_display` must be non-empty"

    api_key = api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY not set (env var or api_key arg).")
        return {"score": None, "bullet_summary": "", "subscores": {}, "news_flow": None, "news_article_scores": []}

    # --- Legal gate check (US-person investability)
    iso2, as_of = _extract_iso2_and_asof(country_display, payload)
    gate = _legal_gate_decision(iso2, as_of)

    # If you ever want to skip the LLM entirely for 1.0 countries, flip short_circuit_if_gate=True
    if gate and short_circuit_if_gate:
        logger.info("Legal-investability gate triggered (short-circuit): %s", gate["name"])
        bullet = (
            f"Legal-investability gate triggered for {gate['name']}: "
            f"{gate['rule']} ⇒ score forced to 1.0."
        )
        # Produce a schema-conformant minimal payload without calling the model
        subs = {
            "conflict_war": 0.20,
            "political_stability": 0.60,
            "governance_corruption": 0.55,
            "macroeconomic_volatility": 0.55,
            "regulatory_uncertainty": 0.98,  # the legal reason
        }
        return {
            "score": 1.0,
            "bullet_summary": bullet[:800],
            "subscores": subs,
            "news_article_scores": [],
            "news_flow": 0.10,
        }

    # --- Normal model path
    evidence_json = json.dumps(payload, ensure_ascii=False)
    articles_json = _articles_to_json(articles)
    prompt = ai_constants.AI_PROMPT.format(
        country=country_display,
        evidence_json=evidence_json,
        articles_json=articles_json
    )

    _llm = llm or ChatOpenAI(
        model=model,
        temperature=temperature,
        max_retries=0,
        api_key=api_key,
        seed=seed,
    )
    structured_llm = _llm.with_structured_output(schema=ai_constants.RISK_SCHEMA, strict=True)

    try:
        data = structured_llm.invoke([SystemMessage(content=prompt)])
    except Exception as exc:
        logger.error("LangChain structured output error: %s", exc)
        return {"score": None, "bullet_summary": "", "subscores": {}, "news_flow": None, "news_article_scores": []}

    # Validate shape minimally
    if not isinstance(data, dict) or "score" not in data or "subscores" not in data or "news_article_scores" not in data:
        logger.error("Model returned invalid structure: %s", str(data)[:300])
        return {"score": None, "bullet_summary": "", "subscores": {}, "news_flow": None, "news_article_scores": []}

    # Diagnostics only (does not affect score)
    try:
        impacts = {e["id"]: float(e["impact"]) for e in data.get("news_article_scores", []) if isinstance(e, dict) and "id" in e and "impact" in e}
    except Exception:
        impacts = {}
    news_flow = _compute_news_flow(_articles_min_list(articles_json), impacts)

    # --- Post-LLM legal override (default behavior)
    model_score = float(data["score"]) if isinstance(data.get("score"), (int, float, str)) else None
    bullet = (data.get("bullet_summary") or "").strip()

    if gate:
        logger.info("Legal-investability gate triggered (override): %s", gate["name"])
        note = f"Legal-investability gate triggered for {gate['name']}: {gate['rule']} ⇒ score forced to 1.0."
        bullet = (note + " " + bullet).strip()

    final_score = 1.0 if gate else model_score

    return {
        "score": final_score,
        "bullet_summary": bullet[:800],
        "subscores": data.get("subscores") or {},
        "news_article_scores": data.get("news_article_scores") or [],
        "news_flow": news_flow,
    }
