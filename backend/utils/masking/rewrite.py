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

import hashlib
import json
import logging
from typing import Any, Dict, Iterable, List, Optional

from backend.utils.ai import client as ai_client
from backend.utils.masking import gazetteer

logger = logging.getLogger(__name__)

# Which item fields to leave alone. Everything else is masked, and the polarity
# matters more than the contents: this began as an allow-list of the three
# fields somebody remembered — title, snippet, text — and `article_input_text`
# prefers `content` over `text` while the legacy prompt shape reads `summary`.
# Both went to the model unmasked, and the allow-list looked complete the whole
# time. A gate defaults to masking; what it skips has to be argued for.
#
# The links are argued for: they are never sent (`prompt_entries` carries ids,
# not URLs) and a path like ".../2018/aug/13/turkey-lira-crisis" masks into
# nonsense. The rest are not text.
_UNMASKED_FIELDS = frozenset({
    "link", "publisher_link", "url", "image", "id", "published", "published_at",
    "relevance_score", "stage1_severity", "_theme", "theme",
})

# What a masked bundle may not contain, written once and shared by both model
# passes so the digest sweep and the body rewrite cannot drift apart.
#
# Rules 3 and 5 exist because a probe run measured them. With people and
# countries gone, six of six bundles were still identified at 0.80-0.90, and the
# evidence the probe quoted was: "the Help America Vote Act" (a statute carrying
# a country's name), "the White House Situation Room" (a named building), "as
# bad as Brexit" (a named event), "the Iranian people" and "Europe" (a demonym
# and a continent for countries outside the roster).
#
# None of those is reachable from the gazetteer. It is a list of roster
# countries, so a non-roster demonym, a continent and a named event are all
# invisible to it — and extending the list would mean enumerating every proper
# noun on earth. This is the layer that can generalise, so this is where the
# scope belongs.
_MASK_RULES = """\
1. Keep every number exactly as written — percentages, dates, rates, amounts, \
counts. Never round, never drop, never convert one. Numbers are evidence.
2. Replace every proper noun with the functional role it plays. A named person \
becomes their office ("the president", "the finance minister", "the central \
bank governor"). A person with no office becomes what they are known for — a \
footballer, a striker, a pop singer, a novelist, a business magnate — never \
their name, and never a role that implies where they play or who they play \
for. A named party becomes "the governing party" or "the main opposition \
party". A named company becomes "a large domestic bank" or similar. A named \
place becomes "the capital", "a major city" or "a neighbouring country".
3. This applies to *every* country, not only the one being described. Never \
name a foreign country, a foreign leader, a foreign city or a foreign \
institution: "another country", "a foreign leader", "a major foreign economy".
4. Keep the region coarse. Never name a continent, an ocean, a hemisphere, a \
supranational bloc, a currency, a currency symbol, a language, a nationality \
or a demonym — "European", "Iranian" and "Asian" are all identifying.
5. Named *things* count as proper nouns and are the easiest ones to miss. \
Rewrite them as what they were:
   - a named law or treaty ("the Help America Vote Act" -> "a voting rights \
law"; "the Good Friday Agreement" -> "a peace agreement");
   - a named event, crisis or referendum ("Brexit" -> "a referendum on leaving \
a trade bloc"; "the Arab Spring" -> "a wave of regional uprisings");
   - a named building, landmark, monument or room ("the White House Situation \
Room" -> "the leader's crisis room"; "Wall Street" -> "the financial district");
   - a named sports team, league, competition or club;
   - a named scandal, military operation, court case or government programme.
6. Change nothing else — same facts, same sequence, roughly the same length."""

