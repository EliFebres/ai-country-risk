import json
from typing import Any, Dict, List, Sequence, Tuple

from backend.util.hashing import content_hash

# ---------------------------------------------------------------------------
# The scoring instrument: the checklist prompt, the schema, and the arithmetic.
# ---------------------------------------------------------------------------
# The model is never asked for a number. Step 5 (docs/scorer-measurement.md)
# showed that a model asked for one 0–100 rating picks from a short menu of
# band edges and switches picks between calls on an identical payload. So the
# model answers sixteen narrow questions, each a level 0–4 with the evidence
# behind it, and code does everything else: the economic data is ranked
# against the roster (`data_score`), the levels and percentiles are combined
# (`combine`), and the composite is arithmetic over many small readings.
#
# The prompt text, the schema's shape and the arithmetic constants are one
# instrument, and `PROMPT_VERSION` covers all three.

#: The sixteen questions: (id, ledger, text). The text is the brief's, verbatim,
#: and is what the model reads under its ledger heading. `edge` is observed and
#: never enters the composite (`SCORING["composite_weights"]["edge"] == 0`).
QUESTIONS: Tuple[Tuple[str, str, str], ...] = (
    ("F1", "friction", "Taxes and fiscal policy. 0 stable and predictable; 1 routine changes, debated normally; 2 abrupt or significant new taxes or cuts, a budget fight with consequences; 3 retroactive or confiscatory measures, a fiscal crisis response; 4 expropriation or a collapse of fiscal policy."),
    ("F2", "friction", "Inflation and prices. 0 near target; 1 moderately above target or easing; 2 persistently high, or price controls introduced; 3 double-digit and rising; 4 very high inflation or hyperinflation, with shortages."),
    ("F3", "friction", "Currency and capital. 0 stable, freely convertible; 1 ordinary volatility; 2 sharp depreciation or intervention; 3 capital controls, FX shortages or multiple exchange rates; 4 currency collapse or a freeze on moving money out."),
    ("F4", "friction", "Regulation, permits and courts for business. 0 predictable; 1 routine new rules; 2 burdensome or unpredictable changes, arbitrary enforcement against firms; 3 targeted seizures, forced sales or abuse of regulators; 4 rule by decree over property and contracts."),
    ("F5", "friction", "Corruption. 0 none reported, or institutions acting on it normally; 1 isolated cases prosecuted; 2 a scandal reaching senior officials; 3 corruption at the centre of government, or anti-corruption bodies disabled; 4 state capture."),
    ("F6", "friction", "Sovereign debt and financing. 0 no stress; 1 rising costs, debated; 2 downgrade, a failed or costly auction, an IMF discussion; 3 an IMF programme under strain, arrears, a restructuring announced; 4 default."),
    ("O1", "order", "Government stability. 0 stable; 1 ordinary coalition friction; 2 a government crisis, a confidence vote, key resignations; 3 collapse of government, or a disputed succession; 4 no functioning government, or a coup."),
    ("O2", "order", "Elections and transfer of power. 0 routine or none scheduled; 1 contested but orderly; 2 serious irregularities alleged, opposition excluded, results disputed; 3 an election postponed or annulled, violence around it; 4 power seized or held unconstitutionally."),
    ("O3", "order", "Rule of law and checks on power. 0 courts independent, executive constrained; 1 routine disputes between branches; 2 pressure on courts, emergency powers debated; 3 the executive defying courts, emergency rule in force; 4 constitutional order suspended."),
    ("O4", "order", "Protest and civil unrest. 0 none beyond routine; 1 peaceful protests; 2 large or sustained protests, arrests; 3 violent unrest, lethal force used; 4 insurrection or unrest halting normal life."),
    ("O5", "order", "Armed conflict and external threat. 0 none; 1 diplomatic tension; 2 military threats, incidents or strikes near the territory (a strike or attack is near, never on, the territory if it landed in a neighbouring country or in international waters, was intercepted in the country's airspace, is described as near, close to, off the coast of or across the border from the country, or the article does not say where it landed); 3 a strike or attack on the territory (only if the article says it landed, hit or occurred inside the country's land borders or territorial waters), or active mobilisation; 4 war on the territory."),
    ("O6", "order", "Political violence and terrorism. 0 none; 1 isolated incidents; 2 attacks on officials, candidates or journalists; 3 a campaign of attacks; 4 violence that governs politics."),
    ("I1", "information", "Press freedom. 0 free; 1 isolated pressure; 2 journalists detained or outlets closed; 3 a systematic crackdown; 4 no independent press."),
    ("I2", "information", "Official statistics and audit. 0 credible; 1 routine revisions and disputes; 2 data delayed, withheld or credibly questioned, or audit bodies obstructed; 3 official figures manipulated; 4 no credible official data."),
    ("I3", "information", "Transparency and information access. 0 open; 1 minor restrictions; 2 internet or platform restrictions, secrecy laws; 3 shutdowns, broad censorship; 4 information blackout."),
    ("E1", "edge", "Business formation and investment. 0 strong and rising; 1 normal; 2 weakening, notable exits; 3 capital and firms leaving; 4 collapse of private investment."),
)

