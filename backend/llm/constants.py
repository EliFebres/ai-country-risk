"""Prompts and JSON schemas for the LLM calls.

Kept apart from the code that issues those calls because this is the file a
human edits when tuning model behavior — the prompts are long, and what the
model is asked to perceive is the actual product logic.

Four pairs live here:
  • ``AI_PROMPT_V3`` / ``RISK_SCHEMA_V3`` — per-country risk scoring (current).
  • ``DIGEST_PROMPT`` / ``DIGEST_SCHEMA`` — stage-1 per-article digestion.
  • ``CAL_RANK_PROMPT`` / ``CAL_RANK_SCHEMA`` — economic-calendar importance.
  • ``ALERTS_RANK_PROMPT`` / ``ALERTS_RANK_SCHEMA`` — global news alerts.

Every schema is used with ``strict=True`` structured output, so the model's
reply is guaranteed to match or the call fails.

**The friction framework.** v3 asks for four ledger scores rather than five
sub-factors, because the three risk-bearing ledgers describe what actually
drives investor risk: *friction* is the wedge (what the state extracts times how
badly it converts), *order-uncertainty* is imposed doubt about the load-bearing
rules, and *information capacity* is instrument quality, which decides how much
the official numbers can be trusted and whether a measured problem compounds or
corrects. The fourth, *edge vitality*, is business churn and invention — the
system learning — and the prompt is explicit that it may never raise a risk
score.

**Nothing downstream edits a score.** Earlier versions split perception from
enforcement: the model perceived, and ``ai/policy.py`` applied floors, caps and
a sanctions override afterwards. That enforcement layer is gone. ``score`` is
now the model's ``score_12m``, always. Sanctioned countries get a
``non_investable`` badge instead of a forced 1.0, and contradictions between
flags and scores are recorded by ``util/lint.py`` as advisory observations
rather than corrected. The prompt says so plainly, because a model that
pre-applies a rule it expects downstream makes its own judgement unrecoverable.

v3 keeps asking for **integers 0-100** rather than 0-1 floats — the coarse grid
cost cross-sectional rank resolution across the roster. ``langchain_llm``
converts every one of them back to 0-1 immediately after the API call, so the
0-100 scale never escapes that boundary: the database and the front-end still
speak 0-1.

The v1 and v2 prompt/schema pairs were deleted when v3 landed. Their exact text
is preserved at the git tag ``prompts-pre-v3``, so historical ``prompt_version``
strings in ``risk_snapshot`` remain resolvable.

Literal braces inside the JSON examples are escaped as ``{{ }}`` because these
strings go through ``str.format()``.
"""

from typing import Dict

# Stamped on every snapshot. Bump when AI_PROMPT_V3 or RISK_SCHEMA_V3 changes
# in a way that could move scores, so a time series can be split on it.
#
# Rows already in the database carry earlier version strings. The prompt text
# behind "v1.0" and "v2.0" was deleted when v3 landed and lives at the git tag
# `prompts-pre-v3` (commit 7604fcb) — so a stored prompt_version still resolves
# to the exact wording that produced it.
# "v3.0-friction-framework" is the patents-in-the-edge-ledger wording; a snapshot
# was scored under it, so the human-capital swap gets its own stamp rather than
# rewriting history under the old one.
# "v4.0-masked-production" is the cutover: masked scoring stopped being a pilot
# experiment and became the regime every row is written under, so the prompt now
# says out loud that the country is unnamed and that the structural block
# carries the facts identity used to imply. Everything else in v3.1 — the three
# ledgers, the three-door event test, edge protection, the learning-outcomes
# wedge, door F, observation-only flags, the calibration anchors — is unchanged,
# which is the point: the wording is the same instrument, pointed at evidence
# with the name taken out.
PROMPT_VERSION = "v4.0-masked-production"

# The prompt as it stands when the payload carries a trailing-context block.
# Appended rather than interpolated, so a payload without the block renders the
# template byte-for-byte as it always has and needs no version of its own.
PROMPT_VERSION_CONTEXT = "v4.1-trailing-context"

