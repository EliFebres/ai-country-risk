"""The publication-lag table and the migration that applies it to stored rows.

Every ``indicator_series`` row used to carry the bulk-fetch date as its
``as_of``, which meant the vintage bound discarded the entire table for any
historical anchor — a 2019 snapshot with no CPI, no exchange rate and no policy
rate, and nothing anywhere saying so. These tests pin the two properties that
keep the fix from turning into its own silent failure:

* an observation may never claim to have been public before the period it
  describes finished, nor more than two years after it;
* the World Bank parquet panel, which was already correctly dated, still
  survives the same bound — a fix for one store that broke the other would be
  invisible in exactly the same way.

No database: ``plan`` is pure over the rows it is handed.
"""

import datetime

import pytest

from backend.utils import data_retrieval
from backend.utils.history.vintage import lags, restamp

FETCHED = datetime.date(2026, 7, 28)


def row(**kwargs):
    """One stored row, fetch-dated like every row written before the migration."""
    return {"country_iso2": "PT", "indicator_code": "CPI.YOY", "freq": "M",
            "period": "2018-03", "value": 1.2, "as_of": FETCHED,
            "source": "IMF CPI", "vintage_scheme": "as-published-latest",
            **kwargs}


class TestLagTable:
    def test_a_market_rate_is_public_when_its_period_closes(self):
        # A monthly average of a daily rate is complete the day the month ends,
        # and there is no revision to wait for.
        assert lags.lag_days("BIS.FX.USD", "M") == 0
        assert lags.lag_days("BIS.POLICY.RATE", "M") == 0

    def test_an_unlisted_series_falls_back_to_its_frequency(self):
        assert lags.lag_days("SOME.NEW.CODE", "A") == 365
        assert lags.lag_days("SOME.NEW.CODE", "M") == 45

    def test_an_unknown_frequency_gets_the_conservative_annual_default(self):
        # Erring long is the whole design: a lag that is too short hands a
        # snapshot a number nobody had, and nothing downstream would show it.
        assert lags.lag_days("SOME.NEW.CODE", "?") == 365

    @pytest.mark.parametrize("period,freq", [
        ("2018-02", "M"), ("2020-02", "M"), ("2018Q1", "Q"), ("2018", "A"),
    ])
    def test_a_print_never_predates_the_period_it_describes(self, period, freq):
        """The invariant, on the lag table itself.

        Some indices really are published mid-year — RSF's 2018 press-freedom
        index came out in May 2018 — and dating them honestly would put `as_of`
        before `period_end`. The floor stays at period end: late by months for
        those three, and safe by construction for everything.
        """
        stamp = lags.published_on(period, freq, "CPI.YOY")
        assert stamp >= lags.period_end(period, freq)
        assert lags.within_bounds(stamp, period, freq)

    def test_a_date_before_the_period_ends_fails_the_invariant(self):
        assert not lags.within_bounds(datetime.date(2018, 1, 1), "2018-03", "M")

    def test_a_date_two_years_late_fails_the_invariant(self):
        assert not lags.within_bounds(datetime.date(2021, 6, 1), "2018-03", "M")

    def test_an_unparseable_period_is_never_in_bounds(self):
        assert not lags.within_bounds(datetime.date(2018, 6, 1), "garbage", "M")


class TestPlan:
    def test_a_fetch_dated_row_is_re_dated_to_its_publication(self):
        changed, _ = restamp.plan([row()])
        assert changed[0]["as_of"] == lags.published_on("2018-03", "M", "CPI.YOY")
        assert changed[0]["vintage_scheme"] == lags.SCHEME

    def test_a_weo_edition_keeps_the_date_its_publisher_gave_it(self):
        """An edition date is a fact; this module's table is an estimate."""
        _, skipped = restamp.plan([row(vintage_scheme="as-published-edition")])
        assert "as-published-edition" in skipped[0]["skip_reason"]

    def test_the_fetch_date_caps_the_estimate(self):
        """The fetch date is proof the value was public by then.

        Without this the annual default pushes a 2025 figure to 2025-12-31 + 365,
        i.e. months into the future — a row claiming to have been published after
        the day it was demonstrably already in the table, which reads as negative
        staleness in the live payload.
        """
        recent = row(indicator_code="SL.TLF.CACT.ZS", freq="A", period="2025")
        changed, skipped = restamp.plan([recent])
        # period_end + 365 lands past the fetch date, so the cap applies and the
        # row is already correctly dated — nothing to change.
        assert not changed
        assert skipped[0]["skip_reason"] == "already dated"

    def test_an_undatable_period_is_reported_not_guessed(self):
        _, skipped = restamp.plan([row(period="garbage")])
        assert skipped[0]["skip_reason"] == "unparseable period"

    def test_a_row_already_correctly_dated_is_left_alone(self):
        dated = row(as_of=lags.published_on("2018-03", "M", "CPI.YOY"))
        changed, skipped = restamp.plan([dated])
        assert not changed and skipped[0]["skip_reason"] == "already dated"


class TestSurvivesTheVintageBound:
    """The point of the whole exercise, asserted at the bound itself."""

    def test_a_fetch_dated_row_is_discarded_by_a_2019_anchor(self):
        obs = data_retrieval._Observation(
            value=1.2, period="2018-03", freq="M",
            period_end=lags.period_end("2018-03", "M"), as_of=FETCHED, source="IMF")
        assert data_retrieval._resolve([obs], as_of=datetime.date(2019, 6, 1)) == []

    def test_a_re_dated_row_survives_it(self):
        changed, _ = restamp.plan([row()])
        obs = data_retrieval._Observation(
            value=1.2, period="2018-03", freq="M",
            period_end=lags.period_end("2018-03", "M"),
            as_of=changed[0]["as_of"], source="IMF")
        assert data_retrieval._resolve([obs], as_of=datetime.date(2019, 6, 1)) == [obs]
        # …and is still refused by an anchor before it was published.
        assert data_retrieval._resolve([obs], as_of=datetime.date(2018, 4, 1)) == []

    def test_the_world_bank_panel_still_survives_the_same_bound(self):
        """The store that was already right must not be broken by fixing the other.

        ``_panel_observations`` stamps each annual value with its own year end,
        which is why the panel was the only thing a historical payload could see
        before this migration. Pinned here because nothing else would notice if
        it changed.
        """
        import pandas as pd
        panel = pd.DataFrame({"year": [2017, 2018, 2024], "gdp": [1.0, 2.0, 3.0]})
        observations = data_retrieval._panel_observations(panel, "gdp")
        assert [o.as_of for o in observations] == [
            datetime.date(2017, 12, 31), datetime.date(2018, 12, 31),
            datetime.date(2024, 12, 31)]
        kept = data_retrieval._resolve(observations, as_of=datetime.date(2019, 6, 1))
        assert [o.period for o in kept] == ["2017", "2018"]
