"""Characterization tests for ``backend.utils.data_fetching.curated_loader``.

The loader's contract is an asymmetry, and the asymmetry is the whole point:

* **an absent file is silent** — it ships empty, and warning every run would
  train the operator to ignore the log;
* **malformed rows are loud** — a row that exists is a row someone meant to be
  used, so a typo must not degrade into missing evidence that looks identical to
  the absent case;
* **a header-only file is neither** — that is the shipped state.

Getting that backwards is the failure mode worth guarding: a loader that
swallowed a bad row would let a typo'd tax rate silently disappear from the
payload, and the model would score confidently on evidence that was supposed to
be there.

No network, no database — the loader is pointed at ``tmp_path``.
"""

import datetime as _dt

import pytest

from backend.utils import constants
from backend.utils.data_fetching import curated_loader as cl


HEADER = "country_iso2,indicator_code,period,value,as_of\n"


def write(tmp_path, text, name="curated.csv"):
    """Drop a curated file into a fixture folder and return its path."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestAbsentIsSilent:
    def test_absent_file_loads_nothing(self, tmp_path):
        assert cl.load_curated_series(tmp_path / "nope.csv") == []

    def test_shipped_file_loads(self):
        # The real file as committed: header only.
        assert cl.load_curated_series() == []


class TestHeaderOnlyIsTheShippedState:
    def test_header_only_loads_zero_rows_without_raising(self, tmp_path):
        assert cl.load_curated_series(write(tmp_path, HEADER)) == []


class TestFilledRowsLoad:
    def test_rows_become_indicator_series_rows(self, tmp_path):
        path = write(tmp_path, HEADER + "PT,STAT.TAX.TOP.RATE,2025,31.5,2026-07-01\n")
        row, = cl.load_curated_series(path)
        assert row == {
            "country_iso2": "PT",
            "indicator_code": "STAT.TAX.TOP.RATE",
            "freq": "A",
            "period": "2025",
            "value": 31.5,
            "as_of": _dt.date(2026, 7, 1),
            "source": "OECD Corporate Tax Statistics",
        }

    def test_freq_and_source_come_from_the_registry(self, tmp_path):
        # Neither is typed per row, so a registry edit reaches curated rows too.
        path = write(tmp_path, HEADER
                     + "PT,RESERVES.USD,2026-06,3.1e10,2026-07-01\n"
                     + "PT,WUI.INDEX,2026Q2,0.31,2026-07-01\n")
        rows = {r["indicator_code"]: r for r in cl.load_curated_series(path)}
        assert rows["RESERVES.USD"]["freq"] == "M"
        assert rows["WUI.INDEX"]["freq"] == "Q"
        assert rows["WUI.INDEX"]["source"] == "World Uncertainty Index"

    def test_blank_value_is_null_not_skipped(self, tmp_path):
        # "reported as unavailable" is a different fact from "we never asked",
        # and only the first one has a row.
        path = write(tmp_path, HEADER + "PT,STAT.TAX.TOP.RATE,2025,,2026-07-01\n")
        rows = cl.load_curated_series(path)
        assert len(rows) == 1 and rows[0]["value"] is None

    def test_off_roster_rows_are_skipped_not_fatal(self, tmp_path):
        # Published datasets cover 190 countries; being wider than the roster is
        # not a defect in the file.
        path = write(tmp_path, HEADER
                     + "PT,STAT.TAX.TOP.RATE,2025,31.5,2026-07-01\n"
                     + "ZZ,STAT.TAX.TOP.RATE,2025,10.0,2026-07-01\n")
        assert [r["country_iso2"] for r in cl.load_curated_series(path)] == ["PT"]

    def test_lowercase_iso2_is_accepted(self, tmp_path):
        path = write(tmp_path, HEADER + "pt,STAT.TAX.TOP.RATE,2025,31.5,2026-07-01\n")
        assert cl.load_curated_series(path)[0]["country_iso2"] == "PT"

    def test_trailing_blank_line_is_not_an_error(self, tmp_path):
        path = write(tmp_path, HEADER + "PT,STAT.TAX.TOP.RATE,2025,31.5,2026-07-01\n,,,,\n")
        assert len(cl.load_curated_series(path)) == 1


class TestMalformedRowsAreLoud:
    def test_wrong_columns_raise(self, tmp_path):
        path = write(tmp_path, "iso,code,year,rate\nPT,X,2025,31.5\n")
        with pytest.raises(cl.CuratedFileError, match="expected columns"):
            cl.load_curated_series(path)

    def test_columns_in_the_wrong_order_raise(self, tmp_path):
        path = write(tmp_path, "indicator_code,country_iso2,period,value,as_of\n")
        with pytest.raises(cl.CuratedFileError, match="expected columns"):
            cl.load_curated_series(path)

    def test_unknown_indicator_code_raises(self, tmp_path):
        # A typo'd code silently accepted would land rows nothing ever reads:
        # the indicator would report absent with no signal at all.
        path = write(tmp_path, HEADER + "PT,STAT.TAX.TOP.RAT,2025,31.5,2026-07-01\n")
        with pytest.raises(cl.CuratedFileError, match="not in INDICATOR_REGISTRY"):
            cl.load_curated_series(path)

    def test_unparseable_value_raises_naming_the_line(self, tmp_path):
        path = write(tmp_path, HEADER + "PT,STAT.TAX.TOP.RATE,2025,thirty,2026-07-01\n")
        with pytest.raises(cl.CuratedFileError, match="line 2"):
            cl.load_curated_series(path)

    def test_wrong_period_format_for_the_frequency_raises(self, tmp_path):
        # An annual indicator given a month is a mistake, not a frequency change.
        path = write(tmp_path, HEADER + "PT,STAT.TAX.TOP.RATE,2025-06,31.5,2026-07-01\n")
        with pytest.raises(cl.CuratedFileError, match="not valid for"):
            cl.load_curated_series(path)

    def test_month_13_raises(self, tmp_path):
        path = write(tmp_path, HEADER + "PT,RESERVES.USD,2026-13,1.0,2026-07-01\n")
        with pytest.raises(cl.CuratedFileError, match="line 2"):
            cl.load_curated_series(path)

    def test_unparseable_as_of_raises(self, tmp_path):
        path = write(tmp_path, HEADER + "PT,STAT.TAX.TOP.RATE,2025,31.5,july\n")
        with pytest.raises(cl.CuratedFileError, match="as_of"):
            cl.load_curated_series(path)


class TestCuratedConstants:
    """The regime vocabulary and the election dates used to be validated by the
    YAML loader. They are hand-edited dicts now, so the check lives here."""

    def test_fx_regimes_use_the_documented_vocabulary(self):
        # A typo'd regime turns suppressed_vol_flag off for that country with no
        # signal at all — metrics.suppressed_vol_flag reads it as "not managed".
        assert set(constants.FX_REGIMES) <= {c["iso2"] for c in constants.COUNTRY_ROSTER}
        assert set(constants.FX_REGIMES.values()) <= {"peg", "managed", "float"}

    def test_election_entries_are_parseable_and_sorted(self):
        for iso2, entries in constants.ELECTIONS.items():
            dates = [_dt.date.fromisoformat(e["date"]) for e in entries]
            assert dates == sorted(dates), f"{iso2}: elections must be sorted by date"
            assert all(e.get("kind") for e in entries), f"{iso2}: every entry needs a kind"

    def test_rome_reference_ratio_is_a_number_or_none(self):
        ratio = constants.ROME_REFERENCE_RATIO
        assert ratio is None or isinstance(ratio, (int, float))
