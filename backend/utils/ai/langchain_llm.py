"""Country risk scoring: the LLM call plus the sanctions override around it.

``country_llm_score`` sends one country's macro payload and its top articles to
the model under ``ai_constants.AI_PROMPT`` / ``RISK_SCHEMA`` and returns the
calibrated 0-1 risk score and summary.

On top of the model's judgement sits a **legal-investability gate**: countries
under a US sanctions regime that makes securities exposure unlawful are forced
to 1.0 regardless of what the model says, because "risk" for a US investor who
legally cannot hold the asset is total. Those rules live in
``legal_restrictions.yaml`` (OFAC-derived, each entry dated) rather than in
code so they can be updated without a deploy. The gate runs *after* the model
call so the summary still explains the underlying situation.
"""

import os
import json
import logging
from datetime import datetime, date
from functools import lru_cache
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import yaml
from langchain_core.messages import SystemMessage

import backend.utils.ai.constants as ai_constants
from backend.utils.ai import client as ai_client

logger = logging.getLogger(__name__)

# Only the N most relevant articles reach the prompt; the rest still get stored.
_MAX_PROMPT_ARTICLES = 10

# Matches the maxLength on RISK_SCHEMA.bullet_summary.
_MAX_SUMMARY_CHARS = 800


# -------------------------
# Helpers for prompt I/O
# -------------------------
def _articles_to_json(articles: List[Dict]) -> str:
    """Serialize the articles for the prompt, keeping only the fields it uses.

    Args:
        articles: fetched article dicts, richest first.

    Returns:
        A JSON array string of at most ``_MAX_PROMPT_ARTICLES`` entries, each
        with the ``a1``-style id the model must reuse in its per-article scores.
    """
    norm = []
    for i, it in enumerate(articles[:_MAX_PROMPT_ARTICLES]):
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
LEGAL_RULES_PATH = Path(__file__).with_name("legal_restrictions.yaml")


@lru_cache(maxsize=1)
def _load_legal_rules_index() -> Dict[str, Dict]:
    """Load the sanctions rules once per process, indexed by iso2/code.

    Returns:
        ``{ISO2: entry}``. Empty if the file is unreadable or malformed — the
        failure is logged and the gate simply never fires.
    """
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
    """Parse a YAML ``effective_from`` date.

    Returns ``date.min`` for missing or unparseable values so a rule with a bad
    date is treated as already in force — the safe direction for a sanctions
    gate, where failing open would understate risk.
    """
    if not s:
        return date.min
    try:
        return datetime.fromisoformat(s[:10]).date()
    except ValueError:
        logger.warning("Unparseable effective_from %r; treating rule as always in force", s)
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
    """Decide whether the sanctions 1.0 override applies to a country.

    Args:
        iso2: country code to look up, or None (gate never fires).
        as_of: date the score is being computed for; a rule applies only from
            its ``effective_from`` onward.

    Returns:
        ``{name, rule, sources}`` describing the triggering rule, or None when
        the country is unrestricted or the rule is not yet in force.
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
) -> Dict[str, object]:
    """Score one country's 12-month investor risk, applying the sanctions gate.

    Args:
        country_display: country name as it should appear in the prompt.
        payload: macro evidence from
            ``data_retrieval.prepare_llm_payload_pretty``; its ``country`` key
            also drives the sanctions lookup.
        articles: recent articles, richest first; only the top
            ``_MAX_PROMPT_ARTICLES`` reach the model.

    Returns:
        ``{"score": float|None, "bullet_summary": str, "subscores": {...},
        "news_article_scores": [...]}``. ``score`` is 1.0 when the legal gate
        fires, else the model's calibrated score, or None if the call failed
        (missing key, network, or a malformed response) — the caller treats a
        None score as "skip this country", so a failure never writes a bogus
        number to the database. ``news_article_scores`` carries each article's
        impact and topic group, which drive Top-3 selection.

    Raises:
        TypeError: if ``payload``/``articles``/``country_display`` have the
            wrong type.
        ValueError: if ``payload`` is empty or ``country_display`` is blank.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"`payload` must be a dict, got {type(payload).__name__}")
    if not payload:
        raise ValueError("`payload` must be a non-empty dict, got an empty one")
    if not isinstance(articles, list):
        raise TypeError(f"`articles` must be a list, got {type(articles).__name__}")
    if not isinstance(country_display, str):
        raise TypeError(f"`country_display` must be a str, got {type(country_display).__name__}")
    if not country_display.strip():
        raise ValueError(f"`country_display` must be non-empty, got {country_display!r}")

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

    structured_llm = ai_client.build_chat(api_key).with_structured_output(
        schema=ai_constants.RISK_SCHEMA, strict=True
    )

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
        "bullet_summary": bullet[:_MAX_SUMMARY_CHARS],
        "subscores": data.get("subscores") or {},
        "news_article_scores": data.get("news_article_scores") or [],
    }
