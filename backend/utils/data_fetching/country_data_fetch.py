"""Write side of the macro panel: assembled DataFrame out to Parquet.

``ingest_panel_wide`` persists one country's panel as a Parquet partition that
``data_retrieval.query_macro_panel`` reads back with DuckDB;
``merge_extra_indicators`` folds in the indicators that don't come from the
World Bank before that write.
"""

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
    ``POL_CORRUPTION`` column, aligned on the panel's int year index. The
    corruption series is clamped to the panel's latest year so this never
    advances ``latest_year`` downstream (which would null out existing
    indicators' ``latest`` values).

    Args:
        panel: Wide, year-indexed WB panel (may be empty).
        iso2: ISO-2 country code.
        iso3_by_iso2: ISO-2 -> ISO-3 map (OWID is ISO-3 keyed).

    Returns:
        The panel with a ``POL_CORRUPTION`` column. Stays empty only if both
        the WB panel and the corruption series are empty.
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
