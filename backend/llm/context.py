"""Trailing context: what the last four quarters were like, as evidence.

The amnesia this fixes. A snapshot reads a 30-day window, which measures
evidence *flow* — how much was reported this month. Institutional decay is a
*stock*. A judiciary that lost its independence eighteen months ago is still
compromised, but nothing was published about it this month, so the payload
carries no trace and the model has nothing to score but the anchor's own
calibration language. Measured on US 2019 that produced nine distinct scores in
fifty-two weeks with a third of them on exactly 0.50.

So: one masked paragraph per calendar quarter, for the four fully-completed
quarters ending before the live window opens. It is **evidence, never a prior
score** — the paragraphs describe what happened, and the prompt is told to read
them for trajectory. Feeding a model its own earlier scores would make the
series autocorrelated by construction and there would be no way to tell that
from the country actually deteriorating.

Three properties this module exists to hold.

**No overlap.** Quarters end before ``as_of - SNAPSHOT_WINDOW_DAYS``, so a fact
is either in the live window or in context, never counted twice.

**No future.** Every source article is published before its quarter's end, which
is itself before the anchor. That is enforced by `snapshot_select.select` rather
than re-implemented here: it is handed the quarter as explicit ``bounds`` and
the quarter's end as its ``as_of``, so the body-vintage rule that already
refuses a capture younger than the anchor applies unchanged.

**Cache invalidation that tracks the evidence.** The key hashes the selected
article *set*, not ``(country, quarter)``. A quarter that has since been
re-harvested, or whose bodies were recovered, produces a different key and a
rebuilt paragraph. The alternative fails silently, because a quarter that has
gained twenty articles looks exactly like one that has not.
"""

import datetime
import hashlib
import json
import logging
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from langchain_core.messages import SystemMessage

from backend.llm import client as ai_client
from backend.llm import constants as ai_constants
from backend.llm import rewrite
from backend.util import config

logger = logging.getLogger(__name__)

# Bump when the prompt below, the quarter arithmetic, or the selection budget
# changes — anything that would make two paragraphs for the same quarter
# incomparable. It rides in the cache key beside the masking versions, so a
# masking change invalidates context for the same reason it invalidates digests.
CONTEXT_VERSION = "c1"

# How many completed quarters. Four is a year of trajectory: enough to see a
# direction, short enough that the oldest paragraph is still about roughly the
# present institutional order.
QUARTERS = 4

# The selection budget for one quarter. Deliberately larger than the snapshot's
# 20: that budget is sized for 30 days, and a quarter is three times the period.
# The per-theme floor still guarantees breadth across the six themes.
CONTEXT_MAX_ARTICLES = 40

_PROMPT = """You are summarising three months of reporting about {country} for an
analyst who has already read the last 30 days separately.

Write ONE paragraph, 60-90 words, describing what changed over the quarter for
this country's institutions, policy and operating conditions. Cover direction —
what deteriorated, what improved, what held steady. Concrete and factual: name
the mechanism and the magnitude where the sources give one.

Do not score, rank or rate anything. Do not use words like "risk", "low",
"moderate" or "high" as a judgement. You are describing evidence, not assessing
it.

{mask_rule}

ARTICLES
{articles}"""


def quarter_label(day: datetime.date) -> str:
    """``2018Q2``. The repo's spelling, as in ``indicator_series.period``."""
    return f"{day.year}Q{(day.month - 1) // 3 + 1}"


def quarter_bounds(label: str) -> Tuple[datetime.datetime, datetime.datetime]:
    """The ``[start, end)`` UTC bounds of a ``YYYYQn`` quarter."""
    year, quarter = int(label[:4]), int(label[5])
    start_month = (quarter - 1) * 3 + 1
    start = datetime.date(year, start_month, 1)
    end = (datetime.date(year + 1, 1, 1) if quarter == 4
           else datetime.date(year, start_month + 3, 1))
    midnight = datetime.time.min
    return (datetime.datetime.combine(start, midnight, tzinfo=datetime.timezone.utc),
            datetime.datetime.combine(end, midnight, tzinfo=datetime.timezone.utc))


