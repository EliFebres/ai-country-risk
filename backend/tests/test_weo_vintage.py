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
from backend.utils.history.vintage import lags, monthly, weo

HEADER = ("WEO Country Code\tISO\tWEO Subject Code\tCountry\tSubject Descriptor\t"
          "Units\tScale\t2016\t2017\t2018\t2019\t2020")


def edition(tmp_path: pathlib.Path, name: str, rows, encoding="latin-1") -> pathlib.Path:
    path = tmp_path / name
    path.write_text("\n".join([HEADER] + rows), encoding=encoding)
    return path


# Portugal inflation: the 2017 estimate as first published, then revised.
# PCPIPCH because that is the one subject `SUBJECTS` maps — the other four have
# no indicator-registry equivalent to be read through, so a fixture using them
# would test a loader whose output nothing consumes.
PT_APR2018 = ("182\tPRT\tPCPIPCH\tPortugal\tInflation, average consumer prices\t"
              "Percent change\tUnits\t1.9\t2.7\t2.3\t2.0\t1.8")
PT_OCT2018 = ("182\tPRT\tPCPIPCH\tPortugal\tInflation, average consumer prices\t"
              "Percent change\tUnits\t1.9\t2.8\t2.3\t2.2\t1.9")


class TestEditionDates:
    def test_the_vintage_comes_from_the_filename(self, tmp_path):
        path = edition(tmp_path, "2018-04.xls", [PT_APR2018])
        assert weo.edition_date(path) == datetime.date(2018, 4, 1)

    def test_a_misnamed_file_has_no_vintage(self, tmp_path):
        assert weo.edition_date(tmp_path / "WEOOct2018all.xls") is None

    def test_an_impossible_month_has_no_vintage(self, tmp_path):
        assert weo.edition_date(tmp_path / "2018-13.xls") is None


class TestTheSubjectMapping:
    """Where the editions were being loaded to before anyone checked.

    Every one of the five subjects mapped onto a World Bank code that reads
    perfectly plausibly and is not in `INDICATOR_REGISTRY`. The builder resolves
    registry codes and nothing else, so all five loaded and none was ever read —
    no error, no warning, correct-looking row counts, and not one WEO number in
    any score.
    """

    def test_every_mapped_subject_lands_on_a_registry_code(self):
        from backend.utils import constants
        for subject, code in weo.SUBJECTS.items():
            assert code in constants.INDICATOR_REGISTRY, (
                f"{subject} maps to {code}, which no ledger requests")

    def test_a_mapped_subject_reaches_a_ledger(self):
        """In the registry is not enough — it has to be evidence, not a helper."""
        from backend.utils import constants
        for subject, code in weo.SUBJECTS.items():
            assert constants.INDICATOR_REGISTRY[code].get("ledger"), (
                f"{subject} maps to {code}, which carries no ledger")

    def test_every_weo_subject_is_mapped(self):
        # All five now have a registry entry of their own. Nothing is loaded to
        # nowhere: a subject worth parsing is a subject worth reading.
        assert set(weo.SUBJECTS) == {"PCPIPCH", "NGDP_RPCH", "GGXWDG_NGDP",
                                     "GGXCNL_NGDP", "BCA_NGDPD"}

    def test_aggregate_growth_is_not_pointed_at_per_capita(self):
        """They differ by population growth, so one served as the other is a
        wrong number rather than a missing one."""
        assert weo.SUBJECTS["NGDP_RPCH"] != "NY.GDP.PCAP.KD.ZG"


