"""Characterization tests for ``backend.utils.metrics``.

Every number the friction framework hands the model comes out of this module, so
an arithmetic slip here is invisible: the payload still looks well-formed and the
model still returns a confident score built on a wrong wedge. Each case below is
hand-computed from the docstring's stated definition rather than from the code,
so the test fails if the definition drifts.

The three behaviors worth pinning hardest are the ones the module promises in its
own docstring: absent inputs yield None instead of a fabricated zero, no metric
consults any other country, and partial sums are marked rather than imputed.

No network, no database, no clock.
"""

import math

import pytest

from backend.utils import metrics


# --- helpers ---------------------------------------------------------------
def _approx(value, expected):
    """Compare a rounded metric to a hand-computed expectation."""
    assert value == pytest.approx(expected, abs=1e-6)


class TestNum:
    """The shared coercion guard every public function routes through."""

    @pytest.mark.parametrize("value", [None, True, False, "abc", "", object(), [], {}])
    def test_unusable_values_are_none(self, value):
        assert metrics._num(value) is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0, 0.0), (0.0, 0.0), (-3, -3.0), ("2.5", 2.5), (7, 7.0)],
    )
    def test_usable_values_coerce(self, value, expected):
        _approx(metrics._num(value), expected)

    def test_bool_is_not_a_number(self):
        # float(True) == 1.0, which would silently become a rate of 1.0.
        assert metrics._num(True) is None

    def test_nan_is_absence_not_a_number(self):
        assert metrics._num(float("nan")) is None

    def test_zero_survives(self):
        # 0.0 is a legitimate reading for every rate and delta in this module,
        # so the guard must test for None, never truthiness.
        assert metrics._num(0.0) == 0.0


class TestConversionLoss:
    """GE z-score and V-Dem corruption, direction-aligned then blended."""

    def test_hand_computed_midpoint(self):
        # ge_quality = (0.0 + 2.5) / 5 = 0.5 ; corruption_quality = 1 - 0.5 = 0.5
        # quality = 0.5 ; loss = 0.5
        _approx(metrics.conversion_loss(0.0, 0.5), 0.5)

    def test_best_case_is_zero_loss(self):
        # ge_quality = (2.5 + 2.5) / 5 = 1.0 ; corruption_quality = 1 - 0 = 1.0
        _approx(metrics.conversion_loss(2.5, 0.0), 0.0)

    def test_worst_case_is_total_loss(self):
        # ge_quality = 0.0 ; corruption_quality = 0.0
        _approx(metrics.conversion_loss(-2.5, 1.0), 1.0)

    def test_realistic_case(self):
        # ge = 1.2  -> (1.2 + 2.5) / 5      = 0.74
        # corr = 0.18 -> 1 - 0.18           = 0.82
        # quality = (0.74 + 0.82) / 2       = 0.78  -> loss = 0.22
        _approx(metrics.conversion_loss(1.2, 0.18), 0.22)

    def test_z_beyond_published_band_clips(self):
        # The WGI band is a construction property, not a hard bound; a z of 3.1
        # clips to quality 1.0 rather than producing a negative loss.
        _approx(metrics.conversion_loss(3.1, 0.0), 0.0)
        _approx(metrics.conversion_loss(-9.0, 1.0), 1.0)

    def test_corruption_outside_unit_interval_clips(self):
        _approx(metrics.conversion_loss(0.0, 1.4), 0.75)

    @pytest.mark.parametrize(
        ("ge", "corruption"),
        [(None, 0.5), (0.0, None), (None, None), (True, 0.5), (0.0, "x")],
    )
    def test_missing_half_is_none(self, ge, corruption):
        assert metrics.conversion_loss(ge, corruption) is None

    def test_does_not_depend_on_any_other_country(self):
        # The whole point of dropping roster normalization: the same inputs give
        # the same answer regardless of call order or surrounding data.
        first = metrics.conversion_loss(1.2, 0.18)
        metrics.conversion_loss(-2.0, 0.9)
        metrics.conversion_loss(2.4, 0.02)
        assert metrics.conversion_loss(1.2, 0.18) == first