QUESTION_IDS: Tuple[str, ...] = tuple(q[0] for q in QUESTIONS)
LEDGER_OF_QUESTION: Dict[str, str] = {q[0]: q[1] for q in QUESTIONS}
QUESTIONS_BY_LEDGER: Dict[str, Tuple[str, ...]] = {
    ledger: tuple(q[0] for q in QUESTIONS if q[1] == ledger)
    for ledger in ("friction", "order", "information", "edge")
}
QUESTION_TEXT: Dict[str, str] = {q[0]: q[2] for q in QUESTIONS}

EVENT_DIRECTIONS: Tuple[str, ...] = ("up", "down", "unclear")

#: The prompt, verbatim from the step 5b brief with the step 5c edits. `{country}` is substituted by
#: plain replacement at call time (not `.format`, so braces elsewhere are safe).
CHECKLIST_PROMPT = """You are a country-risk analyst. Today is {as_of}. You are reading the news about {country} published in the 30 days before today. Answer each question below with a level from 0 to 4, using ONLY these articles. Do not use your own background knowledge of {country}, and do not let its reputation stand in for evidence. Judge each condition as it stands today: an article about an earlier event counts only if the event happened in the last 30 days or the article says the condition is still going on.

For every question, answer in this order:
- evidence: the article ids (a1, a2, ...) that bear on the question. Empty if none do.
- quote: up to 25 words copied exactly from one of the cited articles (its title, digest or full text) that shows the level. Copy, do not paraphrase. Empty if evidence is empty.
- note: one sentence naming what the evidence shows.
- level: 0 to 4 using the descriptions given, or null if the articles say nothing about it. Null is correct and expected for many questions. Use 0 only when an article shows normal functioning, never because nothing was reported. A level other than null MUST have evidence and a quote.

The levels mean the same thing on every question:
  0 = no stress: the evidence shows normal functioning.
  1 = minor: friction or dispute within ordinary politics and markets.
  2 = meaningful: a real problem that an investor would price.
  3 = serious: a problem that threatens the rules, the currency, or public order.
  4 = severe: breakdown, or on the edge of it.

FRICTION: what is taken, and how well effort converts
F1 Taxes and fiscal policy. 0 stable and predictable; 1 routine changes, debated normally; 2 abrupt or significant new taxes or cuts, a budget fight with consequences; 3 retroactive or confiscatory measures, a fiscal crisis response; 4 expropriation or a collapse of fiscal policy.
F2 Inflation and prices. 0 near target; 1 moderately above target or easing; 2 persistently high, or price controls introduced; 3 double-digit and rising; 4 very high inflation or hyperinflation, with shortages.
F3 Currency and capital. 0 stable, freely convertible; 1 ordinary volatility; 2 sharp depreciation or intervention; 3 capital controls, FX shortages or multiple exchange rates; 4 currency collapse or a freeze on moving money out.
F4 Regulation, permits and courts for business. 0 predictable; 1 routine new rules; 2 burdensome or unpredictable changes, arbitrary enforcement against firms; 3 targeted seizures, forced sales or abuse of regulators; 4 rule by decree over property and contracts.
F5 Corruption. 0 none reported, or institutions acting on it normally; 1 isolated cases prosecuted; 2 a scandal reaching senior officials; 3 corruption at the centre of government, or anti-corruption bodies disabled; 4 state capture.
F6 Sovereign debt and financing. 0 no stress; 1 rising costs, debated; 2 downgrade, a failed or costly auction, an IMF discussion; 3 an IMF programme under strain, arrears, a restructuring announced; 4 default.

ORDER: will the rules hold
O1 Government stability. 0 stable; 1 ordinary coalition friction; 2 a government crisis, a confidence vote, key resignations; 3 collapse of government, or a disputed succession; 4 no functioning government, or a coup.
O2 Elections and transfer of power. 0 routine or none scheduled; 1 contested but orderly; 2 serious irregularities alleged, opposition excluded, results disputed; 3 an election postponed or annulled, violence around it; 4 power seized or held unconstitutionally.
O3 Rule of law and checks on power. 0 courts independent, executive constrained; 1 routine disputes between branches; 2 pressure on courts, emergency powers debated; 3 the executive defying courts, emergency rule in force; 4 constitutional order suspended.
O4 Protest and civil unrest. 0 none beyond routine; 1 peaceful protests; 2 large or sustained protests, arrests; 3 violent unrest, lethal force used; 4 insurrection or unrest halting normal life.
O5 Armed conflict and external threat. 0 none; 1 diplomatic tension; 2 military threats, incidents or strikes near the territory (a strike or attack is near, never on, the territory if it landed in a neighbouring country or in international waters, was intercepted in the country's airspace, is described as near, close to, off the coast of or across the border from the country, or the article does not say where it landed); 3 a strike or attack on the territory (only if the article says it landed, hit or occurred inside the country's land borders or territorial waters), or active mobilisation; 4 war on the territory.
O6 Political violence and terrorism. 0 none; 1 isolated incidents; 2 attacks on officials, candidates or journalists; 3 a campaign of attacks; 4 violence that governs politics.

INFORMATION: can what you know about {country} be trusted
I1 Press freedom. 0 free; 1 isolated pressure; 2 journalists detained or outlets closed; 3 a systematic crackdown; 4 no independent press.
I2 Official statistics and audit. 0 credible; 1 routine revisions and disputes; 2 data delayed, withheld or credibly questioned, or audit bodies obstructed; 3 official figures manipulated; 4 no credible official data.
I3 Transparency and information access. 0 open; 1 minor restrictions; 2 internet or platform restrictions, secrecy laws; 3 shutdowns, broad censorship; 4 information blackout.

EDGE: is the system learning (observed only)
E1 Business formation and investment. 0 strong and rising; 1 normal; 2 weakening, notable exits; 3 capital and firms leaving; 4 collapse of private investment.

SCHEDULED EVENTS
List every dated event in these articles that falls within the 90 days after today and could change the answers above: an election, a debt maturity, a budget deadline, a ceasefire expiry, a court ruling. For each give: the article id, the date, the event, the question it bears on (for example O2), and direction (up = more stress likely, down = less, unclear). Give an empty list if there are none. Never invent a date. Only list an event whose date is written in the article. Do not list routine, recurring deadlines such as tax filing dates, statistical releases or regular parliamentary sessions."""


