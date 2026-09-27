"""
The relevance gate: does this article bear on country risk at all?

Every candidate is classified once, per country, before anything expensive
touches it. A cheap model reads the article and answers one question, and only
articles that pass are eligible to be scored.

The question is not "what is this article about". It is:

    who acted, and on whom?

Asking about topic made the model match keywords: "press freedom" or "military"
next to the country's name read as relevant. Eli's sixty labels follow an actor
rule instead, and the prompt states it as exclusions and tests. The model names
the exclusion and the test, and the label is computed from them in code
(`label_of`), where the rule is exact. Two earlier prompts, a three-label bar and
a topic definition, agreed with him on 7 of 30 and 23 of 30 (deferred.md §4, §15).

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

import datetime as dt
import json
import logging
import os
from pathlib import Path
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
    "story_clusters",
    "dedupe_stories",
    "duplicate_story_report",
]


# The four ledgers the score is built from. `security` is a retrieval theme, not
# a ledger: conflict reaches the score through `order`.
LEDGERS = ("friction", "order", "information", "edge")

LABELS = ("relevant", "irrelevant")

# What the model answers. The label is not among them: it is computed from the
# exclusion and the test by `label_of`, where the rule is exact.
EXCLUSIONS = ("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "none")
TESTS = ("T1", "T2", "T3", "T4", "T5", "none")

# The ledger an article bears on. `order_security` is the `order` ledger under
# the name the prompt uses. `edge` has no gate value: edge runs on data, not
# news (three-door-test.md).
RISK_AREAS = ("friction", "order_security", "information", "none")
LEDGER_OF_AREA = {"friction": "friction", "order_security": "order", "information": "information"}

# What the gate reads. `snippet` is the title, the outlet and the feed's
# description. On Google News the description is the outlet's name again, so
# `snippet` is in practice the headline. `body` is the title, the outlet and the
# opening of the extracted body.
INPUT_MODES = ("snippet", "body")
DEFAULT_INPUT_MODE = "body"
BODY_INPUT_CHARS = 1500

# Fixed on 2026-09-27: gpt-4o-mini in body mode. It is the cheapest cell of the
# v3 grid and cleared the recall bar there (20 of 22). From here only the prompt
# and the harness change (deferred.md §15).
#
# Dated id: an alias moves under you, and a gate that silently changes model
# silently changes the evidence behind every score.
DEFAULT_MODEL = "gpt-4o-mini-2024-07-18"

ARTICLE_BUDGET = 20
FULL_TEXT_K = 3

# A few times the schema's real size: five short fields and two sentences.
# Leaving the ceiling at the model's default lets a degenerate input burn
# thousands of tokens producing nothing usable.
MAX_OUTPUT_TOKENS = 400

# What each country is exposed to, for the spillover test (T5). Built by
# `data_fetching.exposure_cards_build` from the CIA World Factbook.
EXPOSURE_CARDS_PATH = Path(__file__).with_name("exposure_cards.json")
EXPOSURE_CARDS: Dict[str, Dict[str, Any]] = json.loads(EXPOSURE_CARDS_PATH.read_text(encoding="utf-8"))
EXPOSURE_CARDS_VERSION = content_hash(EXPOSURE_CARDS_PATH.read_text(encoding="utf-8"))


# The prompt is Eli's actor test, used verbatim from the 2026-09-27 brief. He
# derived it by reading all sixty of his labelled articles: the question is who
# acted and on whom, not what the article is about. The `ledger` line keeps the
# three areas the previous prompt defined, word for word. The fourteen worked
# examples are his articles, in pairs that share a topic and differ in label.
RELEVANCE_PROMPT = """You screen news for an analyst who scores the investable risk of {country}: how risky it is to hold stocks or bonds, or run a business, exposed to {country}. Decide whether this article belongs in front of that analyst this week.

The test is WHO ACTED, and ON WHOM. Topic alone never decides. An article that mentions {country} alongside words like "military", "press freedom", "customs" or "corruption" is relevant only if it passes one of the tests in Step 2.

{country} exposures (use for Test T5):
- Main export partners: {export_partners}
- Main import partners: {import_partners}
- Main exports: {main_exports}
- Neighbours: {neighbours}
- Security rivals: {security_rivals}

Article published: {published_date}. Today: {run_date}.

