"""Knobs for the History Machine.

Separate from ``util.constants`` because none of this belongs to the live daily
run: the pilot roster is a deliberately small, deliberately *varied* slice
chosen to stress the machine rather than to cover the investable universe, and
the date floors are properties of the upstream archives, not of the product.

Two halves. The harvest knobs decide what gets collected and how politely; the
run-plan knobs decide what gets scored and what it may cost. Both live here so
the pilot's shape is one file to read and one file to change.
"""

from backend.util import constants

# Four countries, chosen for how differently they behave rather than for size.
# US is mandated (and is the hardest case for the Guardian window subdivision —
# it has an order of magnitude more coverage than the rest). TR is a crisis EM,
# PT a quiet DM, KR calm-with-one-shock. If the machine produces a defensible
# series for all four it is not overfitted to loud countries.
#
# BR was the fifth and came out at the projection. Turkey already carries the
# crisis-EM case — currency collapse, central-bank interference, an inflation
# regime the model has to read without being told where it is — and Brazil was
# the most redundant of the five against it. Cutting the least differentiated
# country is the right thing to lose when the projection needs to lose one.
#
# **Its harvest stays** — which stopped being true, and is true again. This
# paragraph used to say 14,576 articles remained in the store and that re-adding
# BR was `--country BR` and about $26 of pure scoring with no re-crawl. The
# schema rebuild deleted the whole `article` table, BR's rows with it, so for the
# length of that gap the number was wrong in the expensive direction: the $26
# had a ten-year re-crawl hidden underneath it.
#
# BR is therefore harvested again alongside the four, and the corpus exists only
# because of that sweep. The ~$26 is the scoring cost **given a harvested
# corpus** — it was never the whole cost of adding a country, only the cheap half
# that survives when the substrate does. What makes the split real is that
# harvesting spends somebody else's rate limit and scoring spends money, so the
# two are worth keeping separable.
#
# BR stays off this list: it is harvested, not scored. No code path treats a
# country present in `article` but absent here as an error — it is simply not in
# the default run.
PILOT_ROSTER: list[str] = ["US", "TR", "PT", "KR"]

# Ten years back. The Guardian and NYT archives reach further; where the blend
# stops being honest is what the step-4 recovery curve is for.
#
# **This is the anchor floor, not the harvest floor.** It pins every measured
# number in the project — the ~522 weekly anchors per country, the GATE2
# baseline, the bake-off windows — so it does not move. What the harvest reaches
# back to is `HARVEST_FLOOR` below, and the two were one constant until the
# distinction was needed.
PILOT_START: str = "2016-08-03"

# How far back the *article* harvest reaches. Deliberately earlier than
# `PILOT_START`, and nothing about anchor generation reads it.
#
# The reason is the trailing-context block: `llm.context` builds the four
# completed quarters ending before `as_of - SNAPSHOT_WINDOW_DAYS`
# (`context.QUARTERS`), so an anchor in the first year after `PILOT_START` wants
# roughly fifteen months of corpus *below* `PILOT_START` or its trailing window
# is silently short — not an error, just a thinner paragraph nothing flags.
#
# The p3-context variant that reads that block was measured and **rejected**
# (more evidence made the instrument coarser), so today this floor buys
# optionality rather than fixing a live gap. It is still worth having now:
# corpus is cheap to harvest while the harvester is already running the roster,
# and expensive to go back for once the archive quota has moved on.
HARVEST_FLOOR: str = "2015-01-01"

# The GDELT DOC 2.0 API's own article floor. Dormant: the pilot is Guardian and
# NYT only, because the DOC endpoint answers roughly one call per multi-minute
# window from a single IP — see the measurement in ``adapters/gdelt``. Kept so
# the adapter still has a floor to harvest from if the ngrams route replaces it.
GDELT_START: str = "2017-01-01"

# **A self-imposed guard, not an external limit** — enforced against metered
# spend, so it cannot be wrong the way a remembered vendor quota can.
# Hard ceiling on the step-4 leakage scan. The scan is the one OpenAI-billable
# thing in the whole harvest phase; past this the run aborts rather than
# quietly spending more.
LEAKAGE_SCAN_BUDGET_USD: float = 3.0


# --- Harvest pacing ---------------------------------------------------------
# Every number below is labelled **measured** or **asserted**. A limit nobody
# checked cost an hour and a wrong plan on 2026-08-15: `GUARDIAN_PAGE_SIZE`'s
# comment claimed a 5,000/day budget, the real figure is 500, and the harvest
# planned against the remembered number until the wall arrived. Where the
# service reports its own limit, read it and treat the constant as a fallback;
# where it does not, say so, so nobody mistakes an assumption for a fact.

# **Asserted, and deliberately so.** Not a vendor limit — a self-imposed floor.
# Guardian, GDELT and the Wayback Machine are all free services being asked for
# thousands of requests. One per second, always, with a real backoff on 429.
REQUEST_INTERVAL_SECONDS: float = 1.0

