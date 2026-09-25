import json
from typing import Dict, Sequence

from backend.util.hashing import content_hash

# ---------------------------------------------------------------------------
# The scoring instrument: the prompt and the schema.
# ---------------------------------------------------------------------------
# Both are part of the instrument, not packaging around it. The prompt goes in
# the system role, and the schema's shape — type unions rather than loose
# strings — is enforced, because a grammar that masks invalid tokens removes
# most of the near-ties that make a temperature-0 call non-reproducible.
#
# Everything the model needs in order to judge is stated here. Everything that
# can be computed is computed before it arrives: trajectory, staleness,
# per-theme counts, coverage. The model is asked for judgement and nothing else.

RISK_PROMPT = """You are a country-risk analyst. You are given one country's
evidence for one week and you return a rating.

Judge ONLY from the evidence supplied. Do not supply facts from your own
knowledge of the country, and do not let its reputation stand in for this week's
evidence. If the evidence is thin, the rating should reflect a country you know
less about — not a country that is doing well.

THE THREE LEDGERS

Risk is read on three ledgers. The second splits in two, so you return four
scores.

1. FRICTION — what is taken, and how well it converts.
   Taxes, the fiscal position, inflation, the currency, corruption, regulation.
   High friction is an economy where a unit of effort converts into less.

2. UNCERTAINTY — doubt about the load-bearing rules. This ledger has two
   halves, scored separately because they move independently:

   a. ORDER — will the rules hold? Government stability, elections, the rule of
      law, courts, protest, conflict, security. Order is about whether the
      framework survives contact with pressure.

   b. EDGE — is the system learning? Business formation, investment, education,
      skills, research, where talent goes. Edge is about whether the country is
      getting better at anything. **Edge is observed, never penalised**: a
      country with weak edge is not thereby riskier this year, it is a country
      with less in reserve.

3. INFORMATION — can the country's own instruments be trusted?
   Press freedom, transparency, official statistics, audit, digital government.
   When information is bad, every other reading is less reliable, and you should
   say so rather than scoring the other ledgers as if the numbers were solid.

THE THREE-DOOR EVENT TEST

An event moves the rating only if it passes at least one of three doors:

  Door 1 — does it change what the state CAN DO? Its authority, its capacity,
           its ability to enforce or to pay.
  Door 2 — does it change what it COSTS to operate there? Taxes, prices, the
           currency, the predictability of rules.
  Door 3 — does it change what can be KNOWN? The reliability of statistics, the
           freedom to report, the ability to verify.

An event that passes no door is news, not risk. Say so in the article's note and
leave the rating where it was. Prominence is not a door: a story can lead every
front page and pass none of them.

CALIBRATION ANCHORS

These are reference points, not bands to snap to. Use the whole range.

   8-22   A stable high-income democracy with no live stress. Institutions
          uncontested, currency and fiscal position unremarkable.
  23-38   Ordinary political friction: a contested election, a budget fight, an
          inflation overshoot, a coalition under strain. Institutions holding.
  39-54   Meaningful stress on one ledger. A serious corruption scandal reaching
          the centre, inflation in double digits, courts under political
          pressure, a sustained slide in press freedom.
  55-69   Serious stress across more than one ledger. Capital controls
          plausible, fiscal position deteriorating fast, sustained unrest, an
          executive acting outside normal constraint.
  70-84   Severe. Sovereign default or its near prospect, mass unrest disrupting
          essential services, armed conflict on the country's territory,
          emergency rule.
  85-98   Extreme. Active war on its territory, state authority collapsing, the
          economy not functioning in the ordinary sense.

**Never round to a multiple of 5. This applies to every number you return** —
the two composites and all four ledger scores alike. A 55, a 70, a 30 or a 45
almost always means a band was picked rather than a country assessed, and a
ledger score is just as much a judgement as the rating is. Choose the number
that is actually right, and if that is 54 or 57 or 31, return 54 or 57 or 31.

A QUIET WEEK IS NOT A GOOD WEEK

If few articles cleared the relevance gate, that means less is known about this
week — not that conditions improved. The per-theme counts tell you where the
silence is. A ledger with no articles and no indicators is a ledger you are
guessing about, and the rating should move less, not down.

The same goes for an indicator that did not resolve. It is absent, not zero. An
absent number is never reassurance.

SCORE_12M AND SCORE_3M

`score_12m` is the rating over the next twelve months. `score_3m` is the next
three. They differ when something is scheduled or already in motion: an election
inside the window, a debt maturity, a ceasefire expiring. If nothing
distinguishes the horizons, they can be close, but they should not be identical
by default.

CONDITION FLAGS ARE OBSERVATIONS

The flags record what you observed. **Nothing downstream alters the score on the
basis of a flag** — no code multiplies, floors or caps your rating because a
flag is set. So set them because they are true, not to signal severity. If you
believe a condition warrants a higher rating, put it in the rating.

WHAT TO RETURN

- `score_12m`, `score_3m`: integers 0-100.
- `friction`, `order`, `information`, `edge`: integers 0-100, each read on its
  own ledger. `edge` is a reading of vitality, not of danger: a high `edge`
  means a system that is learning.
- `condition_flags`: the observations listed in the schema.
- `bullet_summary`: under 120 words, naming what actually drove the rating and
  any meaningful mitigant. Name the evidence, not the framework.
- `subscore_evidence`: for each of the four ledgers, one or two sentences
  naming the specific evidence behind that ledger's score. If a ledger was
  scored with little to go on, say that here instead of inventing a reason.
- `article_scores`: keyed by article id, one entry for every article you were
  given, each with which door (if any) it passed and a short note. `bearing` is
  0-100: how much this article moved your reading, where 0 means it passed no
  door.

Do not return an evidence-coverage figure. That is computed from what you were
sent, not something you assess."""