STEP 1: EXCLUSIONS. If any one applies, stop: the article is excluded.
E1  Sport, entertainment, celebrity, culture, travel or lifestyle.
E2  Academic or science programmes: fellowships, scholarships, research centres, course rankings.
E3  A guide or explainer to rules already in force (how to file, deadlines, checklists). A NEW rule, or a CHANGE to a rule, is not an explainer.
E4  Opinion, advocacy or proposals from people outside {country}'s government (experts, companies, NGOs, commentators) about something no {country} authority has proposed or decided.
E5  Ordinary crime or a court case about private individuals: drugs, scams, murder, theft, extradition of a private person. NOT excluded: cases involving politicians, officials, elections, journalists or activists.
E6  A company's own business: its deals, investments, results or products, especially abroad. NOT excluded: a {country} regulator, court or government acting on a company.
E7  {country} is only the place where it happened, or is mentioned in passing, and the consequences fall on other countries.
E8  People or groups outside {country} acting ABOUT {country}: protests held abroad, foreign NGO appeals, anniversaries of old events, when the article's main subject is the people or groups outside {country}, not events inside it.

STEP 2: TESTS. If no exclusion applies, find the first test that passes.
T1  {country}'s own state acts or speaks officially: head of state, government, ministers, parliament, courts on public matters, regulators, central bank, anti-corruption body, statistics office, armed forces, diplomats. Routine official activity counts: hearings, budgets, trainings, speeches, consultations, agreements, bills introduced in the legislature.
T2  A measurement or assessment of {country}'s economy, markets, institutions or security: data releases, forecasts, rankings, analysis of conditions.
T3  {country}'s political process: elections, parties, political violence, prosecution of politicians or officials, election crimes, protests inside {country}.
T4  An outside state or force acts ON {country}: military strikes or threats against its territory, pressure from a major power over its status, tariffs, sanctions or border measures aimed at it, rules of a bloc it belongs to, agreements it signs.
T5  Spillover through an exposure: news about one of {country}'s main trade partners' economies or trade policy, its main export commodities, or its neighbours' or security rivals' economies, borders or security, even when {country} is not named.

If no test passes, the answer is none.

HOW TO ANSWER
- what_happened: one sentence, naming who did what to whom.
- exclusion: the first exclusion that applies (E1 to E8), or "none".
- test: if exclusion is "none", the first test that passes (T1 to T5), or "none". If an exclusion applies, "none".
- high_impact_event: true only for a coup or coup attempt, a military attack on {country}'s territory, a sovereign default, or the killing of a candidate or senior official.
- ledger: the one area the article bears on most, or `none`.
    - `friction`: the cost and difficulty of doing business. Taxes, customs, permits, regulation, courts and contract enforcement, corruption, capital controls, expropriation, sanctions on or by {country}.
    - `order_security`: government stability, elections and their conduct, coups, protests, unrest, armed conflict, terrorism, violence against officials, candidates or journalists, the central bank, inflation, currency, debt, default, IMF programmes, and the state of the economy as a whole.
    - `information`: whether you can trust what you know about {country}. Press freedom, censorship, the integrity of the statistics office, audits, judicial independence.
- reason: one sentence saying which words in the article decided the exclusion or test.

EXAMPLES

Country: Turkey. Published: 2026-09-18.
Article: Turkey must release journalist Ahmet Polad following his transfer from Syria after months of captivity - Committee to Protect Journalists. "The Committee to Protect Journalists calls on Turkish authorities to immediately release Ahmet Polad, a Kurdish-Turkish journalist who was transferred to Turkey this month after nearly eight months of enforced disappearance in Syria and is reported to be in extremely poor health."
{"what_happened": "Turkish authorities are holding journalist Ahmet Polad after his transfer from Syria, and the CPJ calls for his release.", "exclusion": "none", "test": "T1", "high_impact_event": false, "ledger": "information", "reason": "\"calls on Turkish authorities to immediately release\": Turkey's own authorities hold the journalist."}