class TestReadingAnEdition:
    def test_rows_carry_the_edition_as_their_vintage(self, tmp_path):
        rows = weo.read_edition(edition(tmp_path, "2018-04.xls", [PT_APR2018]), ["PT"])
        assert rows
        assert all(r["as_of"] == datetime.date(2018, 4, 1) for r in rows)
        assert all(r["vintage_scheme"] == "as-published-edition" for r in rows)

    def test_projections_are_not_loaded_as_observations(self, tmp_path):
        # A 2018 edition's guess at 2020 is not a fact about 2020.
        rows = weo.read_edition(edition(tmp_path, "2018-04.xls", [PT_APR2018]), ["PT"])
        years = {int(r["period"]) for r in rows}
        assert years == {2016, 2017, 2018}
        assert 2019 not in years and 2020 not in years

    def test_the_period_is_the_one_the_payload_builder_can_parse(self, tmp_path):
        """The contract with the consumer, not with ourselves.

        These rows were written with a dated period ("2017-12-31") for as long as
        this module existed. Every edition loaded, every row count looked right,
        `_period_to_date` returned None for all of them, and the payload builder
        dropped the lot — so no WEO value ever reached a score and nothing said
        so. Asserting the loader's own convention is what let that survive; this
        asserts the one the reader uses.
        """
        rows = weo.read_edition(edition(tmp_path, "2018-04.xls", [PT_APR2018]), ["PT"])
        assert rows
        for row in rows:
            assert data_retrieval._period_to_date(row["period"], row["freq"]) is not None

    def test_the_revision_is_visible_across_editions(self, tmp_path):
        """The whole reason this module exists."""
        april = weo.read_edition(edition(tmp_path, "2018-04.xls", [PT_APR2018]), ["PT"])
        october = weo.read_edition(edition(tmp_path, "2018-10.xls", [PT_OCT2018]), ["PT"])
        pick = lambda rows: next(r["value"] for r in rows if r["period"] == "2017")
        assert pick(april) == 2.7
        assert pick(october) == 2.8

    def test_a_revision_actually_reaches_the_resolver(self):
        """End to end: two vintages of one year, resolved at two anchors.

        The unit tests above all pass with a period the payload builder cannot
        read. This one fails unless the row survives `_resolve`.
        """
        rows = []
        for vintage, value in ((datetime.date(2018, 4, 1), 2.7),
                               (datetime.date(2018, 10, 1), 2.8)):
            rows.append(data_retrieval._Observation(
                value=value, period="2017", freq="A",
                period_end=data_retrieval._period_to_date("2017", "A"),
                as_of=vintage, source=f"IMF WEO {vintage:%Y-%m}"))
        assert [o.value for o in data_retrieval._resolve(rows, datetime.date(2018, 6, 4))] == [2.7]
        assert [o.value for o in data_retrieval._resolve(rows, datetime.date(2018, 12, 1))] == [2.8]

    def test_countries_outside_the_roster_are_dropped(self, tmp_path):
        spain = ("184\tESP\tPCPIPCH\tSpain\tInflation, average consumer prices\t"
                 "Percent change\tUnits\t3.2\t3.0\t2.7\t2.2\t2.0")
        rows = weo.read_edition(edition(tmp_path, "2018-04.xls", [PT_APR2018, spain]), ["PT"])
        assert {r["country_iso2"] for r in rows} == {"PT"}

    def test_absent_is_absent_never_zero(self, tmp_path):
        blank = ("182\tPRT\tPCPIPCH\tPortugal\tInflation, average consumer prices\t"
                 "Percent change\tUnits\t1.9\tn/a\t--\t2.0\t1.8")
        rows = weo.read_edition(edition(tmp_path, "2018-04.xls", [blank]), ["PT"])
        assert {int(r["period"]) for r in rows} == {2016}

    def test_thousands_separators_parse(self, tmp_path):
        big = ("182\tPRT\tPCPIPCH\tPortugal\tInflation, average consumer prices\t"
               "Percent change\tUnits\t1,129.5\t125.7\t121.5\t119.0\t117.0")
        rows = weo.read_edition(edition(tmp_path, "2018-04.xls", [big]), ["PT"])
        assert next(r["value"] for r in rows if r["period"] == "2016") == 1129.5

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

    def test_a_real_vintage_outranks_the_panels_year_end_stamp(self):
        """January to March is a quarter of the anchors, and it was reading today.

        The panel stamps every annual figure with 31 December of its own year,
        because it has no record of when the World Bank published it. Between
        that year end and the next WEO edition, the placeholder had the newer
        date and won — so a February 2018 snapshot read 2026's revision of 2017
        rather than the October 2017 estimate that was the newest thing anyone
        could actually have had.
        """
        panel_stamp = data_retrieval._Observation(
            value=1.37, period="2017", freq="A",
            period_end=datetime.date(2017, 12, 31),
            as_of=datetime.date(2017, 12, 31), source="World Bank panel", dated=False)
        real_edition = data_retrieval._Observation(
            value=1.581, period="2017", freq="A",
            period_end=datetime.date(2017, 12, 31),
            as_of=datetime.date(2017, 10, 1), source="IMF WEO 2017-10", dated=True)
        merged = data_retrieval._resolve([panel_stamp, real_edition],
                                         as_of=datetime.date(2018, 2, 20))
        assert [o.value for o in merged] == [1.581], "the placeholder outranked the edition"

    def test_the_panel_still_wins_when_it_is_the_only_source(self):
        panel_stamp = data_retrieval._Observation(
            value=1.37, period="2017", freq="A",
            period_end=datetime.date(2017, 12, 31),
            as_of=datetime.date(2017, 12, 31), source="World Bank panel", dated=False)
        merged = data_retrieval._resolve([panel_stamp], as_of=datetime.date(2018, 2, 20))
        assert [o.value for o in merged] == [1.37]

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