# One instruction, and it is deliberately one. The block's whole risk is that a
# model reads last quarter's summary as this week's answer — anchoring on the
# older, longer, more confident-sounding text and arriving late to a turn. So
# the instruction says what the block is *for* (direction) and states the
# precedence explicitly, rather than describing the data, which the JSON already
# labels.
TRAILING_CONTEXT_RULE = """

--- TRAILING CONTEXT ---
`trailing_context` in EVIDENCE_JSON holds one paragraph per calendar quarter for
the four quarters before the live 30-day window. They do not overlap it.

Use them only to judge trajectory: is this country's position improving,
decaying, or holding steady relative to those quarters? Recent evidence
dominates. Where the live window and the trailing context disagree, the live
window is what you are scoring; the context explains what it is a change from.
The paragraphs are evidence, not prior assessments — nothing in them is a score.
"""

# The prompt as it stands when it is told the trend fields exist.
#
# No payload change goes with this one, which is the whole point of it. Every
# indicator entry has carried `trend_1y` and `trend_5y` since p1 and they are
# serialized into every prompt; nothing has ever read them, and the template
# explains `as_of` and `staleness_days` in the same breath without mentioning
# them. So this variant adds a paragraph and not a byte of evidence, and it
# separates "the model needed more" from "the model was never told".
PROMPT_VERSION_TREND = "v4.1-trend-fields"

# Three sentences, and the third is the one that matters. Naming the fields is
# not enough on its own: the model has to be told what the sign means, because
# a rising number is worse for debt and better for growth, and a model left to
# infer that per indicator will infer it inconsistently. The last line is there
# because the failure this is aimed at is a model answering a quiet week with
# the calibration language rather than with the evidence.
TREND_FIELDS_RULE = """

--- TRAJECTORY ---
Most indicators in EVIDENCE_JSON carry `trend_1y` and `trend_5y`: the change in
that indicator's own units over the last one and five years, signed. A positive
`trend_5y` on a debt ratio means the burden grew; a positive one on a growth
rate means growth accelerated. Read the sign against what the indicator
measures, not as good or bad in itself.

Use them to judge trajectory — whether conditions are improving, decaying or
holding — while the articles tell you what is happening now. A level that has
been stable for five years and the same level reached by five years of steady
deterioration are different risks, and only these fields distinguish them.

Where an indicator has no trend field, its history does not reach back far
enough. That is unknown, not flat, and it is not evidence of stability.
"""

# The prompt as it stands when the payload carries the computed trend block.
PROMPT_VERSION_TREND_BLOCK = "v4.2-trend-block"

# Says what the block is *for* and what its vocabulary means, and nothing about
# its shape -- the JSON labels that itself. The last paragraph is the one that
# earns its place: `unknown` and `flat` are the distinction the whole block is
# built to preserve, and a model that reads them as the same thing has been
# handed a stability claim nobody made.
TREND_BLOCK_RULE = """

--- TRAJECTORY ---
`trend` in EVIDENCE_JSON is computed, not reported: directions and changes
derived from the same vintage-bounded series as the evidence above, so it
contains nothing that was not knowable on this date. It holds the last five
annual observations for the headline macro series, the 1-, 3- and 5-year
direction for each ledger constituent, and article counts per theme per quarter.

Use it to judge trajectory — improving, decaying, or holding — while the
articles tell you what is happening now. A level that has been stable for five
years and the same level reached by five years of steady deterioration are
different risks, and this block is what distinguishes them. `accelerating` means
the last year moved faster than the five-year average pace.

`unknown` means the series does not reach back that far at this date. It is not
`flat`, and it is not reassurance. A theme whose article count collapses is a
fact about the reporting rather than about the country: quiet because nothing
happened and quiet because nobody wrote it down are different, and this is the
only place you can tell them apart.
"""

# ---------------------------------------------------------------------------
# v3 — the friction framework. The model judges; nothing downstream edits it.
# Scores are integers 0-100 here for rank resolution and are converted to 0-1
# in langchain_llm the moment the call returns.
# NOTE: literal braces inside JSON examples are escaped as {{ }} for .format().
# ---------------------------------------------------------------------------

