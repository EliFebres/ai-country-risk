"""Per-edition WEO loading, and the vintage rule that uses it.

The macro half of the no-future rule. A revised GDP figure looks exactly like
an unrevised one, so the only defence is refusing to read a vintage that did not
exist yet — and proving the refusal, since nothing in a score would ever show it.

The fixtures are real WEO shape: tab-delimited text with an .xls name, "n/a"
for absent, thousands separators, and forward columns of projections.
"""

import datetime
import pathlib

import pytest

from backend.utils import data_retrieval
from backend.utils.history.vintage import weo

HEADER = ("WEO Country Code\tISO\tWEO Subject Code\tCountry\tSubject Descriptor\t"
          "Units\tScale\t2016\t2017\t2018\t2019\t2020")


def edition(tmp_path: pathlib.Path, name: str, rows, encoding="latin-1") -> pathlib.Path:
    path = tmp_path / name
    path.write_text("\n".join([HEADER] + rows), encoding=encoding)
    return path


# Portugal real GDP growth: the 2017 estimate as first published, then revised.
PT_APR2018 = ("182\tPRT\tNGDP_RPCH\tPortugal\tGross domestic product\t"
              "Percent change\tUnits\t1.9\t2.7\t2.3\t2.0\t1.8")
PT_OCT2018 = ("182\tPRT\tNGDP_RPCH\tPortugal\tGross domestic product\t"
              "Percent change\tUnits\t1.9\t2.8\t2.3\t2.2\t1.9")


class TestEditionDates:
    def test_the_vintage_comes_from_the_filename(self, tmp_path):
        path = edition(tmp_path, "2018-04.xls", [PT_APR2018])
        assert weo.edition_date(path) == datetime.date(2018, 4, 1)

    def test_a_misnamed_file_has_no_vintage(self, tmp_path):
        assert weo.edition_date(tmp_path / "WEOOct2018all.xls") is None

    def test_an_impossible_month_has_no_vintage(self, tmp_path):
        assert weo.edition_date(tmp_path / "2018-13.xls") is None


class TestReadingAnEdition:
    def test_rows_carry_the_edition_as_their_vintage(self, tmp_path):
        rows = weo.read_edition(edition(tmp_path, "2018-04.xls", [PT_APR2018]), ["PT"])
        assert rows
        assert all(r["as_of"] == datetime.date(2018, 4, 1) for r in rows)
        assert all(r["vintage_scheme"] == "as-published-edition" for r in rows)

    def test_projections_are_not_loaded_as_observations(self, tmp_path):
        # A 2018 edition's guess at 2020 is not a fact about 2020.
        rows = weo.read_edition(edition(tmp_path, "2018-04.xls", [PT_APR2018]), ["PT"])
        years = {r["period"].year for r in rows}
        assert years == {2016, 2017, 2018}
        assert 2019 not in years and 2020 not in years

    def test_the_revision_is_visible_across_editions(self, tmp_path):
        """The whole reason this module exists."""
        april = weo.read_edition(edition(tmp_path, "2018-04.xls", [PT_APR2018]), ["PT"])
        october = weo.read_edition(edition(tmp_path, "2018-10.xls", [PT_OCT2018]), ["PT"])
        pick = lambda rows: next(r["value"] for r in rows if r["period"].year == 2017)
        assert pick(april) == 2.7
        assert pick(october) == 2.8

    def test_countries_outside_the_roster_are_dropped(self, tmp_path):
        spain = ("184\tESP\tNGDP_RPCH\tSpain\tGross domestic product\t"
                 "Percent change\tUnits\t3.2\t3.0\t2.7\t2.2\t2.0")
        rows = weo.read_edition(edition(tmp_path, "2018-04.xls", [PT_APR2018, spain]), ["PT"])
        assert {r["country_iso2"] for r in rows} == {"PT"}

    def test_absent_is_absent_never_zero(self, tmp_path):
        blank = ("182\tPRT\tNGDP_RPCH\tPortugal\tGross domestic product\t"
                 "Percent change\tUnits\t1.9\tn/a\t--\t2.0\t1.8")
        rows = weo.read_edition(edition(tmp_path, "2018-04.xls", [blank]), ["PT"])
        assert {r["period"].year for r in rows} == {2016}

    def test_thousands_separators_parse(self, tmp_path):
        big = ("182\tPRT\tGGXWDG_NGDP\tPortugal\tGeneral government gross debt\t"
               "Percent of GDP\tUnits\t1,129.5\t125.7\t121.5\t119.0\t117.0")
        rows = weo.read_edition(edition(tmp_path, "2018-04.xls", [big]), ["PT"])
        assert next(r["value"] for r in rows if r["period"].year == 2016) == 1129.5

    def test_utf16_editions_read_too(self, tmp_path):
        path = edition(tmp_path, "2016-10.xls", [PT_APR2018], encoding="utf-16")
        assert weo.read_edition(path, ["PT"])

    def test_an_unparseable_file_is_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "2018-04.xls"
        path.write_text("not a weo table at all", encoding="latin-1")
        assert weo.read_edition(path, ["PT"]) == []


