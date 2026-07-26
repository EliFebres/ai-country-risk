"""Read side of the macro panel: Parquet in, LLM-ready payload out.

``main.py`` writes each country's World Bank panel to
``backend/data/wb_panel_wide/country_code=XX/*.parquet`` (see
``data_fetching/country_data_fetch.ingest_panel_wide``); this module reads it
back with DuckDB and shapes it into the compact JSON payload the risk prompt
and the database upsert both consume.

The payload is deliberately "pretty": indicators carry their display names and
units, values are rounded, and only a short recent window plus a couple of
percent-change horizons are included — enough for the model to reason about
trend and level without spending context on a full history.
"""

import re
import duckdb
import pathlib
import pandas as pd

from datetime import datetime, timezone

from backend.utils import constants
from backend.utils.dates import utc_minute_iso


# Anchor all data paths to the backend/ folder (this file lives in backend/utils/)
BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]   # .../backend
DATA_DIR    = BACKEND_DIR / "data" / "wb_panel_wide"        # .../backend/data/wb_panel_wide

_ISO_CODE_RE = re.compile(r"[A-Z]{2,3}")


def _validate_iso_code(value: object, param: str) -> str:
    """Return ``value`` if it is a 2- or 3-letter uppercase ISO code.

    Args:
        value: the candidate code.
        param: parameter name, used in the error message.

    Returns:
        The validated code.

    Raises:
        TypeError: if ``value`` is not a string.
        ValueError: if it is not 2-3 uppercase letters.
    """
    if not isinstance(value, str):
        raise TypeError(f"`{param}` must be a str, got {type(value).__name__}: {value!r}")
    if not _ISO_CODE_RE.fullmatch(value):
        raise ValueError(
            f"`{param}` must be a 2- or 3-letter uppercase ISO code, got {value!r}"
        )
    return value


def query_macro_panel(country_iso_code: str) -> pd.DataFrame:
    """Load one country's macro panel (years >= 2000) from Parquet.

    Args:
        country_iso_code: 2- or 3-letter uppercase ISO code naming the
            partition to read.

    Returns:
        Year-ordered DataFrame with one column per indicator.

    Raises:
        TypeError: if ``country_iso_code`` is not a string.
        ValueError: if it is not a valid ISO code.
        FileNotFoundError: if the country has no Parquet partition yet — the
            backfill in ``main.ensure_missing_country_panels`` has not run for it.
    """
    _validate_iso_code(country_iso_code, "country_iso_code")

    part_dir = DATA_DIR / f"country_code={country_iso_code}"
    parquet_files = sorted(part_dir.glob("*.parquet"))

    if not parquet_files:
        raise FileNotFoundError(
            f"No parquet files found for {country_iso_code} at {part_dir}/*.parquet\n"
            f"HINTS:\n"
            f"  • Ensure writes go to {DATA_DIR}\n"
            f"  • Run the backfill (main.ensure_missing_country_panels) or confirm\n"
            f"    the country exists in constants.COUNTRY_ROSTER\n"
            f"  • Check permissions / paths in your runtime environment"
        )

    # Use glob form so DuckDB can read the full partition if multiple files exist
    parquet_glob = (part_dir / "*.parquet").as_posix()

    sql = f"""
        SELECT *
        FROM read_parquet('{parquet_glob}')
        WHERE year >= 2000
        ORDER BY year
    """
    return duckdb.sql(sql).df()


def prepare_llm_payload_pretty(
    country_iso: str,
    indicators: dict[str, str],
    *,
    since: int = 2015,
    lookback: int = 10,
    deltas: tuple[int, ...] = (1, 5),
) -> dict:
    """Build the compact macro payload sent to the risk prompt and the DB.

    Args:
        country_iso: 2- or 3-letter uppercase ISO code.
        indicators: raw column name -> World Bank code. Only the keys are used
            here (to select columns); display names come from
            ``constants.NICE_NAME``.
        since: earliest year to include.
        lookback: how many recent values to keep per indicator series.
        deltas: percent-change horizons in years, emitted as ``Δ{h}y`` keys.

    Returns:
        ``{country, latest_year, indicators: {pretty_name: {latest, Δ..y,
        series}}, _meta: {units, source, generated_at, series_lookback,
        data_dir}}``. ``_meta.generated_at`` is the timestamp
        ``data_push.upsert_snapshot`` parses back into the snapshot's ``as_of``.

    Raises:
        TypeError: if ``country_iso`` or ``indicators`` has the wrong type.
        ValueError: if the ISO code, ``since``, ``lookback``, or ``deltas``
            are out of range.
        FileNotFoundError: if the country has no Parquet partition.
    """
    _validate_iso_code(country_iso, "country_iso")

    if not isinstance(indicators, dict):
        raise TypeError(f"`indicators` must be a dict, got {type(indicators).__name__}")
    if not indicators:
        raise ValueError("`indicators` must not be empty")
    bad_keys = [k for k in indicators if not (isinstance(k, str) and k)]
    if bad_keys:
        raise ValueError(f"indicator keys must be non-empty str, got {bad_keys!r}")

    this_year = datetime.now().year
    if not isinstance(since, int) or not 1900 <= since <= this_year:
        raise ValueError(f"`since` must be a year between 1900 and {this_year}, got {since!r}")
    if not isinstance(lookback, int) or lookback <= 0:
        raise ValueError(f"`lookback` must be a positive int, got {lookback!r}")
    bad_deltas = [h for h in deltas if not (isinstance(h, int) and h > 0)]
    if bad_deltas:
        raise ValueError(f"`deltas` must contain positive ints, got {bad_deltas!r}")

    # ---- load & filter panel ----------------------------------------------
    df = query_macro_panel(country_iso)
    df = df[df.year >= since]

    latest_row  = df.tail(1).squeeze()
    latest_year = int(latest_row["year"])

    # ---- per-indicator build ----------------------------------------------
    year_indexed = df.set_index("year")
    ind_payload: dict[str, dict] = {}
    for raw_col in indicators.keys():
        pretty_name = constants.NICE_NAME.get(raw_col, raw_col)
        column = year_indexed[raw_col]

        # last `lookback` values
        series = column.dropna().tail(lookback).round(2).to_dict()

        # Δ-changes
        delta_vals = {}
        for h in deltas:
            pct = column.pct_change(h, fill_method=None).round(3).tail(1).iloc[0]
            delta_vals[f"Δ{h}y"] = None if pd.isna(pct) else float(pct)

        ind_payload[pretty_name] = {
            "latest": None if pd.isna(latest_row[raw_col]) else round(float(latest_row[raw_col]), 2),
            **delta_vals,
            "series": series,
        }

    return {
        "country": country_iso,
        "latest_year": latest_year,
        "indicators": ind_payload,
        "_meta": {
            "units": constants.UNITS,
            "source": "World Bank",
            "generated_at": utc_minute_iso(datetime.now(timezone.utc)),
            "series_lookback": lookback,
            "data_dir": str(DATA_DIR),
        },
    }