def trailing_quarters(as_of: datetime.date,
                      count: int = QUARTERS) -> List[str]:
    """The last ``count`` completed quarters ending before the live window.

    A quarter qualifies only if it ended on or before ``as_of - 30 days``, so
    the newest context never overlaps the newest evidence. Returned oldest
    first, which is the order a trajectory reads in.
    """
    cutoff = as_of - datetime.timedelta(days=config.SNAPSHOT_WINDOW_DAYS)
    labels: List[str] = []
    # Walk back from the quarter containing the cutoff, keeping only quarters
    # whose end is at or before it. The quarter the cutoff falls inside is
    # partial by definition and is skipped.
    probe = datetime.date(cutoff.year, ((cutoff.month - 1) // 3) * 3 + 1, 1)
    while len(labels) < count:
        probe = (probe - datetime.timedelta(days=1))
        probe = datetime.date(probe.year, ((probe.month - 1) // 3) * 3 + 1, 1)
        label = quarter_label(probe)
        _, end = quarter_bounds(label)
        if end.date() <= cutoff:
            labels.append(label)
    return list(reversed(labels))


def cache_key(iso2: str, label: str, urls: Sequence[str]) -> str:
    """Identity of one quarter's paragraph: the country, the quarter, the set.

    The URL set is in the key because the paragraph is a function of it. A
    re-harvest that adds twenty articles to a quarter must not keep serving the
    paragraph written without them, and nothing else in the row would reveal
    that it had.
    """
    joined = "\n".join(sorted(urls))
    return hashlib.sha256(f"{iso2}:{label}:{joined}".encode("utf-8")).hexdigest()


def _paragraph(items: List[Dict[str, Any]], country_display: str,
               api_key: str, masked: bool) -> Optional[str]:
    """One quarter's paragraph from its selected articles, or None."""
    if not items:
        return None
    lines = []
    for it in items:
        title = (it.get("title") or "").strip()
        snippet = (it.get("text") or it.get("summary") or "")[:400].strip()
        published = str(it.get("published") or "")[:10]
        lines.append(f"- [{published}] {title}\n  {snippet}")
    prompt = _PROMPT.format(
        country=country_display,
        mask_rule=ai_constants.DIGEST_MASK_RULE if masked else "",
        articles="\n".join(lines),
    )
    try:
        chat = ai_client.build_digest_chat(api_key, max_tokens=400)
        reply = chat.invoke([SystemMessage(content=prompt)])
        text = (getattr(reply, "content", "") or "").strip()
    except Exception as exc:  # noqa: BLE001 - context is additive, never fatal
        logger.warning("[context] paragraph failed: %s", exc)
        return None
    return text or None


def build(iso2: str, as_of: datetime.date, *,
          masked: bool = True,
          cache: Optional[Any] = None,
          select=None) -> List[Dict[str, str]]:
    """The trailing-context block for one anchor: up to four labelled paragraphs.

    Additive by construction. Every failure path — no key, no articles, a model
    error, a mask leak — drops the quarter and returns what it has, because a
    missing paragraph costs some trajectory and a wrong one costs the snapshot.

    Args:
        cache: a store exposing ``read_context_cache`` / ``write_context_cache``.
            Omitted in tests; the daily run passes ``store``.
        select: injected for testing, defaults to `snapshot_select.select`.

    Returns:
        ``[{"quarter": "2018Q2", "summary": "..."}]``, oldest first. Empty when
        nothing could be built — the caller omits the block entirely rather than
        telling the model this country has no history.
    """
    if select is None:
        from backend.news_fetching import snapshot_select
        select = snapshot_select.select

    # Imported here, not at module scope: `langchain_llm` is the scoring path and
    # this module is one of its inputs, so a top-level import would close a cycle
    # the moment anything wires them the other way. Same reason `digest_engine`
    # defers its `store` import.
    from backend.llm.langchain_llm import MASKED_COUNTRY_LABEL

    mode = "masked" if masked else "named"
    version = f"{CONTEXT_VERSION}:{rewrite.gazetteer.MASK_MAP_VERSION}:{rewrite.SWEEP_VERSION}"
    country_display = (MASKED_COUNTRY_LABEL if masked
                       else config.country_name(iso2))

    plan = []
    for label in trailing_quarters(as_of):
        start, end = quarter_bounds(label)
        items = select(iso2, end.date(), CONTEXT_MAX_ARTICLES,
                       bounds=(start, end))
        if not items:
            continue
        urls = [it.get("link") or it.get("url") or "" for it in items]
        plan.append((label, items, cache_key(iso2, label, urls)))
    if not plan:
        return []

    hits: Dict[str, str] = {}
    if cache is not None:
        try:
            hits = cache.read_context_cache([k for _, _, k in plan], version, mode)
        except Exception as exc:  # noqa: BLE001 - an unreadable cache is a miss
            logger.warning("[%s] context cache read failed (%s); rebuilding", iso2, exc)

    api_key = os.getenv("OPENAI_API_KEY")
    out: List[Dict[str, str]] = []
    fresh: List[Dict[str, str]] = []
    for label, items, key in plan:
        text = hits.get(key)
        if not text:
            if not api_key:
                logger.warning("[%s] no OPENAI_API_KEY; %s context skipped", iso2, label)
                continue
            text = _paragraph(items, country_display, api_key, masked)
            if not text:
                continue
            if masked:
                # Swept like a digest, and through the same call, so context and
                # digests cannot drift apart in what they consider masked. The
                # paragraph rides in `what_happened` rather than a new field:
                # adding one to `_DIGEST_SWEEP_FIELDS` would move SWEEP_VERSION,
                # invalidating every cached masked digest and tripping the
                # version freeze, to rename a key the prompt never reads.
                swept = rewrite.sweep_digest({"what_happened": text}, api_key)
                if swept and (swept.get("what_happened") or "").strip():
                    text = swept["what_happened"].strip()
                try:
                    # Fail at generation rather than at scoring. The scoring gate
                    # would catch it too, but there it costs the snapshot.
                    rewrite.assert_clean(text)
                except rewrite.MaskLeak as exc:
                    logger.error("[%s] %s context leaked and was dropped: %s",
                                 iso2, label, exc)
                    continue
            fresh.append({"key": key, "text": text})
        out.append({"quarter": label, "summary": text})

    if fresh and cache is not None:
        try:
            cache.write_context_cache(fresh, version, mode)
        except Exception as exc:  # noqa: BLE001 - a cache write is not the work
            logger.warning("[%s] context cache write failed: %s", iso2, exc)

    logger.info("[%s] trailing context: %d quarter(s), %d fresh",
                iso2, len(out), len(fresh))
    return out