class TestMonthlyRestamping:
    """A backfilled monthly print has to be dated when it landed, not today."""

    def test_a_month_ends_on_its_last_day(self):
        assert lags.period_end("2018-02", "M") == datetime.date(2018, 2, 28)
        assert lags.period_end("2020-02", "M") == datetime.date(2020, 2, 29)
        assert lags.period_end("2018-12", "M") == datetime.date(2018, 12, 31)

    def test_a_quarter_ends_on_its_last_day(self):
        assert lags.period_end("2018Q1", "Q") == datetime.date(2018, 3, 31)
        assert lags.period_end("2018Q4", "Q") == datetime.date(2018, 12, 31)

    def test_a_year_ends_in_december(self):
        assert lags.period_end("2018", "A") == datetime.date(2018, 12, 31)

    def test_the_print_lands_after_the_period_it_describes(self):
        # Late rather than early: reading a number a fortnight before anyone
        # had it is a leak; a fortnight after is a small staleness.
        assert lags.published_on("2018-03", "M") > datetime.date(2018, 3, 31)

    def test_restamping_replaces_today_with_the_publication_date(self):
        rows = monthly.restamp([{
            "country_iso2": "PT", "indicator_code": "CPI.YOY", "freq": "M",
            "period": "2018-03", "value": 1.2, "as_of": datetime.date.today(),
        }])
        # With the indicator code, because the lag is CPI's own 25 days rather
        # than the monthly default — the table stopped being one number per
        # frequency when it started dating market rates too.
        assert rows[0]["as_of"] == lags.published_on("2018-03", "M", "CPI.YOY")
        assert rows[0]["vintage_scheme"] == "publication-lag-estimate"

    def test_an_undatable_row_is_dropped_not_misdated(self):
        assert monthly.restamp([{"period": "garbage", "freq": "M"}]) == []

    def test_a_restamped_row_survives_its_own_vintage_filter(self):
        """The bug this module exists to prevent: rows stamped 'today' are
        discarded by the vintage bound, so a 2018 snapshot silently loses all
        of its monthly macro."""
        stamp = lags.published_on("2018-03", "M")
        obs = data_retrieval._Observation(
            value=1.2, period="2018-03", freq="M",
            period_end=lags.period_end("2018-03", "M"),
            as_of=stamp, source="IMF")
        assert data_retrieval._resolve([obs], as_of=datetime.date(2018, 9, 3)) == [obs]
        # …and is correctly refused before it was published.
        assert data_retrieval._resolve([obs], as_of=datetime.date(2018, 4, 1)) == []


class TestTheLeapDay:
    """A February month-end has no counterpart in the year before it.

    `date(2020, 2, 29).replace(year=2019)` raises, and both of the payload's
    "N years before this" call sites used exactly that. It is not an edge case
    anyone has to reach for: monthly FX and CPI observations end on the last day
    of their month, so every snapshot anchored in the months after a leap
    February hit it — 2016, 2020 and 2024 inside the pilot window alone. The
    failure surfaced as a *snapshot*, not as a traceback: `score_one` records the
    exception and moves on, so the series would simply have been missing weeks.
    """

    def monthly(self, period, value, as_of):
        return data_retrieval._Observation(
            value=value, period=period, freq="M",
            period_end=lags.period_end(period, "M"),
            as_of=as_of, source="BIS XRU")

    def test_a_trend_off_a_leap_february_does_not_raise(self):
        observations = [
            self.monthly("2019-02", 1.0, datetime.date(2019, 3, 31)),
            self.monthly("2020-02", 3.0, datetime.date(2020, 3, 31)),
        ]
        assert observations[-1].period_end == datetime.date(2020, 2, 29)
        # Clamped to the 28th, which is what "a year before a month-end" means.
        assert data_retrieval._trend(observations, 1) == 2.0

    def test_a_five_year_trend_off_a_leap_february(self):
        observations = [
            self.monthly("2015-02", 1.0, datetime.date(2015, 3, 31)),
            self.monthly("2020-02", 4.5, datetime.date(2020, 3, 31)),
        ]
        assert data_retrieval._trend(observations, 5) == 3.5

    def test_the_long_history_cutoff_survives_a_leap_day_anchor(self):
        """This one takes the *anchor* rather than an observation, so on
        29 February it fails for every country in the daily run at once."""
        code = next(iter(data_retrieval._LONG_HISTORY_CODES))
        entry = data_retrieval._stamp(
            [self.monthly("2024-01", 2.0, datetime.date(2024, 2, 25))],
            code, datetime.date(2024, 2, 29))
        assert entry is not None and "history" in entry