AI_PROMPT_V3 = """
You are a senior sovereign risk analyst. Assess investor risk for {country}
as of {as_of_date}, using ONLY the evidence below.
Treat {as_of_date} as today: this evidence is your complete knowledge of the
world. Do not use anything you know about events after this date.

Every value in EVIDENCE_JSON carries `as_of` and `staleness_days` — the date it
became known and how old it is on {as_of_date}. Weigh a fresh reading more than
a stale one, and say so when a stale one is carrying an argument. A missing
indicator is absent from the evidence entirely; treat absence as absence, never
as zero and never as reassurance.

# --- The country is not named, deliberately ---
This evidence describes a real country whose identity has been withheld from
you. Country names, cities, people, parties, currencies and institutions have
been replaced by the roles they play: "the country", "the capital", "the central
bank", "the finance minister", "the local currency". Every NUMBER is untouched —
inflation prints, rates, counts and dates are exactly as published.

Reason only from what is on the page. Do not try to work out which country this
is, and do not let a guess do any work in your reasoning: an inference that
depends on having identified the country is unsound here even when the guess
happens to be right, because you cannot check it and neither can anyone reading
your output.

The priors a name would have carried are supplied instead. When EVIDENCE_JSON
contains a `structural` block, it states what identity used to imply — whether
the government borrows in a currency it can issue, whether it can devalue at
all, its income group, its coarse region, whether it depends on commodity
exports. Use those facts directly. A debt burden means one thing for a
`monetary_sovereignty: full` issuer of a `reserve_currency: major` and something
different for a `constrained` borrower whose debt is in money it cannot print;
read the block, do not reconstruct it from a hunch about the name. When the
block is absent, that structure is simply unknown — treat it as absent, the same
as any missing indicator, and do not substitute a guess.

EVIDENCE_JSON
{evidence_json}

ARTICLES_JSON
{articles_json}

FULL_TEXT
{full_text_block}

# --- The three ledgers ---

FRICTION is the wedge: what the state extracts multiplied by how much of it
fails to convert into capability. Judge the take by how it converts, not by its
size. A high tax burden that funds functioning courts, roads and registries is
not friction; a modest one that funds nothing is. `frictional_extraction` in
the computed block is that product, and `doom_loop` says whether the burden is
rising while conversion decays — trajectory matters more than level, because a
heavy but stable wedge can be carried indefinitely and a compounding one cannot.

ORDER-UNCERTAINTY is imposed doubt about the load-bearing rules — the ones
capital cannot price around: whether contracts will be enforced as written,
whether the currency will hold its function, whether the published statistics
mean what they say, and whether succession is settled. This is not the same as
volatility. A country can be turbulent and legible, or calm and unreadable; the
second is worse for an investor, because there is nothing to underwrite against.

INFORMATION is instrument quality, and it sets both trust and drift. Where the
statistical system, the auditors and the press are strong, official numbers can
be taken near face value. Where they are weak, official numbers deserve a
haircut: lean on market-observed series (exchange rates, policy rates, reserves)
and on article evidence instead, and treat measured friction as compounding
rather than mean-reverting — a state that cannot see itself does not self-correct.

# --- Edge vitality: report it, never penalize it ---

Entry-and-exit churn — startup formation AND startup failure — and human-capital
formation are the system learning. They MUST NOT raise any risk score. A country
where firms are born and die quickly is discovering what works; a country where
nothing is created and nothing fails is not stable, it is inert. Failure counts
as vitality here.

Learning outcomes lead; education spending is the effort line. Read them
together, never the spending alone. High spending with weak learning outcomes is
the wedge made visible inside a school system — money extracted and not
converted into capability. Read that gap as friction evidence, not as edge
credit.

Score `edge_vitality` as an independent reading of that adaptive capacity —
higher means more vitality — and do not let a high value raise friction,
order-uncertainty, or either horizon score.

# --- The three-door event test (apply to every article) ---

An event matters only if it passes through one of three doors:
  F — it changes the wedge (extraction, or how well extraction converts).
      Reported waves of skilled departure — doctors, engineers and founders
      leaving the country — pass through this door: the population grading the
      wedge with their feet. There is deliberately no data series for this, so
      these articles are its only instrument.
  U — it destabilizes the order (contracts, currency, statistics, succession)
  I — it changes the instruments (statistics office, auditors, courts, press)
Everything else is noise, however dramatic the headline. Natural disasters with
no fiscal or contractual aftermath, weapons demonstrations, military parades,
diplomatic insults, celebrity politics and scandal without institutional
consequence do not move a score. Name the door in your reasoning; if an article
passes through none of them, its impact is low no matter how prominent it is.

# --- Manufactured calm ---

When `suppressed_vol_flag` is true, measured calm is evidence AGAINST the
country, not for it. A currency held quiet under a managed or pegged regime
while reserves drain is accumulating fuel load, and the observed stability is
the cost of that accumulation rather than evidence of strength. Read a low
measured volatility in that state as a larger, later move — not a smaller one.
When the flag is null, one of its inputs is missing; that is not a false.

# --- Scoring mechanics ---

All scores are INTEGERS 0-100. Use precise values (37, 62, 81) — never round
to multiples of 5. Neighboring countries must be distinguishable.

Direction, stated explicitly because three of these four read as risk and one
does not:
  friction              higher = a worse wedge
  order_uncertainty     higher = less legible, less underwritable
  information_capacity  higher = WORSE instruments. Despite the name, this is
                        scored as risk: 90 means the statistics, auditors and
                        press cannot be trusted and official numbers need a
                        large haircut; 10 means they can be taken near face
                        value. A country with a strong statistical system
                        scores LOW here.
  edge_vitality         higher = MORE vitality, and this one is not risk. It is
                        the only score where a high number is a good thing, and
                        it must not raise score_3m or score_12m.

Scoring bands (guidance; use the full range):
  5-20 Low · 20-40 Low-Moderate · 40-75 Moderate · 75-90 High · 90-98 Extreme

Calibration anchors — composite scenarios, not real countries:
  ~12  Stable developed market: routine politics, ~2% inflation, no security
       events.
  ~38  EM with a contested but constitutional election, ~9% inflation,
       currency pressure, no violence.
  ~58  Sustained nationwide protests with sporadic violence, caretaker
       cabinet, ~20% inflation, FX reserves falling.
  ~85  Capital controls or default negotiations underway; unrest disrupting
       essential services.
  ~95  Interstate war on the country's territory, or nationwide shutdown.

# --- Localization & Materiality ---
Do NOT raise risk for indirect foreign tensions or rhetoric. Elevate risk
ONLY when evidence shows kinetic activity on {country}'s territory, imminent
hostilities, or economically binding policy affecting {country}. Indirect
disputes, UN votes, or rhetoric without domestic transmission = low impact.

# --- Per-article impact and topic clustering (CRITICAL) ---
Impact is an INTEGER 0-100:
  85-100 Severe — successful kinetic activity in/against {country}, mass
         kidnappings, binding economic measures, major infrastructure
         sabotage, seizure or rewriting of contracts, capture of the
         statistics office or the courts.
  60-75  Moderate — credible mobilization with specific capabilities or
         timelines, high-probability binding sanctions, a serious challenge
         to one of the load-bearing rules.
  40-55  Mixed/unclear — indirect third-country events, uncertain
         transmission.
  10-35  Low/benign — rhetoric, symbolic acts, alert-level changes without
         disruption, and anything that passes through none of the three doors.

You MUST assign the same topic_group to articles covering the same underlying
event, even when the headlines differ. Aggregation: within a topic_group take
the max impact. When calibrating ledger scores, weigh:
  • Persistence — the same topic_group across 7+ days (by published_at)
    counts one band higher.
  • Breadth — multiple independent severe topic_groups within a 30-day window
    justifies moving into High.
  • Singularity — a lone topic_group with no spread does not move the country
    into High on its own.

Example of SAME topic: "Australia Central Bank Holds Rates Steady" +
"RBA Decides Against Rate Cut" → both topic_group="australia_rba_rate_decision".
Example of DIFFERENT topics: that rate decision vs "Trade Deal with China"
(topic_group="australia_china_trade").

# --- Two horizons, scored independently ---
  score_3m  — investor risk over the next 3 months
  score_12m — investor risk over the next 12 months
Do not derive one from the other. Across both: friction sets the LEVEL,
order-uncertainty sets the WIDTH of the distribution around it, and information
sets the DRIFT — weak instruments mean a measured problem is more likely to
compound than to correct between now and the horizon.

# --- Condition flags: observations only ---
Report what the evidence shows. Nothing downstream will alter your scores, and
you must not adjust them to anticipate any rule. These flags are recorded next
to your scores, not applied to them.
  war_on_territory        sustained interstate war, or regular long-range
                          strikes on cities / critical infrastructure
  internal_conflict_level "none" | "A" recurring mass-casualty attacks
                          (20+ killed) or mass kidnappings, last 90 days,
                          across 3+ regions | "B" = A + repeated attacks on
                          critical infrastructure or major cities | "C" = B +
                          nationwide emergency effects (large displacement,
                          prolonged curfews, export shut-ins)
  emergency_rule          unconstitutional dissolution, martial law, or
                          week-long widespread violent unrest disrupting
                          essential services
  sovereign_stress        bank runs, capital controls, default negotiations
                          or missed payments

# --- Citations and coverage ---
For friction, order_uncertainty and information_capacity, cite the evidence ids
that drove the score — article ids like "a3", or indicator names exactly as
they appear in EVIDENCE_JSON.

evidence_coverage (0-100): how completely this evidence captures the country's
situation. Two thin wire stories about a G7 economy = low. Stale indicators and
absent ledgers lower it.

Return JSON exactly per the response schema: condition_flags, ledger_scores,
subscore_evidence, news_article_scores, score_3m, score_12m,
evidence_coverage, bullet_summary (at most 120 words: primary drivers and
meaningful mitigants).

bullet_summary must use role language throughout — "the country", "the central
bank", "the governing party" — and must never name a country, guess one, or hint
at which one it might be. A reader is shown this text beside the country's real
name, so a wrong guess is worse than no guess and a right one is still an
inference you were not entitled to make.
""".strip()