Country: Hong Kong SAR, China. Published: 2026-09-14.
Article: Where is Hong Kong journalist Minnie Chan, who disappeared three years ago in China? - RSF. "It will soon be the third anniversary of Minnie Chan's disappearance. The defence reporter was last seen covering the Forum in 2023. Reporters Without Borders (RSF) remains deeply concerned about her fate and whereabouts, especially as several RSF sources believe she is being detained by the Chinese authorities."
{"what_happened": "RSF asks where Hong Kong journalist Minnie Chan is, three years after she disappeared in mainland China.", "exclusion": "E8", "test": "none", "high_impact_event": false, "ledger": "none", "reason": "\"third anniversary\" and \"detained by the Chinese authorities\": a foreign NGO appeal about China's action, with nothing new happening in Hong Kong."}

Country: United States. Published: 2026-09-24.
Article: Four Aliens Charged With Election Fraud - Department of Justice. "Four aliens, including 2 illegal aliens, have been charged with election-related crimes, announced United States Attorney David Metcalf. 'Anyone, including illegal aliens, who votes illegally in our elections will be prosecuted for corrupting our democracy.'"
{"what_happened": "A US Attorney charged four non-citizens with voting illegally in US elections.", "exclusion": "none", "test": "T3", "high_impact_event": false, "ledger": "order_security", "reason": "\"charged with election-related crimes\": an election crime."}

Country: Hong Kong SAR, China. Published: 2026-09-25.
Article: Hong Kong Customs seizes suspected ketamine and suspected illicit cigarettes worth about $2.2 million - customs.gov.hk. "Hong Kong Customs on September 22 detected a dangerous drug trafficking case and seized about 5.7 kilograms of suspected ketamine. Twenty-seven thousand six hundred suspected illicit cigarettes were also seized. Three persons were arrested."
{"what_happened": "Hong Kong Customs seized ketamine and illicit cigarettes and arrested three people.", "exclusion": "E5", "test": "none", "high_impact_event": false, "ledger": "none", "reason": "\"dangerous drug trafficking case\" and \"three persons were arrested\": ordinary crime by private individuals."}

Country: New Zealand. Published: 2026-09-19.
Article: Real estate agency fined $18k after six-year breach - NZ Herald. "The agency had made numerous genuine attempts to appoint an auditor without success. Last month, the agency was fined $18,000 after admitting a charge of reckless contravention of the Real Estate Agents Act 2008."
{"what_happened": "New Zealand's Real Estate Authority fined an agency $18,000 for breaching the Real Estate Agents Act.", "exclusion": "none", "test": "T1", "high_impact_event": false, "ledger": "friction", "reason": "\"fined $18,000 ... Real Estate Agents Act 2008\": a New Zealand regulator acting on a company."}

Country: Taiwan. Published: 2026-09-23.
Article: Taiwan Cement Giant Makes Rare Wartime Investment in Ukraine - Kyiv Post. "Taiwan Cement Corporation (TCC), one of the world's largest cement producers, is set to become a strategic investor in PJSC Ivano-Frankivskcement (IFCEM), Ukraine's leading cement manufacturer. The deal marks a rare example of a global industrial group investing in a Ukrainian manufacturing asset while the country remains at war."
{"what_happened": "Taiwan Cement Corporation agreed to become the owner of Ukraine's leading cement maker.", "exclusion": "E6", "test": "none", "high_impact_event": false, "ledger": "none", "reason": "\"set to become a strategic investor in ... Ukraine's leading cement manufacturer\": a company's own deal abroad."}

Country: Norway. Published: 2026-09-07.
Article: Russia Identifies US Missile Sites in Norway It Would Target in a Military Conflict - UNITED24 Media. "Russia has identified US missile deployment sites and their command-and-control centers in Norway as potential targets in the event of a military conflict, following Washington's reported deployment of a missile system capable of launching Tomahawk cruise missiles."
{"what_happened": "Russia's Foreign Ministry named US missile sites in Norway as targets in a military conflict.", "exclusion": "none", "test": "T4", "high_impact_event": false, "ledger": "order_security", "reason": "\"identified US missile deployment sites ... in Norway as potential targets\": an outside power threatens Norway's territory."}

Country: Hong Kong SAR, China. Published: 2026-09-24.
Article: Chinese authorities reportedly in possession of F-35 components in Hong Kong - cnbc.com. "Chinese authorities are reportedly in possession of F-35 stealth fighter parts after components of the aircraft were 'inexplicably diverted to Hong Kong.' Shipping giant UPS was sending a cockpit canopy and weapons bay door to the U.S. from Australia in late May when it was diverted."
{"what_happened": "F-35 parts shipped from Australia to the US were diverted to Hong Kong and are reportedly held by Chinese authorities.", "exclusion": "E7", "test": "none", "high_impact_event": false, "ledger": "none", "reason": "\"inexplicably diverted to Hong Kong\": Hong Kong is only where it happened, and the consequences fall on China, the US and Australia."}