class TestFrictionalExtraction:
    """The headline wedge: take x loss."""

    def test_hand_computed(self):
        # 34.0% of GDP taken, 22% of it lost -> 7.48% of GDP
        _approx(metrics.frictional_extraction(34.0, 0.22), 7.48)

    def test_perfect_conversion_is_no_friction(self):
        _approx(metrics.frictional_extraction(45.0, 0.0), 0.0)

    def test_zero_take_is_no_friction(self):
        _approx(metrics.frictional_extraction(0.0, 0.9), 0.0)

    @pytest.mark.parametrize(("take", "loss"), [(None, 0.2), (30.0, None), (None, None)])
    def test_missing_half_is_none(self, take, loss):
        assert metrics.frictional_extraction(take, loss) is None

    def test_composes_with_conversion_loss(self):
        loss = metrics.conversion_loss(1.2, 0.18)
        _approx(metrics.frictional_extraction(34.0, loss), 7.48)


class TestDoomLoop:
    """Trajectory pair plus the both-directions boolean."""

    def test_burden_up_quality_down_is_true(self):
        out = metrics.doom_loop(4.2, -0.31)
        assert out["burden_up_quality_down"] is True
        _approx(out["burden_5y_delta"], 4.2)
        _approx(out["conversion_quality_5y_delta"], -0.31)

    @pytest.mark.parametrize(
        ("burden", "quality"),
        [(4.2, 0.31), (-4.2, -0.31), (-4.2, 0.31)],
    )
    def test_any_other_direction_pair_is_false(self, burden, quality):
        assert metrics.doom_loop(burden, quality)["burden_up_quality_down"] is False

    def test_flat_is_false_on_both_edges(self):
        # Strict inequalities: no change is not a doom loop.
        assert metrics.doom_loop(0.0, -0.5)["burden_up_quality_down"] is False
        assert metrics.doom_loop(3.0, 0.0)["burden_up_quality_down"] is False

    @pytest.mark.parametrize(("burden", "quality"), [(None, -0.3), (4.0, None), (None, None)])
    def test_missing_half_is_none(self, burden, quality):
        assert metrics.doom_loop(burden, quality) is None


class TestRomeGap:
    """Statutory intensity vs collection, against a frozen reference."""

    def test_hand_computed_with_reference(self):
        # 52% top rate over 26% of GDP collected = ratio 2.0, reference 1.6
        out = metrics.rome_gap(52.0, 26.0, 1.6)
        _approx(out["ratio"], 2.0)
        _approx(out["reference_ratio"], 1.6)
        _approx(out["gap"], 0.4)

    def test_ratio_still_reported_without_the_reference(self):
        # The curated constant ships null; the ratio is useful before it is set.
        out = metrics.rome_gap(52.0, 26.0, None)
        _approx(out["ratio"], 2.0)
        assert out["reference_ratio"] is None
        assert out["gap"] is None

    def test_negative_gap_when_collection_beats_the_reference(self):
        out = metrics.rome_gap(30.0, 25.0, 1.6)
        _approx(out["ratio"], 1.2)
        _approx(out["gap"], -0.4)

    def test_zero_revenue_is_none_not_a_division_error(self):
        assert metrics.rome_gap(52.0, 0.0, 1.6) is None

    @pytest.mark.parametrize(("rate", "revenue"), [(None, 26.0), (52.0, None)])
    def test_missing_half_is_none(self, rate, revenue):
        assert metrics.rome_gap(rate, revenue, 1.6) is None


