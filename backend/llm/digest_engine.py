"""Stage 1 of the two-stage scoring pipeline: digest every fetched article.

The scorer used to see only each article's title and first-240-word summary,
for at most 10 articles — the full bodies ``news_fetching`` had already
downloaded were thrown away at the prompt boundary. This module closes that
gap cheaply: a small model reads every article's full text and returns a
strict-JSON factual digest plus a 0-100 ``stage1_severity``. The scorer then
reasons over *every* digest and reads only the few highest-severity articles
in full.

Digests are cached in Postgres (``llm_artifact``, ``kind='digest'``) keyed by
``(country_iso2, as_of, url)`` with a sha256 of the digested text, so a
same-day re-run with unchanged articles makes ~zero stage-1 calls. The pilot
passes a content-addressed cache instead, which is what lets four overlapping
weekly snapshots share one digest of the same article.

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
import re
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


# Publisher furniture that arrives inside the body and is not about the country.
#
# Found by reading a payload dump, then by searching for its siblings -- which
# is the right order and not the one that finds everything. Counts over the
# 80,975 bodies on the corpus DB:
#
#     This article was amended/corrected on ...      2,405   2.97%
#     Support the Guardian ...                         494   0.61%
#     the letters block (Join the debate ... )          253   0.31%
#
# Two shapes, and they want different handling. The letters block and the
# amendment footer are *terminal* -- 98 to 168 characters of tail, always the
# last thing in the body -- so they are cut to the end. The fundraising line is
# inline, sometimes with eight thousand characters of article after it, so only
# the sentence goes.
#
# Deliberately NOT stripped: `Follow ...` (11.5% of bodies), `Read more` (2.7%)
# and `subscribe` (2.0%). Each is mostly ordinary prose -- "subscribers to the
# service", "Follow the money" -- and a pattern that eats article text to
# remove furniture is worse than the furniture.
#
# The amendment footer is the one that is not merely cosmetic. It carries a date
# *after* the article's own publication, because the Guardian Content API serves
# the current version of a piece rather than the version that was published; on
# PT 2019 six anchors were served a body whose footer postdates the anchor, two
# of them in the full-text block. See `deferred.md` and the `usable_body` item:
# removing the footer removes the symptom, not the mechanism.
# The separator is whitespace and an optional bullet -- never `\W`, which
# includes the full stop that ends the article's own last sentence. An earlier
# version used `[\s\W]{0,4}` and quietly took the period with the footer.
_SEP = r"\s*[•·|‧∙・–—-]?\s*"

_BOILERPLATE_TAIL = re.compile(
    rf"{_SEP}(?:Join the debate{_SEP}email guardian\.letters@theguardian\.com"
    r"|This article was (?:amended|corrected) on\s+\d)"
    r".*\Z",
    re.IGNORECASE | re.DOTALL)

# End-of-string is a terminator as well as a full stop: the fundraising line is
# sometimes the last thing in the body and carries no closing period, which is
# how the first version of this pattern left it in place on exactly the bodies
# where it was most visible.
#
# Case-sensitive, unlike the tail patterns, and that is the whole guard. The
# fundraising line is a sentence and starts with a capital; "the bank would
# support the Guardian angel programme" is a sentence about a country. An
# earlier version of this pattern ate the second one.
_BOILERPLATE_INLINE = re.compile(
    r"\s*Support the Guardian(?:'s|’s)?[^.]{0,160}(?:\.|\Z)")


def strip_publisher_boilerplate(text: str) -> str:
    """Remove publisher furniture from an article body.

    Runs at the read chokepoint rather than at harvest, so the stored corpus is
    untouched and the rule can change without a re-crawl. It does change the
    digest cache key -- `_content_sha` hashes this function's output -- so an
    affected article is re-digested once. Measured before it was written: 17 of
    1,040 selected article-slots on US 2019 and 20 of 1,051 on TR 2018, about
    1.8%.
    """
    if not text:
        return text
    text = _BOILERPLATE_TAIL.sub("", text)
    text = _BOILERPLATE_INLINE.sub("", text)
    return text.strip()


def article_input_text(item: Dict) -> str:
    """The fullest text we hold for an article — the stage-1/full-text input.

    The longer of ``content`` (fallback scraper) and ``text`` (trafilatura);
    if neither exists, ``summary``, then ``snippet``, then ``""``. Thin or
    empty text is still digestible — the digest prompt's "not stated" rule
    handles it.

    The one chokepoint both the digest input and the prompt's full-text block
    route through, which is why the boilerplate strip lives here rather than in
    each of them.

    Raises:
        TypeError: if ``item`` is not a dict.
    """
    if not isinstance(item, dict):
        raise TypeError(f"`item` must be a dict, got {type(item).__name__}")
    content = item.get("content") or ""
    text = item.get("text") or ""
    best = content if len(content) >= len(text) else text
    return strip_publisher_boilerplate(
        (best or item.get("summary") or item.get("snippet") or "").strip())


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


def _default_cache():
    """The content-addressed store, imported lazily to keep the layering.

    ``data_upsert.store`` imports this package's neighbours, so a module-level
    import closes a cycle. Same reason as the lazy import in ``_content_sha``.
    """
    try:
        from backend.data_upsert import store
        return store
    except Exception:  # noqa: BLE001 - no database is a cold cache, not an error
        return None


def digest_coverage(
    items: List[Dict],
    *,
    iso2: str,
    as_of: datetime.date,
    masked: bool = False,
    content_cache: Optional["ContentCache"] = None,
) -> List[str]:
    """The content hashes ``digest_articles`` would have to generate. Free.

    Both caches are consulted in the same order and by the same keys the real
    call uses, and nothing is written. An empty list means digesting these items
    costs nothing; anything else is the number of model calls it would buy.

    This exists because "will this be free" was being answered by a proxy.
    ``rebuild_snapshot`` compared the stored ``sweep_version`` against this
    tree's and proceeded when they matched — but the cache key is
    ``masked:{mask_map_version}:{sweep_version}``, so a gazetteer bump alone
    invalidates every masked digest while the sweep check reports clean. The row
    that surfaced it was stamped ``g3`` against a ``g5`` tree with no sweep
    recorded at all, so the guard passed it and a "free" rebuild would have
    bought twenty digests. Asking the cache is the only answer that cannot drift
    from the key.
    """
    if content_cache is None:
        content_cache = _default_cache()
    if content_cache is None:
        return [_content_sha(article_input_text(item), masked) for item in items]

    missing = [_content_sha(article_input_text(item), masked) for item in items]
    mode = "masked" if masked else "named"
    try:
        hits = content_cache.read_digest_cache(
            sorted(set(missing)), ai_client.digest_model(), mode)
    except Exception as exc:  # noqa: BLE001 - an unreadable cache is a full miss
        logger.warning("[%s] digest cache read failed (%s); assuming no coverage",
                       iso2, exc)
        hits = {}
    return [sha for sha in missing if sha not in hits]


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

    Checks the ``llm_artifact`` digest cache first (same url, same content hash →
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

    if content_cache is None:
        content_cache = _default_cache()

    urls: List[str] = [news_core.dedupe_key(it) for it in items]
    texts: List[str] = [article_input_text(it) for it in items]
    shas: List[str] = [_content_sha(text, masked) for text in texts]
    mode = "masked" if masked else "named"

    hits: Dict[str, Dict] = {}
    if content_cache is not None:
        try:
            hits = content_cache.read_digest_cache(
                sorted(set(shas)), ai_client.digest_model(), mode)
        except Exception as exc:  # noqa: BLE001 - an unreadable cache is a miss
            logger.warning("[%s] digest cache read failed (%s); digesting everything",
                           iso2, exc)

    pending_idx: List[int] = []
    cached = 0
    for i, it in enumerate(items):
        hit = hits.get(shas[i])
        if hit and isinstance(hit.get("digest"), dict):
            it["digest"] = hit["digest"]
            it["stage1_severity"] = _severity_or_none(hit.get("stage1_severity"))
            cached += 1
        else:
            it["digest"] = None
            it["stage1_severity"] = None
            pending_idx.append(i)
    from_content = 0

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
            structured_llm = ai_client.build_stage1_chat(api_key).with_structured_output(
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
                new_rows.append({
                    "content_sha256": shas[i],
                    "digest": res,
                    "stage1_severity": it["stage1_severity"],
                })
            if new_rows and content_cache is not None:
                try:
                    content_cache.write_digest_cache(
                        new_rows, ai_client.digest_model(), mode)
                except Exception as exc:  # noqa: BLE001 - a cache write is not the work
                    logger.warning("[%s] digest cache write failed: %s", iso2, exc)

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
