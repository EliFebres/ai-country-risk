"""Write side of the macro panel: World Bank data out to Parquet.

``backfill_missing_panels`` is the ETL's first phase — it builds a panel for
every rostered country that doesn't have one yet and writes it to the Parquet
store that ``data_retrieval.query_macro_panel`` reads back with DuckDB.
``ingest_panel_wide`` does the write, and ``merge_extra_indicators`` folds in
the indicators that don't come from the World Bank beforehand.

The backfill is incremental by design: World Bank fetches are slow and the
data is annual, so a country keeps its panel until it is rebuilt on purpose.
A normal run does no fetching here at all; ``main.py`` passes ``force=True``
on its monthly cadence to pick up revisions and newly published years.
"""

import logging
import duckdb
import pathlib
import pandas as pd

from typing import Mapping

from backend.utils import constants
import backend.utils.data_fetching.fetch_metrics as fetch_metrics
import backend.utils.data_fetching.political_corruption_fetch as political_corruption_fetch

logger = logging.getLogger(__name__)

# Parquet panel store: backend/data/wb_panel_wide (this file is in
# backend/utils/data_fetching/).
PANEL_DIR = pathlib.Path(__file__).resolve().parents[2] / "data" / "wb_panel_wide"


def ingest_panel_wide(panel: pd.DataFrame, country_code: str, root: pathlib.Path) -> None:
    """Persist a wide World Bank panel to Parquet, partitioned by country.

    The input ``panel`` is expected to be **wide** (rows = years, columns =
    indicators) with the index representing calendar years. The function resets
    the index to a ``year`` column, attaches the provided ``country_code``,
    and uses an in-memory DuckDB connection to `COPY` the data as Parquet
    files partitioned by ``country_code`` under ``root``.

    Args:
        panel: Non-empty, wide-form DataFrame whose index are years and whose
            columns are indicator codes. The index becomes a ``year`` column.
        country_code: ISO-2 country code, used both as a data column and the
            Parquet partition key.
        root: Output directory, created if missing.

    Raises:
        TypeError: if ``panel`` is not a DataFrame, ``country_code`` is not a
            string, or ``root`` is not a Path.
        ValueError: if ``panel`` is empty or ``country_code`` is blank —
            writing either would produce an unreadable partition.
    """
    if not isinstance(panel, pd.DataFrame):
        raise TypeError(f"`panel` must be a pandas DataFrame, got {type(panel).__name__}")
    if panel.empty:
        raise ValueError("`panel` must be a non-empty DataFrame, got an empty one")
    if not isinstance(country_code, str):
        raise TypeError(f"`country_code` must be a str, got {type(country_code).__name__}")
    if not country_code.strip():
        raise ValueError(f"`country_code` must be a non-empty str, got {country_code!r}")
    if not isinstance(root, pathlib.Path):
        raise TypeError(f"`root` must be a pathlib.Path, got {type(root).__name__}")

    # Tidy Dataframe For Duckdb
    df: pd.DataFrame = (
        panel.reset_index(names="year")          # index → 'year'
             .assign(country_code=country_code)  # partition column
    )

    # Ensure Destination Exists
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    # Write Via Duckdb
    con = None
    try:
        con = duckdb.connect(":memory:")
        con.register("df", df)

        target = str(root).replace("'", "''")  # escape single quotes for SQL literal
        con.execute(
            f"""
            COPY df
            TO '{target}'
            (FORMAT PARQUET,
             PARTITION_BY ('country_code'),
             OVERWRITE_OR_IGNORE 1);
            """
        )
    finally:
        if con is not None:
            con.close()


def merge_extra_indicators(
    panel: pd.DataFrame,
    iso2: str,
    iso3_by_iso2: Mapping[str, str],
) -> pd.DataFrame:
    """Merge non-World-Bank indicators into a country's wide WB panel.

    Currently adds the OWID/V-Dem **Political Corruption Index** as a
    ``POL_CORRUPTION`` column, aligned on the panel's int year index. OWID
    often publishes a year ahead of the World Bank, so the join is an outer one
    and the extra year is kept: ``data_retrieval`` anchors each indicator on its
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


def has_country_partition(root: pathlib.Path, iso2: str) -> bool:
    """True if ``root`` holds a non-empty ``country_code=XX`` Parquet partition.

    Args:
        root: the ``wb_panel_wide`` directory.
        iso2: country code naming the partition.

    Returns:
        True only when the directory exists and contains at least one
        ``.parquet`` file. An unreadable directory counts as "no partition",
        which triggers a harmless re-fetch.
    """
    part_dir = root / f"country_code={iso2}"
    if not part_dir.is_dir():
        return False
    try:
        return any(p.suffix == ".parquet" for p in part_dir.glob("*.parquet"))
    except OSError:
        return False


def backfill_missing_panels(force: bool = False) -> None:
    """Build Parquet panels for any rostered country that lacks one.

    Incremental and idempotent: countries that already have a partition are
    skipped, so a normal daily run does no World Bank fetching at all and only
    a first run (or a newly added country) pays for it.

    A country whose build fails is logged with a traceback and skipped — the
    others still get their panels.

    Args:
        force: rebuild every rostered country's panel, not just the missing
            ones, to pick up World Bank revisions and newly published years.
            Each country is overwritten in place once its fetch succeeds, so a
            failed one keeps the panel it already had.
    """
    root = PANEL_DIR
    root.mkdir(parents=True, exist_ok=True)

    roster = constants.COUNTRY_ROSTER
    iso3_by_iso2 = constants.ISO3_BY_ISO2

    missing = []
    for country in roster:
        iso2 = str(country["iso2"]).strip()
        if not iso2:
            continue
        if force or not has_country_partition(root, iso2):
            missing.append(iso2)

    if not missing:
        logger.info("All %d countries already have parquet partitions in %s.", len(roster), root)
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

            ingest_panel_wide(panel, iso2, root)
            logger.info("[%s] Wrote panel with %d years × %d indicators.", iso2, panel.shape[0], panel.shape[1])
        except Exception:
            # One country's failed backfill must not block the others.
            logger.exception("[%s] ERROR while backfilling panel", iso2)
