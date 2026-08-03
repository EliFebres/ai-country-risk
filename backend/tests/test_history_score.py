"""The runner that turns one snapshot into a decade of them.

It is deliberately thin — everything below it is the daily pipeline with `as_of`
pinned — so what is worth testing is exactly the four things it adds, each of
which is a way a long run goes quietly wrong:

* anchors that drift off the cadence, so the pilot stops being a scale model of
  the 48-country backfill;
* a resume that retries completed work or, worse, skips failed work;
* a diagnostic arm writing to `risk_snapshot` and overwriting the production
  series on its own primary key;
* a sample that picks different dates every run, so a diagnostic cannot be
  compared with its own previous run.

No database and no API: the store and the pipeline are both injected.
"""

import datetime

import pytest

from backend.utils.history import config, score

MONDAY = datetime.date(2019, 1, 7)


@pytest.fixture
def ledger(monkeypatch):
    """An in-memory stand-in for `history_run_ledger`."""
    rows = []
    monkeypatch.setattr(score.store, "write_run",
                        lambda as_of, iso2, mode, **kw: rows.append(
                            {"as_of": as_of, "country_iso2": iso2, "mode": mode, **kw}))
    monkeypatch.setattr(score.store, "total_spend_usd", lambda: 0.0)
    monkeypatch.setattr(score.store, "completed_runs", lambda mode, iso2=None: set())
    return rows


@pytest.fixture
def scored(monkeypatch):
    """Capture what `_process_country` was asked to do, without doing it."""
    calls = []

    def fake(country_name, iso2, pool, **kw):
        calls.append({"iso2": iso2, **kw})
        return {"score": 0.5}, {"schema_version": 1}

    monkeypatch.setattr(score.pipeline, "_process_country", fake)
    monkeypatch.setattr(score.snapshot_select, "select",
                        lambda iso2, as_of: [{"id": "a1", "title": "x"}])
    return calls


class TestAnchors:
    def test_every_anchor_is_a_monday(self):
        days = score.anchors(datetime.date(2019, 1, 1), datetime.date(2019, 12, 31))
        assert {d.weekday() for d in days} == {0}

    def test_a_year_is_fifty_two_weeks(self):
        assert len(score.anchors(datetime.date(2019, 1, 1),
                                 datetime.date(2019, 12, 31))) == 52

    def test_ten_years_is_the_pilot_size(self):
        """~522 per country x 5 is the 2,610 the budget was built on."""
        days = score.anchors(datetime.date.fromisoformat(config.PILOT_START),
                             datetime.date(2026, 8, 3))
        assert 515 <= len(days) <= 530


class TestTheDiagnosticArmsStayOutOfTheSeries:
    """A named row shares (country, as_of) with its masked twin. Written to
    `risk_snapshot` it would overwrite the production series, and a series that
    silently changes regime half way through its own history is worse than no
    series."""

    @pytest.mark.parametrize("mode", ["named", "masked_nostructural"])
    def test_a_diagnostic_arm_does_not_upsert(self, ledger, scored, mode):
        score.score_one("PT", MONDAY, mode)
        assert scored[0]["upsert"] is False

    def test_the_masked_arm_does_upsert(self, ledger, scored):
        score.score_one("PT", MONDAY, "masked")
        assert scored[0]["upsert"] is True

    @pytest.mark.parametrize("mode", ["named", "masked_nostructural"])
    def test_a_diagnostic_arms_output_lands_in_the_ledger(self, ledger, scored, mode):
        """It has nowhere else to live, so the ledger row is the result."""
        score.score_one("PT", MONDAY, mode)
        assert ledger[0]["result"] == {"score": 0.5}

    def test_the_masked_arms_output_does_not(self, ledger, scored):
        """It is in `risk_snapshot`, where the front end reads it. Duplicating
        it into the ledger would create a second copy to disagree with."""
        score.score_one("PT", MONDAY, "masked")
        assert ledger[0]["result"] is None

    def test_every_diagnostic_mode_is_a_known_mode(self):
        assert set(config.DIAGNOSTIC_MODES) < set(config.SCORING_MODES)


