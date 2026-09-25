"""
When did this number become knowable?

That is what `as_of` answers, and it is not the same question as "what period
does it cover" or "when did we fetch it". Until now the payload answered the
third: `datetime.now()` at fetch time, carried through to the date that keys
every snapshot. Under that rule a 2024 World Bank figure pulled today is stamped
today, and the model is told a two-year-old number is fresh.

The rule here:

* **Where the source publishes a date, use it.** Scheme: `source-published`.
* **Where it does not, use the period's end plus that source's declared
  publication lag.** Scheme: `publication-lag-estimate`.
* **But never later than the day we first held the value.** The lag estimates
  when the source published; having fetched the number proves it was published
  by then. Where the estimate lands after that day — a 2025 WDI figure is
  estimated at July 2027 and was in hand in September 2026 — the day we first
  saw it wins. Scheme: `first-seen`. This is a ceiling, not the fetch clock:
  it only ever moves a date earlier, so a two-year-old number still reads as
  two years old. An `as_of` can therefore never exceed the run date.

The lags are not guesses. Each one is a statement about the publisher's own
release calendar, and each carries a citation to it beside the constant. The
World Bank does not publish a date per observation, but it does publish when it
updates, and that schedule is knowable.

**Err long.** An overestimated lag costs a little freshness at the margin. An
underestimated one claims a number was knowable before it was, which is the same
class of error as the fetch clock and is the direction this instrument cannot
afford. Where a series family updates irregularly, the constant is its typical
lag plus a margin, and says so.

The scheme travels with the date. Anything downstream can then tell a measured
date from a derived one, and the census counts each scheme per run — because a
run where everything is an estimate is a different thing from one where
everything was published, even if the dates look alike.
"""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Dict, Optional, Tuple

__all__ = [
    "SOURCE_PUBLISHED",
    "LAG_ESTIMATE",
    "FIRST_SEEN",
    "LAG_DAYS",
    "period_end",
    "as_of_for",
    "staleness_days",
    "CADENCE_DAYS",
]


SOURCE_PUBLISHED = "source-published"
LAG_ESTIMATE = "publication-lag-estimate"
FIRST_SEEN = "first-seen"


# Days from the end of the period to the day the figure became public.
# Each entry cites the publisher's own release calendar. Rounded UP, always.
LAG_DAYS: Dict[str, int] = {
    # The World Bank's WDI is refreshed on a published quarterly cycle, and the
    # prior calendar year's values land in the spring/summer update rather than
    # in January. 18 months covers the December update for a year that closed
    # the previous December, plus margin for a country that reports late.
    # https://datatopics.worldbank.org/world-development-indicators/ (release notes)
    "World Bank WDI": 548,

    # The Worldwide Governance Indicators are an annual release, published in
    # the September/October following the reference year. 18 months again,
    # because the underlying sources are themselves lagged.
    # https://www.worldbank.org/en/publication/worldwide-governance-indicators
    "World Bank WGI": 548,

    # The Statistical Performance Indicators are published annually alongside
    # WDI updates, on the same lag.
    # https://www.worldbank.org/en/programs/statistical-performance-indicators
    "World Bank SPI": 548,

    # V-Dem's annual dataset is released each March for the year that ended the
    # previous December; OWID mirrors it shortly after. 15 months with margin.
    # https://v-dem.net/data/the-v-dem-dataset/
    "V-Dem via OWID": 456,

    # RSF publishes the World Press Freedom Index each year on World Press
    # Freedom Day, 3 May, for the year that closed the previous December.
    # https://rsf.org/en/index
    "RSF World Press Freedom Index": 154,

    # PISA results are published in early December of the year AFTER the
    # assessment round (2022 round -> 5 December 2023). 12 months plus margin.
    # https://www.oecd.org/pisa/
    "OECD PISA": 380,

    # The IMF's SDMX responses carry their own vintage, so this is a fallback
    # only, for a series that arrives without one. Monthly CPI is typically
    # published within 6 weeks of month end.
    # https://data.imf.org/
    "World Bank WDI / IMF CPI": 45,
}

