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
from typing import Dict, List, Optional, Protocol, Sequence

from langchain_core.messages import SystemMessage

import backend.llm.constants as ai_constants
from backend.llm import client as ai_client
from backend.data_upsert import data_push
from backend.news_fetching import core as news_core

logger = logging.getLogger(__name__)

# Where `masking.rewrite.sweep_digest` parks the swept headline. Named here so
# this module does not import the masking package just to read one key.
_SWEPT_TITLE_KEY = "masked_title"

# Marks a digest built from a deliberately shortened article. Present only on
# those, so an ordinary digest's bytes — and therefore its prompt hash — are
# exactly what they were before the retry existed.
DIGEST_SOURCE_KEY = "digest_source"


class ContentCache(Protocol):
    """A digest cache keyed on content rather than on ``(country, as_of, url)``.

    Structural only — ``history.store`` satisfies it by having the two functions,
    with no import in either direction.
    """

    def read_digest_cache(self, hashes: Sequence[str], digest_model: str,
                          mode: str) -> Dict[str, Dict]: ...

    def write_digest_cache(self, rows: Sequence[Dict], digest_model: str,
                           mode: str) -> int: ...

# Stage-1 calls in flight at once. High enough to keep a 20-article country
# fast, low enough to stay clear of per-minute rate limits.
_MAX_CONCURRENCY = 8

# How much of an article the runaway-recovery retry sends. Long enough that the
# digest is still about the article, short enough to be a materially different
# prompt from the one that looped — a plain retry at temperature 0 with a fixed
# seed reproduces the loop exactly.
_RETRY_INPUT_CHARS = 6000


def _ran_away(result: object) -> bool:
    """Whether a stage-1 result is the output-ceiling loop rather than a real error.

    Matched on the message because that is all the exception carries: LangChain
    surfaces OpenAI's length-limit refusal as a generic parse failure, and the
    only thing distinguishing "the model looped forever" from "the API was down"
    is the sentence it came back with. A network failure must not be retried on
    truncated input — it must simply fail — so this stays narrow.
    """
    return isinstance(result, Exception) and "length limit" in str(result).lower()


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


def _content_sha(text: str, masked: bool) -> str:
    """The cache key for one article's digest, mode and masking version included.

    The mode has to be in here. The cache is keyed on (country, as_of, url) and
    validated against this hash, so without it the same article text digested
    under the two modes collides — and a *named* digest served to a masked run
    puts the president's name straight into the prompt with every gate reporting
    clean, because the gate scans for country names and a person is not one.

    So does the masking version, and for a subtler reason. The hashed text is
    the *input* to the digest; the sweep that strips the names the digest model
    wrote anyway runs on its *output*, and only on freshly generated digests —
    never on a cache hit. So a change to the gazetteer or to the sweep prompt
    leaves this hash exactly where it was, and the cache happily serves digests
    produced by the previous behaviour for as long as they live. That is not
    hypothetical: it is what `84c5b9f` and `1fab1b1` would have done to the
    pilot, which is a ten-year series in which half the rows were masked one way
    and half another, with nothing on any row to tell them apart.

    Prefixed rather than mixed in, so an existing *named* row keeps its current
    hash — the sweep is a masked-mode stage and nothing about named digests
    changed.
    """
    if masked:
        # Imported here rather than at module scope: `masking.rewrite` imports
        # this module's neighbours, and a top-level import closes the cycle.
        # Same reason as the lazy import in `digest_articles`.
        from backend.llm import gazetteer, rewrite
        prefix = f"masked:{gazetteer.MASK_MAP_VERSION}:{rewrite.SWEEP_VERSION}\n"
    else:
        prefix = ""
    return hashlib.sha256((prefix + text).encode("utf-8")).hexdigest()


