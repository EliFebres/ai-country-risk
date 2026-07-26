# backend/utils/ai/langchain_llm.py
import os
import json
import logging
from datetime import datetime, date
from functools import lru_cache
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv(), override=False)

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage

import backend.utils.ai.constants as ai_constants

logger = logging.getLogger(__name__)

# -------------------------
# Helpers for prompt I/O
# -------------------------
def _articles_to_json(articles: List[Dict]) -> str:
    """Normalize article fields used in the prompt."""
    norm = []
    for i, it in enumerate(articles[:10]):
        norm.append({
            "id": f"a{i+1}",
            "source": (it.get("source") or "").strip(),
            "published_at": (it.get("published") or "")[:10],
            "title": (it.get("title") or "").strip(),
            "summary": (it.get("summary") or it.get("text") or it.get("snippet") or "").strip(),
        })
    return json.dumps(norm, ensure_ascii=False)

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

def _extract_iso2_and_asof(payload: Dict) -> Tuple[Optional[str], date]:
    """
    Read the country's iso2 from the payload's ``country`` key (the shape
    ``data_retrieval.prepare_llm_payload_pretty`` emits). The gate is evaluated
    as of today. Returns ``(None, today)`` if no 2-letter code is present, in
    which case the gate simply won't fire.
    """
    iso2 = None
    v = payload.get("country")
    if isinstance(v, str) and len(v) == 2:
        iso2 = v.upper()
    return iso2, date.today()

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
    model: str = "gpt-4o-2024-08-06",
    seed: int = 42,
) -> Dict[str, object]:
    """
    Returns:
      {
        "score": float|None,        # final score (after legal gate override)
        "bullet_summary": str,
        "subscores": {...},         # model diagnostics only
        "news_article_scores": [...],  # includes topic_group
      }
    """
    assert isinstance(payload, dict) and payload, "`payload` must be a non-empty dict"
    assert isinstance(articles, list), "`articles` must be a list"
    assert isinstance(country_display, str) and country_display.strip(), "`country_display` must be non-empty"

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY not set.")
        return {"score": None, "bullet_summary": "", "subscores": {}, "news_article_scores": []}

    # --- Legal gate check (US-person investability); applied after the model runs
    iso2, as_of = _extract_iso2_and_asof(payload)
    gate = _legal_gate_decision(iso2, as_of)

    evidence_json = json.dumps(payload, ensure_ascii=False)
    articles_json = _articles_to_json(articles)
    prompt = ai_constants.AI_PROMPT.format(
        country=country_display,
        evidence_json=evidence_json,
        articles_json=articles_json
    )

    _llm = ChatOpenAI(
        model=model,
        temperature=0.0,
        max_retries=0,
        api_key=api_key,
        seed=seed,
    )
    structured_llm = _llm.with_structured_output(schema=ai_constants.RISK_SCHEMA, strict=True)

    try:
        data = structured_llm.invoke([SystemMessage(content=prompt)])
    except Exception as exc:
        logger.error("LangChain structured output error: %s", exc)
        return {"score": None, "bullet_summary": "", "subscores": {}, "news_article_scores": []}

    # Validate shape minimally
    if not isinstance(data, dict) or "score" not in data or "subscores" not in data or "news_article_scores" not in data:
        logger.error("Model returned invalid structure: %s", str(data)[:300])
        return {"score": None, "bullet_summary": "", "subscores": {}, "news_article_scores": []}

    # --- Post-LLM legal override
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
    }
