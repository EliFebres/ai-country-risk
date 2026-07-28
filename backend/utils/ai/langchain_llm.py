"""Country risk scoring: the LLM call and the scale boundary around it.

``country_llm_score`` sends one country's three-ledger evidence payload and its
articles to the model under ``ai_constants.AI_PROMPT_V3`` / ``RISK_SCHEMA_V3``
and returns what the model said, at both horizons.

This module owns exactly one conversion: the prompt asks for **integers 0-100**
because that grid has the rank resolution the roster needs, and everything
downstream — the database, the front-end — speaks 0-1. ``_from_100`` runs the
moment the call returns, so a 0-100 number never leaves this file.

**Nothing here edits a score.** Two earlier designs did: first the floors lived
in the prompt, then they moved to a versioned enforcement layer that overwrote
the model's numbers after the call. Both are gone. The single assignment to
``score`` in this module is the model's own ``score_12m``, rescaled. The
sanctions lookup in ``ai/policy.py`` now returns a ``non_investable`` badge and
the rule behind it, which are stored beside the score rather than folded into
it; ``_parse_iso_date``, ``_load_legal_rules_index`` and
``_legal_gate_decision`` remain importable from here as aliases onto that
module. Contradictions between the model's flags and its scores are recorded by
``utils/lint.py`` and corrected by nobody.
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

# Matches the maxLength on RISK_SCHEMA_V3.bullet_summary.
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
# Legal investability — owned by ai/policy.py.
# Aliased here so existing importers (and the characterization tests that pin
# this behavior) keep working against the same functions.
# -------------------------
LEGAL_RULES_PATH = policy.LEGAL_RULES_PATH
_load_legal_rules_index = policy._load_legal_rules_index
_parse_iso_date = policy._parse_iso_date
_legal_gate_decision = policy._legal_gate_decision


def _extract_iso2(payload: Dict) -> Optional[str]:
    """Read the country's iso2 out of either payload shape.

    The evidence payload nests it under ``_meta.country``; the older panel
    payload has it at the top level. Both are accepted so this keeps working if
    a caller passes either.

    Returns:
        The uppercase 2-letter code, or None if the payload has no usable one —
        in which case the investability lookup simply never applies.
    """
    for candidate in (payload.get("country"), (payload.get("_meta") or {}).get("country")):
        if isinstance(candidate, str) and len(candidate) == 2:
            return candidate.upper()
    return None


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
        "ledger_scores": {},
        "news_article_scores": [],
        "score_3m": None,
        "raw_score_12m": None,
        "raw_score_3m": None,
        "subscore_evidence": {},
        "condition_flags": {},
        "evidence_coverage": None,
        "applied_rules": [],
        "legal_gate": None,
        "non_investable": False,
        "model_id": ai_client.MODEL_NAME,
        "prompt_version": ai_constants.PROMPT_VERSION,
        "policy_version": policy.POLICY_VERSION,
    }


# -------------------------
# Main entry — the model judges, and nothing here edits what it said
# -------------------------
def country_llm_score(
    *,
    country_display: str,
    payload: Dict,
    articles: List[Dict],
    as_of: date,
    fulltext_ids: Optional[List[str]] = None,
) -> Dict[str, object]:
    """Score one country at both horizons under the friction framework.

    There is no enforcement step. ``score`` is the model's ``score_12m``,
    rescaled to 0-1 and otherwise untouched; the sanctions lookup contributes a
    ``non_investable`` badge beside the score rather than a value inside it.

    Args:
        country_display: country name as it should appear in the prompt.
        payload: the three-ledger evidence from
            ``data_retrieval.build_evidence_payload``. Its ``_meta.country``
            drives the investability lookup.
        articles: recent articles, richest first, each annotated by
            ``digest_engine.digest_articles``. Every article's digest reaches
            the model; if stage 1 produced no digests at all, the prompt falls
            back to the legacy title+summary shape (first
            ``_MAX_PROMPT_ARTICLES``) so the country still scores.
        as_of: the date being scored — the same one the snapshot is keyed on
            (``data_push.payload_as_of``), not today's date. It is both the
            prompt's "treat this as today" anchor and the date the sanctions
            rules are evaluated against, so re-running history is deterministic.
        fulltext_ids: ids whose full text the model should read (from
            ``digest_engine.select_fulltext_ids``); empty/None means no
            FULL_TEXT section.

    Returns:
        A dict whose first keys keep their historical names and 0-1 scale:
        ``score`` (the 12-month score, what ``risk_snapshot.score`` stores),
        ``bullet_summary``, ``subscores`` and ``news_article_scores`` (impacts
        converted to 0-1, driving Top-3 selection). ``subscores`` now carries
        the four ledger scores under their own names rather than the old five
        sub-factors; ``ledger_scores`` is the same mapping under a clearer key.

        Alongside them: ``score_3m``, ``raw_score_12m``/``raw_score_3m`` (equal
        to the gated pair now that nothing gates them — kept so anything reading
        the raw columns keeps working across the change), ``subscore_evidence``,
        ``condition_flags``, ``evidence_coverage``, ``applied_rules``
        (observations only), ``legal_gate`` (the triggering sanctions rule —
        ``{name, rule, sources}`` — or None), ``non_investable``, and the
        ``model_id``/``prompt_version``/``policy_version`` stamps.

        ``score`` is None if the call failed (missing key, network, or a
        malformed response) — the caller treats that as "skip this country", so
        a failure never writes a bogus number to the database. A sanctioned
        country whose call failed stays failed and is **not** resurrected with a
        badge-derived number.

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

    # Evaluated against the caller's `as_of`, not today, so a backfill of an old
    # date gets that date's rules.
    iso2 = _extract_iso2(payload)

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
    prompt = ai_constants.AI_PROMPT_V3.format(
        country=country_display,
        as_of_date=as_of.isoformat(),
        evidence_json=evidence_json,
        articles_json=article_digests_json,
        full_text_block=fulltext_block if fulltext_block != "(none)"
        else "(no full-text articles supplied)",
    )

    # Client construction is inside the guard, not just the call: a malformed
    # key or a langchain init error is as much a "this country did not score" as
    # a network timeout, and letting it propagate would contradict this module's
    # promise that a failure returns the no-score shape.
    try:
        structured_llm = ai_client.build_chat(api_key).with_structured_output(
            schema=ai_constants.RISK_SCHEMA_V3, strict=True
        )
        data = structured_llm.invoke([SystemMessage(content=prompt)])
    except Exception as exc:
        logger.error("LangChain structured output error: %s", exc)
        return _failure_result()

    # Validate shape minimally
    if (not isinstance(data, dict) or "score_12m" not in data
            or "ledger_scores" not in data or "news_article_scores" not in data):
        logger.error("Model returned invalid structure: %s", str(data)[:300])
        return _failure_result()

    # --- Leave the 0-100 scale here and never return to it.
    score_12m = _from_100(data.get("score_12m"))
    score_3m = _from_100(data.get("score_3m"))
    ledger_scores = {k: _from_100(v) for k, v in (data.get("ledger_scores") or {}).items()}
    evidence_coverage = _from_100(data.get("evidence_coverage"))
    article_scores = [
        {**a, "impact": _from_100(a.get("impact"))}
        for a in (data.get("news_article_scores") or [])
        if isinstance(a, dict)
    ]
    condition_flags = data.get("condition_flags") or {}

    # --- Legal investability: a badge beside the score, never a value inside it.
    investability = policy.assess_investability(iso2=iso2, as_of=as_of)

    bullet = (data.get("bullet_summary") or "").strip()
    if investability.note:
        bullet = (investability.note + " " + bullet).strip()

    return {
        # The model's own 12-month judgement, rescaled and otherwise untouched.
        # This is the only assignment to `score` anywhere in the backend.
        "score": score_12m,
        "bullet_summary": bullet[:_MAX_SUMMARY_CHARS],
        # `subscores` keeps its name because the DB column and every existing
        # reader use it; its contents are now the four ledger scores.
        "subscores": ledger_scores,
        "ledger_scores": ledger_scores,
        "news_article_scores": article_scores,
        "score_3m": score_3m,
        # Equal to the pair above now that nothing gates them. Still written so
        # anything reading the raw columns keeps working across the change, and
        # so a p1.0 row and a p2.0 row stay column-compatible.
        "raw_score_12m": score_12m,
        "raw_score_3m": score_3m,
        "subscore_evidence": data.get("subscore_evidence") or {},
        "condition_flags": condition_flags,
        "evidence_coverage": evidence_coverage,
        "applied_rules": investability.applied_rules,
        "legal_gate": investability.legal_gate,
        "non_investable": investability.non_investable,
        "model_id": ai_client.MODEL_NAME,
        "prompt_version": ai_constants.PROMPT_VERSION,
        "policy_version": policy.POLICY_VERSION,
    }
