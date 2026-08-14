"""The loaders, and the consumer contract each one has to satisfy.

Every bug this folder has had was a producer that worked. The WEO subjects
mapped onto plausible codes no ledger requests and loaded cleanly for a year;
the periods were dated and round-tripped fine and were dropped by the payload
builder. Correct row counts, no error, and not one value in any score.

So the tests that matter here assert the *reader*: a loaded row must survive
`_period_to_date` and `_resolve`, and a mapped subject must land on a registry
code that carries a ledger. Asserting the loader's own convention is exactly
what let those survive.

No network: every fixture is a real-shaped file in `tmp_path`.
"""

import datetime
import pathlib

import pytest

from backend.util import constants
from backend.utils import data_retrieval
from backend.data_fetching import curated_loader as cl
from backend.data_fetching import imf_macro_fetch as imf
from backend.data_fetching.vintage import lags, monthly, restamp, weo

HEADER = ("WEO Country Code\tISO\tWEO Subject Code\tCountry\tSubject Descriptor\t"
          "Units\tScale\t2016\t2017\t2018\t2019\t2020\tEstimates Start After")


def edition(tmp_path: pathlib.Path, name: str, rows, encoding="latin-1") -> pathlib.Path:
    path = tmp_path / name
    path.write_text("\n".join([HEADER] + rows), encoding=encoding)
    return path


# Portugal inflation: the 2017 estimate as first published, then revised.
# `Estimates Start After` is 2017 in both, which is what the real files say: an
# edition published in April 2018 does not have 2018's actuals, because 2018 has
# not happened. Checked against 2016-04 (2015), 2020-10 (2019) and 2025-04
# (2024) — every roster country, every subject, always the year before.
PT_APR2018 = ("182\tPRT\tPCPIPCH\tPortugal\tInflation, average consumer prices\t"
              "Percent change\tUnits\t1.9\t2.7\t2.3\t2.0\t1.8\t2017")
PT_OCT2018 = ("182\tPRT\tPCPIPCH\tPortugal\tInflation, average consumer prices\t"
              "Percent change\tUnits\t1.9\t2.8\t2.3\t2.2\t1.9\t2017")


class TestTheSubjectMapping:
    """Where the editions were being loaded to before anyone checked.

    Every one of the five subjects mapped onto a World Bank code that reads
    perfectly plausibly and is not in `INDICATOR_REGISTRY`. The builder resolves
    registry codes and nothing else, so all five loaded and none was ever read —
    no error, no warning, correct-looking row counts, and not one WEO number in
    any score.
    """

    def test_every_mapped_subject_lands_on_a_registry_code(self):
        for subject, code in weo.SUBJECTS.items():
            assert code in constants.INDICATOR_REGISTRY, (
                f"{subject} maps to {code}, which no ledger requests")

    def test_a_mapped_subject_reaches_a_ledger(self):
        """In the registry is not enough — it has to be evidence, not a helper."""
        for subject, code in weo.SUBJECTS.items():
            assert constants.INDICATOR_REGISTRY[code].get("ledger"), (
                f"{subject} maps to {code}, which carries no ledger")

    def test_every_weo_subject_is_mapped(self):
        # Nothing is loaded to nowhere: a subject worth parsing is a subject
        # worth reading.
        assert set(weo.SUBJECTS) == {"PCPIPCH", "NGDP_RPCH", "GGXWDG_NGDP",
                                     "GGXCNL_NGDP", "BCA_NGDPD"}

    def test_aggregate_growth_is_not_pointed_at_per_capita(self):
        """They differ by population growth, so one served as the other is a
        wrong number rather than a missing one."""
        assert weo.SUBJECTS["NGDP_RPCH"] != "NY.GDP.PCAP.KD.ZG"


