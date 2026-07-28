"""Characterization tests for ``backend.utils.data_fetching.curated_loader``.

The loader's contract is an asymmetry, and the asymmetry is the whole point:

* **absent files are silent** — every template ships empty, and warning on each
  one every run would train the operator to ignore the log;
* **malformed files are loud** — a file that exists is a file someone meant to
  be used, so a wrong column must not degrade into missing evidence that looks
  identical to the absent case;
* **header-only files are neither** — that is the shipped state.

Getting that backwards is the failure mode worth guarding: a loader that
swallowed a malformed file would let a typo'd tax rate silently disappear from
the payload, and the model would score confidently on evidence that was supposed
to be there.

No network, no database — the loader is pointed at ``tmp_path``.
"""

import datetime as _dt

import pytest

from backend.utils.data_fetching import curated_loader as cl


def write(tmp_path, name, text):
    """Drop a curated file into a fixture folder and return its path."""
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


HEADER = "country_iso2,period,value\n"


class TestAbsentFilesAreSilent:
    def test_empty_directory_loads_nothing(self, tmp_path):
        assert cl.load_curated_series(tmp_path) == []

    def test_absent_lookups_are_empty_not_defaulted(self, tmp_path):
        # An absent regime file must read as "unknown", which is what makes
        # suppressed_vol_flag return None instead of False.
        assert cl.load_fx_regimes(tmp_path) == {}
        assert cl.load_election_calendar(tmp_path) == {}
        assert cl.load_reference_constants(tmp_path) == {}

    def test_absent_weo_directory_is_empty(self, tmp_path):
        assert cl.load_weo_revisions(tmp_path / "nope") == {}


class TestHeaderOnlyIsTheShippedState:
    def test_header_only_csv_loads_zero_rows_without_raising(self, tmp_path):
        write(tmp_path, "statutory_rates.csv", HEADER)
        assert cl.load_curated_series(tmp_path) == []

    def test_shipped_templates_all_load(self):
        # The real folder as committed: every template present, all empty.
        assert cl.load_curated_series() == []
        assert cl.load_fx_regimes() == {}
        assert cl.load_election_calendar() == {}
        assert cl.load_weo_revisions() == {}
        assert cl.load_reference_constants()["rome_reference_ratio"] is None


class TestFilledFilesLoad:
    def test_annual_rows_become_indicator_series_rows(self, tmp_path):
        write(tmp_path, "statutory_rates.csv", HEADER + "PT,2025,31.5\nUS,2025,25.6\n")
        rows = cl.load_curated_series(tmp_path)
        assert len(rows) == 2
        row = next(r for r in rows if r["country_iso2"] == "PT")
        assert row["indicator_code"] == "STAT.TAX.TOP.RATE"
        assert row["freq"] == "A"
        assert row["period"] == "2025"
        assert row["value"] == 31.5
        assert row["source"] == "OECD Corporate Tax Statistics"
        assert isinstance(row["as_of"], _dt.date)

    def test_monthly_and_quarterly_periods(self, tmp_path):
        write(tmp_path, "reserves_monthly.csv", HEADER + "PT,2026-06,3.1e10\n")
        write(tmp_path, "wui_quarterly.csv", HEADER + "PT,2026Q2,0.31\n")
        rows = {r["indicator_code"]: r for r in cl.load_curated_series(tmp_path)}
        assert rows["RESERVES.USD"]["freq"] == "M"
        assert rows["WUI.INDEX"]["freq"] == "Q"

    def test_blank_value_is_null_not_skipped(self, tmp_path):
        # "reported as unavailable" is a different fact from "we never asked",
        # and only the first one has a row.
        write(tmp_path, "statutory_rates.csv", HEADER + "PT,2025,\n")
        rows = cl.load_curated_series(tmp_path)
        assert len(rows) == 1 and rows[0]["value"] is None

    def test_off_roster_rows_are_skipped_not_fatal(self, tmp_path):
        # Published datasets cover 190 countries; being wider than the roster is
        # not a defect in the file.
        write(tmp_path, "statutory_rates.csv", HEADER + "PT,2025,31.5\nZZ,2025,10.0\n")
        rows = cl.load_curated_series(tmp_path)
        assert [r["country_iso2"] for r in rows] == ["PT"]

    def test_lowercase_iso2_is_accepted(self, tmp_path):
        write(tmp_path, "statutory_rates.csv", HEADER + "pt,2025,31.5\n")
        assert cl.load_curated_series(tmp_path)[0]["country_iso2"] == "PT"

    def test_trailing_blank_line_is_not_an_error(self, tmp_path):
        write(tmp_path, "statutory_rates.csv", HEADER + "PT,2025,31.5\n,,\n")
        assert len(cl.load_curated_series(tmp_path)) == 1


