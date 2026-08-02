"""Knobs for the History Machine's harvest phase.

Separate from ``utils.constants`` because none of this belongs to the live daily
run: the pilot roster is a deliberately small, deliberately *varied* slice
chosen to stress the machine rather than to cover the investable universe, and
the date floors are properties of the upstream archives, not of the product.

Nothing here scores anything. These values decide what gets harvested and how
politely.
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

# How far past publication a Wayback capture may sit and still be treated as a
# capture "of" the article. Beyond six months the page has usually been
# re-templated, and later edits start showing up as if they were original.
WAYBACK_WINDOW_DAYS: int = 180


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