# The cadence each frequency implies, for measuring staleness against the
# series' own rhythm rather than against the calendar. An annual number that is
# 400 days old is normal; a monthly one that is 400 days old is broken.
CADENCE_DAYS: Dict[str, int] = {"A": 365, "Q": 92, "M": 31}

# The margin beyond the declared lag that the invariant test allows. A date
# further out than this is not a late publication, it is a bug.
MAX_EXTRA_DAYS = 120


def period_end(period: object, freq: str = "A") -> dt.date:
    """Return the last day of the period an observation covers.

    Args:
        period: A year (``2024`` or ``"2024"``), an ISO date, or a date.
        freq: ``A``, ``Q`` or ``M``.

    Raises:
        ValueError: if the period cannot be read.
    """
    if isinstance(period, dt.datetime):
        period = period.date()
    if isinstance(period, dt.date):
        return period

    text = str(period).strip()
    if text.isdigit() and len(text) == 4:
        return dt.date(int(text), 12, 31)

    try:
        d = dt.date.fromisoformat(text[:10])
    except ValueError:
        raise ValueError(f"cannot read a period from {period!r}") from None

    if freq == "A":
        return dt.date(d.year, 12, 31)
    if freq == "Q":
        q_end_month = ((d.month - 1) // 3) * 3 + 3
        return dt.date(d.year, q_end_month, calendar.monthrange(d.year, q_end_month)[1])
    return dt.date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def as_of_for(
    source: str,
    period: object,
    *,
    freq: str = "A",
    published: Optional[object] = None,
    seen: Optional[dt.date] = None,
) -> Tuple[dt.date, str]:
    """Return ``(as_of, scheme)`` — when this observation became knowable.

    This is the single chokepoint. Nothing else in the codebase may stamp an
    `as_of`, and in particular nothing may reach for the clock: a fetch date
    tells you when *we* looked, which is a fact about us and not about the data.

    Args:
        source: The registry's `source` string, which selects the lag.
        period: The period the observation covers.
        freq: The series' cadence.
        published: The date the source itself stated, if it stated one.
        seen: The first day we held this value. Caps an estimate that would
            otherwise land after it; ignored when the source stated a date.

    Returns:
        ``(as_of, scheme)`` where scheme is `source-published`,
        `publication-lag-estimate` or `first-seen`.

    Raises:
        KeyError: if the source has no declared lag and published no date.
            Deliberate: a source nobody has dated must not quietly default to
            zero lag, which is the fetch-clock bug wearing a different hat.
    """
    if published is not None:
        if isinstance(published, dt.datetime):
            published = published.date()
        elif not isinstance(published, dt.date):
            published = dt.date.fromisoformat(str(published)[:10])
        return published, SOURCE_PUBLISHED

    if source not in LAG_DAYS:
        raise KeyError(
            f"no publication lag declared for {source!r}. Add it to "
            f"vintage.LAG_DAYS with a citation to the publisher's release "
            f"calendar rather than letting the date default to the period end."
        )

    estimate = period_end(period, freq) + dt.timedelta(days=LAG_DAYS[source])
    if seen is not None and seen < estimate:
        return seen, FIRST_SEEN
    return estimate, LAG_ESTIMATE


def staleness_days(as_of: dt.date, today: Optional[dt.date] = None) -> int:
    """How many days ago this became knowable."""
    today = today or dt.date.today()
    return (today - as_of).days


def is_plausible(as_of: dt.date, source: str, period: object, freq: str = "A") -> bool:
    """Whether `as_of` could be a real publication date for this observation.

    The invariant the fetch-clock bug would have failed on day one: a value's
    `as_of` never precedes the end of the period it describes, and never exceeds
    period end plus that source's declared maximum lag.
    """
    end = period_end(period, freq)
    if as_of < end:
        return False
    ceiling = end + dt.timedelta(days=LAG_DAYS.get(source, 0) + MAX_EXTRA_DAYS)
    return as_of <= ceiling
