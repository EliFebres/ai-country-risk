"""Country risk scoring: the LLM call and the scale boundary around it.

``country_llm_score`` sends one country's macro payload and its articles to the
model under ``ai_constants.AI_PROMPT_V2`` / ``RISK_SCHEMA_V2`` and returns both
horizons — raw as the model reported them, and gated after ``ai/policy.py``
has applied the versioned floors, caps and sanctions gate.

This module owns exactly one conversion: the prompt asks for **integers 0-100**
because that grid has the rank resolution the roster needs, and everything
downstream — policy, the database, the front-end — speaks 0-1. ``_from_100``
runs the moment the call returns, so a 0-100 number never leaves this file.

Enforcement itself is not here. It used to be: the floors lived in the prompt
and the sanctions gate overwrote the score in this function, destroying the
model's own number. Both now live in ``ai/policy.py``, which is pure and
versioned. ``_parse_iso_date``, ``_load_legal_rules_index`` and
``_legal_gate_decision`` remain importable from here as aliases onto that
module.
"""

import os
import json
import logging
from datetime import date
from typing import Any, List, Dict, Optional, Set, Tuple

from langchain_core.messages import SystemMessage

import backend.utils.ai.constants as ai_constants
from backend.utils.ai import client as ai_client
from backend.utils.ai import digest_engine
from backend.utils.ai import policy

logger = logging.getLogger(__name__)

# Prompt cap for the legacy fallback path only (stage 1 entirely down): the
# digest path sends every fetched article.
_MAX_PROMPT_ARTICLES = 10

# Matches the maxLength on RISK_SCHEMA_V2.bullet_summary.
_MAX_SUMMARY_CHARS = 800

# Per-article cap for the FULL_TEXT block: 3 articles × 12k chars ≈ 9k tokens,
# which keeps the scoring prompt bounded while covering nearly every article
# in full (trafilatura bodies are capped upstream at 24k).
_MAX_FULLTEXT_CHARS = 12_000


# -------------------------
# Helpers for prompt I/O
# -------------------------
def _legacy_entry(it: Dict) -> Dict:
    """The pre-digest prompt shape for one article: title + summary only."""
    return {
        "id": it.get("id") or "",
        "source": (it.get("source") or "").strip(),
        "published_at": (it.get("published") or "")[:10],
        "title": (it.get("title") or "").strip(),
        "summary": (it.get("summary") or it.get("text") or it.get("snippet") or "").strip(),
    }


def prompt_entries(articles: List[Dict]) -> List[Dict]:
    """The per-article dicts the scoring prompt carries, in prompt order.

    The single representation of "which articles reached the model, and as what
    text". Three consumers read it: the prompt string itself
    (``_digests_to_json``), ``prompt_article_ids``, and the provenance manifest,
    which hashes each entry — so the recorded hash is of the bytes the model
    actually saw, and the recorded id set can never drift from it.

    Every article's digest reaches the model. Items whose stage-1 digest failed
    (``digest is None``) fall back to the legacy title+summary shape, so the
    scorer still sees them, just with less depth. If stage 1 produced no digest
    at all, the whole prompt falls back to that shape and to the first
    ``_MAX_PROMPT_ARTICLES`` items — the pre-digest behavior, kept so one
    country still scores when stage 1 is down.

    Ids are taken verbatim from ``item["id"]`` (single-sourced from the
    pipeline — never re-derived by position). Non-dict entries are dropped.

    Args:
        articles: article dicts, each carrying ``id`` and (if stage 1
            succeeded) ``digest`` and ``stage1_severity``.

    Returns:
        One normalized dict per article that reaches the prompt.
    """
    items = [it for it in articles if isinstance(it, dict)]
    if not any(isinstance(it.get("digest"), dict) for it in items):
        return [_legacy_entry(it) for it in items[:_MAX_PROMPT_ARTICLES]]

    norm = []
    for it in items:
        digest = it.get("digest")
        if isinstance(digest, dict):
            norm.append({
                "id": it.get("id") or "",
                "source": (it.get("source") or "").strip(),
                "published_at": (it.get("published") or "")[:10],
                "title": (it.get("title") or "").strip(),
                "digest": digest,
                "stage1_severity": it.get("stage1_severity"),
            })
        else:
            norm.append(_legacy_entry(it))
    return norm


def prompt_article_ids(articles: List[Dict]) -> Set[str]:
    """Ids of the articles that reached the prompt, per ``prompt_entries``."""
    return {e["id"] for e in prompt_entries(articles) if e.get("id")}


def _digests_to_json(items: List[Dict]) -> str:
    """Serialize the prompt's ARTICLE_DIGESTS_JSON block."""
    return json.dumps(prompt_entries(items), ensure_ascii=False)


