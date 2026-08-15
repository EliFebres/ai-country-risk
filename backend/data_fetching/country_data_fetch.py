"""Write side of the macro panel: World Bank data into ``indicator_series``.

``backfill_missing_panels`` is the ETL's first phase — it fetches a panel for
every rostered country that has no annual rows yet and writes it to the store
``payload.query_macro_panel`` reads back. ``panel_rows`` maps the wide frame's
``panel_col`` names onto registry codes, and ``merge_extra_indicators`` folds
in the indicators that don't come from the World Bank beforehand.

This used to write Parquet under ``backend/data``, which meant the annuals had
a different home and different vintage semantics from every other observation,
and a clone could only get them by copying files.

The backfill is incremental by design: World Bank fetches are slow and the
data is annual, so a country keeps its panel until it is rebuilt on purpose.
A normal run does no fetching here at all; ``main.py`` passes ``force=True``
on its monthly cadence to pick up revisions and newly published years.
"""

import datetime
import logging
import pandas as pd

from typing import Mapping

from backend.util import constants
import backend.data_fetching.fetch_metrics as fetch_metrics
import backend.data_fetching.political_corruption_fetch as political_corruption_fetch

logger = logging.getLogger(__name__)

# What the panel's own rows are stamped with. The same string the Parquet
# reader used to attach on the way out, so a value's provenance reads the same
# as it always did — and the one thing that tells a World Bank annual apart
# from the WEO edition that shares its indicator code.
PANEL_SOURCE = "World Bank panel"


def merge_extra_indicators(
    panel: pd.DataFrame,
    iso2: str,
    iso3_by_iso2: Mapping[str, str],
) -> pd.DataFrame:
    """Merge non-World-Bank indicators into a country's wide WB panel.

    Currently adds the OWID/V-Dem **Political Corruption Index** as a
    ``POL_CORRUPTION`` column, aligned on the panel's int year index. OWID
    often publishes a year ahead of the World Bank, so the join is an outer one
    and the extra year is kept: ``payload`` anchors each indicator on its
    own newest observation, so a longer corruption series costs the others
    nothing.

    Args:
        panel: Wide, year-indexed WB panel (may be empty).
        iso2: ISO-2 country code.
        iso3_by_iso2: ISO-2 -> ISO-3 map (OWID is ISO-3 keyed).

    Returns:
        The panel with a ``POL_CORRUPTION`` column. Stays empty only if both
        the WB panel and the corruption series are empty.
    """
    has_panel = isinstance(panel, pd.DataFrame) and not panel.empty

    series = political_corruption_fetch.corruption_series_for_iso2(
        iso2, iso3_by_iso2
    ).rename("POL_CORRUPTION")

    if not series.empty:
        if has_panel:
            return panel.join(series, how="outer").sort_index()
        return series.to_frame().sort_index()

    # No corruption data for this country: keep schema stable when we have a panel.
    if has_panel:
        panel = panel.copy()
        panel["POL_CORRUPTION"] = pd.NA
    return panel


def panel_rows(panel: pd.DataFrame, iso2: str) -> list:
    """One wide World Bank panel as ``indicator_series`` rows.

    The panel's columns are registry ``panel_col`` names; the store is keyed by
    registry code, so this is where the two vocabularies meet.

    ``as_of`` is 31 December of the value's own year, **capped at today**. The
    World Bank publishes no release date per observation, so the year end is
    the honest stand-in — the same stamp the Parquet reader used to apply on
    the way out, moved to where the value is written. A real WEO edition
    carries its own publication date and therefore outranks this at the same
    period, which is exactly what ``payload._resolve`` is for.

    The cap matters and is the same rule ``vintage.restamp`` applies: without
    it the current year's figure is stamped four months from now, which reads
    as *negative* staleness in the live payload and is plainly false, since the
    value is already in the table.
    """
    by_column = {str(spec["panel_col"]): code
                 for code, spec in constants.INDICATOR_REGISTRY.items()
                 if spec.get("panel_col")}

    today = datetime.date.today()
    rows = []
    for column in panel.columns:
        code = by_column.get(str(column))
        if not code:
            continue
        for year, value in panel[column].dropna().items():
            try:
                year = int(year)
                value = float(value)
            except (TypeError, ValueError):
                continue
            rows.append({
                "country_iso2": iso2,
                "indicator_code": code,
                "freq": "A",
                "period": str(year),
                "value": value,
                "as_of": min(datetime.date(year, 12, 31), today),
                # Its own source string, not the registry's. Two reasons, and
                # the second is load-bearing. The registry names the source a
                # code *usually* comes from — CPI.YOY says "IMF CPI" — but this
                # value came from the World Bank panel, and attributing it to
                # the IMF is simply wrong on the row. And it is the only way to
                # ask "does this country have its panel yet": CPI.YOY is also a
                # WEO subject, so the code alone cannot distinguish 35,929 WEO
                # rows from a World Bank annual, and a check by code reports
                # every country as backfilled before the fetch has ever run.
                "source": PANEL_SOURCE,
                "vintage_scheme": "as-published-latest",
            })
    return rows


def backfill_missing_panels(force: bool = False) -> None:
    """Fetch each rostered country's World Bank panel into ``indicator_series``.

    Was a Parquet write under ``backend/data``; the annuals now go to Postgres
    like every other observation, so there is one macro store and a fresh clone
    builds it by fetching rather than by finding files somebody copied in.

    Incremental and idempotent: a country that already has annual rows is
    skipped, so a normal daily run does no World Bank fetching at all and only
    a first run (or a newly added country) pays for it. The upsert is keyed
    ``(country, code, freq, period, as_of)``, so a re-run overwrites in place.

    A country whose fetch fails is logged with a traceback and skipped — the
    others still get their panels.

    Args:
        force: refetch every rostered country, not just the ones with no rows,
            to pick up World Bank revisions and newly published years. A failed
            country keeps the rows it already had.
    """
    from backend.data_upsert import data_push

    roster = constants.COUNTRY_ROSTER
    iso3_by_iso2 = constants.ISO3_BY_ISO2

    # By source, not by code. The WEO editions write 160k annual rows before
    # this step runs and CPI.YOY is both a WEO subject and a panel column, so
    # any check by code reports every country as already backfilled and this
    # fetches nothing at all — twice, in two different ways, before the source
    # turned out to be the only honest signal.
    missing = []
    for country in roster:
        iso2 = str(country["iso2"]).strip()
        if not iso2:
            continue
        if force or not data_push.has_annual_series(iso2, source=PANEL_SOURCE):
            missing.append(iso2)

    if not missing:
        logger.info("All %d countries already have annual macro rows.", len(roster))
        return

    logger.info("Backfilling %d panels (force=%s) → %s", len(missing), force, missing)
    for iso2 in missing:
        try:
            panel = fetch_metrics.build_country_panel(iso2, constants.INDICATORS)

            # Merge non-WB indicators (e.g. Political Corruption Index from OWID)
            panel = merge_extra_indicators(panel, iso2, iso3_by_iso2)

            if panel is None or panel.empty:
                logger.info("[%s] No rows for selected indicators — skipping write.", iso2)
                continue

            rows = panel_rows(panel, iso2)
            data_push.upsert_indicator_series(rows)
            logger.info("[%s] Wrote %d annual observations across %d indicators.",
                        iso2, len(rows), panel.shape[1])
        except Exception:
            # One country's failed backfill must not block the others.
            logger.exception("[%s] ERROR while backfilling panel", iso2)