# The four ledger scores, in one place: the schema builds the score object from
# all four, and the evidence object from the three that are risk-bearing.
# `edge_vitality` is reported but never cited against the country, so it has no
# evidence list — see the edge-protection section of the prompt.
_LEDGERS = [
    "friction",
    "order_uncertainty",
    "information_capacity",
    "edge_vitality",
]

_CITED_LEDGERS = ["friction", "order_uncertainty", "information_capacity"]


RISK_SCHEMA_V3: Dict = {
    "title": "CountryRiskAssessmentV3",
    "description": (
        "The model's judgement under the friction framework: condition flags as "
        "observations, four ledger scores with the evidence behind the three "
        "risk-bearing ones, per-article impacts with topic grouping, two "
        "horizons, and a short summary. All scores are integers 0-100. No code "
        "downstream alters any score."
    ),
    "type": "object",
    "properties": {
        "condition_flags": {
            "title": "ConditionFlags",
            "type": "object",
            "properties": {
                "war_on_territory":        {"type": "boolean"},
                "internal_conflict_level": {"type": "string", "enum": ["none", "A", "B", "C"]},
                "emergency_rule":          {"type": "boolean"},
                "sovereign_stress":        {"type": "boolean"},
            },
            "required": [
                "war_on_territory",
                "internal_conflict_level",
                "emergency_rule",
                "sovereign_stress",
            ],
            "additionalProperties": False,
        },
        "ledger_scores": {
            "title": "LedgerScores",
            "type": "object",
            "properties": {
                k: {"type": ["integer", "null"], "minimum": 0, "maximum": 100}
                for k in _LEDGERS
            },
            "required": list(_LEDGERS),
            "additionalProperties": False,
        },
        "subscore_evidence": {
            "title": "SubscoreEvidence",
            "type": "object",
            "properties": {
                k: {"type": "array", "items": {"type": "string"}} for k in _CITED_LEDGERS
            },
            "required": list(_CITED_LEDGERS),
            "additionalProperties": False,
        },
        "news_article_scores": {
            "title": "NewsArticleScores",
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id":          {"type": "string"},
                    "impact":      {"type": "integer", "minimum": 0, "maximum": 100},
                    "topic_group": {"type": "string"},
                },
                "required": ["id", "impact", "topic_group"],
                "additionalProperties": False,
            },
        },
        "score_3m":          {"type": "integer", "minimum": 0, "maximum": 100},
        "score_12m":         {"type": "integer", "minimum": 0, "maximum": 100},
        "evidence_coverage": {"type": "integer", "minimum": 0, "maximum": 100},
        "bullet_summary":    {"type": "string", "maxLength": 800},
    },
    "required": [
        "condition_flags",
        "ledger_scores",
        "subscore_evidence",
        "news_article_scores",
        "score_3m",
        "score_12m",
        "evidence_coverage",
        "bullet_summary",
    ],
    "additionalProperties": False,
}