#: What the scorer is told the country is when its input is masked (step 5e,
#: Fix 8). The questions never name the country, so the label is the only
#: place the name would have appeared.
MASKED_COUNTRY_LABEL = "the country"


def prompt_for(country_name: str, as_of: Any = None) -> str:
    """The checklist prompt with the country's label and the run date filled in.

    `country_name` is the real name for a named run and `MASKED_COUNTRY_LABEL`
    for a masked one. `as_of` is the run date (ISO string or date); the prompt
    tells the model what "today" is because retrieval reaches back 30 days and
    the articles carry their own dates.
    """
    as_of_text = as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of or "")
    return CHECKLIST_PROMPT.replace("{country}", country_name).replace("{as_of}", as_of_text)


# ---------------------------------------------------------------------------
# The arithmetic constants. Eli's to change. All of them are inside
# PROMPT_VERSION, so a changed weight is a changed instrument and the stamped
# rows say so.
# ---------------------------------------------------------------------------
SCORING: Dict = {
    # A question's level runs 0..level_max. Each question is an item worth
    # news_scale × level when answered with valid evidence (or carried
    # forward), and the ledger's data score when null: silence means "what
    # the data says", never "no stress" and never "ignored". The news score
    # for a ledger is the mean of its items, so 0..100, and one answered
    # question moves its ledger by 1/n of the gap rather than setting it.
    "level_max": 4,
    "news_scale": 25,
    "null_item": "data_score",
    # ledger_score = data × ledger_weights.data + news × ledger_weights.news,
    # or data alone when every news level is null, or news alone when no
    # indicator resolved.
    "ledger_weights": {"data": 0.5, "news": 0.5},
    # score_12m = round(Σ composite_weights[ledger] × ledger_score). Edge is
    # observed and never enters the composite. Weights are renormalised over
    # the ledgers that have a score when one is null.
    "composite_weights": {"friction": 0.4, "order": 0.4, "information": 0.2, "edge": 0.0},
    # score_3m = score_12m + clamp(Σ ±event_step per scheduled event,
    # −event_cap, +event_cap); "unclear" counts 0; no events → equal.
    "event_step": 3,
    "event_cap": 9,
    # A scheduled event counts only when its date is inside this window after
    # the run date.
    "event_horizon_days": 90,
    # Null carry-forward in production: a null question takes the most recent
    # original level from the last carry_forward_runs snapshots, none older
    # than carry_forward_max_days.
    "carry_forward_runs": 8,
    "carry_forward_max_days": 70,
}

