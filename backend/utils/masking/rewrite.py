"""Masking the payload the scorer actually sees, and refusing to send it if not.

Two passes and a gate.

The **gazetteer pass** is deterministic and runs over everything: every title,
abstract and body in the snapshot. It is cheap, exact, and blind to anything
nobody wrote down.

The **model pass** runs only over the handful of full texts the scorer reads
end to end. Ten years of news is ten years of politicians, parties, companies
and stadiums, and no hand-written list survives that. So the digest model is
asked to replace what remains with the role it plays — a named finance minister
becomes "the finance minister" — under one non-negotiable instruction: keep
every number exactly as written. A masked run that also lost the magnitudes
would be measuring something else entirely.

The **gate** is `assert_clean`. The gazetteer is a list somebody wrote and the
model pass is a model, so neither is trusted: before anything leaves for the
API, the whole outbound payload is scanned for every roster country's names,
and a hit raises. Not a warning — a masked snapshot that names its country is
not a degraded result, it is a wrong one, and it would sit in the series
looking exactly like a right one.

The scan covers the *whole roster*, not just the country being scored: an
article naming a different roster country lets the probe rule countries out by
elimination, which is the same leak wearing a hat. So the gazetteer pass masks
the whole roster too — the scored country by its roles, everyone else flattened
to "another country" — because a gate that fires on every real snapshot is a
gate somebody turns off.
"""

import logging
from typing import Any, Dict, Iterable, List, Optional

from backend.utils.ai import client as ai_client
from backend.utils.masking import gazetteer

logger = logging.getLogger(__name__)

# Which item fields carry text a reader could identify a country from. `link`
# is deliberately absent and deliberately never sent: a URL like
# ".../2018/aug/13/turkey-lira-crisis" names the country in the path, so masked
# payloads carry ids, not URLs.
_TEXT_FIELDS = ("title", "snippet", "text")

_REWRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "rewritten": {
            "type": "string",
            "description": "The article text with every remaining proper noun "
                           "replaced by the functional role it plays.",
        },
    },
    "required": ["rewritten"],
    "additionalProperties": False,
}

_REWRITE_PROMPT = """\
Rewrite the article below so that no reader could identify which country it is \
about, while changing nothing else.

Rules, in order of importance:
1. Keep every number exactly as written — percentages, dates, rates, amounts, \
counts. Never round, never drop, never convert one.
2. Replace every remaining proper noun with the functional role it plays: a \
named person becomes their office ("the finance minister", "the opposition \
leader"), a named party becomes "the governing party" or "the main opposition \
party", a named company becomes "a large domestic bank" or similar, a named \
place becomes "the capital" or "a major city".
3. Keep the region coarse. Never name a continent, a neighbouring country, a \
currency, a language or a nationality.
4. Change nothing else. Keep the same events, the same sequence, the same tone \
and roughly the same length. Do not summarise, do not add analysis, do not \
soften anything.

Article:
{text}
"""


class MaskLeak(RuntimeError):
    """A payload about to be sent still names a roster country.

    Deliberately fatal. A masked snapshot that saw its country's name is not a
    degraded snapshot, it is a mislabelled one — and in a ten-year series it
    would be indistinguishable from a sound one forever after.
    """


def mask_text(text: str, iso2: str, roster: Optional[Iterable[str]] = None) -> str:
    """The scored country by its roles, every other roster country flattened.

    Both passes, in this order, because the scored country's central bank must
    survive as "the central bank" while everyone else's collapses to "another
    country" — running the flat pass first would eat the specific one.
    """
    return gazetteer.mask_foreign(gazetteer.mask(text, iso2), iso2, roster)


def mask_item(item: Dict[str, Any], iso2: str,
              roster: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """One article with its text masked by the gazetteer. Non-mutating.

    The original item is left alone because the DB and the front end still show
    the real headline: masking is a transform at the scoring boundary, not a
    property of the stored article.
    """
    masked = dict(item)
    for field in _TEXT_FIELDS:
        if masked.get(field):
            masked[field] = mask_text(masked[field], iso2, roster)
    return masked


def mask_items(items: Iterable[Dict[str, Any]], iso2: str,
               roster: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """Every article in a snapshot, masked by the gazetteer."""
    return [mask_item(item, iso2, roster) for item in items]


def mask_payload(value: Any, iso2: str,
                 roster: Optional[Iterable[str]] = None) -> Any:
    """The same two passes over every string in a nested payload. Non-mutating.

    The evidence payload names the country in its ``_meta``, in series labels
    and in provenance, and all of it is serialized straight into the prompt. It
    is masked *whole* rather than field by field for the same reason
    :func:`assert_clean` scans it whole: the leak is wherever nobody looked.
    """
    if isinstance(value, str):
        return mask_text(value, iso2, roster)
    if isinstance(value, dict):
        return {k: mask_payload(v, iso2, roster) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_payload(v, iso2, roster) for v in value]
    return value


def rewrite_body(text: str, api_key: str, model_chat: Optional[Any] = None) -> str:
    """Replace whatever proper nouns the gazetteer could not know about.

    Runs only on the two or three articles the scorer reads in full — the rest
    reach it as digests, which are themselves generated from masked text.

    Fails **closed**, like the leakage scan: if the rewrite errors or comes back
    empty, the caller gets the empty string and the article degrades to its
    masked title rather than being sent with a name in it. Being short one body
    costs a week some evidence; one leaked name costs the whole comparison.
    """
    if not text:
        return ""
    try:
        chat = model_chat or ai_client.build_digest_chat(api_key)
        result = chat.with_structured_output(
            schema=_REWRITE_SCHEMA, strict=True).invoke(_REWRITE_PROMPT.format(text=text))
    except Exception as exc:  # noqa: BLE001
        logger.warning("mask rewrite failed (%s); degrading the article to title-only", exc)
        return ""
    if not isinstance(result, dict):
        return ""
    return str(result.get("rewritten") or "")


def assert_clean(payload: Any, roster: Optional[Iterable[str]] = None) -> None:
    """Refuse to send anything that still names a roster country.

    Walks the payload rather than taking a string, because the thing actually
    sent is a nest of dicts and lists and the leak is wherever nobody looked.

    Raises:
        MaskLeak: naming the forms found, so the gazetteer can be fixed rather
            than the run merely retried.
    """
    roster = list(roster or gazetteer.DEFAULT_ROSTER)
    found = sorted(set(_scan_any(payload, roster)))
    if found:
        raise MaskLeak(
            f"masked payload still names {len(found)} roster term(s): "
            f"{', '.join(found[:10])}"
            + (" …" if len(found) > 10 else "")
        )


def _scan_any(value: Any, roster: List[str]) -> List[str]:
    """Every roster term anywhere in a nested payload."""
    if isinstance(value, str):
        return gazetteer.scan(value, roster)
    if isinstance(value, dict):
        return [hit for v in value.values() for hit in _scan_any(v, roster)]
    if isinstance(value, (list, tuple)):
        return [hit for v in value for hit in _scan_any(v, roster)]
    return []
