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

# The GDELT DOC 2.0 API's own article floor. Months before this are Guardian and
# NYT only — a real thinning of the corpus, not a bug, and the harvest counts
# report it rather than hiding it.
GDELT_START: str = "2017-01-01"

# Hard ceiling on the step-4 leakage scan. The scan is the one OpenAI-billable
# thing in the whole harvest phase; past this the run aborts rather than
# quietly spending more.
LEAKAGE_SCAN_BUDGET_USD: float = 3.0


# --- Harvest pacing ---------------------------------------------------------
# Guardian, GDELT and the Wayback Machine are all free services being asked for
# thousands of requests. One per second, always, with a real backoff on 429.
REQUEST_INTERVAL_SECONDS: float = 1.0

# Guardian's maximum page size. The dominant lever on the free tier's daily call
# budget: at 200 a country-year of one theme is usually a single call.
GUARDIAN_PAGE_SIZE: int = 200

# A window needing more than this many pages is split — year into quarters,
# quarter into months — for that country/theme only. Expected to fire on the US.
# Subdivision costs calls; not subdividing costs a window that pages forever.
GUARDIAN_SUBDIVIDE_ABOVE_PAGES: int = 5

# Records per GDELT DOC-API call. Its own documented maximum.
GDELT_MAX_RECORDS: int = 250

# GDELT's own stated limit, quoted verbatim from the body it returns with a 429:
# "Please limit requests to one every 5 seconds". One per second is five times
# too fast, and it does not merely throttle — every retry inside the backoff
# window 429s too, so the whole harvest dies on its first call. Five seconds
# makes the full harvest ~5 hours instead of ~1, which is the actual price of
# this dataset being free.
GDELT_REQUEST_INTERVAL_SECONDS: float = 5.0

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
SCORING_MODES: tuple[str, ...] = ("masked", "named")

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
