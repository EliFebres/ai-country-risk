"""Characterization tests for ``prepare_llm_payload_pretty``'s per-indicator anchoring.

The World Bank publishes these indicators on different lags — WGI z-scores
trail a year, Gini two — so the panel's newest row is populated for the fastest
series only. Anchoring ``latest`` and the deltas on that shared row reported
null for the slower half of the set (5 of 9 for Portugal) while the values sat
one row above, which is what these tests pin down.

The deltas are absolute differences rather than percent changes because several
of these series cross zero: Portugal's inflation going -0.01 -> 2.34 is +2.35pp,
but ``pct_change`` calls it -188.8, sign-flipped by the negative base.
"""

import duckdb
import numpy as np
import pandas as pd

from backend.utils import data_retrieval

# year -> value. FAST reports through 2024; SLOW lags a year; CROSS dips
# negative in 2019 so a percent change off it would flip sign.
PANEL = pd.DataFrame({
    "year":  [2019, 2020, 2021, 2022, 2023, 2024],
    "FAST":  [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
    "SLOW":  [10.0, 20.0, 30.0, 40.0, 50.0, np.nan],
    "CROSS": [-0.02, 0.1, 0.2, 0.3, 0.4, 2.33],
})

INDICATORS = {"FAST": "X.FAST", "SLOW": "X.SLOW", "CROSS": "X.CROSS"}


def build(tmp_path, monkeypatch) -> dict:
    """Write PANEL as a country partition and return its payload."""
    part = tmp_path / "country_code=ZZ"
    part.mkdir()
    # DuckDB, not pandas.to_parquet: it is what writes these panels in
    # country_data_fetch, and the only parquet engine the venv actually has.
    out = (part / "data_0.parquet").as_posix()
    duckdb.sql(f"COPY (SELECT * FROM PANEL) TO '{out}' (FORMAT PARQUET)")
    monkeypatch.setattr(data_retrieval, "DATA_DIR", tmp_path)
    return data_retrieval.prepare_llm_payload_pretty("ZZ", INDICATORS)


class TestLatest:
    def test_uses_each_indicators_own_last_observation(self, tmp_path, monkeypatch):
        ind = build(tmp_path, monkeypatch)["indicators"]
        assert ind["FAST"]["latest"] == 6.0
        # 2024 is null for SLOW; the 2023 value must not be reported as missing.
        assert ind["SLOW"]["latest"] == 50.0

    def test_latest_year_is_the_panels_newest_row(self, tmp_path, monkeypatch):
        assert build(tmp_path, monkeypatch)["latest_year"] == 2024


class TestDeltas:
    def test_absolute_difference_not_percent(self, tmp_path, monkeypatch):
        ind = build(tmp_path, monkeypatch)["indicators"]
        assert ind["FAST"]["Δ1y"] == 1.0   # 6 - 5, not 0.2
        assert ind["FAST"]["Δ5y"] == 5.0   # 6 - 1, not 5.0x

    def test_negative_base_keeps_its_sign(self, tmp_path, monkeypatch):
        # 2.33 - (-0.02) = +2.35. pct_change would report -117.5 here.
        assert build(tmp_path, monkeypatch)["indicators"]["CROSS"]["Δ5y"] == 2.35

    def test_measured_from_the_indicators_own_latest(self, tmp_path, monkeypatch):
        # SLOW's newest observation is 2023, so Δ1y spans 2022 -> 2023.
        assert build(tmp_path, monkeypatch)["indicators"]["SLOW"]["Δ1y"] == 10.0

    def test_missing_base_year_is_none(self, tmp_path, monkeypatch):
        # SLOW's Δ5y would need 2018, which predates the panel.
        assert build(tmp_path, monkeypatch)["indicators"]["SLOW"]["Δ5y"] is None
