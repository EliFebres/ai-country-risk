"""IMF monthly series, back to the pilot window, stamped with when each print landed.

``imf_macro_fetch.fetch_series_rows`` already reaches back as far as it is
asked to. What it cannot do is date the rows correctly for a backfill: it
stamps every observation with a single ``as_of``, because for the daily run
every observation genuinely did arrive today.

For history that is wrong in whichever direction you pick. Stamp them today and
``data_retrieval._resolve``'s vintage bound discards all of them — a 2018
snapshot sees no monthly macro at all, silently, because a row published in
2026 was not knowable in 2018. Stamp them today and skip the bound, and a 2018
snapshot reads 2026's revisions of 2018's CPI. Neither is a backfill.

So each row is re-stamped with when its print actually became public:
period end plus a publication lag. The IMF's IFS monthly series land roughly
six weeks after the month they describe — an exact date per country and
indicator does not exist in any machine-readable form, so this is an
approximation, and it is deliberately a *late* one. Being a fortnight
pessimistic costs a snapshot one stale month; being optimistic hands it a
number nobody had.

Not wired into any runner yet, and deliberately: writing these rows
**overwrites** the daily run's copies, since ``indicator_series`` is keyed on
``(country_iso2, indicator_code, freq, period)`` and carries ``as_of`` as a
column rather than in the key. That changes the ``as_of`` the live payload
reports for old periods — arguably to something more truthful, but it is a
live-path change and therefore Eli's call, not this module's.
"""

import datetime
import logging
from typing import Any, Dict, List, Optional

from backend.utils import constants
from backend.utils.data_fetching import imf_macro_fetch
from backend.utils.history import config

logger = logging.getLogger(__name__)

# How long after a period ends its print becomes public. Six weeks for monthly
# IFS, a quarter-end lag for quarterly. Chosen to be late rather than early:
# a snapshot reading a number a fortnight after it was really available is a
# small loss, and one reading it a fortnight before is a leak.
PUBLICATION_LAG_DAYS: Dict[str, int] = {"M": 45, "Q": 90, "A": 180}


def period_end(period: str, freq: str) -> Optional[datetime.date]:
    """The last day covered by an ``indicator_series`` period label.

    Accepts the three shapes that store writes: ``'2018-03'``, ``'2018Q1'``
    and ``'2018'``.
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


def published_on(period: str, freq: str) -> Optional[datetime.date]:
    """When a period's print became public, approximately."""
    end = period_end(period, freq)
    if end is None:
        return None
    return end + datetime.timedelta(days=PUBLICATION_LAG_DAYS.get(freq, 45))


def restamp(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Re-date fetched rows from "today" to when each print landed.

    Rows whose period cannot be parsed are dropped rather than kept with a
    wrong date: an unusable observation is better than an undatable one in a
    series whose whole point is knowing what was knowable when.
    """
    out = []
    for row in rows:
        stamp = published_on(str(row.get("period")), str(row.get("freq")))
        if stamp is None:
            logger.debug("[monthly] undatable period %r; dropped", row.get("period"))
            continue
        out.append({**row, "as_of": stamp, "vintage_scheme": "publication-lag-estimate"})
    return out


def backfill(roster: Optional[List[str]] = None,
             since: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every pilot country's IMF monthly history, correctly dated.

    Returns rows for ``data_push.upsert_indicator_series`` rather than writing
    them, so the caller decides whether to accept the overwrite described in
    the module docstring.
    """
    roster = roster or config.PILOT_ROSTER
    start = datetime.date.fromisoformat(since or config.PILOT_START)
    lookback = datetime.date.today().year - start.year + 1

    rows: List[Dict[str, Any]] = []
    for iso2 in roster:
        iso3 = constants.ISO3_BY_ISO2.get(iso2)
        if not iso3:
            logger.warning("[monthly] %s has no ISO3; skipped", iso2)
            continue
        try:
            fetched = imf_macro_fetch.fetch_series_rows(iso2, iso3, lookback_years=lookback)
        except Exception:  # noqa: BLE001
            # One country's IMF outage costs that country's monthly history,
            # not the other four.
            logger.exception("[monthly] %s failed; continuing", iso2)
            continue
        dated = restamp(fetched)
        logger.info("[monthly] %s: %d row(s), %d datable", iso2, len(fetched), len(dated))
        rows += dated
    return rows