def _fulltext_block(items: List[Dict], fulltext_ids: List[str]) -> str:
    """Build the prompt's FULL_TEXT section for the chosen article ids.

    Args:
        items: article dicts, each carrying ``id``.
        fulltext_ids: ids (from ``digest_engine.select_fulltext_ids``) whose
            full text the scorer should read, in order.

    Returns:
        One ``--- id: a3 · <title> ---`` header + capped body per chosen id,
        or ``"(none)"`` when nothing was selected.
    """
    by_id = {it.get("id"): it for it in items if isinstance(it, dict) and it.get("id")}
    blocks = []
    for aid in fulltext_ids:
        it = by_id.get(aid)
        if not it:
            continue
        title = (it.get("title") or "").strip()
        text = digest_engine.article_input_text(it)[:_MAX_FULLTEXT_CHARS]
        blocks.append(f"--- id: {aid} · {title} ---\n{text}")
    return "\n\n".join(blocks) if blocks else "(none)"


# -------------------------
# Legal-investability gate — now owned by ai/policy.py.
# Aliased here so existing importers (and the characterization tests that pin
# this behavior) keep working against the same functions.
# -------------------------
LEGAL_RULES_PATH = policy.LEGAL_RULES_PATH
_load_legal_rules_index = policy._load_legal_rules_index
_parse_iso_date = policy._parse_iso_date
_legal_gate_decision = policy._legal_gate_decision


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