Country: Peru. Published: 2026-09-20.
Article: Shot dead the journalist and regional candidate Susy Aponte Polo during a rally in Peru - Demócrata. "The journalist and aspiring regional governor Susy Isabel Aponte Polo has been shot dead this Saturday while participating in a campaign event in the city of Caraz, in the northwest of Peru, as confirmed by Peruvian authorities."
{"what_happened": "A gunman shot dead regional candidate Susy Aponte Polo at a campaign event in Peru.", "exclusion": "none", "test": "T3", "high_impact_event": true, "ledger": "order_security", "reason": "\"shot dead ... while participating in a campaign event\": political violence, the killing of a candidate."}

Country: Turkey. Published: 2026-09-22.
Article: Greek LGBTQI+ Groups Plan Protest Rally in Solidarity With Turkey's LGBTQI+ Community - tovima.com. "LGBTQI+ collectives and organizations in Greece have called on the public to join a protest outside the Turkish Embassy in Athens, on Friday, September 25, in solidarity with Turkey's LGBTQI+ community."
{"what_happened": "LGBTQI+ groups in Greece called a protest outside the Turkish Embassy in Athens.", "exclusion": "E8", "test": "none", "high_impact_event": false, "ledger": "none", "reason": "\"a protest outside the Turkish Embassy in Athens\": people abroad acting about Turkey."}

Country: Canada. Published: 2026-09-17.
Article: Pay cynicism has doubled since 2024 - just as Canada's transparency rules expand - Benefits and Pensions Monitor. "Six provinces now require pay disclosure. The share of employees who believe it's easier to get a pay raise by quitting and rejoining their employer has more than doubled since 2024. This finding lands as Canada's own patchwork of provincial pay transparency laws continues to expand."
{"what_happened": "Canadian provinces keep adding pay transparency laws, as a survey finds pay cynicism has doubled.", "exclusion": "none", "test": "T1", "high_impact_event": false, "ledger": "friction", "reason": "\"Canada's own patchwork of provincial pay transparency laws continues to expand\": new rules from Canadian governments."}

Country: Hong Kong SAR, China. Published: 2026-09-18.
Article: Hong Kong Profits Tax Filing Guide 2026: Deadlines, Requirements and Preparation - China Briefing. "The Hong Kong Profits Tax Filing Guide 2026 helps businesses navigate the latest tax return deadlines, filing requirements, and compliance obligations."
{"what_happened": "China Briefing explains Hong Kong's 2026 profits tax filing deadlines and requirements.", "exclusion": "E3", "test": "none", "high_impact_event": false, "ledger": "none", "reason": "\"helps businesses navigate the latest tax return deadlines\": an explainer of rules already in force."}

Country: United States. Published: 2026-09-24.
Article: Global trade is changing how the Canadian economy works - Bank of Canada. "Tariffs and other trade barriers are changing where goods are produced. International trade benefits the Canadian economy, but changes to our trade relationships are now forcing businesses to adjust to a new reality."
{"what_happened": "The Bank of Canada says tariffs and trade barriers are changing how Canada's economy works.", "exclusion": "none", "test": "T5", "high_impact_event": false, "ledger": "friction", "reason": "\"Tariffs and other trade barriers are changing where goods are produced\" in Canada, the United States' top export partner."}