class TestReadingAnEdition:
    def test_the_vintage_comes_from_the_filename(self, tmp_path):
        assert weo.edition_date(edition(tmp_path, "2018-04.xls", [PT_APR2018])) == \
            datetime.date(2018, 4, 1)

    def test_a_misnamed_or_impossible_file_has_no_vintage(self, tmp_path):
        assert weo.edition_date(tmp_path / "WEOOct2018all.xls") is None
        assert weo.edition_date(tmp_path / "2018-13.xls") is None

    def test_rows_carry_the_edition_as_their_vintage(self, tmp_path):
        rows = weo.read_edition(edition(tmp_path, "2018-04.xls", [PT_APR2018]), ["PT"])
        assert rows
        assert all(r["as_of"] == datetime.date(2018, 4, 1) for r in rows)
        assert all(r["vintage_scheme"] == "as-published-edition" for r in rows)

    def test_projections_are_not_loaded_as_observations(self, tmp_path):
        """A 2018 edition's guess at 2020 is not a fact about 2020 — and neither
        is its guess at 2018, which is the part the old rule got wrong.

        The boundary is the file's own `Estimates Start After` (2017 here), not
        the edition's year. Inferring it from the year admitted exactly one
        forecast per edition, every edition, for the life of the loader."""
        rows = weo.read_edition(edition(tmp_path, "2018-04.xls", [PT_APR2018]), ["PT"])
        years = {int(r["period"]) for r in rows}
        assert years == {2016, 2017}
        assert not years & {2018, 2019, 2020}

    def test_the_column_decides_not_the_edition_year(self, tmp_path):
        """Same edition, a country whose actuals run a year longer. The old rule
        could not express this at all: it had one boundary for the whole file,
        and the column is per country per subject."""
        ahead = ("182\tPRT\tPCPIPCH\tPortugal\tInflation, average consumer prices\t"
                 "Percent change\tUnits\t1.9\t2.7\t2.3\t2.0\t1.8\t2019")
        rows = weo.read_edition(edition(tmp_path, "2018-04.xls", [ahead]), ["PT"])
        assert {int(r["period"]) for r in rows} == {2016, 2017, 2018, 2019}

    def test_a_missing_column_falls_back_loudly(self, tmp_path, caplog):
        """Silence is how the original bug survived a year of loading."""
        bare = ("182\tPRT\tPCPIPCH\tPortugal\tInflation, average consumer prices\t"
                "Percent change\tUnits\t1.9\t2.7\t2.3\t2.0\t1.8\t")
        rows = weo.read_edition(edition(tmp_path, "2018-04.xls", [bare]), ["PT"])
        assert {int(r["period"]) for r in rows} == {2016, 2017}
        assert "Estimates Start After" in caplog.text

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

    def test_a_revision_actually_reaches_the_resolver(self, tmp_path):
        """End to end: two vintages of one year, resolved at two anchors.

        The unit tests above all pass with a period the payload builder cannot
        read. This one fails unless the row survives `_resolve`.
        """
        april = weo.read_edition(edition(tmp_path, "2018-04.xls", [PT_APR2018]), ["PT"])
        october = weo.read_edition(edition(tmp_path, "2018-10.xls", [PT_OCT2018]), ["PT"])
        observations = [
            data_retrieval._Observation(
                value=r["value"], period=r["period"], freq="A",
                period_end=data_retrieval._period_to_date(r["period"], "A"),
                as_of=r["as_of"], source="IMF WEO")
            for r in april + october if r["period"] == "2017"
        ]
        assert [o.value for o in data_retrieval._resolve(
            observations, datetime.date(2018, 6, 4))] == [2.7]
        assert [o.value for o in data_retrieval._resolve(
            observations, datetime.date(2018, 12, 1))] == [2.8]

    def test_countries_outside_the_roster_are_dropped(self, tmp_path):
        spain = ("184\tESP\tPCPIPCH\tSpain\tInflation, average consumer prices\t"
                 "Percent change\tUnits\t3.2\t3.0\t2.7\t2.2\t2.0\t2017")
        rows = weo.read_edition(
            edition(tmp_path, "2018-04.xls", [PT_APR2018, spain]), ["PT"])
        assert {r["country_iso2"] for r in rows} == {"PT"}

    def test_absent_is_absent_never_zero(self, tmp_path):
        blank = ("182\tPRT\tPCPIPCH\tPortugal\tInflation, average consumer prices\t"
                 "Percent change\tUnits\t1.9\tn/a\t--\t2.0\t1.8\t2017")
        rows = weo.read_edition(edition(tmp_path, "2018-04.xls", [blank]), ["PT"])
        assert {int(r["period"]) for r in rows} == {2016}

    def test_thousands_separators_parse(self, tmp_path):
        big = ("182\tPRT\tPCPIPCH\tPortugal\tInflation, average consumer prices\t"
               "Percent change\tUnits\t1,129.5\t125.7\t121.5\t119.0\t117.0\t2017")
        rows = weo.read_edition(edition(tmp_path, "2018-04.xls", [big]), ["PT"])
        assert next(r["value"] for r in rows if r["period"] == "2016") == 1129.5

    def test_utf16_editions_read_too(self, tmp_path):
        assert weo.read_edition(
            edition(tmp_path, "2016-10.xls", [PT_APR2018], encoding="utf-16"), ["PT"])

    def test_an_unparseable_file_is_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "2018-04.xls"
        path.write_text("not a weo table at all", encoding="latin-1")
        assert weo.read_edition(path, ["PT"]) == []

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


