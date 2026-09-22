"""
Political Corruption Index (V-Dem, via Our World in Data).

The World Bank API does **not** serve the V-Dem "Political Corruption Index", so
this module fetches it from Our World in Data's stable grapher CSV endpoint and
exposes it per-country in the same shape the World Bank panel uses (a year-indexed
``pandas.Series``). The series is merged into each country's wide panel by
``country_data_fetch.merge_extra_indicators`` so the rest of the pipeline
(parquet -> data_retrieval -> data_push -> Postgres) treats it like any other
indicator.

Scale: 0-1, **higher = more corrupt** (opposite polarity to the WB z-score
indicators). Country code in the source CSV is ISO-3; callers pass an
ISO-2 -> ISO-3 mapping (see ``constants.ISO3_BY_ISO2``).

The whole CSV (all countries, all years) is downloaded **once per process** via
``functools.lru_cache`` — one HTTP GET serves every country in a run.
"""

import io
import logging
from functools import lru_cache
from typing import Mapping, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# Stable OWID grapher CSV (republishes V-Dem). One file, all countries/years.
OWID_CSV_URL = "https://ourworldindata.org/grapher/political-corruption-index.csv"

# Polite identification; OWID recommends sending a User-Agent.
_HEADERS = {"User-Agent": "AI-Country-Risk-Dashboard/1.0 (+political-corruption-index)"}
_TIMEOUT = 30  # seconds


@lru_cache(maxsize=1)
def _download_owid_df() -> pd.DataFrame:
    """Download the OWID Political Corruption Index CSV once and cache it.

    Returns an empty DataFrame on any network/parse failure so the surrounding
    pipeline degrades gracefully (mirrors ``fetch_metrics.wb_series`` returning
    empty on missing data) rather than aborting the whole run.
    """
    try:
        resp = requests.get(OWID_CSV_URL, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        logger.info("Fetched OWID Political Corruption Index CSV: %d rows.", len(df))
        return df
    except Exception as e:  # noqa: BLE001 - graceful degradation by design
        logger.warning("Could not fetch OWID Political Corruption Index CSV: %s", e)
        return pd.DataFrame()


def _value_column(df: pd.DataFrame) -> Optional[str]:
    """Identify the corruption-value column (robust to minor OWID header changes)."""
    meta_cols = {"Entity", "Code", "Year"}
    candidates = [c for c in df.columns if c not in meta_cols and "region" not in c.lower()]
    for c in candidates:
        if "corruption" in c.lower():
            return c
    return candidates[0] if candidates else None


@lru_cache(maxsize=1)
def load_corruption_by_iso3() -> dict[str, pd.Series]:
    """Return ``{ISO3: year-indexed float Series}`` for the Political Corruption Index.

    Empty dict if the CSV could not be fetched or lacks the expected columns.
    """
    df = _download_owid_df()
    if df.empty or not {"Code", "Year"}.issubset(df.columns):
        return {}

    value_col = _value_column(df)
    if value_col is None:
        logger.warning("OWID CSV has no recognizable value column; columns=%s", list(df.columns))
        return {}

    out: dict[str, pd.Series] = {}
    work = df[["Code", "Year", value_col]].dropna(subset=["Code"])
    for iso3, grp in work.groupby("Code"):
        series = (
            grp.set_index("Year")[value_col]
               .dropna()
               .astype("float64")
        )
        # Coerce the year index to plain ints so it aligns with the WB panel index.
        series.index = series.index.astype(int)
        series = series[~series.index.duplicated(keep="last")].sort_index()
        out[str(iso3)] = series
    return out


def corruption_series_for_iso2(
    iso2: str,
    iso3_by_iso2: Mapping[str, str],
    *,
    max_year: Optional[int] = None,
) -> pd.Series:
    """Year-indexed Political Corruption Index series for one ISO-2 country.

    Args:
        iso2: ISO-2 country code (the World Bank / DB key).
        iso3_by_iso2: Mapping ISO-2 -> ISO-3 (e.g. ``constants.ISO3_BY_ISO2``);
            the OWID CSV is keyed by ISO-3.
        max_year: If given, drop years greater than this. Used to clamp the
            series to the World Bank panel's latest year so adding this indicator
            never advances the panel's ``latest_year`` and silently nulls the
            existing indicators' ``latest`` values.

    Returns:
        A float64 ``pandas.Series`` indexed by int year (empty if the country is
        absent from OWID or the CSV could not be fetched).
    """
    iso3 = iso3_by_iso2.get(iso2)
    if not iso3:
        return pd.Series(dtype="float64")

    series = load_corruption_by_iso3().get(iso3)
    if series is None or series.empty:
        return pd.Series(dtype="float64")

    if max_year is not None:
        series = series[series.index <= int(max_year)]

    return series.astype("float64")