Country: Portugal. Published: 2026-09-23.
Article: Spain and Brazil take FISU World University beach handball titles in Portugal - IHF. "Honours were split between two of the world heavyweights in beach handball at the 2026 FISU World University Championship Beach Sports championships which concluded in Figueira da Foz, Portugal on Tuesday."
{"what_happened": "Spain and Brazil won the beach handball titles at a university championship held in Portugal.", "exclusion": "E1", "test": "none", "high_impact_event": false, "ledger": "none", "reason": "\"beach handball titles\": sport, and Portugal is only the venue."}"""

# The fourteen articles above. None of them may report the gate's accuracy.
EXAMPLE_IDS = ("b21", "h12", "h21", "h10", "b02", "h01", "b29", "h11",
               "b01", "b18", "b15", "b03", "h30", "h24")


RELEVANCE_SCHEMA: Dict[str, Any] = {
    "name": "article_relevance",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        # Order is the instrument: the model says what happened, then whether an
        # exclusion applies, before it is asked which test passes.
        "properties": {
            "what_happened": {"type": "string"},
            "exclusion": {"type": "string", "enum": list(EXCLUSIONS)},
            "test": {"type": "string", "enum": list(TESTS)},
            "high_impact_event": {"type": "boolean"},
            "ledger": {"type": "string", "enum": list(RISK_AREAS)},
            "reason": {"type": "string"},
        },
        "required": ["what_happened", "exclusion", "test", "high_impact_event",
                     "ledger", "reason"],
    },
    "strict": True,
}


# Derived, never written down. See the module docstring.
RELEVANCE_PROMPT_VERSION = content_hash(RELEVANCE_PROMPT)


def label_of(verdict: Dict[str, Any]) -> str:
    """The rule, in code: relevant only when nothing excludes and a test passes."""
    if verdict.get("exclusion") == "none" and verdict.get("test") not in (None, "none"):
        return "relevant"
    return "irrelevant"


def cache_version(model: str, input_mode: str) -> str:
    """The cache's version column: the prompt, the exposure cards, the model and
    the input mode.

    The run date is in the prompt but not in the version, so a verdict earned on
    one day is served on another. Only E8 ("anniversaries of old events") leans on
    the date, and a verdict a week old is judged against the week it was read.
    """
    return content_hash(
        f"{RELEVANCE_PROMPT_VERSION}\n{EXPOSURE_CARDS_VERSION}\n{model}\n{input_mode}"
    )


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


def _listed(values: Sequence[str]) -> str:
    return ", ".join(values) if values else "none"


def gate_prompt(
    country_name: str,
    iso2: str,
    article: Dict[str, Any],
    input_mode: str = DEFAULT_INPUT_MODE,
    run_date: Optional[dt.date] = None,
) -> str:
    """The whole message the gate sends for one article."""
    card = EXPOSURE_CARDS.get(iso2) or {}
    published = str(article.get("page_published_at") or article.get("published") or "")[:10]
    fields = {
        "{country}": country_name,
        "{export_partners}": _listed(card.get("export_partners", [])),
        "{import_partners}": _listed(card.get("import_partners", [])),
        "{main_exports}": _listed(card.get("main_exports", [])),
        "{neighbours}": _listed(card.get("neighbours", [])),
        "{security_rivals}": _listed(card.get("security_rivals", [])),
        "{published_date}": published or "unknown",
        "{run_date}": (run_date or dt.date.today()).isoformat(),
    }
    text = RELEVANCE_PROMPT
    for placeholder, value in fields.items():
        text = text.replace(placeholder, value)
    return (
        f"{text}\n\n"
        f"NOW THE ARTICLE TO JUDGE\n\n"
        f"Country: {country_name}. Published: {published or 'unknown'}.\n"
        f"Article:\n{article_input_text(article, input_mode)}"
    )


def classify(
    articles: Sequence[Dict[str, Any]],
    country_name: str,
    iso2: str,
    *,
    model: str = DEFAULT_MODEL,
    input_mode: str = DEFAULT_INPUT_MODE,
    run_date: Optional[dt.date] = None,
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
        iso2: Roster code, part of the cache key, and whose exposure card is sent.
        model: Dated model id. Part of the cache version.
        input_mode: 'snippet' or 'body'. Part of the cache version.
        run_date: "Today" in the prompt; defaults to the current date.
        meter: Records real token usage if supplied.
        use_cache: False re-asks the model for everything, for measurement.
        mode: 'named' or 'masked'; part of the cache key.

    Returns:
        ``{relevance_key: {what_happened, exclusion, test, high_impact_event,
        ledger, reason, label, ledgers, input_mode, cached}}``. `label` is
        computed by `label_of`, not answered by the model. `input_mode` is the
        mode the article was actually read in. An article the model failed on is
        absent, and is treated downstream as ineligible rather than as passing.
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
            response = structured.invoke([SystemMessage(
                content=gate_prompt(country_name, iso2, article, input_mode, run_date)
            )])
        except Exception as e:
            logger.warning("relevance call failed for %s: %s", article.get("title"), e)
            continue

        if meter is not None:
            meter.add_response(model, response)

        parsed = response.get("parsed") if isinstance(response, dict) else response
        if not isinstance(parsed, dict) or parsed.get("exclusion") not in EXCLUSIONS \
                or parsed.get("test") not in TESTS:
            logger.warning("relevance returned an unusable answer for %s", article.get("title"))
            continue

        parsed["label"] = label_of(parsed)
        # The ledger that selection spreads the budget over, derived from the
        # area. An irrelevant article bears on none, whatever area it named.
        area = parsed.get("ledger")
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
    same_story_as: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Choose what the scorer reads. Only `relevant` articles are eligible.

    `same_story_as` is `dedupe_stories`' verdict, ``{url: kept url}``. An
    eligible article in it is another outlet's telling of a story already in
    the pool. It is never selected, so its slot goes to the next eligible
    article. It still counts as eligible, because the gate passed it.

    **Nothing tops up from ineligible articles.** If six qualify, six are
    scored. Per-theme counts are reported; they are not floors, and no article
    is admitted to meet one. A zero means no relevant coverage was found, which
    is a fact about the week and not a gap to be filled.

    The order:

    1. **High-impact events first, always.** A coup attempt, a military attack
       on the country's territory, a sovereign default or the killing of a
       candidate or senior official is read before anything else, and so takes
       the full-text slots.
    2. **Then round-robin across the ledgers each article bears on**, newest
       first within each ledger, until the budget fills. Twenty newest articles
       on a busy country can be eight versions of one story; interleaving by
       ledger spreads the budget without ever admitting something ineligible.
       A ledger with nothing eligible is simply skipped, and its count says zero.
    3. **Prefer a publisher not already selected** when two candidates tie, so
       one outlet cannot take the whole budget.
    4. **Tie-break on URL**, so the order is deterministic for a given eligible
       set.

    Articles that are relevant but bear on no ledger are not discarded; they
    are taken after the ledgered ones, in the same order.

    Returns:
        ``{"selected", "eligible", "rejected", "same_story", "gate_labels",
        "per_theme", "per_ledger", "counts"}``.
    """
    eligible: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    same_story_as = same_story_as or {}
    same_story: List[Dict[str, Any]] = []

    for article in articles:
        verdict = labels.get(relevance_key(article, iso2, input_mode))
        if verdict and verdict.get("label") == "relevant":
            enriched = dict(article)
            enriched["relevance"] = verdict
            eligible.append(enriched)
            url = article.get("publisher_link") or article.get("link")
            if url in same_story_as:
                same_story.append({"url": url, "same_story_as": same_story_as[url]})
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
    dropped = {d["url"] for d in same_story}
    pool = [a for a in eligible
            if (a.get("publisher_link") or a.get("link")) not in dropped]
    events = sorted(
        [a for a in pool if a["relevance"].get("high_impact_event")],
        key=order_key,
        reverse=True,
    )
    rest = [a for a in pool if not a["relevance"].get("high_impact_event")]

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
            "what_happened": verdict.get("what_happened", ""),
            "exclusion": verdict.get("exclusion", ""),
            "test": verdict.get("test", ""),
            "high_impact_event": bool(verdict.get("high_impact_event")),
            "ledger": verdict.get("ledger", ""),
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
        "same_story": same_story,
        "gate_labels": gate_labels,
        "per_theme": per_theme,
        "per_ledger": per_ledger,
        "counts": {
            "candidates": len(articles),
            "classified": len(labels),
            "eligible": len(eligible),
            "same_story": len(same_story),
            "selected": len(selected),
            "budget": budget,
            "rejected_by_label": label_counts,
        },
    }