class TestAProjectionNeverReachesAConsumer:
    """The producer-side tests above all pass on a loader nothing reads.

    That is the shape of every bug this module has had, so this asserts the
    consumer: a forecast row must not come back out of `_resolve` at an anchor
    that would otherwise admit it.
    """

    def observation(self, year, vintage, value):
        return data_retrieval._Observation(
            value=value, period=str(year), freq="A",
            period_end=data_retrieval._period_to_date(str(year), "A"),
            as_of=vintage, source=f"IMF WEO {vintage:%Y-%m}")

    def test_the_tail_anchor_reads_the_last_actual_not_the_forecast(self, tmp_path):
        """The 6.1% case, end to end.

        With no edition later than 2018-04 on disk, an anchor in 2019 wants the
        2018 figure — and 2018 in that edition is a forecast. Loading it meant
        the payload served the IMF's April-2018 guess at 2018 as an observation,
        with nothing on the row to say so. Dropped at load, the anchor falls back
        to the 2017 actual: staleness the payload reports honestly, rather than
        freshness it made up.
        """
        rows = weo.read_edition(edition(tmp_path, "2018-04.xls", [PT_APR2018]), ["PT"])
        observations = [self.observation(int(r["period"]), r["as_of"], r["value"])
                        for r in rows]
        resolved = data_retrieval._resolve(observations, as_of=datetime.date(2019, 6, 3))
        # `_stamp` reports the last of these as the value; 2018 must not be it.
        assert "2018" not in {o.period for o in resolved}
        assert resolved[-1].period == "2017" and resolved[-1].value == 2.7

    def test_a_later_edition_supplies_the_actual_when_it_exists(self, tmp_path):
        """Why this was survivable until the archive ran out of tail: with the
        next edition present, the same anchor gets 2018 as a real actual."""
        apr2019 = ("182\tPRT\tPCPIPCH\tPortugal\tInflation, average consumer "
                   "prices\tPercent change\tUnits\t1.9\t2.7\t2.4\t2.0\t1.8\t2018")
        rows = (weo.read_edition(edition(tmp_path, "2018-04.xls", [PT_APR2018]), ["PT"])
                + weo.read_edition(edition(tmp_path, "2019-04.xls", [apr2019]), ["PT"]))
        observations = [self.observation(int(r["period"]), r["as_of"], r["value"])
                        for r in rows]
        resolved = data_retrieval._resolve(observations, as_of=datetime.date(2019, 6, 3))
        assert resolved[-1].period == "2018" and resolved[-1].value == 2.4