# **Measured.** Their documented maximum is 200 and that was the setting,
# because it is the dominant lever on the free tier's daily call budget.
#
# 200 is not reliable. On 2026-08-03 one window — KR 2020, the friction query —
# returned 503 for `page-size=200&page=2` on every attempt, while the same 594
# results came back fine at `page-size=100&page=3` and at `page-size=50&page=5`.
# A specific (query, size, page) triple failing deterministically is a server
# bug, not a rate limit, and 100 halves the exposure at a cost of roughly twice
# the calls.
#
# The old version of this comment finished "— about 1,200 for a full pilot
# harvest, against a 5,000/day budget". Both halves were wrong and neither had
# been checked. The 2026-08-15 harvest spent 1,461 calls on **one** country's
# first eight years, and the advertised daily budget is 500 (see
# `GUARDIAN_DAILY_CALL_BUDGET`). The trade this constant makes is still the
# right one; the arithmetic that justified it was fiction.
#
# ponytail: blunt instrument. If a 503 ever recurs at 100, the real fix is to
# retry the failing page at half the size rather than to keep halving this.
GUARDIAN_PAGE_SIZE: int = 100

# **Asserted — a fallback only.** `adapters.guardian` reads the real allowance
# from `X-RateLimit-Limit-Day` / `-Remaining-Day` on every response and paces
# off that; this is what it plans with before the first call has answered, and
# on a response that carries no headers.
#
# 500 is what the API advertised on 2026-08-15. The observed throughput that
# day was 1,461 page-calls before `Remaining-Day` reached zero, so the
# advertised number and the enforced one disagree by about 3x. `Remaining-Day`
# is the value that actually hits zero, so that is what the harvester obeys —
# and `guardian.quota()` reports both, because a limit that lies by 3x is
# exactly what quietly misinforms the next estimate.
GUARDIAN_DAILY_CALL_BUDGET: int = 500

# **A choice, not a limit.** A window needing more than this many pages is split
# — year into quarters, quarter into months — for that country/theme only.
# Expected to fire on the US, and on 2026-08-15 it fired on every US year:
# subdivision is why one country cost 183 calls a year against the six-call
# no-subdivision floor. A month-wide window that still overflows is truncated
# with a warning, which happened 3 times across US 2016-2023.
GUARDIAN_SUBDIVIDE_ABOVE_PAGES: int = 5

# **Asserted** — GDELT's own documented maximum, on a dormant source.
GDELT_MAX_RECORDS: int = 250

# **Measured, and known false as documented** — the model every other constant
# here should be read against.
#
# GDELT's own stated limit, quoted verbatim from the body it returns with a 429:
# "Please limit requests to one every 5 seconds".
#
# It is not the real limit, and this constant is why the source is dormant.
# Measured on 2026-08-03, five seconds is far too fast and so is thirty: the
# endpoint answers the first call after a long idle and 429s everything after
# it, spacing regardless. Honouring the stated interval would still have failed
# the harvest — the number in their error message is not the number they
# enforce.
GDELT_REQUEST_INTERVAL_SECONDS: float = 5.0

# **Asserted, and unverifiable from the wire.** The claim is that the NYT
# developer tier allows five requests a minute and that twelve seconds apart is
# that limit exactly. Nobody has confirmed it, and on 2026-08-15 a probe of the
# archive endpoint found the response carries **no rate-limit headers at all**
# — no `X-RateLimit-*`, no `Retry-After`, nothing to derive from. So unlike the
# Guardian budget beside it, this one cannot be read from the service; it can
# only be asserted or discovered by being refused.
#
# What is known: a 121-month harvest at 12s completed without a single 429, so
# 12s is *safe*. That is a lower bound on politeness, not a measurement of the
# limit, and the two must not be confused — the true ceiling could be 5s or 60s
# and this harvest would look identical.
#
# The cost of being wrong is small in the direction that matters: the archive
# endpoint needs only ~121 calls for ten years — one per month, covering the
# whole world, so it does not grow with the roster — which is 24 minutes at this
# spacing. There is nothing to gain by crowding it, which is why it stays
# conservative rather than being tuned against a number nobody can see.
NYT_REQUEST_INTERVAL_SECONDS: float = 12.0

# **Measured.** Most relevant articles kept per country per month from the NYT
# archive.
#
# The archive returns the whole paper, and the whole paper is mostly about the
# United States: in a measured month (2018-08) the roster matched 1,824 articles
# and 1,687 of them were US. A month holds about 4.3 weekly snapshots wanting 20
# articles each, so ~86 is the real appetite and 1,687 is twenty times the
# oversupply — bought at roughly 100MB of storage for articles that could never
# be selected.
#
# The cut uses the live relevance score, so what is dropped is the tail no
# snapshot would have picked, and `harvest_month` logs how many went. A cap
# nobody reports reads afterwards as "we harvested everything".
NYT_MAX_PER_COUNTRY_MONTH: int = 150