class TestMonetaryDilution:
    def test_hand_computed(self):
        _approx(metrics.monetary_dilution(14.5, 2.3), 12.2)

    def test_negative_when_output_outpaces_money(self):
        _approx(metrics.monetary_dilution(1.5, 4.0), -2.5)

    @pytest.mark.parametrize(("money", "output"), [(None, 2.0), (10.0, None), (None, None)])
    def test_missing_half_is_none(self, money, output):
        assert metrics.monetary_dilution(money, output) is None


class TestRealPolicyRate:
    def test_hand_computed_positive(self):
        _approx(metrics.real_policy_rate(11.25, 4.8), 6.45)

    def test_deeply_negative(self):
        _approx(metrics.real_policy_rate(8.5, 61.2), -52.7)

    def test_zero_rate_is_a_reading_not_an_absence(self):
        _approx(metrics.real_policy_rate(0.0, 2.4), -2.4)

    @pytest.mark.parametrize(("rate", "cpi"), [(None, 2.0), (5.0, None), (None, None)])
    def test_missing_half_is_none(self, rate, cpi):
        assert metrics.real_policy_rate(rate, cpi) is None


class TestPrecommittedShare:
    """Sum of the two unreallocatable lines; never imputes the missing half."""

    def test_both_halves_present(self):
        out = metrics.precommitted_share(12.4, 31.6)
        _approx(out["value"], 44.0)
        assert out["partial"] is False

    def test_missing_social_protection_returns_interest_only_marked_partial(self):
        out = metrics.precommitted_share(12.4, None)
        _approx(out["value"], 12.4)
        assert out["partial"] is True

    def test_zero_social_protection_is_a_reading_not_an_absence(self):
        out = metrics.precommitted_share(12.4, 0.0)
        _approx(out["value"], 12.4)
        assert out["partial"] is False

    def test_missing_interest_is_none(self):
        assert metrics.precommitted_share(None, 31.6) is None

    def test_never_imputes(self):
        # The partial value must equal interest exactly — no filled-in average.
        assert metrics.precommitted_share(12.4, None)["value"] == 12.4


class TestWageProductivityGap:
    def test_hand_computed(self):
        _approx(metrics.wage_productivity_gap(5.4, 1.1), 4.3)

    @pytest.mark.parametrize(("wages", "productivity"), [(None, 1.0), (5.0, None)])
    def test_missing_half_is_none(self, wages, productivity):
        assert metrics.wage_productivity_gap(wages, productivity) is None


class TestDependencyTrajectory:
    def test_level_and_delta(self):
        out = metrics.dependency_trajectory(28.4, 37.9)
        _approx(out["current"], 28.4)
        _approx(out["projected_10y"], 37.9)
        _approx(out["delta"], 9.5)

    def test_level_reported_without_the_projection(self):
        # unwpp_old_age_projection.csv ships empty.
        out = metrics.dependency_trajectory(28.4, None)
        _approx(out["current"], 28.4)
        assert out["projected_10y"] is None
        assert out["delta"] is None

    def test_missing_level_is_none(self):
        assert metrics.dependency_trajectory(None, 37.9) is None