# ---------------------------------------------------------------------------
# Publication lags and the restamp migration
# ---------------------------------------------------------------------------

FETCHED = datetime.date(2026, 7, 28)


def stored_row(**kwargs):
    """One stored row, fetch-dated like every row written before the migration."""
    return {"country_iso2": "PT", "indicator_code": "CPI.YOY", "freq": "M",
            "period": "2018-03", "value": 1.2, "as_of": FETCHED,
            "source": "IMF CPI", "vintage_scheme": "as-published-latest",
            **kwargs}


class TestTheLagTable:
    @pytest.mark.parametrize("period,freq", [
        ("2018-02", "M"), ("2020-02", "M"), ("2018Q1", "Q"), ("2018", "A"),
    ])
    def test_a_print_never_predates_the_period_it_describes(self, period, freq):
        """Some indices really are published mid-year — RSF's 2018 press-freedom
        index came out in May 2018 — and dating them honestly would put `as_of`
        before `period_end`. The floor stays at period end: late by months for
        those three, and safe by construction for everything."""
        stamp = lags.published_on(period, freq, "CPI.YOY")
        assert stamp >= lags.period_end(period, freq)
        assert lags.within_bounds(stamp, period, freq)

    def test_a_date_outside_the_bounds_fails_the_invariant(self):
        assert not lags.within_bounds(datetime.date(2018, 1, 1), "2018-03", "M")
        assert not lags.within_bounds(datetime.date(2021, 6, 1), "2018-03", "M")
        assert not lags.within_bounds(datetime.date(2018, 6, 1), "garbage", "M")

    def test_a_market_rate_is_public_when_its_period_closes(self):
        # A monthly average of a daily rate is complete the day the month ends,
        # and there is no revision to wait for.
        assert lags.lag_days("BIS.FX.USD", "M") == 0

    def test_an_unlisted_series_falls_back_to_its_frequency(self):
        assert lags.lag_days("SOME.NEW.CODE", "A") == 365
        assert lags.lag_days("SOME.NEW.CODE", "M") == 45


class TestTheRestampPlan:
    def test_a_fetch_dated_row_is_re_dated_to_its_publication(self):
        changed, _ = restamp.plan([stored_row()])
        assert changed[0]["as_of"] == lags.published_on("2018-03", "M", "CPI.YOY")
        assert changed[0]["vintage_scheme"] == lags.SCHEME

    def test_a_weo_edition_keeps_the_date_its_publisher_gave_it(self):
        """An edition date is a fact; this module's table is an estimate."""
        _, skipped = restamp.plan([stored_row(vintage_scheme="as-published-edition")])
        assert "as-published-edition" in skipped[0]["skip_reason"]

    def test_the_fetch_date_caps_the_estimate(self):
        """The fetch date is proof the value was public by then.

        Without this the annual default pushes a 2025 figure to 2025-12-31 + 365,
        i.e. months into the future — a row claiming to have been published after
        the day it was demonstrably already in the table, which reads as negative
        staleness in the live payload.
        """
        recent = stored_row(indicator_code="SL.TLF.CACT.ZS", freq="A", period="2025")
        changed, skipped = restamp.plan([recent])
        assert not changed
        assert skipped[0]["skip_reason"] == "already dated"

    def test_an_undatable_period_is_reported_not_guessed(self):
        _, skipped = restamp.plan([stored_row(period="garbage")])
        assert skipped[0]["skip_reason"] == "unparseable period"

    def test_a_row_already_correctly_dated_is_left_alone(self):
        dated = stored_row(as_of=lags.published_on("2018-03", "M", "CPI.YOY"))
        changed, skipped = restamp.plan([dated])
        assert not changed and skipped[0]["skip_reason"] == "already dated"