class TestMalformedFilesAreLoud:
    def test_wrong_columns_raise(self, tmp_path):
        write(tmp_path, "statutory_rates.csv", "iso,year,rate\nPT,2025,31.5\n")
        with pytest.raises(cl.CuratedFileError, match="expected columns"):
            cl.load_curated_series(tmp_path)

    def test_columns_in_the_wrong_order_raise(self, tmp_path):
        write(tmp_path, "statutory_rates.csv", "period,country_iso2,value\n2025,PT,31.5\n")
        with pytest.raises(cl.CuratedFileError):
            cl.load_curated_series(tmp_path)

    def test_unparseable_value_raises_naming_the_line(self, tmp_path):
        write(tmp_path, "statutory_rates.csv", HEADER + "PT,2025,thirty\n")
        with pytest.raises(cl.CuratedFileError, match="line 2"):
            cl.load_curated_series(tmp_path)

    def test_wrong_period_format_for_the_frequency_raises(self, tmp_path):
        # An annual file given a month is a mistake, not a frequency change.
        write(tmp_path, "statutory_rates.csv", HEADER + "PT,2025-06,31.5\n")
        with pytest.raises(cl.CuratedFileError, match="not valid for frequency"):
            cl.load_curated_series(tmp_path)

    def test_month_13_raises(self, tmp_path):
        write(tmp_path, "reserves_monthly.csv", HEADER + "PT,2026-13,1.0\n")
        with pytest.raises(cl.CuratedFileError):
            cl.load_curated_series(tmp_path)


class TestFxRegimes:
    def test_loads_and_normalizes(self, tmp_path):
        write(tmp_path, "fx_regimes.yaml", "regimes:\n  pt: Float\n  SA: peg\n")
        assert cl.load_fx_regimes(tmp_path) == {"PT": "float", "SA": "peg"}

    def test_empty_regimes_mapping_is_empty(self, tmp_path):
        write(tmp_path, "fx_regimes.yaml", "version: 1\nregimes: {}\n")
        assert cl.load_fx_regimes(tmp_path) == {}

    def test_unknown_regime_raises(self, tmp_path):
        # A typo'd regime silently ignored would turn the suppressed-volatility
        # flag off for that country with no signal at all.
        write(tmp_path, "fx_regimes.yaml", "regimes:\n  PT: crawling-peg\n")
        with pytest.raises(cl.CuratedFileError, match="expected one of"):
            cl.load_fx_regimes(tmp_path)

    def test_non_mapping_raises(self, tmp_path):
        write(tmp_path, "fx_regimes.yaml", "regimes:\n  - PT\n")
        with pytest.raises(cl.CuratedFileError):
            cl.load_fx_regimes(tmp_path)


