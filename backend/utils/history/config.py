"""Knobs for the History Machine.

Separate from ``utils.constants`` because none of this belongs to the live daily
run: the pilot roster is a deliberately small, deliberately *varied* slice
chosen to stress the machine rather than to cover the investable universe, and
the date floors are properties of the upstream archives, not of the product.

Two halves. The harvest knobs decide what gets collected and how politely; the
run-plan knobs decide what gets scored and what it may cost. Both live here so
the pilot's shape is one file to read and one file to change.
"""

from backend.utils import constants

# Five countries, chosen for how differently they behave rather than for size.
# US is mandated (and is the hardest case for the Guardian window subdivision —
# it has an order of magnitude more coverage than the rest). TR is a crisis EM,
# BR a mid EM, PT a quiet DM, KR calm-with-one-shock. If the machine produces a
# defensible series for all five it is not overfitted to loud countries.
PILOT_ROSTER: list[str] = ["US", "TR", "BR", "PT", "KR"]

# Ten years back. The Guardian and NYT archives reach further; where the blend
# stops being honest is what the step-4 recovery curve is for.
PILOT_START: str = "2016-08-03"

# The GDELT DOC 2.0 API's own article floor. Dormant: the pilot is Guardian and
# NYT only, because the DOC endpoint answers roughly one call per multi-minute
# window from a single IP — see the measurement in ``adapters/gdelt``. Kept so
# the adapter still has a floor to harvest from if the ngrams route replaces it.
GDELT_START: str = "2017-01-01"

# Hard ceiling on the step-4 leakage scan. The scan is the one OpenAI-billable
# thing in the whole harvest phase; past this the run aborts rather than
# quietly spending more.
LEAKAGE_SCAN_BUDGET_USD: float = 3.0


# --- Harvest pacing ---------------------------------------------------------
# Guardian, GDELT and the Wayback Machine are all free services being asked for
# thousands of requests. One per second, always, with a real backoff on 429.
REQUEST_INTERVAL_SECONDS: float = 1.0

# Guardian page size. Their documented maximum is 200 and that was the setting,
# because it is the dominant lever on the free tier's daily call budget.
#
# 200 is not reliable. On 2026-08-03 one window — KR 2020, the friction query —
# returned 503 for `page-size=200&page=2` on every attempt, while the same 594
# results came back fine at `page-size=100&page=3` and at `page-size=50&page=5`.
# A specific (query, size, page) triple failing deterministically is a server
# bug, not a rate limit, and 100 halves the exposure at a cost of roughly twice
# the calls — about 1,200 for a full pilot harvest, against a 5,000/day budget.
#
# ponytail: blunt instrument. If a 503 ever recurs at 100, the real fix is to
# retry the failing page at half the size rather than to keep halving this.
GUARDIAN_PAGE_SIZE: int = 100

# A window needing more than this many pages is split — year into quarters,
# quarter into months — for that country/theme only. Expected to fire on the US.
# Subdivision costs calls; not subdividing costs a window that pages forever.
GUARDIAN_SUBDIVIDE_ABOVE_PAGES: int = 5

# Records per GDELT DOC-API call. Its own documented maximum.
GDELT_MAX_RECORDS: int = 250

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

# The NYT developer tier allows five requests a minute. Twelve seconds apart is
# that limit exactly, and the archive endpoint needs only ~120 calls for ten
# years — one per month, covering the whole world — so there is nothing to gain
# by crowding it.
NYT_REQUEST_INTERVAL_SECONDS: float = 12.0

# Most relevant articles kept per country per month from the NYT archive.
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
# rather than a projection. Projection is ≈ $95; the gap is deliberate headroom,
# not budget to spend.
PILOT_BUDGET_USD: float = 110.0

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
