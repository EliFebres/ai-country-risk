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
        """523 per country x 4 is the 2,092 masked snapshots the budget is built
        on; the two diagnostic arms add 96 on top of it. It was x 5 and 2,610
        until BR came out at the projection."""
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

    def test_a_mask_leak_costs_its_snapshot_and_not_the_run(
            self, ledger, monkeypatch, scored):
        """`MaskLeak` is deliberately fatal to a snapshot. It must not be fatal
        to the pilot.

        A masked snapshot that names its country is mislabelled rather than
        degraded, so refusing to send it is right. Refusing to send it at anchor
        1,500 of 2,188 and taking the other 688 with it is not — and the euro
        symbol that survived the foreign pass would have raised this on any
        bundle quoting a foreign currency figure, which is not a rare bundle.
        """
        from backend.utils.masking import rewrite as mask_rewrite

        calls = []

        def leak_once(country_name, iso2, pool, **kw):
            calls.append(kw["as_of"])
            if len(calls) == 1:
                raise mask_rewrite.MaskLeak("payload still names 1 roster term(s): €")
            return {"score": 0.5}, {"schema_version": 1}

        monkeypatch.setattr(score.pipeline, "_process_country", leak_once)
        days = score.anchors(MONDAY, MONDAY + datetime.timedelta(weeks=3))
        totals = score.run(roster=["PT"], start=days[0], end=days[-1], mode="masked")

        # The run continued: every remaining anchor was attempted.
        assert len(calls) == len(days)
        assert totals["failed"] == 1 and totals["scored"] == len(days) - 1

    def test_a_leaked_snapshot_is_left_retryable_for_the_resume(
            self, ledger, monkeypatch, scored):
        """`completed_runs` counts only 'complete', so a failed row is retried
        rather than silently skipped — which is the difference between a resume
        that heals the gap and one that bakes it in."""
        from backend.utils.masking import rewrite as mask_rewrite

        monkeypatch.setattr(score.pipeline, "_process_country",
                            lambda *a, **k: (_ for _ in ()).throw(
                                mask_rewrite.MaskLeak("names a roster term")))
        score.score_one("PT", MONDAY, "masked")

        row = ledger[-1]
        assert row["status"] == "failed"
        assert "roster term" in row["manifest"]["error"]
        # The ledger's own resume rule, stated as the assertion it protects.
        assert row["status"] != "complete"

    def test_the_governor_fires_on_a_snapshot_that_overruns(
            self, ledger, monkeypatch, scored):
        """`_confirm_spend` checks a projection before the run; this checks the
        meter after each snapshot. Without it a run costing three times its
        projection spends past `PILOT_BUDGET_USD` with nothing to stop it —
        `Meter` deliberately never raises from its own callback."""
        from backend.utils.history import usage
        monkeypatch.setattr(score.store, "total_spend_usd",
                            lambda: config.PILOT_BUDGET_USD + 1.0)
        with pytest.raises(usage.BudgetExhausted):
            score.score_one("PT", MONDAY, "masked")

    def test_the_overrunning_snapshot_is_still_banked_before_the_stop(
            self, ledger, monkeypatch, scored):
        """The snapshot is paid for either way. A budget stop that also loses
        the work it just bought is the worst of both, and a resume would then
        buy it a second time."""
        from backend.utils.history import usage
        monkeypatch.setattr(score.store, "total_spend_usd",
                            lambda: config.PILOT_BUDGET_USD + 1.0)
        with pytest.raises(usage.BudgetExhausted):
            score.score_one("PT", MONDAY, "masked")
        assert ledger[-1]["status"] == "complete"

    def test_the_run_stops_rather_than_propagating(self, ledger, monkeypatch, scored):
        """`run` owns the stop: one country's budget stop ends the run cleanly
        with its totals, rather than a traceback out of a multi-hour pilot."""
        monkeypatch.setattr(score.store, "total_spend_usd",
                            lambda: config.PILOT_BUDGET_USD + 1.0)
        totals = score.run(roster=["PT"], start=MONDAY, end=MONDAY, mode="masked")
        assert totals["failed"] == 1 and totals["scored"] == 0


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
        monkeypatch.setattr(score, "_masked_series", lambda iso2, since=None, until=None: self.series(400))
        assert score.diagnostic_dates("PT") == score.diagnostic_dates("PT")

    def test_both_sides_of_the_cutoff_are_sampled(self, monkeypatch):
        """"Can the model identify this country" means something different when
        the model might simply remember the week."""
        monkeypatch.setattr(score, "_masked_series", lambda iso2, since=None, until=None: self.series(500))
        cutoff = datetime.date.fromisoformat(config.CUTOFF_DATE)
        picked = score.diagnostic_dates("PT")
        assert any(d < cutoff for d in picked) and any(d >= cutoff for d in picked)

    def test_the_loud_weeks_are_in_the_sample(self, monkeypatch):
        """The extremes are where masking either survives or does not."""
        monkeypatch.setattr(score, "_masked_series", lambda iso2, since=None, until=None: self.series(400))
        picked = set(score.diagnostic_dates("PT"))
        # Weeks 10/11 and 40/41 are the pre-cutoff jumps (each step in *and*
        # out of a spike is a large delta), so the sample must contain some of
        # them rather than one particular one — which of the four equal deltas
        # wins is a tie-break, not a property worth pinning.
        loud = {datetime.date(2018, 1, 1) + datetime.timedelta(weeks=w)
                for w in (10, 11, 40, 41)}
        assert picked & loud

    def test_a_short_series_yields_fewer_dates_rather_than_padding(self, monkeypatch):
        monkeypatch.setattr(score, "_masked_series", lambda iso2, since=None, until=None: self.series(3))
        assert len(score.diagnostic_dates("PT")) < config.NAMED_SAMPLE_PER_COUNTRY

    def test_no_series_yields_no_sample(self, monkeypatch):
        monkeypatch.setattr(score, "_masked_series", lambda iso2, since=None, until=None: [])
        assert score.diagnostic_dates("PT") == []

    def test_a_gap_in_the_series_does_not_become_a_calm_week(self, monkeypatch):
        """A None score has no delta. Treated as zero it would file as calm and
        the control group would fill with weeks that were never scored."""
        days = [datetime.date(2018, 1, 1) + datetime.timedelta(weeks=i) for i in range(40)]
        monkeypatch.setattr(score, "_masked_series",
                            lambda iso2, since=None, until=None:
                            [(d, None if i % 2 else 0.5)
                             for i, d in enumerate(days)])
        assert score.diagnostic_dates("PT") == []

    def test_a_series_on_one_side_of_the_cutoff_yields_only_that_side(self, monkeypatch):
        """The gate-2 dry run scores a single pre-cutoff year, so the post half
        is empty and the sample is half size. That is the honest answer.

        What must not happen is the sampler quietly making up the difference
        from the side it does have: the two halves are a stratification, and a
        sample that rebalances is reporting twelve dates while measuring one
        era. At pilot scale every country spans both halves, so this would never
        fire in anger — which is exactly why it would be hard to notice."""
        monkeypatch.setattr(score, "_masked_series", lambda iso2, since=None, until=None: self.series(52))
        cutoff = datetime.date.fromisoformat(config.CUTOFF_DATE)
        picked = score.diagnostic_dates("PT")

        assert picked, "a pre-cutoff-only series should still yield a sample"
        assert all(d < cutoff for d in picked)
        # Half the per-country number, not all of it borrowed from one era.
        assert len(picked) <= config.NAMED_SAMPLE_PER_COUNTRY // 2

    def test_the_post_cutoff_half_alone_behaves_the_same(self, monkeypatch):
        monkeypatch.setattr(score, "_masked_series",
                            lambda iso2, since=None, until=None: self.series(52, datetime.date(2024, 1, 1)))
        cutoff = datetime.date.fromisoformat(config.CUTOFF_DATE)
        picked = score.diagnostic_dates("PT")
        assert picked and all(d >= cutoff for d in picked)
        assert len(picked) <= config.NAMED_SAMPLE_PER_COUNTRY // 2

    def test_the_window_reaches_the_query(self, monkeypatch):
        """One stray snapshot from another year is enough to break the halves.

        PT's dry run scored 2019 and the store also held a single 2026 masked
        row from an earlier session. Unfiltered, that row sat alone on the far
        side of the cutoff and became the whole post-cutoff era, so a correctly
        half-size six-date sample came back with seven — one of them a date the
        run had never scored, whose |Δ| was measured across a seven-year gap and
        called a week's movement.
        """
        seen = {}

        def _series(iso2, since=None, until=None):
            seen.update(since=since, until=until)
            return self.series(52)

        monkeypatch.setattr(score, "_masked_series", _series)
        since, until = datetime.date(2019, 1, 1), datetime.date(2019, 12, 31)
        score.diagnostic_plan(["PT"], since=since, until=until)
        assert seen == {"since": since, "until": until}

    def test_an_unbounded_plan_is_still_the_default(self, monkeypatch):
        """The pilot wants the whole series; only a one-year run wants a window."""
        seen = {}

        def _series(iso2, since=None, until=None):
            seen.update(since=since, until=until)
            return self.series(52)

        monkeypatch.setattr(score, "_masked_series", _series)
        score.diagnostic_plan(["PT"])
        assert seen == {"since": None, "until": None}