# --- One story, one article -------------------------------------------------
#
# Sending the scorer one event from five outlets makes one incident look like a
# trend. Title overlap cannot catch a rewrite: "Shot dead the journalist and
# regional candidate..." and "Two arrested for murder of journalist and
# candidate..." share almost no words and are one story. So a cheap model reads
# the gate's own one-sentence account of each eligible article and groups them,
# before the budget is filled, and one article per story goes forward.

STORY_MODEL = DEFAULT_MODEL
STORY_MAX_OUTPUT_TOKENS = 2000

# The fallback, when the model's answer cannot be read as one story per id.
STORY_TITLE_JACCARD = 0.6

STORY_PROMPT = """You group news items about {country} by story. Each line is an id and one sentence saying what happened.

Two items are the same story when they report the same underlying event, or its direct follow-up. A shooting and the arrests for that shooting are one story. A bill and the vote on that bill are one story. Items that only share a topic, a place or an institution are different stories: two different corruption cases, or one minister's statements on two different matters.

For every id, answer with the id of the FIRST item in the list that tells the same story. An item whose story no earlier item tells answers with its own id.

ITEMS
{items}"""


def _story_schema(ids: Sequence[str]) -> Dict[str, Any]:
    """Every id is a required key, so an answer that skips one cannot be
    decoded. A list of clusters cannot require that, and on the first test run
    gpt-4o-mini left one to five ids out of every country's list."""
    return {
        "name": "story_clusters",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "same_story_as": {
                    "type": "object",
                    "additionalProperties": False,
                    # No enum on the values: strict mode allows 1,000 enum
                    # values in a whole schema, and 48 ids would need 2,304.
                    # `_clusters_from_links` checks them instead.
                    "properties": {sid: {"type": "string"} for sid in ids},
                    "required": list(ids),
                },
            },
            "required": ["same_story_as"],
        },
        "strict": True,
    }