class TestLoadAll:
    def test_a_missing_directory_is_a_warning_not_a_crash(self, tmp_path, caplog):
        # The pilot can run without vintages; it just has to say so.
        assert weo.load_all(["PT"], tmp_path / "nope") == []
        assert "as-published-latest" in caplog.text

    def test_every_edition_is_loaded(self, tmp_path):
        edition(tmp_path, "2018-04.xls", [PT_APR2018])
        edition(tmp_path, "2018-10.xls", [PT_OCT2018])
        rows = weo.load_all(["PT"], tmp_path)
        assert {r["as_of"] for r in rows} == {datetime.date(2018, 4, 1),
                                              datetime.date(2018, 10, 1)}


class TestTheVintageRuleInThePayload:
    """`_resolve` is where a vintage either gets used or refused."""

    def observation(self, period_year, as_of, value):
        return data_retrieval._Observation(
            value=value, period=str(period_year), freq="A",
            period_end=datetime.date(period_year, 12, 31),
            as_of=as_of, source="IMF WEO")

    def test_the_newest_vintage_not_after_the_anchor_wins(self):
        merged = data_retrieval._resolve([
            self.observation(2017, datetime.date(2018, 4, 1), 2.7),
            self.observation(2017, datetime.date(2018, 10, 1), 2.8),
            self.observation(2017, datetime.date(2026, 4, 1), 3.5),
        ], as_of=datetime.date(2018, 6, 4))
        assert [o.value for o in merged] == [2.7], "June 2018 knew April's estimate only"

    def test_a_later_vintage_is_used_once_it_exists(self):
        merged = data_retrieval._resolve([
            self.observation(2017, datetime.date(2018, 4, 1), 2.7),
            self.observation(2017, datetime.date(2018, 10, 1), 2.8),
        ], as_of=datetime.date(2018, 12, 1))
        assert [o.value for o in merged] == [2.8]

    def test_a_period_covering_the_future_is_refused(self):
        # In June 2018 nobody knows 2018's annual figure.
        merged = data_retrieval._resolve([
            self.observation(2017, datetime.date(2018, 4, 1), 2.7),
            self.observation(2018, datetime.date(2018, 4, 1), 2.3),
        ], as_of=datetime.date(2018, 6, 4))
        assert [o.period for o in merged] == ["2017"]

    def test_the_daily_run_is_unaffected(self):
        """No as_of means no filtering — the live path must not change."""
        observations = [
            self.observation(2026, datetime.date(2026, 4, 1), 1.1),
            self.observation(2017, datetime.date(2026, 4, 1), 3.5),
        ]
        assert len(data_retrieval._resolve(observations)) == 2

    def test_build_evidence_payload_defaults_to_no_vintage_filter(self):
        import inspect
        sig = inspect.signature(data_retrieval.build_evidence_payload)
        assert sig.parameters["vintage_as_of"].default is None