#: How many scorer calls production makes per country before looking at the
#: votes. 3 unanimous calls are merged as the per-question median of three
#: (`combine.merge_answers`); a split vote gets more calls, not the lower level
#: (step 5e, Fix 7): VOTE_CALLS in all, merged by plurality
#: (`combine.vote_merge`), and one more call per remaining tie up to
#: VOTE_MAX_CALLS. A question is non-null once VOTE_MIN_ANSWERED calls
#: answered it. None of these is inside PROMPT_VERSION: they change how many
#: times the instrument is read, not what the instrument is.
CALLS_PER_COUNTRY = 3
VOTE_CALLS = 5
VOTE_MAX_CALLS = 7
VOTE_MIN_ANSWERED = 3


# Nullable fields use type unions rather than a bare type plus a convention.
# The grammar is part of the instrument: strict schema enforcement masks every
# token that would make the output invalid, which removes most of the near-ties
# that make a temperature-0 call vary between runs.
_LEVEL = {"type": ["integer", "null"], "minimum": 0, "maximum": SCORING["level_max"]}

CONDITION_FLAGS = (
    "sovereign_stress",
    "capital_controls",
    "armed_conflict_on_territory",
    "election_or_transition_underway",
    "press_freedom_deteriorating",
    "official_data_in_doubt",
)

LEDGER_FIELDS = ("friction", "order", "information", "edge")

#: One question's answer. The id is the key the entry sits under. Evidence is a
#: list of free strings; the validator keeps only the article ids that were sent
#: and counts the rest. The order is the instrument (step 5e, Fix 4): structured
#: outputs generate in schema order, so the model writes its evidence and the
#: quote before it commits to a level, and a level with nothing behind it has
#: nowhere to come from. The quote is checked by code against the cited
#: articles' text (Fix 3).
QUESTION_ANSWER_SCHEMA: Dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "evidence": {"type": "array", "items": {"type": "string"}},
        "quote": {"type": "string"},
        "note": {"type": "string"},
        "level": _LEVEL,
    },
    "required": ["evidence", "quote", "note", "level"],
}

SCHEDULED_EVENT_SCHEMA: Dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "article_id": {"type": "string"},
        "date": {"type": "string"},
        "event": {"type": "string"},
        "question": {"type": "string", "enum": list(QUESTION_IDS)},
        "direction": {"type": "string", "enum": list(EVENT_DIRECTIONS)},
    },
    "required": ["article_id", "date", "event", "question", "direction"],
}