class TestFailureIsRecordedNotRaised:
    def test_a_failed_snapshot_costs_its_own_week(self, ledger, monkeypatch, scored):
        monkeypatch.setattr(score.pipeline, "_process_country",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        result = score.score_one("PT", MONDAY, "masked")
        assert result["status"] == "failed"
        assert ledger[0]["status"] == "failed" and "boom" in ledger[0]["manifest"]["error"]

    def test_a_thin_week_is_complete_and_empty_not_failed(self, ledger, monkeypatch):
        """An empty window is a real answer about that week. Recorded failed, a
        resume would retry it forever and the report would call the corpus
        broken."""
        monkeypatch.setattr(score.snapshot_select, "select", lambda iso2, as_of: [])
        result = score.score_one("PT", MONDAY, "masked")
        assert result["status"] == "complete" and result["llm_score"] is None
        assert ledger[0]["manifest"] == {"articles": 0}

    def test_budget_exhaustion_stops_the_run_rather_than_being_skipped(
            self, ledger, monkeypatch, scored):
        from backend.utils.history import usage
        monkeypatch.setattr(score.pipeline, "_process_country",
                            lambda *a, **k: (_ for _ in ()).throw(usage.BudgetExhausted("x")))
        with pytest.raises(usage.BudgetExhausted):
            score.score_one("PT", MONDAY, "masked")
        assert ledger[0]["status"] == "failed"


class TestResume:
    def test_a_completed_anchor_is_skipped(self, ledger, scored, monkeypatch):
        monkeypatch.setattr(score.store, "completed_runs",
                            lambda mode, iso2=None: {MONDAY})
        totals = score.run(roster=["PT"], start=MONDAY, end=MONDAY, mode="masked")
        assert totals == {"scored": 0, "skipped": 1, "failed": 0, "spend_usd": 0.0}
        assert not scored

    def test_an_unknown_mode_is_refused_before_anything_runs(self):
        with pytest.raises(ValueError):
            score.run(mode="masked-ish")


class TestTheDiagnosticSample:
    def series(self, n, start=datetime.date(2018, 1, 1)):
        # A calm series with two loud weeks either side of the cutoff.
        days = [start + datetime.timedelta(weeks=i) for i in range(n)]
        scores = [0.50 + (0.30 if i in (10, 40, 300, 320) else 0.0)
                  for i in range(n)]
        return list(zip(days, scores))

    def test_dates_are_stable_across_runs(self, monkeypatch):
        """A sample that redraws every run cannot be compared with itself, and
        the whole point of the arm is comparison."""
        monkeypatch.setattr(score, "_masked_series", lambda iso2: self.series(400))
        assert score.diagnostic_dates("PT") == score.diagnostic_dates("PT")

    def test_both_sides_of_the_cutoff_are_sampled(self, monkeypatch):
        """"Can the model identify this country" means something different when
        the model might simply remember the week."""
        monkeypatch.setattr(score, "_masked_series", lambda iso2: self.series(500))
        cutoff = datetime.date.fromisoformat(config.CUTOFF_DATE)
        picked = score.diagnostic_dates("PT")
        assert any(d < cutoff for d in picked) and any(d >= cutoff for d in picked)

    def test_the_loud_weeks_are_in_the_sample(self, monkeypatch):
        """The extremes are where masking either survives or does not."""
        monkeypatch.setattr(score, "_masked_series", lambda iso2: self.series(400))
        picked = set(score.diagnostic_dates("PT"))
        # Weeks 10/11 and 40/41 are the pre-cutoff jumps (each step in *and*
        # out of a spike is a large delta), so the sample must contain some of
        # them rather than one particular one — which of the four equal deltas
        # wins is a tie-break, not a property worth pinning.
        loud = {datetime.date(2018, 1, 1) + datetime.timedelta(weeks=w)
                for w in (10, 11, 40, 41)}
        assert picked & loud

    def test_a_short_series_yields_fewer_dates_rather_than_padding(self, monkeypatch):
        monkeypatch.setattr(score, "_masked_series", lambda iso2: self.series(3))
        assert len(score.diagnostic_dates("PT")) < config.NAMED_SAMPLE_PER_COUNTRY

    def test_no_series_yields_no_sample(self, monkeypatch):
        monkeypatch.setattr(score, "_masked_series", lambda iso2: [])
        assert score.diagnostic_dates("PT") == []

    def test_a_gap_in_the_series_does_not_become_a_calm_week(self, monkeypatch):
        """A None score has no delta. Treated as zero it would file as calm and
        the control group would fill with weeks that were never scored."""
        days = [datetime.date(2018, 1, 1) + datetime.timedelta(weeks=i) for i in range(40)]
        monkeypatch.setattr(score, "_masked_series",
                            lambda iso2: [(d, None if i % 2 else 0.5)
                                          for i, d in enumerate(days)])
        assert score.diagnostic_dates("PT") == []


class TestProjection:
    def test_it_falls_back_before_there_is_anything_to_measure(self, monkeypatch):
        monkeypatch.setattr(score.store, "read_runs", lambda mode=None: [])
        assert 1.5 < score.projection(52) < 2.5      # the ~$2 dry run
        assert 85 < score.projection(2610) < 105     # the ~$95 pilot

    def test_it_follows_the_observed_cost_once_there_is_one(self, monkeypatch):
        """A projection that does not move when the real cost moves is not a
        number anybody should approve a budget against."""
        monkeypatch.setattr(score.store, "read_runs",
                            lambda mode=None: [{"status": "complete", "spend_usd": 0.10}])
        assert score.projection(100) == pytest.approx(10.0)

    def test_free_and_failed_rows_do_not_drag_the_average_down(self, monkeypatch):
        """Empty weeks cost nothing and would halve the estimate for the weeks
        that do cost something."""
        monkeypatch.setattr(score.store, "read_runs", lambda mode=None: [
            {"status": "complete", "spend_usd": 0.10},
            {"status": "complete", "spend_usd": 0.0},
            {"status": "failed", "spend_usd": 0.50},
        ])
        assert score.projection(100) == pytest.approx(10.0)