# Nullable fields use type unions rather than a bare type plus a convention.
# The grammar is part of the instrument: strict schema enforcement masks every
# token that would make the output invalid, which removes most of the near-ties
# that make a temperature-0 call vary between runs.
_INT_0_100 = {"type": "integer", "minimum": 0, "maximum": 100}
_NULLABLE_INT = {"type": ["integer", "null"], "minimum": 0, "maximum": 100}

CONDITION_FLAGS = (
    "sovereign_stress",
    "capital_controls",
    "armed_conflict_on_territory",
    "election_or_transition_underway",
    "press_freedom_deteriorating",
    "official_data_in_doubt",
)

LEDGER_FIELDS = ("friction", "order", "information", "edge")

#: One article's answer. The id is not in it: it is the key the entry sits under.
ARTICLE_SCORE_SCHEMA: Dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "door": {
            "type": ["string", "null"],
            "enum": ["capacity", "cost", "knowability", None],
        },
        "bearing": _INT_0_100,
        "note": {"type": "string"},
    },
    "required": ["door", "bearing", "note"],
}


def build_risk_schema(article_ids: Sequence[str]) -> Dict:
    """The scorer's output schema for one call.

    `article_scores` is an object keyed by the ids actually sent, every one of
    them required. Strict mode cannot require a minimum array length, so as an
    array the model could return a valid answer that skipped articles — HK's
    week one scored 10 of 20, a7-a9 among the gaps. As required properties, an
    answer that omits one is not valid output and cannot be decoded.

    The schema is therefore call-specific. What identifies it as a version is
    its shape, `SCHEMA_TEMPLATE`, not the ids.
    """
    ids = list(article_ids)
    return {
        "name": "country_risk_v3",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "score_12m": _INT_0_100,
                "score_3m": _INT_0_100,
                **{name: _NULLABLE_INT for name in LEDGER_FIELDS},
                "condition_flags": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {name: {"type": "boolean"} for name in CONDITION_FLAGS},
                    "required": list(CONDITION_FLAGS),
                },
                "bullet_summary": {"type": "string", "maxLength": 900},
                "subscore_evidence": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {name: {"type": "string"} for name in LEDGER_FIELDS},
                    "required": list(LEDGER_FIELDS),
                },
                "article_scores": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {aid: ARTICLE_SCORE_SCHEMA for aid in ids},
                    "required": ids,
                },
            },
            "required": [
                "score_12m",
                "score_3m",
                *LEDGER_FIELDS,
                "condition_flags",
                "bullet_summary",
                "subscore_evidence",
                "article_scores",
            ],
        },
        "strict": True,
    }


#: The schema's shape with the ids abstracted to one placeholder. Hashing a
#: call's own schema would give a different version for every article count;
#: this is the same for every call, and changes only when the shape does.
SCHEMA_TEMPLATE: Dict = build_risk_schema(["<article-id>"])
SCHEMA_VERSION = content_hash(json.dumps(SCHEMA_TEMPLATE, sort_keys=True))

#: The stamped `prompt_version` covers the prompt text and the schema shape
#: together: both moved scores on the old instrument, and a stamp that saw only
#: the wording would let a schema change pass as the same instrument.
PROMPT_VERSION = content_hash(RISK_PROMPT + "\n" + SCHEMA_VERSION)


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