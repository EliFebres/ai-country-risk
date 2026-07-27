"""Stage 1 of the two-stage scoring pipeline: digest every fetched article.

The scorer used to see only each article's title and first-240-word summary,
for at most 10 articles — the full bodies ``news_fetching`` had already
downloaded were thrown away at the prompt boundary. This module closes that
gap cheaply: a small model reads every article's full text and returns a
strict-JSON factual digest plus a 0-100 ``stage1_severity``. The scorer then
reasons over *every* digest and reads only the few highest-severity articles
in full.

Digests are cached in Postgres (``article_digest``) keyed by
``(country_iso2, as_of, url)`` with a sha256 of the digested text, so a
same-day re-run with unchanged articles makes ~zero stage-1 calls.

This module boundary IS the swappable interface: pointing stage 1 at a local
model later means editing this module only. Failures never propagate — an
article that can't be digested keeps ``digest = None`` (the scorer falls back
to its title+summary), and cache read/write problems just mean "no cache".
The daily run always completes.
"""

import datetime
import hashlib
import logging
import os
from typing import Dict, List, Optional

from langchain_core.messages import SystemMessage

import backend.utils.ai.constants as ai_constants
from backend.utils.ai import client as ai_client
from backend.utils.data_upsert import data_push

logger = logging.getLogger(__name__)

# Stage-1 calls in flight at once. High enough to keep a 20-article country
# fast, low enough to stay clear of per-minute rate limits.
_MAX_CONCURRENCY = 8


def article_input_text(item: Dict) -> str:
    """The fullest text we hold for an article — the stage-1/full-text input.

    The longer of ``content`` (fallback scraper) and ``text`` (trafilatura);
    if neither exists, ``summary``, then ``snippet``, then ``""``. Thin or
    empty text is still digestible — the digest prompt's "not stated" rule
    handles it.

    Raises:
        TypeError: if ``item`` is not a dict.
    """
    if not isinstance(item, dict):
        raise TypeError(f"`item` must be a dict, got {type(item).__name__}")
    content = item.get("content") or ""
    text = item.get("text") or ""
    best = content if len(content) >= len(text) else text
    return (best or item.get("summary") or item.get("snippet") or "").strip()


def _severity_or_none(value) -> Optional[float]:
    """Coerce a severity to a clamped 0..100 float, or None if malformed."""
    try:
        severity = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(100.0, severity))


