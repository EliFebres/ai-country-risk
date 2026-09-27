"""
The relevance gate: does this article bear on country risk at all?

Every candidate is classified once, per country, before anything expensive
touches it. A cheap model reads the article and answers one question, and only
articles that pass are eligible to be scored.

The question is not "is this about the country". It is:

    is this article material to the risk of holding investments exposed to
    THIS country?

That is Eli's definition, and his blind labels are what the gate is measured
against. An earlier three-label prompt asked whether an article described a
condition or only an event, and agreed with him on 7 of 30 (deferred.md §4).

Three properties this file is designed around:

**The country is in the cache key.** A German election story that mentions
Portugal once is relevant for Germany and irrelevant for Portugal. The same
text therefore has two answers, so the hash covers the text *and* the ISO-2.

**The version is the prompt's own hash.** Editing the prompt and forgetting to
bump a version number is how a cache serves answers to a question nobody is
asking any more. `RELEVANCE_PROMPT_VERSION` cannot drift from the prompt because
it is computed from it. The cache is keyed on that version, the model and the
input mode together, so none of the three can change without a miss.

**Rejections are stored.** Without the record, nobody can ask later whether the
gate judged well, or re-run selection under a different rule on the same week.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Sequence

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

LABELS = ("relevant", "irrelevant")

# What the gate says an article touches. `order_security` is the `order` ledger
# under the name the prompt uses. `edge` has no gate area, because research,
# fellowships and university news are defined as irrelevant.
RISK_AREAS = ("friction", "order_security", "information", "none")
BEARINGS = ("direct", "spillover", "none")
LEDGER_OF_AREA = {"friction": "friction", "order_security": "order", "information": "information"}

# What the gate reads. `snippet` is the title, the outlet and the feed's
# description. On Google News the description is the outlet's name again, so
# `snippet` is in practice the headline. `body` is the title, the outlet and the
# opening of the extracted body. Until 2026-09-27 the gate read the title, the
# outlet and 4,000 body characters whenever a body had been fetched, which is
# neither mode.
INPUT_MODES = ("snippet", "body")
DEFAULT_INPUT_MODE = "body"
BODY_INPUT_CHARS = 1500

# Measured, not assumed. `gate_bakeoff.py` on 2026-09-22 measured stability for
# the old three-label prompt (gpt-4o-mini 98%, 4.1-nano 98%, 4.1-mini 96%). Eli's
# blind labels on 2026-09-27 then showed that prompt agreed with him on 7 of 30,
# and that no model fixed it (deferred.md §4). The binary prompt below is
# measured on a held-out draw before any model or mode is adopted (§15).
#
# Dated id: an alias moves under you, and a gate that silently changes model
# silently changes the evidence behind every score.
DEFAULT_MODEL = "gpt-4o-mini-2024-07-18"

ARTICLE_BUDGET = 20
FULL_TEXT_K = 3

# A few times the schema's real size: five short fields and a one-sentence
# reason. Leaving the ceiling at the model's default lets a degenerate input burn
# thousands of tokens producing nothing usable.
MAX_OUTPUT_TOKENS = 400


# The worked examples are ten of Eli's blind-labelled thirty (v2 draw), collapsed
# to binary. The other twenty are the dev set the prompt was tuned against, and
# none of the thirty may be used to report the gate's accuracy.
RELEVANCE_PROMPT = """You screen news for a sovereign-risk analyst covering {country}. The analyst scores how risky it is to hold investments exposed to {country}. Decide whether this article is MATERIAL to that risk.

RELEVANT: the article bears directly on {country} through at least one of these.
- Friction: the cost and difficulty of doing business. Taxes, customs, permits, regulation, courts and contract enforcement, corruption, capital controls, expropriation, sanctions on or by {country}.
- Order and security: government stability, elections and their conduct, coups, protests, unrest, armed conflict, terrorism, violence against officials, candidates or journalists, the central bank, inflation, currency, debt, default, IMF programmes, and the state of the economy as a whole. Also the treaties, alliances and defence or trade agreements {country}'s government enters into.
- Information: whether you can trust what you know about {country}. Press freedom, censorship, the integrity of the statistics office, audits, judicial independence.

