"""When a number became public, as opposed to what period it describes.

The whole no-future rule rests on one date per observation: not the period the
value covers, but the day a reader could first have seen it. ``indicator_series``
carries that as ``as_of``, and every bulk fetcher stamps it with the fetch date,
because for the daily run every observation genuinely did arrive today.

For a backfill that is wrong in whichever direction you pick. Stamp them today
and ``data_retrieval._resolve``'s vintage bound discards all of them — a 2018
snapshot sees no macro at all, silently, because a row written in 2026 was not
knowable in 2018. Stamp them today and skip the bound and the 2018 snapshot
reads 2026's revisions of 2018. Neither is a backfill.

So each row is re-dated to *period end plus a publication lag*, and the lags
live here rather than beside any one fetcher because they are a property of the
publisher, not of the code that happens to call it.

**Every lag errs long.** An over-long lag costs the live run a little freshness
at the margin — a snapshot reading a number a fortnight after it was really
available. An under-short one hands a snapshot a number nobody had, which is the
failure this module exists to prevent, and which is invisible afterwards. When
the two readings disagree, take the later one.

**Never negative.** Some indices are published *during* the year they are named
for: RSF's 2018 press-freedom index came out in May 2018, months before 2018
ended. Stamping it May 2018 would be more accurate and would also mean an
observation whose ``as_of`` precedes its own period end — a shape nothing
downstream expects and the invariant test forbids. The floor is period end,
which is late by up to seven months for those three indices and safe by
construction. That is the trade this module keeps making.

Where a source exposes a **real release date**, prefer it over anything here.
The WEO editions do, and ``vintage/weo.py`` uses it: an edition-dated ``as_of``
is a fact, and these constants are an estimate standing in for one.
"""

import datetime
from typing import Dict, Optional

# The scheme name written into `indicator_series.vintage_scheme` for any row
# dated by this module, so a value's provenance says which of the two regimes
# produced it — an estimate, or a publisher's own edition date.
SCHEME = "publication-lag-estimate"

# Per-indicator lags, in days after the period ends. Only the ones that differ
# meaningfully from their frequency's default are listed; everything else falls
# through to `_DEFAULT_BY_FREQ`, and a source not listed anywhere gets the
# conservative annual default rather than a guess.
LAG_DAYS: Dict[str, int] = {
    # Market observables. The period *is* the observation: a monthly average of
    # a daily exchange rate is complete the day the month ends, and BIS
    # publishes within a week. Zero is both true and the safe side.
    "BIS.FX.USD": 0,
    # A policy rate changes on the day it is announced, which is the day it is
    # public. There is no revision and no release lag to model.
    "BIS.POLICY.RATE": 0,
    # National statistical offices publish CPI between 10 and 25 days after the
    # month closes, depending on the country. Take the long end for all of them
    # rather than carrying a per-country table that would be wrong differently.
    "CPI.YOY": 25,
    # IMF IRFCL monthly reserves, roughly a month behind.
    "RESERVES.USD": 30,
    # The World Uncertainty Index is built from EIU country reports and posted a
    # quarter behind the quarter it scores.
    "WUI.INDEX": 90,
}

# The fallback when an indicator is not named above. Annual is deliberately a
# full year: the World Bank's WDI and WGI land 9-18 months after the year they
# describe (WGI publishes around September of year+1), and a single conservative
# number beats a per-series table nobody will maintain.
_DEFAULT_BY_FREQ: Dict[str, int] = {"M": 45, "Q": 120, "A": 365}

# The invariant's upper bound. Nothing this module produces may sit further than
# this past its own period end; a lag beyond two years is a bug in the table,
# not a slow publisher.
MAX_LAG_DAYS = 730


def lag_days(indicator_code: str, freq: str) -> int:
    """How long after its period ends this indicator's print becomes public."""
    if indicator_code in LAG_DAYS:
        return LAG_DAYS[indicator_code]
    return _DEFAULT_BY_FREQ.get(freq, _DEFAULT_BY_FREQ["A"])


def period_end(period: str, freq: str) -> Optional[datetime.date]:
    """The last day covered by an ``indicator_series`` period label.

    Accepts the three shapes the store writes — ``'2018-03'``, ``'2018Q1'`` and
    ``'2018'`` — and returns None for anything else. An undatable period is
    dropped by the callers rather than kept with a guessed date: in a series
    whose whole point is knowing what was knowable when, an unusable observation
    beats an undatable one.
    """
    try:
        if freq == "M":
            year, month = int(period[:4]), int(period[5:7])
            nxt = datetime.date(year + month // 12, month % 12 + 1, 1)
            return nxt - datetime.timedelta(days=1)
        if freq == "Q":
            year, quarter = int(period[:4]), int(period[5])
            month = quarter * 3
            nxt = datetime.date(year + month // 12, month % 12 + 1, 1)
            return nxt - datetime.timedelta(days=1)
        if freq == "A":
            return datetime.date(int(period), 12, 31)
    except (TypeError, ValueError, IndexError):
        return None
    return None


def published_on(period: str, freq: str,
                 indicator_code: str = "") -> Optional[datetime.date]:
    """When a period's print became public, approximately and deliberately late.

    Args:
        period: the ``indicator_series`` period label.
        freq: ``'M'``, ``'Q'`` or ``'A'``.
        indicator_code: the series, so a market rate is not given a CPI's lag.
            Omitted, the frequency default applies.

    Returns:
        ``period_end + lag``, or None when the period cannot be read.
    """
    end = period_end(period, freq)
    if end is None:
        return None
    return end + datetime.timedelta(days=lag_days(indicator_code, freq))


def within_bounds(as_of: datetime.date, period: str, freq: str) -> bool:
    """The invariant, as a function the migration and the tests both call.

    A value may never claim to have been public before the period it describes
    finished, and may never sit more than :data:`MAX_LAG_DAYS` after it. The
    first half catches a lag table gone negative; the second catches a period
    label parsed into the wrong century.
    """
    end = period_end(period, freq)
    if end is None:
        return False
    return end <= as_of <= end + datetime.timedelta(days=MAX_LAG_DAYS)