def digest_articles(
    items: List[Dict],
    *,
    country_display: str,
    iso2: str,
    as_of: datetime.date,
    masked: bool = False,
    content_cache: Optional["ContentCache"] = None,
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
        masked: run the digest prompt in mask mode, so names the gazetteer
            cannot know — people, parties, companies, named events — come back
            as the roles they play. The input text is already masked; this is
            about what the *model writes*, and without it `actors` reads "who
            did what to whom" as an instruction to name them. It also enters
            the cache key, because the same article text digested under the two
            modes produces two different digests and only one of them is safe
            to send.
        content_cache: an optional second cache keyed on the *content hash*
            rather than on ``(country, as_of, url)``, consulted before the model
            and written after it.

            The daily run passes None and behaves exactly as before. A backfill
            passes one because its anchors overlap: weekly anchors with a 30-day
            window put the same article in about four consecutive snapshots, and
            keyed on ``as_of`` each of those is a miss, so the pilot would pay
            to digest every article four times over for digests that are
            identical by construction.

            Kept as a parameter rather than an import so this module does not
            reach into the backfill package — the same layering the masking
            package was moved out of ``history`` to preserve.

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
        url = news_core.dedupe_key(it)
        urls.append(url)
        texts.append(text)

        sha = _content_sha(text, masked)
        hit = cache.get(url) if url else None
        if hit and hit.get("content_sha256") == sha and isinstance(hit.get("digest"), dict):
            it["digest"] = hit["digest"]
            it["stage1_severity"] = _severity_or_none(hit.get("stage1_severity"))
            cached += 1
        else:
            it["digest"] = None
            it["stage1_severity"] = None
            pending_idx.append(i)

    # Second chance for the misses: the same article in last week's snapshot has
    # a different `as_of` and so missed above, but its digest is byte-identical.
    from_content = 0
    if pending_idx and content_cache is not None:
        mode = "masked" if masked else "named"
        shas = {i: _content_sha(texts[i], masked) for i in pending_idx}
        try:
            hits = content_cache.read_digest_cache(
                sorted(set(shas.values())), ai_client.DIGEST_MODEL_NAME, mode)
        except Exception as exc:
            logger.warning("[%s] content digest cache read failed (%s); "
                           "digesting everything", iso2, exc)
            hits = {}
        still_pending = []
        for i in pending_idx:
            hit = hits.get(shas[i])
            if hit and isinstance(hit.get("digest"), dict):
                items[i]["digest"] = hit["digest"]
                items[i]["stage1_severity"] = _severity_or_none(hit.get("stage1_severity"))
                from_content += 1
            else:
                still_pending.append(i)
        pending_idx = still_pending

    ok = 0
    swept = 0
    if pending_idx:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.warning("[%s] OPENAI_API_KEY not set; %d articles left undigested", iso2, len(pending_idx))
        else:
            prompts = [
                ai_constants.DIGEST_PROMPT.format(
                    country=country_display, article_text=texts[i],
                    mask_rule=ai_constants.DIGEST_MASK_RULE if masked else "")
                for i in pending_idx
            ]
            structured_llm = ai_client.build_digest_chat(api_key).with_structured_output(
                schema=ai_constants.DIGEST_SCHEMA, strict=True
            )
            def _batch(prompt_list):
                try:
                    return structured_llm.batch(
                        [[SystemMessage(content=p)] for p in prompt_list],
                        config={"max_concurrency": _MAX_CONCURRENCY},
                        return_exceptions=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[%s] stage-1 batch failed outright: %s", iso2, exc)
                    return [exc] * len(prompt_list)

            results = _batch(prompts)

            # One retry, on a shorter article, for the failures that ran away.
            #
            # The stage-1 model sometimes loops to its output ceiling — seven
            # times in one twenty-bundle probe run, always at exactly
            # `completion_tokens=16384`, on prompts of only three to five
            # thousand. The article is not lost to an outage; it is lost to a
            # generation that never terminated, and it reaches the scorer as a
            # truncated body instead of a digest.
            #
            # A plain retry would not help: temperature is 0 and the seed is
            # fixed, so the same call produces the same loop. Truncating the
            # input makes it a *different* call, and a shorter article is far
            # less likely to induce the loop in the first place. Once only —
            # this is a recovery pass, not a retry policy.
            retry_idx = [n for n, res in enumerate(results) if _ran_away(res)]
            if retry_idx:
                logger.warning("[%s] %d digest(s) hit the output ceiling; "
                               "retrying on truncated text", iso2, len(retry_idx))
                retry = _batch([
                    ai_constants.DIGEST_PROMPT.format(
                        country=country_display,
                        article_text=texts[pending_idx[n]][:_RETRY_INPUT_CHARS],
                        mask_rule=ai_constants.DIGEST_MASK_RULE if masked else "")
                    for n in retry_idx
                ])
                recovered = 0
                for n, res in zip(retry_idx, retry):
                    if isinstance(res, dict):
                        # A recovered digest is a digest of a *truncated*
                        # article, not of the article. Stamped so a snapshot
                        # cannot record it as a clean one: a recovery that
                        # silently changes what the evidence is would be the
                        # same class of bug as everything else this branch found.
                        #
                        # Set inside the digest so it survives into the cache and
                        # marks every later snapshot that reuses it. Absent on a
                        # normal digest rather than false, so ordinary prompts
                        # keep their exact bytes and stay comparable to rows
                        # written before this existed.
                        res[DIGEST_SOURCE_KEY] = "truncated-retry"
                        results[n] = res
                        recovered += 1
                logger.info("[%s] recovered %d/%d runaway digest(s) from "
                            "truncated text", iso2, recovered, len(retry_idx))

            # Results come back in input order — map by index, never by
            # completion order, so output order stays deterministic.
            new_rows: List[Dict] = []
            for i, res in zip(pending_idx, results):
                it = items[i]
                if isinstance(res, Exception) or not isinstance(res, dict):
                    logger.warning("[%s] digest failed for %s: %s", iso2, it.get("id"), res)
                    continue
                if masked:
                    # The digest prompt asked for roles and the digest model
                    # wrote names anyway — which is what the probe kept quoting
                    # back. Swept here, before caching, so it is paid once per
                    # article rather than once per snapshot that reuses it.
                    #
                    # The headline rides along in the same call: it is sent for
                    # every article, digest or not, and had only ever been
                    # gazetteer-masked.
                    from backend.llm import rewrite as _rewrite
                    clean = _rewrite.sweep_digest(res, api_key,
                                                  title=str(it.get("title") or ""))
                    if clean is not None:
                        res, swept = clean, swept + 1
                it["digest"] = res
                it["stage1_severity"] = _severity_or_none(res.get("stage1_severity"))
                ok += 1
                if urls[i]:  # no url → nothing to key the cache row on
                    new_rows.append({
                        "country_iso2": iso2,
                        "as_of": as_of,
                        "url": urls[i],
                        "published_at": it.get("published"),
                        "content_sha256": _content_sha(texts[i], masked),
                        "digest": res,
                        "stage1_severity": it["stage1_severity"],
                        "model_id": ai_client.DIGEST_MODEL_NAME,
                    })
            if new_rows:
                try:
                    data_push.upsert_article_digests(new_rows)
                except Exception as exc:
                    logger.warning("[%s] digest cache write failed: %s", iso2, exc)
                if content_cache is not None:
                    try:
                        content_cache.write_digest_cache(
                            new_rows, ai_client.DIGEST_MODEL_NAME,
                            "masked" if masked else "named")
                    except Exception as exc:
                        logger.warning("[%s] content digest cache write failed: %s",
                                       iso2, exc)

    # Apply the swept headline however the digest arrived — fresh, per-day cache
    # or content cache. Doing it here rather than at each of the three sites is
    # what stops a cache hit from serving a masked digest beside a named title.
    if masked:
        for it in items:
            digest = it.get("digest")
            if isinstance(digest, dict) and digest.get(_SWEPT_TITLE_KEY):
                it["title"] = digest[_SWEPT_TITLE_KEY]

    failed = len(items) - ok - cached - from_content
    logger.info("[%s] digests: ok=%d cached=%d content-cached=%d failed=%d%s",
                iso2, ok, cached, from_content, failed,
                f" swept={swept}" if masked and pending_idx else "")
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
