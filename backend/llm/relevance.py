"""
The relevance gate: does this article bear on country risk at all?

Every candidate is classified once, per country, before anything expensive
touches it. A cheap model reads the article and answers one question, and only
articles that pass are eligible to be scored.

The question is not "is this about the country". It is:

    does this article tell you something about the condition or direction of
    THIS country's institutions, economy or social order, or does it tell you
    that one thing happened?

The distinction is the whole instrument. A country risk score built from
incidents moves with whatever was in the news that week; one built from
structural reporting moves when the country does.

Three properties this file is designed around:

**The country is in the cache key.** A German election story that mentions
Portugal once is structural for Germany and irrelevant for Portugal. The same
text therefore has two answers, so the hash covers the text *and* the ISO-2.

**The version is the prompt's own hash.** Editing the prompt and forgetting to
bump a version number is how a cache serves answers to a question nobody is
asking any more. `RELEVANCE_PROMPT_VERSION` cannot drift from the prompt because
it is computed from it.

**Rejections are stored.** Without the record, nobody can ask later whether the
gate judged well, or re-run selection under a different rule on the same week.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence

from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI

from backend.data_upsert import store
from backend.util import usage
from backend.util.hashing import content_hash

logger = logging.getLogger(__name__)

__all__ = [
    "RELEVANCE_PROMPT",
    "RELEVANCE_SCHEMA",
    "RELEVANCE_PROMPT_VERSION",
    "LEDGERS",
    "LABELS",
    "DEFAULT_MODEL",
    "ARTICLE_BUDGET",
    "FULL_TEXT_K",
    "article_input_text",
    "relevance_key",
    "classify",
    "select",
    "duplicate_story_report",
]


# The four ledgers the score is built from. `security` is a retrieval theme, not
# a ledger: conflict reaches the score through `order`.
LEDGERS = ("friction", "order", "information", "edge")

LABELS = ("structural", "incident", "irrelevant")

# Measured, not assumed — see `backend/llm/gate_bakeoff.py`. Dated id: an alias
# moves under you, and a gate that silently changes model silently changes the
# evidence behind every score.
DEFAULT_MODEL = "gpt-4o-mini-2024-07-18"

ARTICLE_BUDGET = 20
FULL_TEXT_K = 3

# A few times the schema's real size. The schema is a label, a one-or-two
# sentence reason and two small fields — a few hundred tokens at most. Leaving
# the ceiling at the model's default lets a degenerate input burn thousands of
# tokens producing nothing usable.
MAX_OUTPUT_TOKENS = 400

# How much of an article the gate reads. The judgement is structural-versus-
# incident, which the opening of a piece almost always settles; paying to send
# the whole body to a classifier that is about to reject most of them is the
# cost this gate exists to avoid.
GATE_INPUT_CHARS = 4000


RELEVANCE_PROMPT = """You classify news articles for a country-risk instrument.

For the country named below, answer one question about the article:

  Does this article tell you something about the CONDITION or DIRECTION of this
  country's institutions, economy, or social order — or does it only tell you
  that one thing happened?

Reporting that describes a condition or a trend is `structural`. Reporting of a
single occurrence, however dramatic, is `incident`. Anything that bears on
country risk not at all is `irrelevant`.

THE LINE, WITH THE CASES THAT DRAW IT

- A school shooting is an incident: it fails. An analysis of mass shootings
  rising over a decade, adjusted for population, is structural: it passes.
- One corruption arrest is an incident. Prosecutions collapsing, or an
  anti-corruption body being defunded, is structural.
- One factory closing is an incident. A sector shedding employment across
  quarters is structural.
- A single event passes only when it is itself structural: a coup, a currency
  collapse, a government falling, a sovereign default, a state of emergency.
- Sport, celebrity, entertainment, crime reporting without institutional
  analysis, weather, human interest and obituaries fail regardless of how
  prominent they are.
- An article about ANOTHER country that merely mentions this one is
  `irrelevant` for this country. A German election story that names Portugal
  once is structural for Germany and irrelevant for Portugal. Judge only the
  country named below.

WHAT TO RETURN

- `label`: one of `structural`, `incident`, `irrelevant`.
- `reason`: one or two sentences naming what in the article decided it, so a
  wrong call can be understood later. Do not restate the headline.