def build_checklist_schema(article_ids: Sequence[str] = ()) -> Dict:
    """The scorer's output schema for one call.

    Every question is a required key, so an answer that skips one cannot be
    decoded. The article ids are not in the grammar: evidence is free text
    that the validator checks against the ids sent, which keeps the schema
    the same shape for every call and lets a wrong citation be counted rather
    than masked.
    """
    return {
        "name": "country_risk_checklist_v1",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                **{qid: QUESTION_ANSWER_SCHEMA for qid in QUESTION_IDS},
                "scheduled_events": {"type": "array", "items": SCHEDULED_EVENT_SCHEMA},
                "bullet_summary": {"type": "string", "maxLength": 900},
                "condition_flags": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {name: {"type": "boolean"} for name in CONDITION_FLAGS},
                    "required": list(CONDITION_FLAGS),
                },
            },
            "required": [
                *QUESTION_IDS,
                "scheduled_events",
                "bullet_summary",
                "condition_flags",
            ],
        },
        "strict": True,
    }


#: The schema's shape. It no longer depends on the article ids, so this is the
#: schema itself; the name is kept because the version and the tests read it.
SCHEMA_TEMPLATE: Dict = build_checklist_schema(["<article-id>"])
SCHEMA_VERSION = content_hash(json.dumps(SCHEMA_TEMPLATE, sort_keys=True))

#: The questions alone, for carry-forward: a level carried from a previous week
#: must have been answered to the same question.
QUESTIONS_VERSION = content_hash(json.dumps(QUESTIONS))

#: The stamped `prompt_version` covers the prompt text, the schema shape and the
#: arithmetic constants together. Each of them moves the score, and a stamp that
#: saw only the wording would let a weight change pass as the same instrument.
PROMPT_VERSION = content_hash(
    CHECKLIST_PROMPT + "\n" + SCHEMA_VERSION + "\n" + json.dumps(SCORING, sort_keys=True)
)


# ---------------------------------------------------------------------------
# Economic-calendar importance ranking (US-tilted)
# Used by ai/calendar_ranker.py to rank upcoming events for the Econ Calendar.
# NOTE: literal braces inside JSON examples are escaped as {{ }} for .format().
# ---------------------------------------------------------------------------

CAL_RANK_PROMPT = """
You are a senior markets strategist. Your audience is **primarily US-based investors**.

You are given a set of economic-calendar events that ALL fall within a SINGLE week
({period}; today is {today}). Rank them **relative to each other within this week**.

EVENTS_JSON
# exactly these items only; each has an id you MUST reuse
# [{{"id":"e1","date":"YYYY-MM-DD","country":"...","event":"...","fmp_importance":"h|m|l"}}]
{events_json}

importance ∈ [0,1] = this event's importance to investors' positioning **relative to the other
events in this week**. Spread your scores across the FULL 0-1 range: the week's most market-moving
event(s) should approach 1.0 and the least important approach ~0.10, with the rest distributed in
between. Even a quiet week MUST have its own clear top and bottom — do NOT compress everything into a
narrow band, and do NOT hold back high scores just because some other week might be busier.

When deciding which events OUTRANK others, weigh relevance to **US markets slightly higher** than the
rest of the world (the audience is primarily US-based), judging by event type, the issuing country's
weight in global markets, and spillover into US rates, equities, credit, and the US dollar.

Ordering guidance (most → least important, all else equal):
  • US monetary policy & top US data — FOMC/Fed decisions & minutes, US CPI/PCE, US jobs (NFP/payrolls).
  • Major global central banks (ECB, BoJ, BoE, PBoC) & first-tier data (GDP, CPI, PMIs) from large
    economies with clear spillover to US assets.
  • Mid-tier data and releases from smaller economies.
  • Minor / low-relevance releases.

Rules:
  • Score EVERY id provided. Do NOT invent ids or add events.
  • rationale ≤ 140 characters, concise, explains the ranking (e.g. "Top US rates driver this week").

Return ONLY valid JSON (no prose) exactly:

{{
  "rankings": [
    {{"id": "<id from EVENTS_JSON>", "importance": <float 0..1>, "rationale": "<=140 chars"}}
  ]
}}
""".strip()


