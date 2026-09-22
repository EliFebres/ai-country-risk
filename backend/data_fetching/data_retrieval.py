import re
import duckdb
import pandas as pd

from datetime import datetime, timezone

from backend.util import constants
from backend.util import paths


# Anchor all data paths to backend/, via the one resolver. The writer of this
# panel (data_fetching) now reads the same constant, so reader and writer cannot
# disagree about where it lives.
BACKEND_DIR = paths.BACKEND_DIR
DATA_DIR    = paths.PANEL_DIR


def query_macro_panel(country_iso_code: str) -> pd.DataFrame:
    """
    Load and return World-Bank macro-panel data for *country_iso_code*
    (years ≥ 2000) from «backend/data/wb_panel_wide/».
    """
    # ---- validation --------------------------------------------------------
    assert isinstance(country_iso_code, str) and country_iso_code, "`country_iso_code` must be a non-empty str"
    assert re.fullmatch(r"[A-Z]{2,3}", country_iso_code), "`country_iso_code` must be a 2- or 3-letter uppercase ISO code"

    # ---- compose partition path -------------------------------------------
    part_dir = DATA_DIR / f"country_code={country_iso_code}"
    parquet_files = sorted(part_dir.glob("*.parquet"))

    if not parquet_files:
        raise FileNotFoundError(
            f"No parquet files found for {country_iso_code} at {part_dir}/*.parquet\n"
            f"HINTS:\n"
            f"  • Ensure writes go to {DATA_DIR}\n"
            f"  • Run backfill or confirm the country exists in your Excel map\n"
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
    """
    Build a compact, human-readable payload of recent macro-economic data
    suitable for an LLM.
    """
    # ---- validation --------------------------------------------------------
    assert isinstance(country_iso, str) and re.fullmatch(r"[A-Z]{2,3}", country_iso), \
        "`country_iso` must be a 2- or 3-letter uppercase ISO code"
    assert isinstance(indicators, dict) and indicators, "`indicators` must be a non-empty dict"
    assert all(isinstance(k, str) and k for k in indicators.keys()), "indicator keys must be non-empty str"
    assert isinstance(since, int) and 1900 <= since <= datetime.now().year, "`since` must be a reasonable year"
    assert isinstance(lookback, int) and lookback > 0, "`lookback` must be a positive int"
    assert all(isinstance(h, int) and h > 0 for h in deltas), "`deltas` must contain positive ints"

    # ---- load & filter panel ----------------------------------------------
    df = query_macro_panel(country_iso)
    df = df[df.year >= since]

    latest_row  = df.tail(1).squeeze()
    latest_year = int(latest_row["year"])

    # ---- per-indicator build ----------------------------------------------
    ind_payload: dict[str, dict] = {}
    for raw_col in indicators.keys():
        pretty_name = constants.NICE_NAME.get(raw_col, raw_col)

        # last `lookback` values
        series = (
            df.set_index("year")[raw_col]
              .dropna()
              .tail(lookback)
              .round(2)
              .to_dict()
        )

        # Δ-changes
        delta_vals = {}
        for h in deltas:
            pct = (
                df.set_index("year")[raw_col]
                  .pct_change(h, fill_method=None)
                  .round(3)
                  .tail(1)
                  .iloc[0]
            )
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
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
            "series_lookback": lookback,
        },
    }


def panel_values(
    country_iso: str,
    *,
    since: int = 2000,
    lookback: int = 10,
) -> dict[str, dict]:
    """Return the panel keyed by its own column names, for the payload builder.

    `prepare_llm_payload_pretty` keys its output by display label, which is a
    presentation decision and a poor join key. The economics block joins on the
    registry instead, so it needs the panel column and the year each value
    belongs to — the year being what `vintage` dates the observation from.

    Returns:
        ``{panel_col: {"latest", "latest_year", "series"}}``, carrying only the
        columns that actually hold a value. A column the panel does not have is
        absent rather than present-and-null, so the caller can tell "this
        country has no row" from "this indicator is not in the panel at all".
    """
    df = query_macro_panel(country_iso)
    df = df[df.year >= since]

    out: dict[str, dict] = {}
    for col in df.columns:
        # The panel carries its partition key (`country_code`) as a column, and
        # a partition key is not an observation. Selecting on dtype rather than
        # on a name list means a new non-numeric column cannot break this the
        # way the first one did.
        if col == "year" or not pd.api.types.is_numeric_dtype(df[col]):
            continue
        s = df.set_index("year")[col].dropna()
        if s.empty:
            continue
        s = s.tail(lookback).round(4)
        latest_year = int(s.index.max())
        out[col] = {
            "latest": float(s.loc[latest_year]),
            "latest_year": latest_year,
            "series": {int(y): float(v) for y, v in s.items()},
        }
    return out