A single event counts if its consequences for {country} are high: an attack on its territory, a coup attempt, a sovereign default, the killing of a candidate.

A story about another country counts only if it names a direct consequence for {country}: a neighbour's war reaching {country}'s border, a partner's sanctions hitting {country}'s exports. {country} mentioned in passing is not enough.

IRRELEVANT: sport, celebrity, entertainment, lifestyle, travel, weather with no economic consequence, obituaries, individual crimes and court cases about one person with no institutional angle, a government's handling of one person's or one small group's case (an extradition, a visa, an evacuation) that changes no rule, company news with no bearing on {country}'s economy or policy, research papers, fellowships and university announcements, guides and explainers that report no change, and articles where {country} is mentioned in passing.

When unsure, ask: would a sovereign-risk analyst covering {country} want this in front of them this week?

HOW TO ANSWER

Fill the fields in order, and decide each before the next:
- subject_country: the country the article is mainly about.
- bearing_on_target: `direct` if the article is about {country}; `spillover` if it is about another country and names a direct consequence for {country}; `none` otherwise.
- risk_area: the one area above it bears on most, or `none`.
- one_person_case: true if the story is one individual's case (a crime, an arrest, a trial) with no institutional consequence.
- reason: one sentence naming what in the article decided it. Do not restate the headline.
- label: `relevant` or `irrelevant`. A `none` bearing, a `none` risk area or a true one_person_case is `irrelevant`.

Judge only from the text supplied. If it is too thin to tell, say so in the reason and answer `irrelevant`.

WORKED EXAMPLES

Country: Peru
Article: Shot dead the journalist and regional candidate Susy Aponte Polo during a rally in Peru - Demócrata. "The journalist and aspiring regional governor Susy Isabel Aponte Polo has been shot dead this Saturday while participating in a campaign event in the city of Caraz, in the northwest of Peru, as confirmed by Peruvian authorities."
{"subject_country": "Peru", "bearing_on_target": "direct", "risk_area": "order_security", "one_person_case": false, "reason": "A candidate killed at a campaign event is violence against the conduct of Peru's elections, a single event with high consequences.", "label": "relevant"}

Country: Poland
Article: Russia hits Poland-Ukraine border area amid wave of attacks targeting gas stations, Kyiv says - Scripps News. "A Russian drone hit a gas station near Ukraine's border with Poland... The latest attack, in the small, northwest Ukrainian town of Yahodyn, happened just hundreds of meters from the border with Poland, a Polish official told Reuters."
{"subject_country": "Ukraine", "bearing_on_target": "spillover", "risk_area": "order_security", "one_person_case": false, "reason": "A neighbour's war is striking a few hundred metres from a Polish border crossing, a direct security consequence for Poland.", "label": "relevant"}

Country: Mexico
Article: Trump administration begins building border wall in Big Bend region of Texas - PBS. "The Trump administration has started building the border wall through a west Texas section of the Big Bend region, marking the first major construction in an area of the U.S.-Mexico border where the administration's plans have met with heavy opposition."
{"subject_country": "United States", "bearing_on_target": "spillover", "risk_area": "friction", "one_person_case": false, "reason": "New US wall construction on the Mexican frontier bears directly on Mexico's border crossings and cross-border trade.", "label": "relevant"}

Country: Malaysia
Article: Malaysia politics: Najib's house arrest order facing legal challenges, calls for transparency - CNA. "Malaysia's Attorney-General says former PM Najib Razak has yet to pay the RM50 million fine required for him to serve the rest of his prison term under house arrest. Meanwhile the house-arrest order is facing legal challenges, amid calls for greater transparency over how the conditional pardon was granted."
{"subject_country": "Malaysia", "bearing_on_target": "direct", "risk_area": "information", "one_person_case": false, "reason": "It is about one man, but the question is how a former prime minister's pardon was granted and whether the courts will uphold it, which is institutional.", "label": "relevant"}