CAL_RANK_SCHEMA: Dict = {
    "title": "CalendarImportanceRanking",
    "description": "Per-event investor-importance score (US-tilted) with a short rationale.",
    "type": "object",
    "properties": {
        "rankings": {
            "title": "Rankings",
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id":         {"type": "string"},
                    "importance": {"type": "number", "minimum": 0, "maximum": 1},
                    "rationale":  {"type": "string", "maxLength": 160},
                },
                "required": ["id", "importance", "rationale"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["rankings"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Global news-alert ranking
# Used by ai/alerts_ranker.py to rank each run's pooled Top-3 country articles
# by importance to the GLOBAL economy, tagging a fixed topic + severity label.
# NOTE: literal braces inside JSON examples are escaped as {{ }} for .format().
# ---------------------------------------------------------------------------

# Fixed topic taxonomy. The model MUST pick exactly one of these per alert
# (enforced as an enum in ALERTS_RANK_SCHEMA). "Macro" covers monetary policy.
ALERT_TOPICS = [
    "Conflict",
    "Sanctions",
    "Macro",
    "Politics",
    "Trade",
    "Energy",
    "Security",
    "Markets",
]

# Fixed severity labels (enum in ALERTS_RANK_SCHEMA), AI-judged per alert.
ALERT_SEVERITIES = ["Critical", "Caution", "Watch"]


ALERTS_RANK_PROMPT = """
You are a senior global-macro strategist. You are given a pool of news articles, each
already selected as a top story for its country. Rank them **relative to each other** by
their importance to the **global economy** right now (today is {today}).

ARTICLES_JSON
# exactly these items only; each has an id you MUST reuse
# [{{"id":"g1","country":"...","source":"...","published_at":"YYYY-MM-DD","title":"...","summary":"..."}}]
{articles_json}

For EACH article return four things:

1) importance ∈ [0,1] — its importance to the GLOBAL economy **relative to the other
   articles in this pool**. Spread your scores across the FULL 0-1 range: the most
   globally consequential story should approach 1.0 and the most local/minor approach
   ~0.05, with the rest distributed in between. Weigh: the size/centrality of the economy
   involved, cross-border spillover into global rates/equities/credit/commodities/FX,
   and how binding/material (vs rhetorical) the development is.

2) topic — EXACTLY ONE label from this fixed list (no others):
   Conflict, Sanctions, Macro, Politics, Trade, Energy, Security, Markets.
   Guidance: Conflict = war/military strikes; Sanctions = export controls/designations;
   Macro = inflation/GDP/growth AND central-bank/monetary policy; Politics =
   elections/government/coups; Trade = tariffs/trade deals; Energy = oil/gas/power;
   Security = terrorism/unrest/crime; Markets = currency/debt/equities/financial system.

3) severity — EXACTLY ONE of: Critical, Caution, Watch.
   • Critical — active war or major escalation, binding sanctions on a large economy,
     sovereign default/financial crisis, or a systemic market shock.
   • Caution  — credible escalation, high-probability policy action, or a notable macro
     surprise with clear cross-border spillover.
   • Watch    — localized, rhetorical, or early-stage; worth monitoring but no immediate
     global impact.

4) rationale ≤ 160 characters explaining the ranking (e.g. "Largest oil exporter; supply
   shock lifts global energy prices").

Rules:
  • Score EVERY id provided. Do NOT invent ids or add items.

Return ONLY valid JSON (no prose) exactly:

{{
  "alerts": [
    {{"id": "<id from ARTICLES_JSON>", "importance": <float 0..1>, "topic": "<one of the 8>", "severity": "<Critical|Caution|Watch>", "rationale": "<=160 chars"}}
  ]
}}
""".strip()


ALERTS_RANK_SCHEMA: Dict = {
    "title": "GlobalNewsAlertRanking",
    "description": "Per-article global-economy importance, fixed topic + severity, and a short rationale.",
    "type": "object",
    "properties": {
        "alerts": {
            "title": "Alerts",
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id":         {"type": "string"},
                    "importance": {"type": "number", "minimum": 0, "maximum": 1},
                    "topic":      {"type": "string", "enum": ALERT_TOPICS},
                    "severity":   {"type": "string", "enum": ALERT_SEVERITIES},
                    "rationale":  {"type": "string", "maxLength": 200},
                },
                "required": ["id", "importance", "topic", "severity", "rationale"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["alerts"],
    "additionalProperties": False,
}