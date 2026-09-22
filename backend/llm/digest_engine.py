"""
Stage one: read every admitted article, and say what it holds.

Feeding twenty full bodies to the scoring model is unaffordable; feeding it
twenty headlines throws away the reporting that was paid for. So the funnel
narrows twice — a cheap model digests every admitted article, and only the top
few are read in full.

This is an **extraction engine, not an analyst.** It is told to use only the
text in front of it and to write "not stated" rather than fill a gap from
outside knowledge. It never sees the macro payload, the other articles, or the
scoring rubric, and it never produces a risk score. Anything it invents here
arrives at the scorer indistinguishable from reporting.

Digests are cached in `llm_artifact` on the hash of the text actually digested
and the hash of this prompt, so a same-day re-run makes almost no calls and a
prompt edit invalidates every row it touches without anyone remembering to.

**Generation is bounded.** A digest model left at its default ceiling can run to
that ceiling on an ordinary input, burning thousands of tokens to produce a
two-hundred-token object. `MAX_OUTPUT_TOKENS` is a few times the schema's real
size, and a length failure is retried once on a shorter slice and stamped
`truncated-retry` so the census can count how often it happens rather than
leaving it as folklore.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI

from backend.data_upsert import store
from backend.util import usage
from backend.util.hashing import content_hash

logger = logging.getLogger(__name__)

__all__ = [
    "DIGEST_PROMPT",
    "DIGEST_SCHEMA",
    "DIGEST_PROMPT_VERSION",
    "DEFAULT_MODEL",
    "MAX_OUTPUT_TOKENS",
    "BODY_CAP_CHARS",
    "RETRY_CHARS",
    "digest_text",
    "digest_articles",
    "body_status_for",
]


# Extractive work, and the same model as the gate unless measurement says
# otherwise: one model across both stages is one fewer noise profile to
# understand. Dated id, never an alias.
DEFAULT_MODEL = "gpt-4o-mini-2024-07-18"

# The schema is four small fields — a couple of hundred tokens of real output.
# A few times that is room to work; the model's own default ceiling is not a
# bound at all.
MAX_OUTPUT_TOKENS = 800

# How much of a body is digested. Documented rather than incidental: the hash
# covers what was actually read, so this number is part of the cache key's
# meaning.
BODY_CAP_CHARS = 12_000

# The second attempt after a length failure.
RETRY_CHARS = 6_000


DIGEST_PROMPT = """You are an extraction engine, not an analyst.

Read the article below and record what it says. Use ONLY the text in front of
you. Where the article does not say something, write "not stated" rather than
supplying it from your own knowledge. You will not be asked to judge risk, you
will not see any other article, and nothing you write here is a score.

Record:

- `what_happened`: two or three sentences, in plain language, stating what the
  article reports. Name the change or the condition, not the framing.
- `institutions`: the named institutions, bodies, ministries, courts, central
  banks, regulators or agencies the article says are involved. Names as the
  article gives them. Empty list if none are named.
- `direction`: whether what the article describes leaves the country better or
  worse off than before — `improving`, `deteriorating`, `mixed` or `unclear`.
  Judge the direction of the country's POSITION, not the direction of the
  number: inflation rising is `deteriorating`, unemployment falling is
  `improving`, a corruption score falling is `deteriorating`. Choose `unclear`
  when the article reports a fact without indicating which way it cuts — that is
  a common and correct answer, not a failure.
- `numbers`: the figures the article states, each as a short phrase carrying the
  quantity and what it measures, for example "inflation 4.2% in August" or
  "EUR 918 million pension bonus". Copy them; do not compute, convert, annualise
  or round. Empty list if the article states none.