Country: Chile
Article: Weak economy puts pressure on Chile government's goals - upi.com. "Chile's Monthly Economic Activity Index fell 1.5% in July from a year earlier, marking its worst performance in more than three years. The unemployment rate reached 9.5% in the May-July period, its highest level since 2021."
{"subject_country": "Chile", "bearing_on_target": "direct", "risk_area": "order_security", "one_person_case": false, "reason": "Activity at a three-year low and unemployment at its highest since 2021 describe the state of Chile's economy and the pressure on its government.", "label": "relevant"}

Country: Singapore
Article: Malaysian man who is alleged mastermind of scam syndicate operating in S'pore arrested - straitstimes.com. "A 27-year-old Malaysian man, nicknamed 'Da Xiang', was arrested for masterminding a scam and money laundering syndicate operating in Singapore involving impersonation of government officials."
{"subject_country": "Singapore", "bearing_on_target": "direct", "risk_area": "none", "one_person_case": true, "reason": "One suspect's arrest in a scam case, with no consequence for Singapore's institutions or economy.", "label": "irrelevant"}

Country: Thailand
Article: Australian man accused of teenage girl's murder appears in Thailand court - ABC News. "An Australian man appeared in court for the first time Friday on charges of murder and concealment of a body in connection with the death of a teenage girl in an eastern tourist city in Thailand, police said."
{"subject_country": "Thailand", "bearing_on_target": "direct", "risk_area": "none", "one_person_case": true, "reason": "One foreign national's murder trial, with no institutional angle.", "label": "irrelevant"}

Country: France
Article: MOPGA 2027: Visiting Fellowship Program for Early Career Researchers - Campus France. "Since 2018, the Make Our Planet Great Again (MOPGA) initiative has continued to attract strong interest from the international scientific community... France is launching a new edition of the MOPGA programme."
{"subject_country": "France", "bearing_on_target": "direct", "risk_area": "none", "one_person_case": false, "reason": "A call for fellowship applications, which says nothing about the risk of holding French assets.", "label": "irrelevant"}

Country: Japan
Article: Oregon, Big Ten Network and DAZN Expand Access for Fans in Japan - University of Oregon Athletics. "The University of Oregon, Big Ten Network and DAZN announced a coordinated content and distribution effort that builds on the momentum of Oregon football's summer trip to Japan."
{"subject_country": "United States", "bearing_on_target": "none", "risk_area": "none", "one_person_case": false, "reason": "A US college football broadcasting deal in which Japan is the audience.", "label": "irrelevant"}

