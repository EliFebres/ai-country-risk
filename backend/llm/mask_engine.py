"""
What the scorer reads is anonymous (step 5e, Fix 8).

The scorer works under one standard that holds for a live run and for any
later backtest: it is never told which country it is reading about. A live
run cannot leak hindsight, because the model cannot know the future; a
backtest can, because the model knows what happened to a named country after
the anchor date. Adopting the standard before the series starts means the live
series and any later history are scored the same way, and the country's
reputation is no longer a shortcut.

Only the scorer's input is masked. Retrieval, the gate, the story check,
selection and the exposure cards decide *which* articles are read and keep
using real names; the scorer decides the *levels*. The database and the
frontend keep real names.

Two rewrites, both by gpt-4o-mini and both cached in `llm_artifact` under
``mode = 'masked'`` so a masked run can never be served a named text:

- the **masked digest**: the ordinary digest prompt plus the masking block,
  with a `masked_title` for the headline, cached as ``kind = 'digest'``;
- the **masked full text**: the body rewritten sentence for sentence with only
  the names changed, cached as ``kind = 'rewrite'``.

The gazetteer (`backend.util.gazetteer`) checks every rewrite in code. A term
that survives is regenerated once, with the leaked terms named; a term that
survives that is replaced by code. Only clean text is cached, so a re-run of
the same week reproduces the same bytes, and the payload-level check before
every scorer call is a tripwire that should never fire.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from langchain_core.messages import SystemMessage

from backend.data_upsert import store
from backend.llm import digest_engine
from backend.util import gazetteer, usage
from backend.util.hashing import content_hash

logger = logging.getLogger(__name__)

__all__ = [
    "MASK_BLOCK", "MASKED_DIGEST_PROMPT", "MASKED_DIGEST_SCHEMA", "MASKED_DIGEST_PROMPT_VERSION",
    "REWRITE_PROMPT", "REWRITE_PROMPT_VERSION", "REWRITE_MAX_OUTPUT_TOKENS",
    "masked_digest_articles", "rewrite_full_texts", "MaskCounts",
]

DEFAULT_MODEL = digest_engine.DEFAULT_MODEL

#: The brief's masking instruction, verbatim.
MASK_BLOCK = (
    "Write every field without naming the country the article is about, or anything that "
    "identifies it. Use roles, not names: \"the country\", \"the capital\", \"a major city\", "
    "\"the head of government\", \"the central bank governor\", \"a former member of parliament\", "
    "\"the anti-corruption authority\", \"the sovereign wealth fund\", \"the national currency\". "
    "Do not name its cities, regions, landmarks, people, parties, companies or currency, and do "
    "not use its demonym. Write other countries by their relation to it: \"a neighbouring "
    "country\", \"a major trading partner\", \"a regional rival\", \"a major power\". Numbers "
    "stay, but write money as \"local currency\" or \"US dollars\" instead of a currency name or "
    "symbol."
)

#: One line before the block, so "the country the article is about" is the
#: subject of the run and not whichever country the article leads with. The
#: digest model is gpt-4o-mini; the scorer never sees this prompt.
_SUBJECT_LINE = "The country this article is read for is {country}; it is \"the country\" below."

MASKED_DIGEST_PROMPT = (
    digest_engine.DIGEST_PROMPT
    + "\n\nMASKING\n"
    + _SUBJECT_LINE + "\n"
    + MASK_BLOCK
    + "\n\nAlso record `masked_title`: the HEADLINE given above the article, rewritten under "
      "the same rules. Keep its meaning and length; change only what identifies the country."
)

MASKED_DIGEST_SCHEMA: Dict[str, Any] = {
    "name": "article_digest_masked",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            **digest_engine.DIGEST_SCHEMA["schema"]["properties"],
            "masked_title": {"type": "string"},
        },
        "required": [*digest_engine.DIGEST_SCHEMA["schema"]["required"], "masked_title"],
    },
    "strict": True,
}

MASKED_DIGEST_PROMPT_VERSION = content_hash(
    MASKED_DIGEST_PROMPT + "\n" + gazetteer.GAZETTEER_VERSION
)

REWRITE_PROMPT = (
    "You are a copy editor, not an analyst. Rewrite the article below so that it no longer "
    "names the country it is about. Keep every sentence and every number; change only the "
    "names. Do not summarise, shorten, reorder or add anything.\n\n"
    + _SUBJECT_LINE + "\n" + MASK_BLOCK
    + "\n\nReturn only the rewritten article body as `text`: no HEADLINE or ARTICLE label, and "
      "not the headline itself."
)
REWRITE_PROMPT_VERSION = content_hash(REWRITE_PROMPT + "\n" + gazetteer.GAZETTEER_VERSION)
REWRITE_SCHEMA: Dict[str, Any] = {
    "name": "article_rewrite_masked",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    "strict": True,
}
#: A 12,000-character body is about 3,000 tokens; the rewrite is the same length.
REWRITE_MAX_OUTPUT_TOKENS = 6000

_DIGEST_FIELDS = ("what_happened", "institutions", "numbers", "masked_title")


class MaskCounts(dict):
    """``hits``, ``regenerated``, ``replaced_by_code`` and the ``terms`` seen."""

    def __init__(self) -> None:
        super().__init__(hits=0, regenerated=0, replaced_by_code=0, terms=[])

    def hit(self, where: str, hits: Sequence[Tuple[str, str]]) -> None:
        self["hits"] += len(hits)
        for term, kind in hits:
            self["terms"].append({"where": where, "term": term, "kind": kind})


def _digest_strings(digest: Dict[str, Any]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for field in _DIGEST_FIELDS:
        v = digest.get(field)
        if isinstance(v, str):
            out.append((field, v))
        elif isinstance(v, list):
            out.extend((f"{field}[{i}]", s) for i, s in enumerate(v) if isinstance(s, str))
    return out


def _digest_hits(iso2: str, digest: Dict[str, Any]) -> List[Tuple[str, str]]:
    hits: List[Tuple[str, str]] = []
    for _field, s in _digest_strings(digest):
        for h in gazetteer.scan(iso2, s):
            if h not in hits:
                hits.append(h)
    return hits


def _redact_digest(iso2: str, digest: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(digest)
    for field in _DIGEST_FIELDS:
        v = out.get(field)
        if isinstance(v, str):
            out[field] = gazetteer.redact(iso2, v)[0]
        elif isinstance(v, list):
            out[field] = [gazetteer.redact(iso2, s)[0] if isinstance(s, str) else s for s in v]
    return out


def _masked_key(title: str, body: str) -> str:
    return content_hash(f"{title}\n\n{body}")


def _digest_once(structured: Any, model: str, prompt: str, title: str, body: str,
                 meter: Optional[usage.Meter]) -> Optional[Dict[str, Any]]:
    for attempt, slice_chars in enumerate((digest_engine.BODY_CAP_CHARS, digest_engine.RETRY_CHARS)):
        text = f"{prompt}\n\nHEADLINE:\n{title}\n\nARTICLE:\n{body[:slice_chars]}"
        try:
            response = structured.invoke([SystemMessage(content=text)])
        except Exception as e:
            logger.warning("masked digest call failed (attempt %d): %s", attempt + 1, e)
            if attempt == 1:
                return None
            continue
        if meter is not None:
            meter.add_response(model, response)
        parsed = response.get("parsed") if isinstance(response, dict) else response
        if isinstance(parsed, dict) and parsed.get("what_happened") and not digest_engine._finished_on_length(response):
            if attempt == 1:
                parsed["truncated_retry"] = True
            return parsed
    return None


def masked_digest_articles(
    articles: Sequence[Dict[str, Any]],
    *,
    iso2: str,
    country_name: str,
    model: str = DEFAULT_MODEL,
    api_key: Optional[str] = None,
    seed: int = 42,
    meter: Optional[usage.Meter] = None,
    use_cache: bool = True,
    structured: Any = None,
) -> Dict[str, Any]:
    """The masked digest, with `masked_title`, for every article with a body.

    Cached under ``kind='digest', mode='masked', country_iso2=iso2`` and the
    masked prompt's version, keyed on the title and the body together (the
    title is an input here). Each generated digest is checked against the
    gazetteer, regenerated once naming the leaked terms, then redacted by
    code; only the clean digest is cached.

    Returns:
        ``{"digests": {url: digest}, "counts": {generated, cached, failed,
        no_body, truncated_retry}, "mask": MaskCounts}``.
    """
    counts = {"generated": 0, "cached": 0, "failed": 0, "truncated_retry": 0, "no_body": 0}
    mask = MaskCounts()
    digests: Dict[str, Any] = {}
    keyed: Dict[str, Tuple[str, str, str]] = {}
    for a in articles:
        url = a.get("publisher_link") or a.get("link")
        body = (a.get("text") or "")[:digest_engine.BODY_CAP_CHARS]
        if not body.strip():
            counts["no_body"] += 1
            continue
        title = (a.get("title") or "").strip()
        keyed[url] = (_masked_key(title, body), title, body)

    cached: Dict[str, Any] = {}
    if use_cache and keyed:
        try:
            cached = store.read_artifacts(
                [h for h, _, _ in keyed.values()], kind="digest",
                version=MASKED_DIGEST_PROMPT_VERSION, mode="masked", country_iso2=iso2,
            )
        except Exception as e:
            logger.warning("masked digest cache unavailable: %s", e)

    prompt = MASKED_DIGEST_PROMPT.replace("{country}", country_name)
    fresh: List[Tuple[str, Any]] = []
    for url, (h, title, body) in keyed.items():
        if h in cached:
            digests[url] = {**cached[h], "cached": True}
            counts["cached"] += 1
            continue
        if structured is None:
            api_key = api_key or os.getenv("OPENAI_API_KEY")
            if not api_key:
                logger.error("OPENAI_API_KEY not set; cannot digest.")
                break
            structured = digest_engine._client(
                model, api_key, seed, digest_engine.MAX_OUTPUT_TOKENS
            ).with_structured_output(schema=MASKED_DIGEST_SCHEMA, strict=True, include_raw=True)

        got = _digest_once(structured, model, prompt, title, body, meter)
        if got is None:
            counts["failed"] += 1
            continue
        if got.get("truncated_retry"):
            counts["truncated_retry"] += 1
        hits = _digest_hits(iso2, got)
        if hits:
            mask.hit(f"digest:{url}", hits)
            named = ", ".join(sorted({t for t, _ in hits}))
            again = _digest_once(
                structured, model,
                prompt + f"\n\nYour previous answer named these terms, which identify the country: {named}. "
                         f"Do not write them or any form of them.",
                title, body, meter,
            )
            mask["regenerated"] += 1
            if again is not None:
                got = again
                hits = _digest_hits(iso2, got)
            if hits:
                mask["replaced_by_code"] += len(hits)
                got = _redact_digest(iso2, got)
                got["mask_redacted"] = [t for t, _ in hits]
        digests[url] = {**got, "cached": False}
        fresh.append((h, got))
        counts["generated"] += 1

    if fresh and use_cache:
        try:
            store.write_artifacts(fresh, kind="digest", version=MASKED_DIGEST_PROMPT_VERSION,
                                  model=model, mode="masked", country_iso2=iso2)
        except Exception as e:
            logger.warning("could not cache masked digests: %s", e)

    return {"digests": digests, "counts": counts, "mask": mask}


_ECHO = re.compile(r"\A\s*HEADLINE:\s*.*?\n\s*\n\s*ARTICLE:\s*\n?", re.DOTALL)


def _strip_echo(text: str) -> str:
    """The rewrite is asked for the body alone; a model that echoes the
    HEADLINE/ARTICLE framing it was given loses it here."""
    out = _ECHO.sub("", text, count=1)
    out = re.sub(r"\A\s*ARTICLE:\s*\n?", "", out, count=1)
    return out.strip()


def rewrite_full_texts(
    articles: Sequence[Dict[str, Any]],
    *,
    iso2: str,
    country_name: str,
    body_cap_chars: int = digest_engine.BODY_CAP_CHARS,
    model: str = DEFAULT_MODEL,
    api_key: Optional[str] = None,
    seed: int = 42,
    meter: Optional[usage.Meter] = None,
    use_cache: bool = True,
    structured: Any = None,
) -> Dict[str, Any]:
    """Rewrite each article's body (the slice the scorer would read) in role
    language. Cached as ``kind='rewrite', mode='masked'`` on the body's hash.
    A term that survives the rewrite is replaced by code; no regeneration.

    Returns:
        ``{"texts": {url: text}, "counts": {generated, cached, failed},
        "mask": MaskCounts}``.
    """
    counts = {"generated": 0, "cached": 0, "failed": 0}
    mask = MaskCounts()
    texts: Dict[str, str] = {}
    keyed: Dict[str, Tuple[str, str, str]] = {}
    for a in articles:
        url = a.get("publisher_link") or a.get("link")
        body = (a.get("text") or "")[:body_cap_chars]
        if body.strip():
            keyed[url] = (content_hash(body), (a.get("title") or "").strip(), body)

    cached: Dict[str, Any] = {}
    if use_cache and keyed:
        try:
            cached = store.read_artifacts(
                [h for h, _, _ in keyed.values()], kind="rewrite",
                version=REWRITE_PROMPT_VERSION, mode="masked", country_iso2=iso2,
            )
        except Exception as e:
            logger.warning("rewrite cache unavailable: %s", e)

    prompt = REWRITE_PROMPT.replace("{country}", country_name)
    fresh: List[Tuple[str, Any]] = []
    for url, (h, title, body) in keyed.items():
        if h in cached and isinstance(cached[h], dict) and cached[h].get("text"):
            texts[url] = cached[h]["text"]
            counts["cached"] += 1
            continue
        if structured is None:
            api_key = api_key or os.getenv("OPENAI_API_KEY")
            if not api_key:
                logger.error("OPENAI_API_KEY not set; cannot rewrite.")
                break
            structured = digest_engine._client(
                model, api_key, seed, REWRITE_MAX_OUTPUT_TOKENS
            ).with_structured_output(schema=REWRITE_SCHEMA, strict=True, include_raw=True)
        try:
            response = structured.invoke([SystemMessage(content=f"{prompt}\n\nHEADLINE:\n{title}\n\nARTICLE:\n{body}")])
        except Exception as e:
            logger.warning("rewrite call failed for %s: %s", url, e)
            counts["failed"] += 1
            continue
        if meter is not None:
            meter.add_response(model, response)
        parsed = response.get("parsed") if isinstance(response, dict) else response
        text = (parsed or {}).get("text") if isinstance(parsed, dict) else None
        if not text or digest_engine._finished_on_length(response):
            counts["failed"] += 1
            continue
        text = _strip_echo(text)
        hits = gazetteer.scan(iso2, text)
        redacted: List[str] = []
        if hits:
            mask.hit(f"rewrite:{url}", hits)
            mask["replaced_by_code"] += len(hits)
            text, _ = gazetteer.redact(iso2, text)
            redacted = [t for t, _ in hits]
        texts[url] = text
        fresh.append((h, {"text": text, "mask_redacted": redacted}))
        counts["generated"] += 1

    if fresh and use_cache:
        try:
            store.write_artifacts(fresh, kind="rewrite", version=REWRITE_PROMPT_VERSION,
                                  model=model, mode="masked", country_iso2=iso2)
        except Exception as e:
            logger.warning("could not cache rewrites: %s", e)

    return {"texts": texts, "counts": counts, "mask": mask}