If the text is too short or too damaged to extract anything, say so in
`what_happened` and leave the other fields empty. A thin digest that says it is
thin is useful; an invented one is not."""


DIGEST_SCHEMA: Dict[str, Any] = {
    "name": "article_digest",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "what_happened": {"type": "string"},
            "institutions": {"type": "array", "items": {"type": "string"}},
            "direction": {
                "type": "string",
                "enum": ["improving", "deteriorating", "mixed", "unclear"],
            },
            "numbers": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["what_happened", "institutions", "direction", "numbers"],
    },
    "strict": True,
}


DIGEST_PROMPT_VERSION = content_hash(DIGEST_PROMPT)


def body_status_for(article: Dict[str, Any], *, full_text: bool) -> Tuple[str, bool, int]:
    """Return ``(body_status, clipped, original_chars)`` for one article.

    The status is honest about how much of the article was actually read, and it
    is told to the scorer: a judgement made from a headline is not a judgement
    made from the reporting, and a payload that hides the difference invites the
    model to treat them alike.
    """
    body = article.get("text") or ""
    original = len(body)
    if not body.strip():
        return "title-only", False, original
    clipped = original > BODY_CAP_CHARS
    if full_text:
        return ("clipped" if clipped else "full"), clipped, original
    return "digest-only", clipped, original


def _client(model: str, api_key: Optional[str], seed: int, max_tokens: int) -> Any:
    return ChatOpenAI(
        model=model,
        temperature=0.0,
        seed=seed,
        max_retries=0,
        max_tokens=max_tokens,
        api_key=api_key,
    )


def _finished_on_length(response: Any) -> bool:
    """Whether the call stopped because it hit the output ceiling."""
    raw = response.get("raw") if isinstance(response, dict) else response
    meta = getattr(raw, "response_metadata", None) or {}
    if meta.get("finish_reason") == "length":
        return True
    # Some wrappers surface it on the generation info instead.
    return bool(getattr(raw, "finish_reason", None) == "length")


def digest_text(
    text: str,
    *,
    model: str = DEFAULT_MODEL,
    api_key: Optional[str] = None,
    seed: int = 42,
    meter: Optional[usage.Meter] = None,
    structured: Any = None,
) -> Optional[Dict[str, Any]]:
    """Digest one piece of text, retrying once on a length failure.

    Returns:
        The digest, carrying ``truncated_retry`` when the second attempt was
        needed, or None if both attempts failed.
    """
    if structured is None:
        api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.error("OPENAI_API_KEY not set; cannot digest.")
            return None
        structured = _client(model, api_key, seed, MAX_OUTPUT_TOKENS).with_structured_output(
            schema=DIGEST_SCHEMA, strict=True, include_raw=True
        )

    for attempt, slice_chars in enumerate((BODY_CAP_CHARS, RETRY_CHARS)):
        body = text[:slice_chars]
        prompt = f"{DIGEST_PROMPT}\n\nARTICLE:\n{body}"
        try:
            response = structured.invoke([SystemMessage(content=prompt)])
        except Exception as e:
            logger.warning("digest call failed (attempt %d): %s", attempt + 1, e)
            if attempt == 1:
                return None
            continue

        if meter is not None:
            meter.add_response(model, response)

        parsed = response.get("parsed") if isinstance(response, dict) else response
        hit_ceiling = _finished_on_length(response)

        if isinstance(parsed, dict) and parsed.get("what_happened") and not hit_ceiling:
            if attempt == 1:
                parsed["truncated_retry"] = True
            return parsed

        if attempt == 0:
            logger.info(
                "digest hit the output ceiling or returned nothing usable; "
                "retrying on the first %d characters",
                RETRY_CHARS,
            )

    return None


def digest_articles(
    articles: Sequence[Dict[str, Any]],
    *,
    model: str = DEFAULT_MODEL,
    api_key: Optional[str] = None,
    seed: int = 42,
    meter: Optional[usage.Meter] = None,
    use_cache: bool = True,
    mode: str = "named",
) -> Dict[str, Any]:
    """Digest every admitted article, serving cached digests where they exist.

    Args:
        articles: The selected articles, each carrying ``text``.

    Returns:
        ``{"digests": {url: digest}, "counts": {...}}``. The counts separate
        generated from cached and record how many needed the truncated retry,
        because "the cache is working" and "nothing was cached" look identical
        from the outside.
    """
    with_body = [a for a in articles if (a.get("text") or "").strip()]
    if not with_body:
        return {"digests": {}, "counts": {"generated": 0, "cached": 0,
                                          "truncated_retry": 0, "failed": 0,
                                          "no_body": len(articles)}}

    keyed = {}
    for a in with_body:
        url = a.get("publisher_link") or a.get("link")
        body = (a.get("text") or "")[:BODY_CAP_CHARS]
        keyed[url] = (content_hash(body), body)

    cached: Dict[str, Any] = {}
    if use_cache:
        try:
            cached = store.read_artifacts(
                [h for h, _ in keyed.values()],
                kind="digest",
                version=DIGEST_PROMPT_VERSION,
                mode=mode,
            )
        except Exception as e:
            logger.warning("digest cache unavailable: %s", e)

    digests: Dict[str, Any] = {}
    fresh: List[Tuple[str, Any]] = []
    counts = {"generated": 0, "cached": 0, "truncated_retry": 0, "failed": 0,
              "no_body": len(articles) - len(with_body)}

    structured = None
    for url, (h, body) in keyed.items():
        if h in cached:
            digests[url] = {**cached[h], "cached": True}
            counts["cached"] += 1
            continue

        if structured is None:
            api_key = api_key or os.getenv("OPENAI_API_KEY")
            if not api_key:
                logger.error("OPENAI_API_KEY not set; cannot digest.")
                break
            structured = _client(
                model, api_key, seed, MAX_OUTPUT_TOKENS
            ).with_structured_output(schema=DIGEST_SCHEMA, strict=True, include_raw=True)

        got = digest_text(body, model=model, meter=meter, structured=structured)
        if got is None:
            counts["failed"] += 1
            continue
        if got.get("truncated_retry"):
            counts["truncated_retry"] += 1
        digests[url] = {**got, "cached": False}
        fresh.append((h, got))
        counts["generated"] += 1

    if fresh and use_cache:
        try:
            store.write_artifacts(
                fresh, kind="digest", version=DIGEST_PROMPT_VERSION,
                model=model, mode=mode,
            )
        except Exception as e:
            logger.warning("could not cache digests: %s", e)

    return {"digests": digests, "counts": counts}