- `ledgers`: which of these the article bears on, possibly none:
    - `friction`   — taxes, fiscal position, inflation, currency, corruption,
                     regulation: what is taken and how well it converts.
    - `order`      — government stability, elections, rule of law, courts,
                     protest, conflict, security: doubt about the load-bearing
                     rules.
    - `information`— press freedom, transparency, official statistics, audit,
                     digital government: whether the country's own instruments
                     can be trusted.
    - `edge`       — business formation, investment, education, skills,
                     research, skilled migration: whether the system is learning.
  Return an empty list if the article is structural but fits none of them, and
  for anything you labelled `incident` or `irrelevant`.
- `is_structural_event`: true ONLY for the coup-or-collapse case above — a
  single event that is itself a change in the country's condition. Never true
  for ordinary trend reporting, and never true for an `incident`.

Judge only from the text supplied. If the text is too thin to tell, say so in
`reason` and label it `irrelevant` rather than guessing."""


RELEVANCE_SCHEMA: Dict[str, Any] = {
    "name": "article_relevance",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "label": {"type": "string", "enum": list(LABELS)},
            "reason": {"type": "string"},
            "ledgers": {
                "type": "array",
                "items": {"type": "string", "enum": list(LEDGERS)},
            },
            "is_structural_event": {"type": "boolean"},
        },
        "required": ["label", "reason", "ledgers", "is_structural_event"],
    },
    "strict": True,
}


# Derived, never written down. See the module docstring.
RELEVANCE_PROMPT_VERSION = content_hash(RELEVANCE_PROMPT)


def article_input_text(article: Dict[str, Any]) -> str:
    """Return the text the gate reads for one article.

    The publisher is included deliberately: "Reuters" and "SAPO Desporto" are
    evidence about what kind of piece this is, and the classifier is allowed to
    use it. Everything else is the article's own words.
    """
    parts = [
        (article.get("title") or "").strip(),
        (article.get("source") or "").strip(),
        ((article.get("text") or article.get("summary") or article.get("snippet") or "")
         .strip()[:GATE_INPUT_CHARS]),
    ]
    return "\n\n".join(p for p in parts if p)


def relevance_key(article: Dict[str, Any], iso2: str) -> str:
    """Return the cache key for (this article, this country).

    The country is part of the key because the same text has a different answer
    for a different country. Leaving it out would let a story that is structural
    for Germany be served as structural for Portugal.
    """
    return content_hash(f"{iso2}\n\n{article_input_text(article)}")


def _client(model: str, api_key: Optional[str], seed: int) -> Any:
    return ChatOpenAI(
        model=model,
        temperature=0.0,
        seed=seed,
        max_retries=0,
        max_tokens=MAX_OUTPUT_TOKENS,
        api_key=api_key,
    )


def classify(
    articles: Sequence[Dict[str, Any]],
    country_name: str,
    iso2: str,
    *,
    model: str = DEFAULT_MODEL,
    api_key: Optional[str] = None,
    seed: int = 42,
    meter: Optional[usage.Meter] = None,
    use_cache: bool = True,
    mode: str = "named",
) -> Dict[str, Dict[str, Any]]:
    """Classify every candidate, one call per uncached (article, country).

    Args:
        articles: The candidate pool from `news_fetching.core.fetch_candidates`.
        country_name: Display name, put to the model.
        iso2: Roster code, part of the cache key.
        model: Dated model id.
        meter: Records real token usage if supplied.
        use_cache: False re-asks the model for everything, for the bake-off.
        mode: 'named' or 'masked'; part of the cache key.

    Returns:
        ``{relevance_key: {label, reason, ledgers, is_structural_event, cached}}``.
        An article the model failed on is absent, and is treated downstream as
        ineligible rather than as passing.
    """
    if not articles:
        return {}

    keys = {id(a): relevance_key(a, iso2) for a in articles}

    cached: Dict[str, Any] = {}
    if use_cache:
        try:
            cached = store.read_artifacts(
                list(keys.values()),
                kind="relevance",
                version=RELEVANCE_PROMPT_VERSION,
                mode=mode,
            )
        except Exception as e:  # a cache that is down must not stop the run
            logger.warning("relevance cache unavailable, classifying all: %s", e)
            cached = {}

    out: Dict[str, Dict[str, Any]] = {}
    for key, payload in cached.items():
        out[key] = {**payload, "cached": True}

    todo = [a for a in articles if keys[id(a)] not in out]
    if not todo:
        return out

    api_key = api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY not set; the relevance gate cannot run.")
        return out

    llm = _client(model, api_key, seed)
    structured = llm.with_structured_output(
        schema=RELEVANCE_SCHEMA, strict=True, include_raw=True
    )

    fresh: List[Any] = []
    for article in todo:
        key = keys[id(article)]
        prompt = (
            f"{RELEVANCE_PROMPT}\n\n"
            f"COUNTRY: {country_name}\n\n"
            f"ARTICLE:\n{article_input_text(article)}"
        )
        try:
            response = structured.invoke([SystemMessage(content=prompt)])
        except Exception as e:
            logger.warning("relevance call failed for %s: %s", article.get("title"), e)
            continue

        if meter is not None:
            meter.add_response(model, response)

        parsed = response.get("parsed") if isinstance(response, dict) else response
        if not isinstance(parsed, dict) or parsed.get("label") not in LABELS:
            logger.warning("relevance returned an unusable answer for %s", article.get("title"))
            continue

        # An `incident` that claims to be a structural event is contradicting
        # itself; trust the label, which is the field the gate acts on.
        if parsed.get("label") != "structural":
            parsed["is_structural_event"] = False
            parsed["ledgers"] = []

        out[key] = {**parsed, "cached": False}
        fresh.append((key, parsed))

    if fresh and use_cache:
        try:
            store.write_artifacts(
                fresh,
                kind="relevance",
                version=RELEVANCE_PROMPT_VERSION,
                model=model,
                mode=mode,
            )
        except Exception as e:
            logger.warning("could not cache relevance labels: %s", e)

    return out


# --- Selection --------------------------------------------------------------


def _published_sort_key(article: Dict[str, Any]) -> str:
    """Newest first. Missing dates sort last, not first."""
    return (article.get("page_published_at") or article.get("published") or "")


def select(
    articles: Sequence[Dict[str, Any]],
    labels: Dict[str, Dict[str, Any]],
    iso2: str,
    *,
    budget: int = ARTICLE_BUDGET,
) -> Dict[str, Any]:
    """Choose what the scorer reads. Only `structural` articles are eligible.

    **Nothing tops up from ineligible articles.** If six qualify, six are
    scored. Per-theme counts are reported; they are not floors, and no article
    is admitted to meet one. A zero means no relevant coverage was found, which
    is a fact about the week and not a gap to be filled.

    The order:

    1. **Structural events first, always** — a coup or a default is read before
       anything else, and so takes the full-text slots.
    2. **Then round-robin across the ledgers each article bears on**, newest
       first within each ledger, until the budget fills. Twenty newest articles
       on a busy country can be eight versions of one story; interleaving by
       ledger spreads the budget without ever admitting something ineligible.
       A ledger with nothing eligible is simply skipped, and its count says zero.
    3. **Prefer a publisher not already selected** when two candidates tie, so
       one outlet cannot take the whole budget.
    4. **Tie-break on URL**, so the order is deterministic for a given eligible
       set.

    Articles that are structural but bear on no ledger are not discarded; they
    are taken after the ledgered ones, in the same order.

    Returns:
        ``{"selected", "eligible", "rejected", "per_theme", "per_ledger", "counts"}``.
    """
    eligible: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    for article in articles:
        verdict = labels.get(relevance_key(article, iso2))
        if verdict and verdict.get("label") == "structural":
            enriched = dict(article)
            enriched["relevance"] = verdict
            eligible.append(enriched)
        else:
            rejected.append({
                "url": article.get("publisher_link") or article.get("link"),
                "title": article.get("title"),
                "publisher": article.get("source"),
                "themes": article.get("themes", []),
                # An article the model never answered on is recorded as such,
                # rather than as a judgement the model did not make.
                "label": (verdict or {}).get("label", "unclassified"),
                "reason": (verdict or {}).get("reason", "no answer from the gate"),
            })

    def order_key(a: Dict[str, Any]):
        return (_published_sort_key(a), a.get("publisher_link") or a.get("link") or "")

    events = sorted(
        [a for a in eligible if a["relevance"].get("is_structural_event")],
        key=order_key,
        reverse=True,
    )
    rest = [a for a in eligible if not a["relevance"].get("is_structural_event")]

    buckets: Dict[str, List[Dict[str, Any]]] = {led: [] for led in LEDGERS}
    buckets["(none)"] = []
    for a in sorted(rest, key=order_key, reverse=True):
        led = a["relevance"].get("ledgers") or []
        if not led:
            buckets["(none)"].append(a)
        for name in led:
            if name in buckets:
                buckets[name].append(a)

    selected: List[Dict[str, Any]] = []
    taken_urls = set()
    publishers_used: Dict[str, int] = {}

    def take(article: Dict[str, Any]) -> bool:
        url = article.get("publisher_link") or article.get("link") or ""
        if url in taken_urls:
            return False
        taken_urls.add(url)
        pub = (article.get("source") or "").strip()
        publishers_used[pub] = publishers_used.get(pub, 0) + 1
        selected.append(article)
        return True

    for article in events:
        if len(selected) >= budget:
            break
        take(article)

    def next_from(bucket: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """First article in `bucket` not already taken, preferring an unused
        publisher so one outlet cannot take the whole budget."""
        fallback = None
        for cand in bucket:
            url = cand.get("publisher_link") or cand.get("link") or ""
            if url in taken_urls:
                continue
            pub = (cand.get("source") or "").strip()
            if publishers_used.get(pub, 0) == 0:
                return cand
            if fallback is None:
                fallback = cand
        return fallback

    # Round-robin: one article from each ledger per round, in a fixed order, so
    # a busy ledger cannot crowd out a quiet one. A ledger with nothing eligible
    # is skipped and its count says zero.
    while len(selected) < budget:
        progressed = False
        for name in LEDGERS:
            if len(selected) >= budget:
                break
            chosen = next_from(buckets[name])
            if chosen is not None and take(chosen):
                progressed = True
        if not progressed:
            break

    # Structural articles that bear on no ledger are eligible and must not be
    # silently dropped; they follow the ledgered ones.
    for article in buckets["(none)"]:
        if len(selected) >= budget:
            break
        take(article)

    per_theme: Dict[str, int] = {}
    for a in selected:
        for t in a.get("themes", []):
            per_theme[t] = per_theme.get(t, 0) + 1

    per_ledger: Dict[str, int] = {led: 0 for led in LEDGERS}
    for a in selected:
        for led in a["relevance"].get("ledgers") or []:
            if led in per_ledger:
                per_ledger[led] += 1

    label_counts: Dict[str, int] = {}
    for r in rejected:
        label_counts[r["label"]] = label_counts.get(r["label"], 0) + 1

    return {
        "selected": selected,
        "eligible": eligible,
        "rejected": rejected,
        "per_theme": per_theme,
        "per_ledger": per_ledger,
        "counts": {
            "candidates": len(articles),
            "classified": len(labels),
            "eligible": len(eligible),
            "selected": len(selected),
            "budget": budget,
            "rejected_by_label": label_counts,
        },
    }


# --- Reported, not fixed ----------------------------------------------------


def duplicate_story_report(selected: Sequence[Dict[str, Any]], *, threshold: float = 0.6):
    """Report how often the selected set holds one story from several outlets.

    Publisher-link dedupe catches identical pages, not the same story rewritten
    five times. This measures the residue rather than acting on it: if it turns
    out to be common, that is the next selection problem, and a number is better
    than a guess.

    Returns:
        ``{"clusters": [[title, ...], ...], "duplicated_slots": int}`` — clusters
        of two or more, and how many budget slots they cost beyond one each.
    """
    def tokens(a):
        return {w for w in (a.get("title") or "").lower().split() if len(w) > 3}

    items = list(selected)
    seen = set()
    clusters = []
    for i, a in enumerate(items):
        if i in seen:
            continue
        group = [i]
        ta = tokens(a)
        if not ta:
            continue
        for j in range(i + 1, len(items)):
            if j in seen:
                continue
            tb = tokens(items[j])
            if not tb:
                continue
            overlap = len(ta & tb) / len(ta | tb)
            if overlap >= threshold:
                group.append(j)
        if len(group) > 1:
            seen.update(group)
            clusters.append([items[k].get("title") for k in group])

    return {
        "clusters": clusters,
        "duplicated_slots": sum(len(c) - 1 for c in clusters),
    }