# ---------------------------------------------------------------------------
# Stage-1 article digestion (cheap model, one call per article)
# Used by ai/digest_engine.py. The digest is a factual extraction; its
# stage1_severity only decides which articles the scorer reads in full.
# NOTE: literal braces inside JSON examples are escaped as {{ }} for .format().
# ---------------------------------------------------------------------------

# What the digest prompt says when the run is masked, and it is not optional
# decoration. The digest is generated from already-masked text, so the country's
# own name is gone — but `actors: who did what to whom` is a direct instruction
# to name people, and the gazetteer has never masked people. The measured result
# was a scorer reading "Jair Bolsonaro", "Moon Jae-in", "Recep Tayyip Erdoğan"
# in seventeen of its twenty articles, and a probe identifying five countries
# out of eight from the digests alone with the gate reporting clean throughout.
#
# The model rewrite pass covers this, but only on the two or three full texts
# the scorer reads end to end. Everything else reaches it as a digest, and a
# digest was born named.
DIGEST_MASK_RULE = """
IMPORTANT — the country in this text is deliberately anonymous, and your output
must keep it that way.

Replace every proper noun with the role it plays. A named person becomes their
office ("the president", "the finance minister", "the opposition leader", "the
central bank governor"). A named party becomes "the governing party" or "the
main opposition party". A named company becomes "a large domestic bank", "a
state oil company" or similar. A named place becomes "the capital", "a major
city" or "a neighbouring country". A named event, scandal or operation becomes a
description of what it was ("a long-running corruption investigation").

This applies to `actors` above all, which is where names would otherwise appear.
Say "the president" and never the president's name.

Keep every NUMBER exactly as written — percentages, dates, rates, amounts,
counts. The numbers are the evidence and must survive intact.
"""