# **Asserted.** A judgement about archive quality, not a rate limit; the
# Wayback Machine publishes no quota, and `wayback.py` paces at one request a
# second with 429 backoff rather than against a number.
# How far past publication a Wayback capture may sit and still be treated as a
# capture "of" the article. Beyond six months the page has usually been
# re-templated, and later edits start showing up as if they were original.
WAYBACK_WINDOW_DAYS: int = 180


# --- The run plan -----------------------------------------------------------
# What the pilot scores, and what it may spend doing it.
#
# The two modes are not symmetric, and which is which is the whole design:
#
#   masked — the continuous weekly series, ~522 anchors per country. This is the
#            regime that becomes production at cutover, so it is the one that
#            gets a real history. Masked snapshots write to `risk_snapshot`
#            through the ordinary `data_push.upsert_snapshot`.
#   named  — a small diagnostic sample, `NAMED_SAMPLE_PER_COUNTRY` dates per
#            country, scored on the same weeks as their masked twins so the
#            divergence is measurable. Named snapshots write to
#            `history_run_ledger` and never touch `risk_snapshot`: a series that
#            silently changes scoring regime half way through its own history is
#            worse than no series at all.
#   masked_nostructural
#          — the same ~60 diagnostic dates, masked, with the `structural` block
#            withheld. It exists because masked-vs-named divergence is ambiguous
#            on its own: a small gap could mean the structural facts recovered
#            what the name was carrying, or that the name never carried
#            anything. Only the third arm tells those apart, and it costs about
#            a dollar. Ledger-only, like `named`.
SCORING_MODES: tuple[str, ...] = ("masked", "named", "masked_nostructural")

# The modes that must never touch `risk_snapshot`. They share (country, as_of)
# with their masked twin and would overwrite the production series on its own
# primary key — a series that silently changes regime half way through its own
# history is worse than no series at all.
DIAGNOSTIC_MODES: tuple[str, ...] = ("named", "masked_nostructural")

# Weekly, anchored on Monday. Matches the cadence the full 48-country backfill
# will use, so the pilot is a scale model rather than a different experiment.
CADENCE: str = "W-MON"

# gpt-4o's knowledge boundary. Not a harvest floor — a stratification axis: the
# diagnostic sample takes half its dates from either side, because "can the
# model identify this country" means something different when the model might
# simply remember the week.
CUTOFF_DATE: str = "2023-10-01"

# Diagnostic dates per country: 6 pre-cutoff + 6 post, and within each half
# 3 largest |Δscore| + 3 random calm weeks. The extremes are where masking
# either survives or does not; the calm weeks are the control.
NAMED_SAMPLE_PER_COUNTRY: int = 12

# Hard abort threshold for the whole pilot, enforced against metered token spend
# rather than a projection.
#
# This is a runaway guard, not a budget. Authorization is the gate — Eli approves
# a run, and this only decides how far a run that has gone wrong gets before it
# stops. Raising it authorizes nothing; a number well clear of the projection is
# what keeps a legitimate run from aborting three-quarters finished, which costs
# the spend and delivers no series.
PILOT_BUDGET_USD: float = 130.0

# The snapshot window, in days back from each anchor. Same 30 days the live run
# uses, so a historical snapshot differs from a live one only in where its
# articles came from.
SNAPSHOT_WINDOW_DAYS: int = 30

# Most of a snapshot's articles that may be NYT abstract-only rows.
#
# The archive API returns no bodies, so every NYT article is a headline and two
# sentences — real evidence, and much thinner evidence than a Guardian piece. It
# is also distributed nothing like evenly: in a measured month the roster matched
# 1,824 NYT articles and 1,687 of them were US. Uncapped, a US snapshot fills
# with abstracts while a Portugal one keeps full bodies, and the two stop being
# the same instrument pointed at different countries.
#
# 0.4 of twenty is eight. High enough that abstracts still fill the gaps they are
# there to fill; low enough that no snapshot is mostly headlines.
#
# The cap is hard, and it can leave a thin week thinner. That is the same trade
# `relevance_snippet` makes: a thin week reported honestly beats a full one
# assembled from whatever was cheapest to harvest.
#
# It is a share of the *budget*, not of the realized snapshot, so a thin week
# reports above this number without the cap having leaked: eight abstracts out
# of a week that only found thirteen articles is 62%, and still eight. Measured
# across the roster it lands at 26-45% per country-year, and PT sits near 5%
# because the NYT barely covers Portugal — which is the corpus telling the truth
# rather than the cap failing.
ABSTRACT_TIER_SHARE: float = 0.4


def country_name(iso2: str) -> str:
    """Display name for a pilot country, from the live roster.

    The roster is the single source of truth for the run universe, so the
    historical path reads names from it rather than keeping a second list that
    could disagree about what "Turkey" is called.

    Raises:
        KeyError: if the code is not in ``constants.COUNTRY_ROSTER`` — a typo in
            ``PILOT_ROSTER`` should fail before a harvest starts, not produce
            an empty query.
    """
    for entry in constants.COUNTRY_ROSTER:
        if entry["iso2"] == iso2:
            return str(entry["name"])
    raise KeyError(f"{iso2!r} is not in constants.COUNTRY_ROSTER")