def _clusters_from_links(links: Any, ids: Sequence[str]) -> Any:
    """Join ``{id: id of the same story}`` into clusters, or return why not."""
    if not isinstance(links, dict):
        return "no answer"
    missing = [i for i in ids if i not in links]
    unknown = sorted({v for v in links.values() if v not in ids} | (set(links) - set(ids)))
    if missing or unknown:
        return f"missing {missing[:5]}, unknown {unknown[:5]}"
    parent = {i: i for i in ids}

    def root(i: str) -> str:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in links.items():
        ri, rj = root(i), root(j)
        if ri != rj:
            parent[max(ri, rj, key=ids.index)] = min(ri, rj, key=ids.index)
    groups: Dict[str, List[str]] = {}
    for i in ids:
        groups.setdefault(root(i), []).append(i)
    return list(groups.values())


def _title_clusters(items: Sequence[Dict[str, Any]], threshold: float) -> List[List[int]]:
    """Index groups whose titles overlap by at least `threshold`, as
    `duplicate_story_report` measures it. Every index is in exactly one group."""
    def tokens(a):
        return {w for w in (a.get("title") or "").lower().split() if len(w) > 3}

    toks = [tokens(a) for a in items]
    seen = set()
    groups: List[List[int]] = []
    for i in range(len(items)):
        if i in seen:
            continue
        group = [i]
        seen.add(i)
        if toks[i]:
            for j in range(i + 1, len(items)):
                if j in seen or not toks[j]:
                    continue
                if len(toks[i] & toks[j]) / len(toks[i] | toks[j]) >= threshold:
                    group.append(j)
                    seen.add(j)
        groups.append(group)
    return groups


def story_clusters(
    items: Sequence[Dict[str, Any]],
    country_name: str,
    *,
    model: str = STORY_MODEL,
    api_key: Optional[str] = None,
    seed: int = 42,
    meter: Optional[usage.Meter] = None,
) -> Dict[str, Any]:
    """Group `items` by story, in one call.

    Each item carries ``what_happened`` (the gate's sentence) and ``title``.
    The model names, for every id, the first item telling the same story, and
    the links are joined into clusters here. An unreadable answer or a failed
    call falls back to title overlap, and says so.

    Returns:
        ``{"clusters": [[index, ...], ...], "method": "llm" | "title_jaccard",
        "problem": str | None}``. Every index is in exactly one cluster.
    """
    if len(items) < 2:
        return {"clusters": [[i] for i in range(len(items))], "method": "llm", "problem": None}

    ids = [f"s{i + 1}" for i in range(len(items))]
    problem: Optional[str] = None
    api_key = api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        problem = "OPENAI_API_KEY not set"
    else:
        lines = "\n".join(
            f"{sid}: {(a.get('what_happened') or a.get('title') or '').strip()}"
            for sid, a in zip(ids, items)
        )
        structured = ChatOpenAI(
            model=model, temperature=0.0, seed=seed, max_retries=0,
            max_tokens=STORY_MAX_OUTPUT_TOKENS, api_key=api_key,
        ).with_structured_output(schema=_story_schema(ids), strict=True, include_raw=True)
        prompt = STORY_PROMPT.replace("{country}", country_name).replace("{items}", lines)
        try:
            response = structured.invoke([SystemMessage(content=prompt)])
            if meter is not None:
                meter.add_response(model, response)
            parsed = response.get("parsed") if isinstance(response, dict) else response
            links = parsed.get("same_story_as") if isinstance(parsed, dict) else None
            clusters = _clusters_from_links(links, ids)
            if isinstance(clusters, str):
                problem = clusters
            else:
                index = {sid: i for i, sid in enumerate(ids)}
                return {"clusters": [[index[m] for m in c] for c in clusters],
                        "method": "llm", "problem": None}
        except Exception as e:
            problem = f"call failed: {e}"[:300]

    logger.warning("story clustering fell back to title overlap: %s", problem)
    return {"clusters": _title_clusters(items, STORY_TITLE_JACCARD),
            "method": "title_jaccard", "problem": problem}