class TestRollingVol:
    """Coverage floor is the load-bearing part: thin windows must not report."""

    def test_hand_computed_sample_stdev(self):
        # sample stdev of [2, 4, 4, 4, 5, 5, 7, 9] = sqrt(32/7) = 2.13809...
        got = metrics.rolling_vol([2, 4, 4, 4, 5, 5, 7, 9], 8)
        _approx(got, math.sqrt(32.0 / 7.0))

    def test_only_the_trailing_window_is_used(self):
        # The leading 100s are outside the window and must not affect the answer.
        series = [100, 100, 100, 2, 4, 4, 4, 5, 5, 7, 9]
        _approx(metrics.rolling_vol(series, 8), math.sqrt(32.0 / 7.0))

    def test_coverage_floor_exactly_met(self):
        # window 4, coverage floor 3.0, three present -> reports.
        assert metrics.rolling_vol([1.0, None, 3.0, 5.0], 4) is not None

    def test_below_coverage_floor_is_none(self):
        # window 4, coverage floor 3.0, two present -> "we don't know".
        assert metrics.rolling_vol([1.0, None, None, 5.0], 4) is None

    def test_short_series_is_none(self):
        # 5 observations cannot support a 36-month volatility.
        assert metrics.rolling_vol([1.0, 2.0, 3.0, 4.0, 5.0], 36) is None

    def test_constant_series_is_zero_volatility(self):
        _approx(metrics.rolling_vol([3.0] * 24, 24), 0.0)

    def test_single_usable_point_is_none(self):
        assert metrics.rolling_vol([4.0, None], 2) is None

    @pytest.mark.parametrize("series", [None, [], ()])
    def test_empty_series_is_none(self, series):
        assert metrics.rolling_vol(series, 24) is None

    @pytest.mark.parametrize("window", [0, 1, -5, None, True, 2.5, "24"])
    def test_unusable_window_is_none_not_an_exception(self, window):
        assert metrics.rolling_vol([1.0, 2.0, 3.0], window) is None

    def test_non_numeric_entries_are_gaps(self):
        assert metrics.rolling_vol(["x", "y", "z", "w"], 4) is None


class TestFxMonthlyReturns:
    """Simple returns, gaps preserved so positions stay aligned."""

    def test_hand_computed(self):
        # 100 -> 105 = +5% ; 105 -> 102.9 = -2%
        got = metrics.fx_monthly_returns([100.0, 105.0, 102.9])
        _approx(got[0], 5.0)
        _approx(got[1], -2.0)

    def test_output_is_one_shorter_than_input(self):
        assert len(metrics.fx_monthly_returns([1.0, 2.0, 3.0, 4.0])) == 3

    def test_gap_becomes_a_single_none_not_a_dead_series(self):
        # This is why simple returns were chosen over log returns.
        got = metrics.fx_monthly_returns([100.0, None, 102.0, 103.02])
        assert got[0] is None
        assert got[1] is None
        _approx(got[2], 1.0)

    def test_zero_rate_does_not_raise(self):
        got = metrics.fx_monthly_returns([0.0, 5.0, 5.5])
        assert got[0] is None
        _approx(got[1], 10.0)

    def test_negative_print_does_not_kill_the_series(self):
        got = metrics.fx_monthly_returns([-2.0, 4.0])
        assert got[0] is not None

    @pytest.mark.parametrize("series", [None, [], [1.0]])
    def test_too_short_is_none(self, series):
        assert metrics.fx_monthly_returns(series) is None

    def test_composes_with_rolling_vol(self):
        # A perfectly steady 1%/month crawl has zero return volatility.
        rates = [100.0 * (1.01 ** i) for i in range(25)]
        _approx(metrics.rolling_vol(metrics.fx_monthly_returns(rates), 24), 0.0)


class TestSuppressedVolFlag:
    """All three conditions, and None is not False."""

    def test_managed_calm_and_bleeding_reserves_is_true(self):
        assert metrics.suppressed_vol_flag("managed", 0.4, -3.2) is True

    def test_peg_is_also_a_managed_regime(self):
        assert metrics.suppressed_vol_flag("peg", 0.1, -1.0) is True

    @pytest.mark.parametrize("regime", ["PEG", "Managed", " managed "])
    def test_regime_matching_is_case_and_space_insensitive(self, regime):
        assert metrics.suppressed_vol_flag(regime, 0.4, -3.2) is True

    def test_float_has_nothing_to_suppress(self):
        assert metrics.suppressed_vol_flag("float", 0.1, -5.0) is False

    def test_calm_without_reserve_loss_is_credible_not_suppressed(self):
        assert metrics.suppressed_vol_flag("managed", 0.4, 2.1) is False

    def test_volatile_managed_rate_is_not_suppressed(self):
        assert metrics.suppressed_vol_flag("managed", 6.8, -3.2) is False

    def test_threshold_is_exclusive_at_the_boundary(self):
        at = metrics._SUPPRESSED_FX_VOL_MAX
        assert metrics.suppressed_vol_flag("managed", at, -1.0) is False
        assert metrics.suppressed_vol_flag("managed", at - 0.01, -1.0) is True

    def test_flat_reserves_are_not_a_downtrend(self):
        assert metrics.suppressed_vol_flag("managed", 0.4, 0.0) is False

    @pytest.mark.parametrize(
        ("regime", "vol", "trend"),
        [(None, 0.4, -1.0), ("managed", None, -1.0), ("managed", 0.4, None), (7, 0.4, -1.0)],
    )
    def test_missing_input_is_none_not_false(self, regime, vol, trend):
        # "no regime file" and "this is a free float" are different facts.
        assert metrics.suppressed_vol_flag(regime, vol, trend) is None

    def test_unknown_regime_string_is_false_not_none(self):
        # The file was read and said something we do not treat as managed.
        assert metrics.suppressed_vol_flag("crawling-band", 0.1, -1.0) is False


