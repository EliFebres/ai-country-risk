"""Asking a model to name the country it was not told.

The masked run's entire claim is that the scorer judged a country it could not
identify. This measures whether that is true, on the same bundles the scorer
saw, with the cheap model.

What the number means needs stating before it is read, because the obvious
reading is wrong. A low guess rate does not prove masking works; it proves
masking worked *on this corpus*. And a high guess rate is not automatically a
failure — the United States is going to be identified nearly every time, from
the size of the numbers, the institutions, the sheer volume of coverage, and
there is no gazetteer that fixes that. That is why it is in the roster: it
calibrates the ceiling. The meter to read is the *spread* between the US and
the rest, not any single country's rate.

The probe never sees the named bundle, so it cannot be scored against its own
answer key by accident.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from backend.utils.ai import client as ai_client
from backend.utils.history import config

logger = logging.getLogger(__name__)

_PROBE_SCHEMA = {
    # Same omission as the rewrite schema had, and worse here: the probe fails
    # *open*, recording a failed call as "no guess", so a nameless schema would
    # have reported perfect masking on every bundle it never actually read.
    "title": "CountryGuess",
    "type": "object",
    "properties": {
        "country": {
            "type": "string",
            "description": "ISO 3166-1 alpha-2 code of the most likely country, "
                           "or 'ZZ' if there is genuinely no way to tell.",
        },
        "confidence": {
            "type": "number",
            "description": "0.0 to 1.0.",
        },
        "evidence": {
            "type": "string",
            "description": "The specific detail that gave it away, or why it is untellable.",
        },
    },
    "required": ["country", "confidence", "evidence"],
    "additionalProperties": False,
}

_PROBE_PROMPT = """\
The news summaries below have had country names, demonyms, currencies, cities \
and institutions removed. Identify which country they are about.

Guess even when unsure — an honest low confidence is more useful than a \
refusal. Answer 'ZZ' only if there is genuinely nothing to go on.

Say what gave it away: the specific number, institution, event or phrasing.

{bundle}
"""

# How much of each article the probe reads. It is measuring identifiability of
# the *evidence*, so it gets what the scorer got, capped so a single long
# article cannot dominate the bundle.
_PER_ARTICLE_CHARS = 1200


def bundle_text(items: List[Dict[str, Any]],
                fulltext_ids: Optional[List[str]] = None) -> str:
    """Exactly what the scoring prompt carries, as one block for the probe.

    This has to mirror the prompt or the meter is measuring the wrong thing, and
    the first version did not. It read ``item["text"]`` for all twenty articles,
    but the scorer never sees twenty bodies: ``prompt_entries`` hands it a title
    and a *digest* per article, and full text for only the two or three ids in
    ``fulltext_ids``. Probing the raw bodies measures a bundle strictly more
    identifiable than the one that gets sent, and would have reported the
    instrument as leakier than it is — forever, and in the direction that looks
    like diligence.

    So the entries come from ``prompt_entries`` rather than from a second
    reading of the items. URLs are excluded there already, which matters: a path
    like "/2018/aug/13/turkey-lira-crisis" would hand over the answer, and it is
    never sent.

    Args:
        items: the masked articles, after stage-1 digesting.
        fulltext_ids: the ids whose full text the prompt carries. Omitted, no
            article contributes a body — the conservative reading, and the right
            one when the caller does not know.
    """
    # Imported here rather than at module scope: `langchain_llm` imports this
    # module's neighbours, and a top-level import closes the cycle.
    from backend.utils.ai import langchain_llm

    chosen = set(fulltext_ids or ())
    parts = []
    for i, entry in enumerate(langchain_llm.prompt_entries(items), start=1):
        digest = entry.get("digest")
        body = json.dumps(digest, ensure_ascii=False) if isinstance(digest, dict) else (
            entry.get("summary") or "")
        if entry.get("id") in chosen:
            item = next((it for it in items if it.get("id") == entry.get("id")), {})
            body = f"{body}\n{item.get('text') or ''}"
        parts.append(f"[{i}] {entry.get('title') or ''}\n{body[:_PER_ARTICLE_CHARS]}".strip())
    return "\n\n".join(parts)


def probe(items: List[Dict[str, Any]], api_key: str,
          model_chat: Optional[Any] = None,
          fulltext_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Ask which country a masked bundle is about.

    Returns:
        ``{'country', 'confidence', 'evidence'}``. A failed call returns 'ZZ'
        at confidence 0.0 with the error as evidence — the opposite of the
        leakage scan's fail-closed, and deliberately: this is a measurement,
        not a gate, and a failed measurement must not be recorded as a
        successful identification.
    """
    if not items:
        return {"country": "ZZ", "confidence": 0.0, "evidence": "empty bundle"}
    try:
        chat = model_chat or ai_client.build_digest_chat(api_key)
        result = chat.with_structured_output(
            schema=_PROBE_SCHEMA, strict=True).invoke(
                _PROBE_PROMPT.format(bundle=bundle_text(items, fulltext_ids)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("probe failed (%s); recorded as no-guess", exc)
        return {"country": "ZZ", "confidence": 0.0, "evidence": f"probe failed: {exc}"}
    if not isinstance(result, dict):
        return {"country": "ZZ", "confidence": 0.0, "evidence": "probe returned no object"}
    return {
        "country": str(result.get("country") or "ZZ").upper()[:2],
        "confidence": float(result.get("confidence") or 0.0),
        "evidence": str(result.get("evidence") or ""),
    }


def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Guess rates per country, and the spread that is the actual meter.

    Args:
        results: dicts with ``country_iso2`` (the truth) and ``guess`` (a
            :func:`probe` result).

    Returns:
        Per-country hit rates plus ``spread``, the gap between the most and
        least identifiable country. The US is expected at the top; if every
        country sits up there with it, masking is not working, and if the
        spread is wide then masking works everywhere except where coverage
        volume gives it away.
    """
    per: Dict[str, Dict[str, Any]] = {}
    for row in results:
        truth = row["country_iso2"]
        stats = per.setdefault(truth, {"n": 0, "hits": 0, "confidence": 0.0})
        stats["n"] += 1
        stats["hits"] += int(row["guess"].get("country") == truth)
        stats["confidence"] += float(row["guess"].get("confidence") or 0.0)

    for stats in per.values():
        stats["rate"] = stats["hits"] / stats["n"] if stats["n"] else 0.0
        stats["mean_confidence"] = stats["confidence"] / stats["n"] if stats["n"] else 0.0
        del stats["confidence"]

    rates = [s["rate"] for s in per.values()]
    return {
        "per_country": per,
        "spread": (max(rates) - min(rates)) if rates else 0.0,
        "ceiling": max(rates) if rates else 0.0,
        "roster": list(config.PILOT_ROSTER),
    }
