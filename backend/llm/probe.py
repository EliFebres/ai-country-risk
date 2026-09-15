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

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

from backend.llm import client as ai_client

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
        "alternatives": {
            "type": "array",
            "description": "The three most likely countries with probabilities "
                           "summing to at most 1.0, most likely first. The "
                           "first entry must match `country`.",
            "items": {
                "type": "object",
                "properties": {
                    "country": {"type": "string"},
                    "probability": {"type": "number"},
                },
                "required": ["country", "probability"],
                "additionalProperties": False,
            },
        },
        "insufficient_information": {
            "type": "boolean",
            "description": "True when the bundle carries nothing country-specific "
                           "and the answer is a prior rather than an inference.",
        },
        "evidence": {
            "type": "string",
            "description": "The specific detail that gave it away, or why it is untellable.",
        },
    },
    "required": ["country", "confidence", "alternatives",
                 "insufficient_information", "evidence"],
    "additionalProperties": False,
}

# Asking for a distribution rather than an answer, and offering a way out.
#
# The old prompt said "guess even when unsure" and allowed only a single code.
# Both push the same way: a model that must name one country will name the one
# its prior favours, and on this roster that is the United States. The meter then
# reports the model's prior as an identifiability rate, and the two are
# indistinguishable in the output — a masked US bundle and an empty bundle both
# come back "US, 0.85".
#
# `alternatives` makes the prior visible: a bundle identified on evidence
# concentrates probability, a bundle answered from prior spreads it.
# `insufficient_information` lets the model say so outright. Neither is a fix for
# masking; both are what make the number readable, and they are why the control
# arm below exists.
_PROBE_PROMPT = """\
The news summaries below have had country names, demonyms, currencies, cities \
and institutions removed. Identify which country they are about.

Give your three most likely candidates with probabilities. Concentrate the \
probability only as far as the evidence warrants: if two countries fit equally \
well, say so with two similar probabilities rather than picking one.

Set `insufficient_information` to true when the text carries nothing \
country-specific and your answer is really a guess from base rates — that is a \
more useful answer than a confident one you cannot support, and it is not \
penalised. Use 'ZZ' as the country in that case if no candidate stands out.

Say what gave it away: the specific number, institution, event or phrasing. If \
you are inferring from base rates, say that instead.

{bundle}
"""

# How much of each article the probe reads. It is measuring identifiability of
# the *evidence*, so it gets what the scorer got, capped so a single long
# article cannot dominate the bundle.
_PER_ARTICLE_CHARS = 1200

# The instrument's own version, on the same derived-not-maintained principle as
# `rewrite.SWEEP_VERSION`.
#
# A probe result is only comparable to another taken with the same instrument.
# Asking for a top-3 distribution and offering `insufficient_information`
# changes what the model reports about identical text — that is the point of the
# change — so a stored result from before it is not a baseline for one after it,
# and a key that could not tell them apart would silently overwrite one with the
# other. Which is the same failure `SWEEP_VERSION` exists to prevent, one layer
# up.
PROBE_VERSION = hashlib.sha256(
    (_PROBE_PROMPT + json.dumps(_PROBE_SCHEMA, sort_keys=True)).encode("utf-8")
).hexdigest()[:8]


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
    from backend.llm import langchain_llm

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
        ``{'country', 'confidence', 'alternatives', 'insufficient_information',
        'evidence'}``. A failed call returns 'ZZ' at confidence 0.0 with the
        error as evidence — the opposite of the leakage scan's fail-closed, and
        deliberately: this is a measurement, not a gate, and a failed
        measurement must not be recorded as a successful identification.
    """
    if not items:
        return _no_guess("empty bundle")
    try:
        chat = model_chat or ai_client.build_digest_chat(api_key)
        result = chat.with_structured_output(
            schema=_PROBE_SCHEMA, strict=True).invoke(
                _PROBE_PROMPT.format(bundle=bundle_text(items, fulltext_ids)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("probe failed (%s); recorded as no-guess", exc)
        return _no_guess(f"probe failed: {exc}")
    if not isinstance(result, dict):
        return _no_guess("probe returned no object")

    alternatives = []
    for entry in result.get("alternatives") or []:
        if not isinstance(entry, dict):
            continue
        try:
            alternatives.append({
                "country": str(entry.get("country") or "ZZ").upper()[:2],
                "probability": float(entry.get("probability") or 0.0),
            })
        except (TypeError, ValueError):
            continue
    return {
        "country": str(result.get("country") or "ZZ").upper()[:2],
        "confidence": float(result.get("confidence") or 0.0),
        "alternatives": alternatives[:3],
        "insufficient_information": bool(result.get("insufficient_information")),
        "evidence": str(result.get("evidence") or ""),
    }


def _no_guess(evidence: str) -> Dict[str, Any]:
    """The shape a probe that did not happen returns.

    ``insufficient_information`` is True here for the same reason the whole
    function fails open: an unanswered probe is not evidence that masking
    worked, and anything averaging over these must be able to exclude them.
    """
    return {"country": "ZZ", "confidence": 0.0, "alternatives": [],
            "insufficient_information": True, "evidence": evidence}