class TestMonthlyRestamping:
    """A backfilled monthly print has to be dated when it landed, not today."""

    def test_a_period_ends_on_its_last_day(self):
        assert lags.period_end("2018-02", "M") == datetime.date(2018, 2, 28)
        assert lags.period_end("2020-02", "M") == datetime.date(2020, 2, 29)
        assert lags.period_end("2018Q1", "Q") == datetime.date(2018, 3, 31)
        assert lags.period_end("2018", "A") == datetime.date(2018, 12, 31)

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


# ---------------------------------------------------------------------------
# The curated file — an asymmetry, and the asymmetry is the point
# ---------------------------------------------------------------------------

CURATED_HEADER = "country_iso2,indicator_code,period,value,as_of\n"


def write_curated(tmp_path, text, name="curated.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestTheCuratedLoader:
    """An absent file is silent — it ships empty, and warning every run would
    train the operator to ignore the log. Malformed rows are loud — a row that
    exists is a row someone meant to be used, so a typo must not degrade into
    missing evidence that looks identical to the absent case. Getting that
    backwards lets a typo'd tax rate disappear from the payload while the model
    scores confidently on evidence that was supposed to be there.
    """

    def test_absent_and_header_only_are_both_silent(self, tmp_path):
        assert cl.load_curated_series(tmp_path / "nope.csv") == []
        assert cl.load_curated_series(write_curated(tmp_path, CURATED_HEADER)) == []
        assert cl.load_curated_series() == []   # the real file, header only

    def test_rows_become_indicator_series_rows(self, tmp_path):
        path = write_curated(
            tmp_path, CURATED_HEADER + "PT,STAT.TAX.TOP.RATE,2025,31.5,2026-07-01\n")
        row, = cl.load_curated_series(path)
        assert row == {
            "country_iso2": "PT", "indicator_code": "STAT.TAX.TOP.RATE",
            "freq": "A", "period": "2025", "value": 31.5,
            "as_of": datetime.date(2026, 7, 1),
            "source": "OECD Corporate Tax Statistics",
        }

    def test_freq_and_source_come_from_the_registry(self, tmp_path):
        # Neither is typed per row, so a registry edit reaches curated rows too.
        path = write_curated(tmp_path, CURATED_HEADER
                             + "PT,RESERVES.USD,2026-06,3.1e10,2026-07-01\n"
                             + "PT,WUI.INDEX,2026Q2,0.31,2026-07-01\n")
        rows = {r["indicator_code"]: r for r in cl.load_curated_series(path)}
        assert rows["RESERVES.USD"]["freq"] == "M"
        assert rows["WUI.INDEX"]["freq"] == "Q"
        assert rows["WUI.INDEX"]["source"] == "World Uncertainty Index"

    def test_blank_value_is_null_not_skipped(self, tmp_path):
        # "reported as unavailable" is a different fact from "we never asked",
        # and only the first one has a row.
        path = write_curated(
            tmp_path, CURATED_HEADER + "PT,STAT.TAX.TOP.RATE,2025,,2026-07-01\n")
        rows = cl.load_curated_series(path)
        assert len(rows) == 1 and rows[0]["value"] is None

    def test_off_roster_rows_are_skipped_not_fatal(self, tmp_path):
        # Published datasets cover 190 countries; being wider than the roster is
        # not a defect in the file.
        path = write_curated(tmp_path, CURATED_HEADER
                             + "PT,STAT.TAX.TOP.RATE,2025,31.5,2026-07-01\n"
                             + "ZZ,STAT.TAX.TOP.RATE,2025,10.0,2026-07-01\n")
        assert [r["country_iso2"] for r in cl.load_curated_series(path)] == ["PT"]

    def test_wrong_columns_raise(self, tmp_path):
        path = write_curated(tmp_path, "iso,code,year,rate\nPT,X,2025,31.5\n")
        with pytest.raises(cl.CuratedFileError, match="expected columns"):
            cl.load_curated_series(path)

    def test_unknown_indicator_code_raises(self, tmp_path):
        # A typo'd code silently accepted would land rows nothing ever reads:
        # the indicator would report absent with no signal at all.
        path = write_curated(
            tmp_path, CURATED_HEADER + "PT,STAT.TAX.TOP.RAT,2025,31.5,2026-07-01\n")
        with pytest.raises(cl.CuratedFileError, match="not in INDICATOR_REGISTRY"):
            cl.load_curated_series(path)

    def test_unparseable_value_raises_naming_the_line(self, tmp_path):
        path = write_curated(
            tmp_path, CURATED_HEADER + "PT,STAT.TAX.TOP.RATE,2025,thirty,2026-07-01\n")
        with pytest.raises(cl.CuratedFileError, match="line 2"):
            cl.load_curated_series(path)

    def test_wrong_period_format_for_the_frequency_raises(self, tmp_path):
        # An annual indicator given a month is a mistake, not a frequency change.
        path = write_curated(
            tmp_path, CURATED_HEADER + "PT,STAT.TAX.TOP.RATE,2025-06,31.5,2026-07-01\n")
        with pytest.raises(cl.CuratedFileError, match="not valid for"):
            cl.load_curated_series(path)

    def test_unparseable_as_of_raises(self, tmp_path):
        path = write_curated(
            tmp_path, CURATED_HEADER + "PT,STAT.TAX.TOP.RATE,2025,31.5,july\n")
        with pytest.raises(cl.CuratedFileError, match="as_of"):
            cl.load_curated_series(path)


class TestTheHandEditedConstants:
    """The regime vocabulary and the election dates used to be validated by a
    YAML loader. They are hand-edited dicts now, so the check lives here."""

    def test_fx_regimes_use_the_documented_vocabulary(self):
        # A typo'd regime turns suppressed_vol_flag off for that country with no
        # signal at all — metrics.suppressed_vol_flag reads it as "not managed".
        assert set(constants.FX_REGIMES) <= {c["iso2"] for c in constants.COUNTRY_ROSTER}
        assert set(constants.FX_REGIMES.values()) <= {"peg", "managed", "float"}

    def test_election_entries_are_parseable_and_sorted(self):
        for iso2, entries in constants.ELECTIONS.items():
            dates = [datetime.date.fromisoformat(e["date"]) for e in entries]
            assert dates == sorted(dates), f"{iso2}: elections must be sorted by date"
            assert all(e.get("kind") for e in entries), f"{iso2}: every entry needs a kind"


class TestImfPeriodParsing:
    """`_period_to_date` turns SDMX period strings into the end-of-period dates
    stored in `recent_indicator`. Pinned here because the style pass replaces
    the hand-rolled end-of-month arithmetic with `calendar.monthrange`."""

    @pytest.mark.parametrize("period,expected", [
        ("2026-M01", datetime.date(2026, 1, 31)),
        ("2026-M04", datetime.date(2026, 4, 30)),
        ("2026-M12", datetime.date(2026, 12, 31)),   # December special case
        ("2024-M02", datetime.date(2024, 2, 29)),    # leap year
        ("2025-M02", datetime.date(2025, 2, 28)),    # non-leap year
        ("2026-Q1", datetime.date(2026, 3, 31)),
        ("2026-Q4", datetime.date(2026, 12, 31)),    # Q4 special case
        ("2026", datetime.date(2026, 12, 31)),       # annual
    ])
    def test_valid_periods(self, period, expected):
        assert imf._period_to_date(period) == expected

    @pytest.mark.parametrize(
        "period", ["2026-M00", "2026-M13", "2026-Q0", "2026-Q5", "banana", "", None])
    def test_invalid_periods_return_none(self, period):
        assert imf._period_to_date(period) is None