# -------------------------
# The 0-100 → 0-1 boundary
# -------------------------
def _from_100(value: Any) -> Optional[float]:
    """Convert one model-reported 0-100 integer to the 0-1 scale.

    The single place this conversion lives. Non-numeric values and None become
    None rather than raising: a malformed field on one article must not take
    down a country's scoring. Clamped, because ``strict=True`` structured
    output enforces the schema's shape but not its ``minimum``/``maximum``.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0.0, min(1.0, float(value) / 100.0))
    except (TypeError, ValueError):
        return None


def _failure_result() -> Dict[str, object]:
    """The no-score return shape, used by every failure path.

    Same keys as a successful call — the caller reads several of them
    unconditionally, so a failure must never return a dict with holes in it.
    ``score is None`` is the signal to skip the country.
    """
    return {
        "score": None,
        "bullet_summary": "",
        "subscores": {},
        "news_article_scores": [],
        "score_3m": None,
        "raw_score_12m": None,
        "raw_score_3m": None,
        "raw_subscores": {},
        "subscore_evidence": {},
        "condition_flags": {},
        "evidence_coverage": None,
        "applied_rules": [],
        "legal_gate": None,
        "model_id": ai_client.MODEL_NAME,
        "prompt_version": ai_constants.PROMPT_VERSION,
        "policy_version": policy.POLICY_VERSION,
    }


# -------------------------
# Main entry — model perception, then policy enforcement
# -------------------------
def country_llm_score(
    *,
    country_display: str,
    payload: Dict,
    articles: List[Dict],
    as_of: date,
    macro_facts: Dict,
    fulltext_ids: Optional[List[str]] = None,
) -> Dict[str, object]:
    """Score one country at both horizons: model perception, then policy.

    Args:
        country_display: country name as it should appear in the prompt.
        payload: macro evidence from
            ``data_retrieval.prepare_llm_payload_pretty``; its ``country`` key
            also drives the sanctions lookup.
        articles: recent articles, richest first, each annotated by
            ``digest_engine.digest_articles``. Every article's digest reaches
            the model; if stage 1 produced no digests at all, the prompt falls
            back to the legacy title+summary shape (first
            ``_MAX_PROMPT_ARTICLES``) so the country still scores.
        as_of: the date being scored — the same one the snapshot is keyed on
            (``data_push.payload_as_of``), not today's date. It is both the
            prompt's "treat this as today" anchor and the date policy evaluates
            the sanctions rules against, so re-running history is deterministic.
        macro_facts: ``{indicator_pretty_name: latest_value}`` from the payload
            (``data_retrieval.macro_latest_facts``). Policy reads the measured
            CPI from here rather than from the model's reading of it.
        fulltext_ids: ids whose full text the model should read (from
            ``digest_engine.select_fulltext_ids``); empty/None means no
            FULL_TEXT section.

    Returns:
        A dict whose first four keys keep their historical names, scale and
        meaning: ``score`` (the **gated 12-month** score, 0-1, what
        ``risk_snapshot.score`` stores), ``bullet_summary``, ``subscores``
        (gated, 0-1) and ``news_article_scores`` (impacts converted to 0-1,
        driving Top-3 selection). Alongside them: ``score_3m`` (gated),
        ``raw_score_12m``/``raw_score_3m``/``raw_subscores`` (exactly what the
        model said, before any floor, cap or gate), ``subscore_evidence``,
        ``condition_flags``, ``evidence_coverage``, ``applied_rules``,
        ``legal_gate`` (the triggering sanctions rule — ``{name, rule,
        sources}`` — or None), and the
        ``model_id``/``prompt_version``/``policy_version`` stamps.

        ``score`` is None if the call failed (missing key, network, or a
        malformed response) — the caller treats that as "skip this country", so
        a failure never writes a bogus number to the database.

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
    if fulltext_ids is not None and not isinstance(fulltext_ids, list):
        raise TypeError(f"`fulltext_ids` must be a list or None, got {type(fulltext_ids).__name__}")

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY not set.")
        return _failure_result()

    # Only the iso2 half: the gate is evaluated against the caller's `as_of`,
    # not today, so a backfill of an old date gets that date's rules.
    iso2 = _extract_iso2_and_asof(payload)[0]

    evidence_json = json.dumps(payload, ensure_ascii=False)
    article_digests_json = _digests_to_json(articles)
    has_digests = any(isinstance(it.get("digest"), dict) for it in articles if isinstance(it, dict))
    if has_digests or not articles:
        # No articles at all is not a stage-1 failure — it serializes to an
        # empty list and an empty FULL_TEXT block, same as it always did.
        fulltext_block = _fulltext_block(articles, fulltext_ids or [])
    else:
        # Country-level fallback: articles exist but stage 1 produced nothing
        # (stage down, no API key). `prompt_entries` has already degraded the
        # digest block to the legacy title+summary shape; there is no digest to
        # pick full-text reading from either, so skip that block — one country's
        # scoring must never die because stage 1 did.
        logger.error("[%s] no stage-1 digests for %d articles; falling back to the legacy prompt",
                     iso2 or country_display, len(articles))
        fulltext_block = "(none)"
    prompt = ai_constants.AI_PROMPT_V2.format(
        country=country_display,
        as_of_date=as_of.isoformat(),
        evidence_json=evidence_json,
        articles_json=article_digests_json,
        # A1 (the two-stage digest pipeline) fills this slot; until then the
        # block is the same one the digest path already builds.
        full_text_block=fulltext_block if fulltext_block != "(none)"
        else "(no full-text articles supplied)",
    )

    structured_llm = ai_client.build_chat(api_key).with_structured_output(
        schema=ai_constants.RISK_SCHEMA_V2, strict=True
    )

    try:
        data = structured_llm.invoke([SystemMessage(content=prompt)])
    except Exception as exc:
        logger.error("LangChain structured output error: %s", exc)
        return _failure_result()

    # Validate shape minimally
    if not isinstance(data, dict) or "score_12m" not in data or "subscores" not in data or "news_article_scores" not in data:
        logger.error("Model returned invalid structure: %s", str(data)[:300])
        return _failure_result()

    # --- Leave the 0-100 scale here and never return to it.
    raw_score_12m = _from_100(data.get("score_12m"))
    raw_score_3m = _from_100(data.get("score_3m"))
    raw_subscores = {k: _from_100(v) for k, v in (data.get("subscores") or {}).items()}
    evidence_coverage = _from_100(data.get("evidence_coverage"))
    article_scores = [
        {**a, "impact": _from_100(a.get("impact"))}
        for a in (data.get("news_article_scores") or [])
        if isinstance(a, dict)
    ]
    condition_flags = data.get("condition_flags") or {}

    # --- Enforcement: floors, caps, and the sanctions gate, from versioned code.
    enforced = policy.apply_policy(
        iso2=iso2,
        as_of=as_of,
        raw_score_12m=raw_score_12m,
        raw_score_3m=raw_score_3m,
        raw_subscores=raw_subscores,
        condition_flags=condition_flags,
        macro_facts=macro_facts if isinstance(macro_facts, dict) else {},
    )

    bullet = (data.get("bullet_summary") or "").strip()
    if enforced.gate_note:
        bullet = (enforced.gate_note + " " + bullet).strip()

    return {
        "score": enforced.score_12m,
        "bullet_summary": bullet[:_MAX_SUMMARY_CHARS],
        "subscores": enforced.subscores,
        "news_article_scores": article_scores,
        "score_3m": enforced.score_3m,
        "raw_score_12m": raw_score_12m,
        "raw_score_3m": raw_score_3m,
        "raw_subscores": raw_subscores,
        "subscore_evidence": data.get("subscore_evidence") or {},
        "condition_flags": condition_flags,
        "evidence_coverage": evidence_coverage,
        "applied_rules": enforced.applied_rules,
        "legal_gate": enforced.legal_gate,
        "model_id": ai_client.MODEL_NAME,
        "prompt_version": ai_constants.PROMPT_VERSION,
        "policy_version": policy.POLICY_VERSION,
    }
