import duckdb
import pathlib
import pandas as pd

from typing import Mapping

import backend.utils.data_fetching.political_corruption_fetch as political_corruption_fetch


def ingest_panel_wide(panel: pd.DataFrame, country_code: str, root: pathlib.Path) -> None:
    """Persist a wide World Bank panel to Parquet, partitioned by country.

    The input ``panel`` is expected to be **wide** (rows = years, columns =
    indicators) with the index representing calendar years. The function resets
    the index to a ``year`` column, attaches the provided ``country_code``,
    and uses an in-memory DuckDB connection to `COPY` the data as Parquet
    files partitioned by ``country_code`` under ``root``.

    Args:
        panel (pd.DataFrame): Non-empty, wide-form DataFrame whose index are
            years and whose columns are indicator codes (or similar). The index
            will be reset to a ``year`` column.
        country_code (str): ISO-2 (or similar) country code used both as a
            data column and the Parquet partition key.
        root (pathlib.Path): Output directory. It will be created if missing,
            then used as the COPY destination for Parquet output.

    Returns:
        None
    """
    # Input Validation
    assert isinstance(panel, pd.DataFrame) and not panel.empty, \
        "`panel` must be a non-empty DataFrame"
    assert isinstance(country_code, str) and country_code.strip(), \
        "`country_code` must be a non-empty str"
    assert isinstance(root, pathlib.Path), "`root` must be a pathlib.Path"

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
    ``POL_CORRUPTION`` column, aligned on the panel's int year index. The
    corruption series is clamped to the panel's latest year so this never
    advances ``latest_year`` downstream (which would null out existing
    indicators' ``latest`` values).

    Args:
        panel (pd.DataFrame): Wide, year-indexed WB panel (may be empty).
        iso2 (str): ISO-2 country code.
        iso3_by_iso2 (Mapping[str, str]): ISO-2 -> ISO-3 map (OWID is ISO-3 keyed).

    Returns:
        pd.DataFrame: The panel with a ``POL_CORRUPTION`` column. Stays empty
        only if both the WB panel and the corruption series are empty.
    """
    has_panel = isinstance(panel, pd.DataFrame) and not panel.empty
    max_year = int(panel.index.max()) if has_panel else None

    series = political_corruption_fetch.corruption_series_for_iso2(
        iso2, iso3_by_iso2, max_year=max_year
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
