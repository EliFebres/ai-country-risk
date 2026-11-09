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


def structured_call(
    *,
    system: str,
    user: str,
    schema: Dict,
    model: str,
    meter: Optional["usage.Meter"] = None,
    seed: int = 42,
    temperature: float = 0.0,
    api_key: Optional[str] = None,
    structured: Any = None,
) -> Dict[str, Any]:
    """One strict structured-output call, metered. The outlook's model call.

    The same client shape as `score_country` (system prompt, one user turn of
    JSON, `strict=True`, `include_raw=True` so the meter can see usage) without
    the scorer's validation, which belongs to the caller. `score_country` is
    left as it is: the scorer is a frozen instrument.

    Returns:
        ``{"parsed", "raw", "failed"}``. On a failed call or an undecodable
        answer `parsed` is None and `failed` says why.
    """
    if structured is None:
        api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            return {"parsed": None, "raw": None, "failed": "OPENAI_API_KEY not set"}
        structured = ChatOpenAI(
            model=model, temperature=temperature, seed=seed, max_retries=0, api_key=api_key,
        ).with_structured_output(schema=schema, strict=True, include_raw=True)

    try:
        response = structured.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    except Exception as exc:
        logger.error("structured call failed: %s", exc)
        return {"parsed": None, "raw": None, "failed": str(exc)}

    if meter is not None:
        meter.add_response(model, response)

    parsed = response.get("parsed") if isinstance(response, dict) else response
    failed = None
    if parsed is None:
        err = response.get("parsing_error") if isinstance(response, dict) else None
        failed = f"answer did not decode: {err}" if err else "answer did not decode"
    return {"parsed": parsed, "raw": response, "failed": failed}


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
    run_date: Optional[date] = None,
    country_label: Optional[str] = None,
    masked: bool = False,
) -> Dict[str, Any]:
    """Ask the checklist about one country from its assembled payload.

    The prompt goes in the **system** role, with the country's label and the
    run date in it, and the evidence in the user turn. The model answers
    sixteen questions with a level and its evidence; it is never asked for a
    score. Both the prompt text and the schema's shape are part of the
    instrument: a strict grammar masks every token that would make the output
    invalid.

    `country_label` is what the prompt calls the country: its name for a named
    run, `MASKED_COUNTRY_LABEL` for a masked one (the default when the payload
    carries no `country` block). With `masked=True` the payload is checked
    against the gazetteer immediately before it is serialized and sent, and a
    surviving term raises `gazetteer.MaskLeak` rather than being sent.

    The decoded answer is validated against the full contract (evidence ids,
    quotes, event dates, headline-only evidence) before `combine` does the
    arithmetic, and the violations and counts are returned so the census can
    count them.

    Returns:
        ``{"answer", "violations", "counts", "badge", "raw", "failed"}``. On a
        failed call `answer` is None and `failed` carries the reason — a
        country with no score is better than a country with a fabricated one.
    """
    if country_label is None:
        country_label = ((payload.get("country") or {}).get("name")
                         or ai_constants.MASKED_COUNTRY_LABEL)
    as_of = run_date or payload.get("as_of")
    if not as_of:
        raise ValueError("score_country needs run_date or payload[\"as_of\"]: the prompt tells the model what today is")
    if masked:
        from backend.util import gazetteer
        gazetteer.assert_clean(iso2, payload)
    if structured is None:
        api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            return {"answer": None, "violations": [], "counts": {}, "badge": None,
                    "raw": None, "failed": "OPENAI_API_KEY not set"}
        structured = ChatOpenAI(
            model=model,
            temperature=temperature,
            seed=seed,
            max_retries=0,
            api_key=api_key,
        ).with_structured_output(
            # Every question is a required key, so an answer that skips one
            # cannot be decoded.
            schema=ai_constants.build_checklist_schema(article_ids), strict=True,
            include_raw=True
        )

    try:
        response = structured.invoke([
            SystemMessage(content=ai_constants.prompt_for(country_label, as_of)),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str)),
        ])
    except Exception as exc:
        logger.error("scoring call failed for %s: %s", iso2, exc)
        return {"answer": None, "violations": [], "counts": {}, "badge": None,
                "raw": None, "failed": str(exc)}

    if meter is not None:
        meter.add_response(model, response)

    parsed = response.get("parsed") if isinstance(response, dict) else response
    checked = validate.validate(parsed, article_ids=article_ids, run_date=run_date,
                                texts=validate.article_texts(payload),
                                title_only=validate.title_only_ids(payload))

    return {
        "answer": checked["answer"],
        "violations": checked["violations"],
        "counts": checked["counts"],
        "badge": legal_badge(iso2),
        "raw": parsed,
        "failed": None,
    }