class TestForecastInstability:
    def test_mean_absolute_revision_ignores_sign(self):
        # |+1.2| + |-0.8| + |+0.4| = 2.4 over 3 vintages = 0.8
        _approx(metrics.forecast_instability([1.2, -0.8, 0.4]), 0.8)

    def test_gaps_are_dropped_from_the_mean(self):
        # Only two usable revisions: (1.0 + 3.0) / 2 = 2.0
        _approx(metrics.forecast_instability([1.0, None, -3.0]), 2.0)

    def test_no_revisions_is_zero_instability(self):
        _approx(metrics.forecast_instability([0.0, 0.0]), 0.0)

    @pytest.mark.parametrize("revisions", [None, [], [None, None], ["x"]])
    def test_nothing_usable_is_none(self, revisions):
        assert metrics.forecast_instability(revisions) is None


class TestInstrumentQuality:
    """Core pair required; supplements sharpen but cannot substitute."""

    def test_core_pair_only(self):
        out = metrics.instrument_quality(64.0, 78.0)
        _approx(out["value"], 71.0)
        assert out["components"] == 2

    def test_all_four_components(self):
        # (64 + 78 + 51 + 83) / 4 = 69.0
        out = metrics.instrument_quality(64.0, 78.0, 51.0, 83.0)
        _approx(out["value"], 69.0)
        assert out["components"] == 4

    def test_one_supplement(self):
        # (64 + 78 + 51) / 3 = 64.333...
        out = metrics.instrument_quality(64.0, 78.0, obs=51.0)
        _approx(out["value"], round(193.0 / 3.0, 4))
        assert out["components"] == 3

    def test_egdi_alone_as_the_supplement(self):
        out = metrics.instrument_quality(64.0, 78.0, egdi=83.0)
        assert out["components"] == 3

    @pytest.mark.parametrize(("spi", "press"), [(None, 78.0), (64.0, None), (None, None)])
    def test_missing_core_input_is_none(self, spi, press):
        # Supplements cannot stand in for the core pair.
        assert metrics.instrument_quality(spi, press, 51.0, 83.0) is None

    def test_zero_score_is_a_reading(self):
        out = metrics.instrument_quality(0.0, 0.0)
        _approx(out["value"], 0.0)
        assert out["components"] == 2


class TestPurity:
    """The module-level promises: no clock, no network, no state."""

    def test_repeated_calls_are_identical(self):
        args = (1.2, 0.18)
        assert len({metrics.conversion_loss(*args) for _ in range(50)}) == 1

    def test_no_io_imports(self):
        # A pure arithmetic module that grows a `requests` or `duckdb` import has
        # stopped being re-runnable over history for free.
        import inspect

        source = inspect.getsource(metrics)
        for banned in ("import requests", "import duckdb", "import psycopg2",
                       "date.today", "datetime.now", "import pandas"):
            assert banned not in source, f"metrics.py must not reference {banned}"