class TestProjection:
    def test_it_refuses_before_there_is_anything_to_measure(self, monkeypatch):
        """It used to return `n * 0.036`.

        That constant was measured before the selector fix moved the median
        snapshot from 6.5 articles to twenty, so it was low by about a third by
        the time anyone read it — and it came back as a float indistinguishable
        from a measured one, into the line of `_confirm_spend` that asks
        somebody to approve a spend. A projection with nothing to project from
        is not a small number, it is not a number.
        """
        monkeypatch.setattr(score.store, "read_runs", lambda mode=None: [])
        with pytest.raises(score.NoObservedCost):
            score.projection(52)

    def test_the_refusal_says_how_to_get_a_real_one(self, monkeypatch):
        monkeypatch.setattr(score.store, "read_runs", lambda mode=None: [])
        with pytest.raises(score.NoObservedCost, match="gate-2"):
            score.projection(2188)

    def test_it_prices_one_arm_when_asked(self, monkeypatch):
        """The arms are not interchangeable — the diagnostic ones reuse their
        masked twin's digests and cost visibly less — so a masked projection off
        a mixed ledger is a blend of two different things."""
        seen = {}

        def read_runs(mode=None):
            seen["mode"] = mode
            return [{"status": "complete", "spend_usd": 0.05}]

        monkeypatch.setattr(score.store, "read_runs", read_runs)
        assert score.projection(100, mode="masked") == pytest.approx(5.0)
        assert seen["mode"] == "masked"

    def test_an_arm_with_no_rows_refuses_rather_than_borrowing_another(self, monkeypatch):
        monkeypatch.setattr(score.store, "read_runs", lambda mode=None: [])
        with pytest.raises(score.NoObservedCost, match="named"):
            score.projection(12, mode="named")

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