def digest_articles(
    items: List[Dict],
    *,
    country_display: str,
    iso2: str,
    as_of: datetime.date,
) -> List[Dict]:
    """Digest every article's full text with the cheap stage-1 model.

    Checks the ``article_digest`` cache first (same url, same content hash →
    no API call), digests the misses concurrently, annotates each item in
    place with ``digest`` (dict or None) and ``stage1_severity`` (float or
    None), and persists the new digests back to the cache.

    Args:
        items: fetched article dicts, each already carrying its ``id``.
            Mutated in place, matching the ``resolve_and_enrich`` convention.
        country_display: country name as it should appear in the digest prompt.
        iso2: 2-letter country code — half of the cache key.
        as_of: the date the snapshot will be keyed on — the other half, so
            digest rows and the snapshot always share a key.

    Returns:
        The same list, mutated. Per-article failures leave ``digest = None``;
        cache problems degrade to "no cache". Neither ever raises.

    Raises:
        TypeError: if ``items``/``country_display``/``iso2``/``as_of`` have
            the wrong type.
        ValueError: if ``country_display`` is blank or ``iso2`` is not 2
            letters.
    """
    if not isinstance(items, list):
        raise TypeError(f"`items` must be a list, got {type(items).__name__}")
    if not isinstance(country_display, str):
        raise TypeError(f"`country_display` must be a str, got {type(country_display).__name__}")
    if not country_display.strip():
        raise ValueError(f"`country_display` must be non-empty, got {country_display!r}")
    if not isinstance(iso2, str):
        raise TypeError(f"`iso2` must be a str, got {type(iso2).__name__}")
    if len(iso2.strip()) != 2:
        raise ValueError(f"`iso2` must be a 2-letter code, got {iso2!r}")
    if not isinstance(as_of, datetime.date):
        raise TypeError(f"`as_of` must be a date, got {type(as_of).__name__}")
    if not items:
        return items

    cache: Dict[str, Dict] = {}
    try:
        cache = data_push.read_article_digests(iso2, as_of)
    except Exception as exc:
        logger.warning("[%s] digest cache read failed (%s); digesting everything", iso2, exc)

    # Classify every item: cache hit (annotate now) or pending (model call).
    urls: List[str] = []
    texts: List[str] = []
    pending_idx: List[int] = []
    cached = 0
    for i, it in enumerate(items):
        text = article_input_text(it)
        url = ((it.get("publisher_link") or it.get("link")) or "").strip()
        urls.append(url)
        texts.append(text)

        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        hit = cache.get(url) if url else None
        if hit and hit.get("content_sha256") == sha and isinstance(hit.get("digest"), dict):
            it["digest"] = hit["digest"]
            it["stage1_severity"] = _severity_or_none(hit.get("stage1_severity"))
            cached += 1
        else:
            it["digest"] = None
            it["stage1_severity"] = None
            pending_idx.append(i)

    ok = 0
    if pending_idx:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.warning("[%s] OPENAI_API_KEY not set; %d articles left undigested", iso2, len(pending_idx))
        else:
            prompts = [
                ai_constants.DIGEST_PROMPT.format(country=country_display, article_text=texts[i])
                for i in pending_idx
            ]
            structured_llm = ai_client.build_digest_chat(api_key).with_structured_output(
                schema=ai_constants.DIGEST_SCHEMA, strict=True
            )
            try:
                results = structured_llm.batch(
                    [[SystemMessage(content=p)] for p in prompts],
                    config={"max_concurrency": _MAX_CONCURRENCY},
                    return_exceptions=True,
                )
            except Exception as exc:
                logger.warning("[%s] stage-1 batch failed outright: %s", iso2, exc)
                results = [exc] * len(pending_idx)

            # Results come back in input order — map by index, never by
            # completion order, so output order stays deterministic.
            new_rows: List[Dict] = []
            for i, res in zip(pending_idx, results):
                it = items[i]
                if isinstance(res, Exception) or not isinstance(res, dict):
                    logger.warning("[%s] digest failed for %s: %s", iso2, it.get("id"), res)
                    continue
                it["digest"] = res
                it["stage1_severity"] = _severity_or_none(res.get("stage1_severity"))
                ok += 1
                if urls[i]:  # no url → nothing to key the cache row on
                    new_rows.append({
                        "country_iso2": iso2,
                        "as_of": as_of,
                        "url": urls[i],
                        "published_at": it.get("published"),
                        "content_sha256": hashlib.sha256(texts[i].encode("utf-8")).hexdigest(),
                        "digest": res,
                        "stage1_severity": it["stage1_severity"],
                        "model_id": ai_client.DIGEST_MODEL_NAME,
                    })
            if new_rows:
                try:
                    data_push.upsert_article_digests(new_rows)
                except Exception as exc:
                    logger.warning("[%s] digest cache write failed: %s", iso2, exc)

    failed = len(items) - ok - cached
    logger.info("[%s] digests: ok=%d cached=%d failed=%d", iso2, ok, cached, failed)
    return items


def select_fulltext_ids(items: List[Dict], k: int = 3) -> List[str]:
    """Pick the article ids whose full text the scorer should read.

    Top ``k`` by ``stage1_severity`` desc, ties broken by ``relevance_score``
    desc, then ``published`` desc, then ``id`` asc. Items with no severity
    rank below every scored item; if fewer than ``k`` items are scored, the
    remainder is filled in ``relevance_score`` order. Pure and deterministic.

    This is a different "top 3" from the dashboard's: severity decides what
    the scorer *reads in full* (pre-call); the model's own impact scores still
    drive the Top-3 display (post-call, via ``article_ranking``).

    Raises:
        TypeError: if ``items`` is not a list or ``k`` is not an int.
        ValueError: if ``k`` is negative.
    """
    if not isinstance(items, list):
        raise TypeError(f"`items` must be a list, got {type(items).__name__}")
    if not isinstance(k, int):
        raise TypeError(f"`k` must be an int, got {type(k).__name__}")
    if k < 0:
        raise ValueError(f"`k` must be >= 0, got {k}")

    scored: List[Dict] = []
    unscored: List[Dict] = []
    for it in items:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        (scored if it.get("stage1_severity") is not None else unscored).append(it)

    def _rel(it: Dict) -> float:
        try:
            return float(it.get("relevance_score") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _pub(it: Dict) -> str:
        return it.get("published") or ""

    # Stable sorts applied lowest-priority first; "" (no date) lands last in
    # the reverse sort, so undated items lose ties.
    for group in (scored, unscored):
        group.sort(key=lambda it: it["id"])
        group.sort(key=_pub, reverse=True)
        group.sort(key=_rel, reverse=True)
    scored.sort(key=lambda it: float(it["stage1_severity"]), reverse=True)

    return [it["id"] for it in (scored + unscored)[:k]]
