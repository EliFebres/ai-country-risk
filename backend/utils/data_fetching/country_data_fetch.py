import shutil
import duckdb
import pathlib
import pandas as pd

from zoneinfo import ZoneInfo
from typing import Mapping, Optional
from datetime import datetime, date, timedelta

from backend.util import constants
import backend.utils.data_fetching.fetch_metrics as fetch_metrics
import backend.utils.data_fetching.political_corruption_fetch as political_corruption_fetch


def _first_monday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(0 - d.weekday()) % 7)

def _is_first_monday_of_quarter(now: datetime) -> bool:
    return now.month in (1, 4, 7, 10) and now.date() == _first_monday(now.year, now.month)


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


def ingest_panels_for_all_countries(
    root: pathlib.Path,
    indicators: Mapping[str, str],
    *,
    start: Optional[int] = None,
    end:   Optional[int] = None
) -> None:
    """Build and persist per-country panels for the hardcoded country roster.

    The country universe comes from ``constants.COUNTRY_ROSTER`` (ISO-2 / ISO-3).
    Each country's World Bank panel is augmented with non-WB indicators (e.g. the
    OWID Political Corruption Index) via :func:`merge_extra_indicators`.

    On the first Monday of each calendar quarter (Jan, Apr, Jul, Oct; timezone
    ``America/New_York``), the function deletes the directory at ``root`` using
    a safety-guarded `shutil.rmtree` and then recreates it to produce a clean
    snapshot before ingest.

    Args:
        root (pathlib.Path): Root output directory where per-country panel
            artifacts are written. On quarterly cleanup days, this directory is
            deleted and recreated at the start of the run.
        indicators (Mapping[str, str]): WB indicator col-name -> code map passed
            to the panel fetcher (e.g. ``constants.INDICATORS``). Must not be empty.
        start (Optional[int], keyword-only): First calendar year to include
            (inclusive). If ``None``, the fetcher’s default is used.
        end (Optional[int], keyword-only): Last calendar year to include
            (inclusive). If ``None``, the fetcher’s default is used.

    Returns:
        None
    """
    # Quarterly cleanup (first Monday of each quarter, America/New_York)
    now = datetime.now(ZoneInfo("America/New_York"))
    if _is_first_monday_of_quarter(now) and root.is_dir():
        # Safety guard: avoid catastrophic deletes (like '/')
        root_resolved = root.resolve()
        if len(root_resolved.parts) <= 3:  # tweak threshold for your project layout
            raise RuntimeError(f"Refusing to delete suspiciously high-level path: {root_resolved}")
        shutil.rmtree(root_resolved)

    # Input Validation
    assert indicators, "`indicators` mapping must not be empty"
    if start is not None and end is not None:
        assert start <= end, "`start` year must be ≤ `end` year"

    # Country roster is hardcoded (see constants.COUNTRY_ROSTER); the Excel file
    # is no longer read.
    iso3_by_iso2 = constants.ISO3_BY_ISO2

    # Iterate & Ingest
    for country in constants.COUNTRY_ROSTER:
        iso_code = country["iso2"]

        # Build the World Bank panel (robust to missing/empty series)
        panel = fetch_metrics.build_country_panel(
            iso_code,
            indicators,
            start=start,
            end=end,
            tidy_fetch=True,
        )

        # Merge non-WB indicators (e.g. Political Corruption Index from OWID)
        panel = merge_extra_indicators(panel, iso_code, iso3_by_iso2)

        # Skip countries with no usable rows from any source
        if panel is None or panel.empty:
            print(f"[{iso_code}] No rows for selected indicators — skipping write.")
            continue

        ingest_panel_wide(panel, iso_code, root)
        print(f"[{iso_code}] Wrote panel with {panel.shape[0]} years × {panel.shape[1]} indicators.")