Country: Hong Kong
Article: Hong Kong Profits Tax Filing Guide 2026: Deadlines, Requirements and Preparation - China Briefing. "The Hong Kong Profits Tax Filing Guide 2026 helps businesses navigate the latest tax return deadlines, filing requirements, and compliance obligations."
{"subject_country": "Hong Kong", "bearing_on_target": "direct", "risk_area": "friction", "one_person_case": false, "reason": "A compliance guide to existing filing deadlines, which reports no change in Hong Kong's tax regime.", "label": "irrelevant"}"""


RELEVANCE_SCHEMA: Dict[str, Any] = {
    "name": "article_relevance",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        # Order is the instrument: the model says who the story is about and
        # whether it touches the country before it is allowed to label it.
        "properties": {
            "subject_country": {"type": "string"},
            "bearing_on_target": {"type": "string", "enum": list(BEARINGS)},
            "risk_area": {"type": "string", "enum": list(RISK_AREAS)},
            "one_person_case": {"type": "boolean"},
            "reason": {"type": "string"},
            "label": {"type": "string", "enum": list(LABELS)},
        },
        "required": ["subject_country", "bearing_on_target", "risk_area",
                     "one_person_case", "reason", "label"],
    },
    "strict": True,
}


# Derived, never written down. See the module docstring.
RELEVANCE_PROMPT_VERSION = content_hash(RELEVANCE_PROMPT)


def cache_version(model: str, input_mode: str) -> str:
    """The cache's version column: the prompt, the model and the input mode.

    The cache used to be keyed on the prompt alone, so a changed model would have
    been served the previous model's verdicts as its own.
    """
    return content_hash(f"{RELEVANCE_PROMPT_VERSION}\n{model}\n{input_mode}")


def effective_input_mode(article: Dict[str, Any], input_mode: str = DEFAULT_INPUT_MODE) -> str:
    """The mode an article is actually read in.

    `body` falls back to `snippet` when there is no body, or when the digest has
    said the body is not the article (a wall, a cookie page, site promotion).
    """
    if input_mode not in INPUT_MODES:
        raise ValueError(f"unknown gate input mode {input_mode!r}")
    if input_mode == "body":
        if article.get("body_quality") == "not_article":
            return "snippet"
        if not (article.get("text") or "").strip():
            return "snippet"
    return input_mode


def article_input_text(article: Dict[str, Any], input_mode: str = DEFAULT_INPUT_MODE) -> str:
    """Return the text the gate reads for one article.

    The publisher is included deliberately: "Reuters" and "SAPO Desporto" are
    evidence about what kind of piece this is, and the classifier is allowed to
    use it. Everything else is the article's own words.
    """
    if effective_input_mode(article, input_mode) == "body":
        rest = (article.get("text") or "").strip()[:BODY_INPUT_CHARS]
    else:
        rest = (article.get("snippet") or "").strip()
    parts = [
        (article.get("title") or "").strip(),
        (article.get("source") or "").strip(),
        rest,
    ]
    return "\n\n".join(p for p in parts if p)


def relevance_key(article: Dict[str, Any], iso2: str, input_mode: str = DEFAULT_INPUT_MODE) -> str:
    """Return the cache key for (this article, this country, as read).

    The country is part of the key because the same text has a different answer
    for a different country. Leaving it out would let a story that is relevant
    for Germany be served as relevant for Portugal.
    """
    return content_hash(f"{iso2}\n\n{article_input_text(article, input_mode)}")


def _client(model: str, api_key: Optional[str], seed: int) -> Any:
    return ChatOpenAI(
        model=model,
        temperature=0.0,
        seed=seed,
        max_retries=0,
        max_tokens=MAX_OUTPUT_TOKENS,
        api_key=api_key,
    )


def gate_prompt(country_name: str, article: Dict[str, Any], input_mode: str = DEFAULT_INPUT_MODE) -> str:
    """The whole message the gate sends for one article."""
    return (
        f"{RELEVANCE_PROMPT.replace('{country}', country_name)}\n\n"
        f"NOW THE ARTICLE TO JUDGE\n\n"
        f"Country: {country_name}\n"
        f"Article:\n{article_input_text(article, input_mode)}"
    )


def classify(
    articles: Sequence[Dict[str, Any]],
    country_name: str,
    iso2: str,
    *,
    model: str = DEFAULT_MODEL,
    input_mode: str = DEFAULT_INPUT_MODE,
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
        model: Dated model id. Part of the cache version.
        input_mode: 'snippet' or 'body'. Part of the cache version.
        meter: Records real token usage if supplied.
        use_cache: False re-asks the model for everything, for measurement.
        mode: 'named' or 'masked'; part of the cache key.

    Returns:
        ``{relevance_key: {subject_country, bearing_on_target, risk_area,
        one_person_case, reason, label, ledgers, input_mode, cached}}``.
        `input_mode` is the mode the article was actually read in. An article
        the model failed on is absent, and is treated downstream as ineligible
        rather than as passing.
    """
    if not articles:
        return {}

    keys = {id(a): relevance_key(a, iso2, input_mode) for a in articles}
    version = cache_version(model, input_mode)

    cached: Dict[str, Any] = {}
    if use_cache:
        try:
            cached = store.read_artifacts(
                list(keys.values()),
                kind="relevance",
                version=version,
                mode=mode,
                country_iso2=iso2,
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
        try:
            response = structured.invoke(
                [SystemMessage(content=gate_prompt(country_name, article, input_mode))]
            )
        except Exception as e:
            logger.warning("relevance call failed for %s: %s", article.get("title"), e)
            continue

        if meter is not None:
            meter.add_response(model, response)

        parsed = response.get("parsed") if isinstance(response, dict) else response
        if not isinstance(parsed, dict) or parsed.get("label") not in LABELS:
            logger.warning("relevance returned an unusable answer for %s", article.get("title"))
            continue

        # The ledger that selection spreads the budget over, derived from the
        # area. An irrelevant article bears on none, whatever area it named.
        area = parsed.get("risk_area")
        parsed["ledgers"] = (
            [LEDGER_OF_AREA[area]] if parsed["label"] == "relevant" and area in LEDGER_OF_AREA else []
        )
        parsed["input_mode"] = effective_input_mode(article, input_mode)

        out[key] = {**parsed, "cached": False}
        fresh.append((key, parsed))

    if fresh and use_cache:
        try:
            store.write_artifacts(
                fresh,
                kind="relevance",
                version=version,
                model=model,
                mode=mode,
                # The country is already inside the hash; as a column it makes
                # "every verdict for PT" a query rather than a recomputation.
                country_iso2=iso2,
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
    input_mode: str = DEFAULT_INPUT_MODE,
) -> Dict[str, Any]:
    """Choose what the scorer reads. Only `relevant` articles are eligible.

    **Nothing tops up from ineligible articles.** If six qualify, six are
    scored. Per-theme counts are reported; they are not floors, and no article
    is admitted to meet one. A zero means no relevant coverage was found, which
    is a fact about the week and not a gap to be filled.

    The order:

    1. **Round-robin across the ledgers each article bears on**, newest
       first within each ledger, until the budget fills. Twenty newest articles
       on a busy country can be eight versions of one story; interleaving by
       ledger spreads the budget without ever admitting something ineligible.
       A ledger with nothing eligible is simply skipped, and its count says zero.
    2. **Prefer a publisher not already selected** when two candidates tie, so
       one outlet cannot take the whole budget.
    3. **Tie-break on URL**, so the order is deterministic for a given eligible
       set.

    Articles that are relevant but bear on no ledger are not discarded; they
    are taken after the ledgered ones, in the same order.

    Returns:
        ``{"selected", "eligible", "rejected", "per_theme", "per_ledger", "counts"}``.
    """
    eligible: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    for article in articles:
        verdict = labels.get(relevance_key(article, iso2, input_mode))
        if verdict and verdict.get("label") == "relevant":
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

    buckets: Dict[str, List[Dict[str, Any]]] = {led: [] for led in LEDGERS}
    buckets["(none)"] = []
    for a in sorted(eligible, key=order_key, reverse=True):
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

    # Relevant articles that bear on no ledger are eligible and must not be
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

    # One row per candidate, whatever the gate decided. The census keeps these
    # so a later session can ask what the classifier said without recomputing a
    # cache key, and can stratify a sample over its judgements.
    gate_labels = []
    for article in articles:
        verdict = labels.get(relevance_key(article, iso2, input_mode)) or {}
        gate_labels.append({
            "url": article.get("publisher_link") or article.get("link"),
            "label": verdict.get("label", "unclassified"),
            "reason": verdict.get("reason", ""),
            "ledgers": verdict.get("ledgers", []),
            "subject_country": verdict.get("subject_country", ""),
            "bearing_on_target": verdict.get("bearing_on_target", ""),
            "risk_area": verdict.get("risk_area", ""),
            "one_person_case": bool(verdict.get("one_person_case")),
            # What the gate actually read: `body` can fall back to `snippet`.
            "input_mode": verdict.get("input_mode", ""),
            # Which theme queries found it, for this country. A fact about the
            # run, so it is kept here rather than on the article.
            "themes": article.get("themes") or [],
        })

    return {
        "selected": selected,
        "eligible": eligible,
        "rejected": rejected,
        "gate_labels": gate_labels,
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
