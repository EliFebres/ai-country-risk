"""IMF monthly series, back to the pilot window, stamped with when each print landed.

``imf_macro_fetch.fetch_series_rows`` already reaches back as far as it is
asked to. What it cannot do is date the rows correctly for a backfill: it
stamps every observation with a single ``as_of``, because for the daily run
every observation genuinely did arrive today.

For history that is wrong in whichever direction you pick. Stamp them today and
``payload._resolve``'s vintage bound discards all of them — a 2018
snapshot sees no monthly macro at all, silently, because a row published in
2026 was not knowable in 2018. Stamp them today and skip the bound, and a 2018
snapshot reads 2026's revisions of 2018's CPI. Neither is a backfill.

So each row is re-stamped with when its print actually became public: period end
plus a publication lag, from :mod:`backend.data_fetching.vintage.lags`. The lags
live there rather than here because they belong to the publisher, not to the
fetcher — and because the same table has to date the annual and quarterly rows
this module never touches.

Writing these rows **overwrites** the daily run's copies, since
``indicator_series`` is keyed on ``(country_iso2, indicator_code, freq, period)``
and carries ``as_of`` as a column rather than in the key. That is now the
intended behaviour rather than a hazard: an ``as_of`` naming when a print landed
is more truthful than one naming when this project happened to fetch it, and the
live payload's staleness numbers become right rather than merely stable. The
migration in :mod:`backend.data_fetching.vintage.restamp` does the same thing to
the rows already stored, and dumps them first.
"""

import datetime
import logging
from typing import Any, Dict, List, Optional

from backend.util import constants
from backend.data_fetching import bis_bulk_fetch, imf_macro_fetch
from backend.util import config
from backend.data_fetching.vintage import lags

logger = logging.getLogger(__name__)


def restamp(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Re-date fetched rows from "today" to when each print landed.

    Rows whose period cannot be parsed are dropped rather than kept with a
    wrong date: an unusable observation is better than an undatable one in a
    series whose whole point is knowing what was knowable when.
    """
    out = []
    for row in rows:
        stamp = lags.published_on(str(row.get("period")), str(row.get("freq")),
                                  str(row.get("indicator_code") or ""))
        if stamp is None:
            logger.debug("[monthly] undatable period %r; dropped", row.get("period"))
            continue
        out.append({**row, "as_of": stamp, "vintage_scheme": lags.SCHEME})
    return out


def _bis(months: int) -> List[Dict[str, Any]]:
    """The BIS half: exchange rate and policy rate, back to the pilot floor.

    The daily run keeps five years of these, which is why the store began in
    2021 and why every snapshot before it had no exchange rate at all. The flat
    CSV always held the whole history — only the write was bounded — so this is
    the same fetch with a wider window, not a new source.

    Whole-roster rather than pilot-only: ``fetch_dataset_rows`` downloads every
    country in one file, so restricting it would cost a filter and save nothing.
    """
    rows: List[Dict[str, Any]] = []
    for code in ("BIS.FX.USD", "BIS.POLICY.RATE"):
        try:
            fetched = bis_bulk_fetch.fetch_dataset_rows(code, keep_months=months)
        except Exception:  # noqa: BLE001
            logger.exception("[monthly] BIS %s failed; continuing", code)
            continue
        dated = restamp(fetched)
        logger.info("[monthly] %s: %d row(s), %d datable", code, len(fetched), len(dated))
        rows += dated
    return rows


def backfill(roster: Optional[List[str]] = None,
             since: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every monthly series the pilot window needs, dated when it landed.

    Two sources with the same problem and the same fix: the IMF's CPI, fetched
    per country, and the BIS exchange and policy rates, which arrive for the
    whole roster in one file.

    Returns rows for ``data_push.upsert_indicator_series`` rather than writing
    them, so the caller decides whether to accept the overwrite described in the
    module docstring.
    """
    # The whole roster, not the pilot four — the same reason `weo.load_all`
    # takes all 48. A country missing from here is a country scored without its
    # monthly CPI or its policy rate, live as well as historically, and the
    # fetch is one call per country against a free API.
    roster = roster or config.HARVEST_ROSTER
    start = datetime.date.fromisoformat(since or config.HARVEST_FLOOR)
    today = datetime.date.today()
    lookback = today.year - start.year + 1
    # One extra year of months, so a partial first year is covered rather than
    # clipped: the floor is a date, not a January.
    months = (today.year - start.year + 1) * 12

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

    return rows + _bis(months)