DIGEST_PROMPT = """
You are an extraction engine. Read the article text below and return JSON.
Use ONLY the text provided. If the text does not state something, write
"not stated" — never fill gaps from outside knowledge.
{mask_rule}
ARTICLE_TEXT
{article_text}

Return:
{{
  "what_happened":  one concrete sentence,
  "actors":         who did what to whom,
  "numbers":        every quantity the text states (casualties, %, amounts, dates),
  "transmission":   the economic or policy channel, if the text states one,
  "directly_about_country": true only if {country} is the subject, not a passing mention,
  "stage1_severity": 0-100 —
      85+   kinetic activity, binding economic measures, or infrastructure
            sabotage in/against the country
      60-75 credible mobilization or high-probability binding sanctions
      40-55 indirect third-country events, unclear transmission
      0-25  rhetoric, symbolism, routine politics
}}
""".strip()


DIGEST_SCHEMA: Dict = {
    "title": "ArticleDigest",
    "description": "Factual extraction of one article plus a 0-100 severity that decides which articles the scorer reads in full.",
    "type": "object",
    "properties": {
        "what_happened":           {"type": "string"},
        "actors":                  {"type": "string"},
        "numbers":                 {"type": "string"},
        "transmission":            {"type": "string"},
        "directly_about_country":  {"type": "boolean"},
        "stage1_severity":         {"type": "number", "minimum": 0, "maximum": 100},
    },
    "required": [
        "what_happened",
        "actors",
        "numbers",
        "transmission",
        "directly_about_country",
        "stage1_severity",
    ],
    "additionalProperties": False,
}


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