_REWRITE_SCHEMA = {
    # The title is not decoration: `with_structured_output(strict=True)` turns
    # the schema into a function definition and OpenAI needs a name for it.
    # Without one every call raised, the pass failed closed on every article,
    # and the only symptom was three bodies quietly degrading to their titles.
    "title": "MaskedArticle",
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
""" + _MASK_RULES + """
7. Do not summarise, do not add analysis, do not soften anything. Keep the same \
tone.

Article:
{text}
"""


_DIGEST_SWEEP_FIELDS = ("what_happened", "actors", "numbers", "transmission")

# The headline is sent for every article, digest or not, and it was reaching the
# model with gazetteer masking alone — so "Brazil Election: Jair Bolsonaro Heads
# to Runoff" became "the country Election: Jair Bolsonaro Heads to Runoff". Six
# of twenty titles in one measured bundle named the politician. It is swept in
# the same call as the digest, so it costs nothing extra and caches with it.
_SWEPT_TITLE_KEY = "masked_title"

_DIGEST_SWEEP_SCHEMA = {
    "title": "MaskedDigest",
    "type": "object",
    "properties": {field: {"type": "string"}
                   for field in _DIGEST_SWEEP_FIELDS + ("headline",)},
    "required": list(_DIGEST_SWEEP_FIELDS) + ["headline"],
    "additionalProperties": False,
}

_DIGEST_SWEEP_PROMPT = """\
The fields below describe an event in a country that is deliberately anonymous. \
Rewrite them so no reader could name the country, changing nothing else.

Rules, in order of importance:
""" + _MASK_RULES + """

Return `headline` as the given headline rewritten under the same rules, keeping \
it a headline.

{fields}
"""

# What the model passes *do*, as a version string, derived rather than
# maintained.
#
# The digest cache keys masked digests on a hash of the masked text, and the
# sweep runs after the digest is generated — so between `84c5b9f` and `1fab1b1`
# the sweep changed twice while the hash did not move an inch. Every digest
# cached before those commits was unswept, carried no `masked_title`, and would
# have been served straight into the pilot with the manifest reporting the same
# `mask_map_version` either way. Two masking behaviours, one cache key.
#
# `MASK_MAP_VERSION` cannot cover this: it versions the gazetteer's data, and
# the sweep is a prompt. Deriving the version from the prompt rather than adding
# a second constant to bump is the point — this repo has already shipped one
# version bump that silently did not happen (`b146104`: the sed sat behind a
# `&&` after a command that exited non-zero), and a hash cannot forget.
#
# One version per cache, now that both model passes have one.
#
# These were briefly a single constant covering both prompts, on the argument
# that no two masking behaviours may share a label. The argument was right and
# the implementation was blunt: the body rewrite cannot change a digest — it runs
# after digesting, on a different text — so folding it in threw away every cached
# digest whenever the body prompt moved.
#
# Two versions, each keying its own cache, and the *manifest carries both*. That
# is what actually satisfies the invariant: a row records the full masking
# behaviour that produced it, while each cache invalidates on exactly the change
# that affects it and no other.
SWEEP_VERSION = hashlib.sha256(
    (_DIGEST_SWEEP_PROMPT + "\x00".join(_DIGEST_SWEEP_FIELDS)).encode("utf-8")
).hexdigest()[:8]

REWRITE_VERSION = hashlib.sha256(
    (_REWRITE_PROMPT + json.dumps(_REWRITE_SCHEMA, sort_keys=True)).encode("utf-8")
).hexdigest()[:8]


def sweep_digest(digest: Dict[str, Any], api_key: str,
                 model_chat: Optional[Any] = None,
                 title: str = "") -> Optional[Dict[str, Any]]:
    """Replace the names a digest kept, despite being told not to keep them.

    The digest prompt already runs in mask mode, so digests are *born* masked —
    that was the argument for sweeping only the two or three full texts. It does
    not survive contact with the measurement. A probe over twenty stored bundles
    identified fifteen, and the evidence it quoted was almost entirely people:
    "Jair Bolsonaro", "Erdoğan", "Park Geun-hye", "Temer", "Lula". Those are in
    the digests, written there by a model that was instructed in the same breath
    not to write them.

    The gazetteer cannot fix this — it is a list of what somebody wrote down,
    and a list cannot know this year's finance minister. This is the layer that
    can, and it now covers the seventeen articles a snapshot reads as digests
    rather than only the three it reads whole.

    Runs once per unique article per mode and is cached with the digest, so the
    cost is amortized over every snapshot that reuses it rather than paid per
    snapshot.

    Returns:
        The digest with its free-text fields swept, or None if the call failed —
        the caller decides whether to keep the unswept digest or drop it. Unlike
        :func:`rewrite_body` this does not fail closed on its own, because a
        digest is not sent whole: dropping it silently would cost the article.
    """
    if not isinstance(digest, dict):
        return None
    fields = {f: str(digest.get(f) or "") for f in _DIGEST_SWEEP_FIELDS}
    if not any(fields.values()) and not title:
        return dict(digest)
    rendered = "\n".join(f"{name}: {text}" for name, text in fields.items() if text)
    if title:
        rendered = f"headline: {title}\n{rendered}"
    try:
        chat = model_chat or ai_client.build_digest_chat(api_key)
        result = chat.with_structured_output(
            schema=_DIGEST_SWEEP_SCHEMA, strict=True).invoke(
                _DIGEST_SWEEP_PROMPT.format(fields=rendered))
    except Exception as exc:  # noqa: BLE001
        logger.warning("digest sweep failed (%s); keeping the unswept digest", exc)
        return None
    if not isinstance(result, dict):
        return None
    swept = dict(digest)
    for field in _DIGEST_SWEEP_FIELDS:
        value = result.get(field)
        if isinstance(value, str) and value.strip():
            swept[field] = value
    headline = result.get("headline")
    if title and isinstance(headline, str) and headline.strip():
        swept[_SWEPT_TITLE_KEY] = headline.strip()
    return swept


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
    """One article masked field by field, ids and links excepted. Non-mutating.

    The original item is left alone because the DB and the front end still show
    the real headline: masking is a transform at the scoring boundary, not a
    property of the stored article.

    The stage-1 ``digest`` is masked too, though it is generated from already
    masked text. It is model output, and the gazetteer is cheaper than trusting
    it.
    """
    return {
        key: value if key in _UNMASKED_FIELDS else mask_payload(value, iso2, roster)
        for key, value in item.items()
    }


def mask_items(items: Iterable[Dict[str, Any]], iso2: str,
               roster: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """Every article in a snapshot, masked by the gazetteer."""
    return [mask_item(item, iso2, roster) for item in items]


def _code_role(value: str, iso2: str, roster: List[str]) -> Optional[str]:
    """The role for a payload value that *is* a country code, or None.

    ISO2 codes are the one identifier the prose gazetteer cannot carry. As
    patterns they are catastrophic — "IT" is information technology, "NO" is
    no, "IN" and "ID" and "AT" are ordinary words in upper case — so masking
    them anywhere in a sentence would shred the corpus.

    But a payload value that is *exactly* "PT" is not prose. It is a field, and
    a field naming the country is the loudest possible leak: the evidence
    payload is serialized whole into the prompt, `_meta.country` and all.
    Matching on the entire string rather than inside it is what makes this both
    safe and sufficient.
    """
    code = value.strip().upper()
    if len(code) != 2 or code not in roster:
        return None
    return gazetteer.ROLES["names"] if code == iso2.upper() else gazetteer.ROLES["foreign"]


def mask_payload(value: Any, iso2: str,
                 roster: Optional[Iterable[str]] = None) -> Any:
    """The same two passes over every string in a nested payload. Non-mutating.

    The evidence payload names the country in its ``_meta``, in series labels
    and in provenance, and all of it is serialized straight into the prompt. It
    is masked *whole* rather than field by field for the same reason
    :func:`assert_clean` scans it whole: the leak is wherever nobody looked.
    """
    roster = list(roster or gazetteer.DEFAULT_ROSTER)
    if isinstance(value, str):
        return _code_role(value, iso2, roster) or mask_text(value, iso2, roster)
    if isinstance(value, dict):
        # Keys as well as values. The payload is serialized to JSON before it
        # reaches the model, so a label is as visible as a number — and the
        # evidence payload really does carry "Exchange rate vs USD" as a key.
        # That one names a country in every country's payload, so masking it
        # costs no information at all; the point is that the gate cannot have
        # exceptions, because the first one is always the reasonable one.
        out: Dict[Any, Any] = {}
        for key, item in value.items():
            masked_key = mask_payload(key, iso2, roster) if isinstance(key, str) else key
            # Two labels differing only by a country name must not merge into
            # one indicator. Vanishingly unlikely, silent if it happened.
            while masked_key in out:
                masked_key = f"{masked_key} "
            out[masked_key] = mask_payload(item, iso2, roster)
        return out
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
        # A value that *is* a country code, checked the same way
        # `mask_payload` masks it. A gate blind to the leak its own masking
        # pass handles is a gate that only ever confirms itself.
        code = value.strip().upper()
        codes = [code] if len(code) == 2 and code in roster else []
        return codes + gazetteer.scan(value, roster)
    if isinstance(value, dict):
        # Keys too — they are serialized into the prompt exactly like values,
        # and scanning only values is how "Exchange rate vs USD" survived.
        return [hit for pair in value.items() for v in pair
                for hit in _scan_any(v, roster)]
    if isinstance(value, (list, tuple)):
        return [hit for v in value for hit in _scan_any(v, roster)]
    return []
