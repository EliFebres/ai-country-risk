# backend/llm/langchain_llm.py
import os
import json
import logging
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from backend.util import env
env.load()

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

import backend.llm.constants as ai_constants
from backend.llm import validate
from backend.util import usage

logger = logging.getLogger(__name__)

# -------------------------
# The sanctions / investability rules
# -------------------------
try:
    import yaml  # PyYAML
except Exception:  # graceful degrade: the badge is simply not rendered
    yaml = None

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

# -------------------------
# The scorer
# -------------------------


def legal_badge(iso2: Optional[str], as_of: Optional[date] = None) -> Optional[Dict]:
    """Return the non-investability badge for a country, or None.

    **Observation only.** This used to overwrite the model's score with 1.0, so
    Russia's rating was a constant and carried no information: the same number
    whether the week held a mobilisation or nothing at all. The legal fact and
    the risk reading are two different statements, and collapsing them threw the
    second one away.

    The badge is now recorded beside the score and alters nothing. The prompt
    says so explicitly, so the model knows the rating is the whole of its answer.
    """
    if not iso2:
        return None
    entry = _load_legal_rules_index().get(iso2.upper())
    if not entry:
        return None
    if (entry.get("trigger") or {}).get("set_score_1_0") is not True:
        return None
    effective = _parse_iso_date(entry.get("effective_from"))
    if (as_of or date.today()) < effective:
        return None
    return {
        "name": entry.get("name") or iso2,
        "rule": entry.get("rule") or "Sanctions investability prohibition",
        "sources": entry.get("sources") or [],
        "effective_from": effective.isoformat(),
    }


def score_country(
    *,
    iso2: str,
    payload: Dict,
    article_ids: Sequence[str] = (),
    model: str = "gpt-4o-2024-08-06",
    temperature: float = 0.0,
    seed: int = 42,
    api_key: Optional[str] = None,
    meter: Optional["usage.Meter"] = None,
    structured: Any = None,
) -> Dict[str, Any]:
    """Score one country from its assembled payload.

    The prompt goes in the **system** role and the evidence in the user turn.
    Both the prompt text and the schema's shape are part of the instrument: a
    strict grammar masks every token that would make the output invalid, which
    removes most of the near-ties that make a temperature-0 call vary between
    runs.

    The decoded answer is validated against the full contract **before any
    rescale**, and the violations are returned so the census can count them.

    Returns:
        ``{"answer", "violations", "badge", "raw", "failed"}``. On a failed call
        `answer` is None and `failed` carries the reason — a country with no
        score is better than a country with a fabricated one.
    """
    if structured is None:
        api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            return {"answer": None, "violations": [], "badge": None, "raw": None,
                    "failed": "OPENAI_API_KEY not set"}
        structured = ChatOpenAI(
            model=model,
            temperature=temperature,
            seed=seed,
            max_retries=0,
            api_key=api_key,
        ).with_structured_output(
            schema=ai_constants.RISK_SCHEMA, strict=True, include_raw=True
        )

    try:
        response = structured.invoke([
            SystemMessage(content=ai_constants.RISK_PROMPT),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str)),
        ])
    except Exception as exc:
        logger.error("scoring call failed for %s: %s", iso2, exc)
        return {"answer": None, "violations": [], "badge": None, "raw": None,
                "failed": str(exc)}

    if meter is not None:
        meter.add_response(model, response)

    parsed = response.get("parsed") if isinstance(response, dict) else response
    checked = validate.validate(parsed, article_ids=article_ids)

    return {
        "answer": checked["answer"],
        "violations": checked["violations"],
        "badge": legal_badge(iso2),
        "raw": parsed,
        "failed": None,
    }