def _keep_order(members: Sequence[Dict[str, Any]], publishers_used: Dict[str, int]) -> List[Dict[str, Any]]:
    """A full body first, then the newest, then an unused publisher, then the
    URL. Stable sorts, least important first."""
    out = sorted(members, key=lambda a: a.get("publisher_link") or a.get("link") or "")
    out.sort(key=lambda a: publishers_used.get((a.get("source") or "").strip(), 0) > 0)
    out.sort(key=_published_sort_key, reverse=True)
    out.sort(key=lambda a: a.get("body_quality") != "full")
    return out


def dedupe_stories(
    articles: Sequence[Dict[str, Any]],
    labels: Dict[str, Dict[str, Any]],
    country_name: str,
    iso2: str,
    *,
    input_mode: str = DEFAULT_INPUT_MODE,
    meter: Optional[usage.Meter] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """One article per story among the gate's `relevant` articles.

    Runs after the gate and before selection, so a slot freed here goes to the
    next eligible article. The article kept from each story has, in order: a
    body a previous digest called `full`, the newest date, a publisher no other
    kept article has, the first URL. The scorer is not told how many outlets
    carried a story.

    `body_quality` is known this early only from the digest cache, as it is for
    the gate, so a story no previous run digested is decided on date.

    Returns:
        ``{"same_story_as": {url: kept url}, "method", "problem", "counts":
        {"eligible", "stories", "multi", "dropped"}}``.
    """
    eligible = []
    for a in articles:
        verdict = labels.get(relevance_key(a, iso2, input_mode))
        if verdict and verdict.get("label") == "relevant":
            eligible.append({**a, "what_happened": verdict.get("what_happened", "")})

    got = story_clusters(eligible, country_name, meter=meter, **kwargs)
    groups = [[eligible[i] for i in c] for c in got["clusters"]]

    publishers_used: Dict[str, int] = {}
    for g in groups:
        if len(g) == 1:
            pub = (g[0].get("source") or "").strip()
            publishers_used[pub] = publishers_used.get(pub, 0) + 1

    same_story_as: Dict[str, str] = {}
    multi = sorted((g for g in groups if len(g) > 1),
                   key=lambda g: max(_published_sort_key(a) for a in g), reverse=True)
    for g in multi:
        ranked = _keep_order(g, publishers_used)
        kept = ranked[0]
        pub = (kept.get("source") or "").strip()
        publishers_used[pub] = publishers_used.get(pub, 0) + 1
        kept_url = kept.get("publisher_link") or kept.get("link")
        for other in ranked[1:]:
            same_story_as[other.get("publisher_link") or other.get("link")] = kept_url

    return {
        "same_story_as": same_story_as,
        "method": got["method"],
        "problem": got["problem"],
        "counts": {
            "eligible": len(eligible),
            "stories": len(groups),
            "multi": len(multi),
            "dropped": len(same_story_as),
        },
    }


# --- Reported, not fixed ----------------------------------------------------


def duplicate_story_report(selected: Sequence[Dict[str, Any]], *, threshold: float = 0.6):
    """Report how often the selected set holds one story from several outlets.

    `dedupe_stories` acts before selection. What this still finds afterwards,
    by title overlap, is what the story model missed.

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