class TestElectionCalendar:
    def test_loads_sorted_by_date(self, tmp_path):
        write(tmp_path, "election_calendar.yaml",
              "elections:\n"
              "  PT:\n"
              "    - {date: '2027-01-10', kind: presidential}\n"
              "    - {date: '2026-10-04', kind: legislative}\n")
        assert cl.load_election_calendar(tmp_path)["PT"] == [
            {"date": "2026-10-04", "kind": "legislative"},
            {"date": "2027-01-10", "kind": "presidential"},
        ]

    def test_missing_kind_defaults_to_unspecified(self, tmp_path):
        write(tmp_path, "election_calendar.yaml",
              "elections:\n  PT:\n    - {date: '2026-10-04'}\n")
        assert cl.load_election_calendar(tmp_path)["PT"][0]["kind"] == "unspecified"

    def test_bad_date_raises(self, tmp_path):
        write(tmp_path, "election_calendar.yaml",
              "elections:\n  PT:\n    - {date: 'october'}\n")
        with pytest.raises(cl.CuratedFileError, match="unparseable date"):
            cl.load_election_calendar(tmp_path)

    def test_entry_without_a_date_raises(self, tmp_path):
        write(tmp_path, "election_calendar.yaml",
              "elections:\n  PT:\n    - {kind: legislative}\n")
        with pytest.raises(cl.CuratedFileError, match="without a `date`"):
            cl.load_election_calendar(tmp_path)


class TestReferenceConstants:
    def test_loads_a_set_ratio(self, tmp_path):
        write(tmp_path, "reference_constants.yaml", "version: 2\nrome_reference_ratio: 1.62\n")
        assert cl.load_reference_constants(tmp_path)["rome_reference_ratio"] == 1.62

    def test_null_ratio_is_the_shipped_state(self, tmp_path):
        write(tmp_path, "reference_constants.yaml", "rome_reference_ratio: null\n")
        assert cl.load_reference_constants(tmp_path)["rome_reference_ratio"] is None

    def test_non_numeric_ratio_raises(self, tmp_path):
        write(tmp_path, "reference_constants.yaml", "rome_reference_ratio: 'about 1.6'\n")
        with pytest.raises(cl.CuratedFileError, match="must be a number or null"):
            cl.load_reference_constants(tmp_path)


class TestWeoRevisions:
    def _vintages(self, tmp_path, first, second):
        write(tmp_path, "weo_202604.csv", "country_iso2,target_year,value\n" + first)
        write(tmp_path, "weo_202610.csv", "country_iso2,target_year,value\n" + second)

    def test_differences_consecutive_vintages(self, tmp_path):
        # April said 2.0 for 2027, October said 1.4 → a −0.6 revision.
        self._vintages(tmp_path, "PT,2027,2.0\n", "PT,2027,1.4\n")
        assert cl.load_weo_revisions(tmp_path) == {"PT": [-0.6]}

    def test_one_vintage_has_nothing_to_revise(self, tmp_path):
        write(tmp_path, "weo_202604.csv", "country_iso2,target_year,value\nPT,2027,2.0\n")
        assert cl.load_weo_revisions(tmp_path) == {}

    def test_vintage_order_comes_from_the_filename(self, tmp_path):
        # Written newest-first on disk; the revision must still be later-minus-
        # earlier, or every sign in the series flips.
        write(tmp_path, "weo_202610.csv", "country_iso2,target_year,value\nPT,2027,1.4\n")
        write(tmp_path, "weo_202604.csv", "country_iso2,target_year,value\nPT,2027,2.0\n")
        assert cl.load_weo_revisions(tmp_path) == {"PT": [-0.6]}

    def test_misnamed_vintage_file_raises(self, tmp_path):
        write(tmp_path, "weo_april.csv", "country_iso2,target_year,value\nPT,2027,2.0\n")
        write(tmp_path, "weo_202610.csv", "country_iso2,target_year,value\nPT,2027,1.4\n")
        with pytest.raises(cl.CuratedFileError, match="weo_<YYYY><MM>"):
            cl.load_weo_revisions(tmp_path)

    def test_feeds_forecast_instability(self, tmp_path):
        from backend.utils import metrics
        self._vintages(tmp_path, "PT,2027,2.0\nPT,2028,1.0\n", "PT,2027,1.4\nPT,2028,1.8\n")
        revisions = cl.load_weo_revisions(tmp_path)["PT"]
        # mean(|−0.6|, |+0.8|) = 0.7
        assert metrics.forecast_instability(revisions) == pytest.approx(0.7)
