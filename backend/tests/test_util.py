"""The payload the model reads, the arithmetic behind it, and the tripwires.

A defect in `build_evidence_payload` is invisible in exactly the way that
matters: the JSON still looks well-formed, the model still returns a confident
score, and nobody can tell it was reasoning about a two-year-old inflation
number. So the load-bearing test here is the loader-to-payload contract — a
written row must be a read row — whose absence let the WEO loader run inert for
its whole life: nineteen editions parsed, sixteen thousand rows written,
correct-looking counts in every log, and no value in any score.

Lint is the other half of observe-only. It must fire on the contradictions
enforcement used to overwrite, stay quiet otherwise, and never return a score.

No network, no database, no clock: every store is passed in.
"""

import datetime as _dt
import inspect
import math
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from backend.main import _every, _weekly
from backend.llm import payload as dr
from backend.util import market_hours
from backend.utils import lint, metrics

AS_OF = _dt.date(2026, 7, 27)


def panel(**columns) -> pd.DataFrame:
    """A parquet-shaped panel: a `year` column plus one column per indicator."""
    years = columns.pop("years", [2022, 2023, 2024, 2025])
    return pd.DataFrame({"year": years, **columns})


def series_rows(code, values, *, freq="M", start="2026-01", as_of=AS_OF,
                source="IMF CPI"):
    """`indicator_series` rows for one indicator, consecutive periods."""
    rows = []
    if freq == "M":
        year, month = (int(p) for p in start.split("-"))
        for value in values:
            rows.append({"period": f"{year:04d}-{month:02d}", "freq": "M",
                         "value": value, "as_of": as_of, "source": source})
            month += 1
            if month > 12:
                month, year = 1, year + 1
    else:
        for offset, value in enumerate(values):
            rows.append({"period": str(int(start) + offset), "freq": freq,
                         "value": value, "as_of": as_of, "source": source})
    return {code: rows}


def build(**kwargs):
    """Build a payload for PT with sensible empty defaults."""
    kwargs.setdefault("panel", pd.DataFrame())
    return dr.build_evidence_payload("PT", as_of=AS_OF, **kwargs)


# ---------------------------------------------------------------------------
# A written row must be a read row
# ---------------------------------------------------------------------------

class TestTheLoaderToPayloadContract:
    """The assertion whose absence let the WEO loader run inert for its whole life.

    A missing indicator is omitted from the payload rather than nulled, so a
    broken loader and a country with no data are byte-identical. This closes
    that: for every registry indicator, a stored row must arrive.
    """

    def test_every_registry_indicator_can_reach_the_payload(self):
        from backend.util import constants
        missing = []
        for code, spec in constants.INDICATOR_REGISTRY.items():
            freq = str(spec["freq"])
            if freq == "M":
                rows = series_rows(code, [1.5, 2.5, 3.5], freq="M", start="2026-04")
            elif freq == "Q":
                rows = {code: [{"period": f"2025Q{q}", "freq": "Q", "value": 1.0 + q,
                                "as_of": AS_OF, "source": str(spec["source"])}
                               for q in (1, 2, 3)]}
            else:
                rows = series_rows(code, [1.5, 2.5, 3.5], freq=freq, start="2023")
            payload = build(series=rows)
            labels = set()
            for block in ("friction_inputs", "uncertainty_inputs",
                          "information_inputs", "edge_inputs"):
                labels |= set((payload.get(block) or {}).keys())
            if str(spec["label"]) not in labels:
                missing.append(code)
        assert not missing, (
            f"{len(missing)} indicator(s) accept a stored row and never appear in "
            f"a payload: {missing}")

    def test_a_dated_period_is_rejected_rather_than_silently_dropped(self):
        """The exact WEO defect: an annual period written as a date.

        `_period_to_date` returns None for "2023-12-31" at freq A, and the row
        vanishes. Pinned so the shape is at least visible in a test rather than
        only in a payload nobody diffed.
        """
        good = build(series={"CPI.YOY": [{"period": "2023", "freq": "A", "value": 1.0,
                                          "as_of": AS_OF, "source": "IMF WEO 2024-04"}]})
        bad = build(series={"CPI.YOY": [{"period": "2023-12-31", "freq": "A",
                                         "value": 1.0, "as_of": AS_OF,
                                         "source": "IMF WEO 2024-04"}]})
        assert any("Inflation" in k for k in (good.get("uncertainty_inputs") or {}))
        assert not any("Inflation" in k for k in (bad.get("uncertainty_inputs") or {}))

    def test_the_weo_block_arrives_with_its_edition_as_the_source(self):
        from backend.util import constants
        codes = ("WEO.NGDP_RPCH", "WEO.GGXWDG_NGDP", "WEO.GGXCNL_NGDP",
                 "WEO.BCA_NGDPD")
        rows = {}
        for code in codes:
            rows.update(series_rows(code, [1.0, 2.0], freq="A", start="2023",
                                    source="IMF WEO 2025-04"))
        payload = build(series=rows)
        seen = {}
        for block in ("friction_inputs", "uncertainty_inputs"):
            seen.update(payload.get(block) or {})
        for code in codes:
            label = str(constants.INDICATOR_REGISTRY[code]["label"])
            assert label in seen, f"{code} did not reach the payload"
            assert seen[label]["source"] == "IMF WEO 2025-04"


class TestFreshestValueWins:
    """An indicator can live in the annual panel, the monthly latest-print table
    and the series store at once. Pick wrong and the model scores a crisis on
    last year's annual average."""

    def test_monthly_series_beats_the_annual_panel(self):
        # The panel says 2.34 for 2025, the monthly series says 3.8 for 2026-06.
        payload = build(
            panel=panel(INFLATION=[7.8, 4.3, 2.4, 2.34]),
            series=series_rows("CPI.YOY", [3.3, 3.5, 3.8], start="2026-04"),
        )
        entry = payload["uncertainty_inputs"]["Inflation (% y/y)"]
        assert entry["value"] == 3.8
        assert entry["period"] == "2026-06" and entry["freq"] == "M"

    def test_recent_indicator_also_competes(self):
        payload = build(
            panel=panel(INFLATION=[7.8, 4.3, 2.4, 2.34]),
            recent={"Inflation (% y/y)": {
                "value": 3.9, "period": _dt.date(2026, 6, 30), "freq": "M",
                "unit": "% y/y", "source": "IMF",
            }},
        )
        assert payload["uncertainty_inputs"]["Inflation (% y/y)"]["value"] == 3.9

    def test_same_period_resolves_to_the_newer_as_of(self):
        # Two stores hold the same period; the one we learned more recently wins.
        stale = {"period": "2026-06", "freq": "M", "value": 3.0,
                 "as_of": _dt.date(2026, 7, 1), "source": "stale"}
        fresh = {"period": "2026-06", "freq": "M", "value": 3.8,
                 "as_of": _dt.date(2026, 7, 20), "source": "fresh"}
        entry = build(series={"CPI.YOY": [stale, fresh]})[
            "uncertainty_inputs"]["Inflation (% y/y)"]
        assert entry["value"] == 3.8 and entry["source"] == "fresh"

    def test_an_older_monthly_print_does_not_beat_a_newer_annual(self):
        # Resolution is by the period covered, not by frequency rank.
        payload = build(
            panel=panel(INFLATION=[1.0, 2.0, 3.0, 4.0],
                        years=[2023, 2024, 2025, 2026]),
            series=series_rows("CPI.YOY", [9.9], start="2024-01"),
        )
        assert payload["uncertainty_inputs"]["Inflation (% y/y)"]["value"] == 4.0

    def test_the_payload_has_every_ledger_section_and_is_stamped(self):
        payload = build()
        for key in ("_meta", "friction_inputs", "uncertainty_inputs",
                    "information_inputs", "edge_inputs", "computed"):
            assert key in payload
        meta = payload["_meta"]
        assert meta["country"] == "PT" and meta["as_of"] == "2026-07-27"
        assert meta["vintage_scheme"] == "as-published-latest"

    def test_indicators_land_in_their_declared_ledger(self):
        payload = build(
            panel=panel(INFLATION=[1.0, 2.0, 3.0, 4.0]),
            series={**series_rows("IQ.SPI.OVRL", [64.0], freq="A", start="2024",
                                  source="World Bank SPI"),
                    **series_rows("IC.BUS.NDNS.ZS", [5.1], freq="A", start="2024",
                                  source="World Bank WDI")},
        )
        assert "Inflation (% y/y)" in payload["uncertainty_inputs"]
        assert "Statistical performance (0–100)" in payload["information_inputs"]
        assert "New business density (per 1,000 working-age)" in payload["edge_inputs"]


# ---------------------------------------------------------------------------
# The friction arithmetic — absent is None, never a fabricated zero
# ---------------------------------------------------------------------------

class TestTheCoercionGuard:
    """Every public metric routes through `_num`. An arithmetic slip here is
    invisible: the payload still looks well-formed and the model still returns a
    confident score built on a wrong wedge."""

    @pytest.mark.parametrize("value", [None, True, False, "abc", "", object(), [], {}])
    def test_unusable_values_are_none(self, value):
        assert metrics._num(value) is None

    def test_bool_is_not_a_number(self):
        # float(True) == 1.0, which would silently become a rate of 1.0.
        assert metrics._num(True) is None

    def test_nan_is_absence_not_a_number(self):
        assert metrics._num(float("nan")) is None

    def test_zero_survives(self):
        # 0.0 is a legitimate reading for every rate and delta in this module,
        # so the guard must test for None, never truthiness.
        assert metrics._num(0.0) == 0.0


class TestTheWedge:
    """Hand-computed from the docstring's stated definition rather than from the
    code, so the test fails if the definition drifts."""

    def test_conversion_loss_is_the_blend_of_two_qualities(self):
        # ge_quality = (1.2 + 2.5) / 5 = 0.74 ; corruption_quality = 1 - 0.18 = 0.82
        # quality = 0.78 -> loss = 0.22
        assert metrics.conversion_loss(1.2, 0.18) == pytest.approx(0.22, abs=1e-6)

    def test_a_z_beyond_the_published_band_clips(self):
        # The WGI band is a construction property, not a hard bound; a z of 3.1
        # clips to quality 1.0 rather than producing a negative loss.
        assert metrics.conversion_loss(3.1, 0.0) == pytest.approx(0.0, abs=1e-6)
        assert metrics.conversion_loss(-9.0, 1.0) == pytest.approx(1.0, abs=1e-6)

    @pytest.mark.parametrize(("ge", "corruption"),
                             [(None, 0.5), (0.0, None), (True, 0.5), (0.0, "x")])
    def test_a_missing_half_is_none_not_zero(self, ge, corruption):
        assert metrics.conversion_loss(ge, corruption) is None

    def test_frictional_extraction_is_take_times_loss(self):
        # 34.0% of GDP taken, 22% of it lost -> 7.48% of GDP
        assert metrics.frictional_extraction(34.0, 0.22) == pytest.approx(7.48, abs=1e-6)
        assert metrics.frictional_extraction(45.0, 0.0) == pytest.approx(0.0, abs=1e-6)

    def test_frictional_extraction_composes_with_conversion_loss(self):
        # The two halves of the wedge, end to end: a 1.2 GE z-score and 0.18
        # corruption give a 0.22 loss, and 34% of GDP through it is 7.48%.
        loss = metrics.conversion_loss(1.2, 0.18)
        assert metrics.frictional_extraction(34.0, loss) == pytest.approx(7.48, abs=1e-6)

    def test_zero_take_is_no_friction(self):
        assert metrics.frictional_extraction(0.0, 0.9) == pytest.approx(0.0, abs=1e-6)

    @pytest.mark.parametrize(("take", "loss"),
                             [(None, 0.2), (30.0, None), (None, None)])
    def test_frictional_extraction_missing_half_is_none(self, take, loss):
        assert metrics.frictional_extraction(take, loss) is None

    def test_no_metric_consults_any_other_country(self):
        # The whole point of dropping roster normalization: the same inputs give
        # the same answer regardless of call order or surrounding data.
        first = metrics.conversion_loss(1.2, 0.18)
        metrics.conversion_loss(-2.0, 0.9)
        metrics.conversion_loss(2.4, 0.02)
        assert metrics.conversion_loss(1.2, 0.18) == first


class TestMonetaryDilution:
    def test_hand_computed(self):
        assert metrics.monetary_dilution(14.5, 2.3) == pytest.approx(12.2, abs=1e-6)

    def test_negative_when_output_outpaces_money(self):
        assert metrics.monetary_dilution(1.5, 4.0) == pytest.approx(-2.5, abs=1e-6)

    @pytest.mark.parametrize(("money", "output"),
                             [(None, 2.0), (10.0, None), (None, None)])
    def test_missing_half_is_none(self, money, output):
        assert metrics.monetary_dilution(money, output) is None


class TestRealPolicyRate:
    def test_hand_computed_positive(self):
        assert metrics.real_policy_rate(11.25, 4.8) == pytest.approx(6.45, abs=1e-6)

    def test_deeply_negative(self):
        assert metrics.real_policy_rate(8.5, 61.2) == pytest.approx(-52.7, abs=1e-6)

    def test_zero_rate_is_a_reading_not_an_absence(self):
        assert metrics.real_policy_rate(0.0, 2.4) == pytest.approx(-2.4, abs=1e-6)

    @pytest.mark.parametrize(("rate", "cpi"),
                             [(None, 2.0), (5.0, None), (None, None)])
    def test_missing_half_is_none(self, rate, cpi):
        assert metrics.real_policy_rate(rate, cpi) is None


class TestRollingVol:
    """Coverage floor is the load-bearing part: thin windows must not report."""

    def test_hand_computed_sample_stdev(self):
        # sample stdev of [2, 4, 4, 4, 5, 5, 7, 9] = sqrt(32/7) = 2.13809...
        assert metrics.rolling_vol([2, 4, 4, 4, 5, 5, 7, 9], 8) == \
            pytest.approx(math.sqrt(32.0 / 7.0), abs=1e-6)

    def test_only_the_trailing_window_is_used(self):
        # The leading 100s are outside the window and must not affect the answer.
        series = [100, 100, 100, 2, 4, 4, 4, 5, 5, 7, 9]
        assert metrics.rolling_vol(series, 8) == \
            pytest.approx(math.sqrt(32.0 / 7.0), abs=1e-6)

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
        assert metrics.rolling_vol([3.0] * 24, 24) == pytest.approx(0.0, abs=1e-6)

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


# ---------------------------------------------------------------------------
# Lint: it must fire, it must stay quiet, and it must never return a score
# ---------------------------------------------------------------------------

LINT_AS_OF = date(2026, 7, 27)


def flags(**overrides) -> dict:
    """The condition_flags object as the schema defines it, all-clear."""
    base = {"war_on_territory": False, "internal_conflict_level": "none",
            "emergency_rule": False, "sovereign_stress": False}
    base.update(overrides)
    return base


def check(**overrides):
    """Run lint for PT with quiet defaults."""
    kwargs = {
        "country_iso2": "PT",
        "as_of": LINT_AS_OF,
        "condition_flags": flags(),
        "score_3m": 60,
        "score_12m": 75,
        "ledger_scores": {"friction": 40, "order_uncertainty": 45,
                          "information_capacity": 70, "edge_vitality": 55},
        "suppressed_vol_flag": None,
        "non_investable": False,
    }
    kwargs.update(overrides)
    return lint.check(**kwargs)


def rules(findings):
    return [f["rule"] for f in findings]


class TestLintStaysQuiet:
    """A rule that fires on every country trains the operator to ignore the log,
    at which point the real findings are invisible too."""

    def test_a_clean_country_produces_nothing(self):
        assert check() == []

    def test_all_none_scores_produce_nothing(self):
        # A failed model call has no scores to contradict its flags.
        assert check(score_3m=None, score_12m=None, ledger_scores={}) == []

    def test_malformed_flags_are_inert(self):
        assert check(condition_flags=None) == []
        assert check(condition_flags="not a dict") == []
        assert check(condition_flags={"war_on_territory": "yes"}) == []

    def test_quiet_at_and_above_the_tripwire(self):
        at = lint.WAR_SCORE_FLOOR
        assert check(condition_flags=flags(war_on_territory=True), score_12m=at) == []
        assert check(score_12m=10) == []   # no flag, no finding
        assert check(condition_flags=flags(war_on_territory=True),
                     score_12m=None) == []

    def test_a_null_suppressed_vol_flag_is_not_a_true(self):
        # None means an input was missing, not that calm was detected.
        assert check(suppressed_vol_flag=None,
                     ledger_scores={"order_uncertainty": 22}) == []


class TestLintFires:
    """A tripwire that never trips is worse than no tripwire, because it reads
    like evidence that nothing is wrong."""

    def test_war_beside_a_low_score_fires_on_the_twelve_month_horizon(self):
        findings = check(condition_flags=flags(war_on_territory=True), score_12m=44)
        assert rules(findings) == ["flag_score_divergence"]
        divergence, = findings[0]["detail"]["divergences"]
        assert divergence["flag"] == "war_on_territory"
        assert divergence["observed_score"] == 44 and divergence["horizon"] == "12m"
        assert divergence["tripwire"] == lint.WAR_SCORE_FLOOR

    def test_sovereign_stress_reads_the_three_month_score(self):
        # A high 12m with a low 3m is exactly the case this catches.
        findings = check(condition_flags=flags(sovereign_stress=True),
                         score_3m=30, score_12m=95)
        divergence, = findings[0]["detail"]["divergences"]
        assert divergence["flag"] == "sovereign_stress" and divergence["horizon"] == "3m"

    def test_suppressed_calm_meeting_low_order_uncertainty_fires(self):
        findings = check(suppressed_vol_flag=True,
                         ledger_scores={"order_uncertainty": 22})
        assert rules(findings) == ["calm_taken_at_face_value"]

    def test_rules_are_unique_within_one_call(self):
        # risk_lint is keyed (country_iso2, as_of, rule). Two findings sharing a
        # rule in one call make Postgres reject the whole INSERT with "ON
        # CONFLICT DO UPDATE command cannot affect row a second time" — which
        # would turn the non-blocking lint pass into a failing one.
        findings = check(
            condition_flags=flags(war_on_territory=True, sovereign_stress=True),
            score_12m=1, score_3m=1,
            suppressed_vol_flag=True, ledger_scores={"order_uncertainty": 1},
            non_investable=True,
        )
        keys = [(f["country_iso2"], f["as_of"], f["rule"]) for f in findings]
        assert len(keys) == len(set(keys)), f"duplicate lint keys: {keys}"
        # One divergence row carrying both contradictions, not two rows.
        assert [d["flag"] for d in findings[0]["detail"]["divergences"]] == [
            "war_on_territory", "sovereign_stress"]

    def test_the_badge_is_bookkeeping_not_a_contradiction(self, caplog):
        with caplog.at_level("INFO"):
            lint.log_findings(check(condition_flags=flags(war_on_territory=True),
                                    score_12m=44))
            lint.log_findings(check(non_investable=True))

        def levels_for(rule):
            return {r.levelname for r in caplog.records if rule in r.getMessage()}

        assert levels_for("flag_score_divergence") == {"WARNING"}
        assert levels_for("non_investable") == {"INFO"}


class TestLintCannotChangeAScore:
    def test_returns_findings_not_scores(self):
        for finding in check(condition_flags=flags(war_on_territory=True),
                             score_12m=44):
            assert set(finding) == {"country_iso2", "as_of", "rule", "detail"}

    def test_the_module_has_no_mutator(self):
        source = inspect.getsource(lint)
        assert "def apply" not in source
        # The tripwire constants are compared against, never assigned from.
        for constant in ("WAR_SCORE_FLOOR", "SOVEREIGN_STRESS_SCORE_FLOOR",
                         "SUPPRESSED_CALM_UNCERTAINTY_FLOOR"):
            assert f"= {constant}" not in source

    def test_it_is_pure(self):
        source = inspect.getsource(lint)
        for banned in ("date.today", "datetime.now", "requests", "psycopg2"):
            assert banned not in source

    def test_thresholds_are_documented_as_advisory(self):
        assert "advisory" in lint.__doc__.lower()


# ---------------------------------------------------------------------------
# Market gating and the scheduler
# ---------------------------------------------------------------------------

def utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


class TestMarketHours:
    """The daemon's API spend and the freshness of the Prices pane both depend
    on `is_open`. The hand-rolled DST arithmetic is pinned including its quirk,
    because the style pass replaces it with `zoneinfo`."""

    def test_the_offset_is_chosen_by_the_utc_date_not_the_wall_clock(self):
        # Quirk pinned deliberately. At 2026-03-08 03:00 UTC the real US/Eastern
        # wall clock is still EST (22:00 Mar 7), but the UTC date is already a
        # DST date, so the code applies -4 and returns 23:00. Any replacement
        # must reproduce this or prove the difference is unobservable via
        # `is_open`.
        assert market_hours.eastern_now(utc(2026, 3, 8, 3, 0)) == \
            datetime(2026, 3, 7, 23, 0)
        assert market_hours.eastern_now(utc(2026, 11, 1, 4, 0)) == \
            datetime(2026, 10, 31, 23, 0)

    def test_the_session_boundaries_are_half_open(self):
        # 2026-07-01 is a Wednesday. EDT = UTC-4, so 09:30 ET = 13:30 UTC.
        assert market_hours.is_open("stocks", utc(2026, 7, 1, 13, 30)) is True
        assert market_hours.is_open("stocks", utc(2026, 7, 1, 13, 29)) is False
        assert market_hours.is_open("stocks", utc(2026, 7, 1, 19, 59)) is True
        assert market_hours.is_open("stocks", utc(2026, 7, 1, 20, 0)) is False

    def test_winter_sessions_use_est(self):
        # 2026-01-14 is a Wednesday; EST = UTC-5, so 09:30 ET = 14:30 UTC.
        assert market_hours.is_open("stocks", utc(2026, 1, 14, 14, 30)) is True
        assert market_hours.is_open("stocks", utc(2026, 1, 14, 14, 29)) is False

    def test_weekends_are_closed_for_stocks(self):
        assert market_hours.is_open("stocks", utc(2026, 7, 4, 15, 0)) is False
        assert market_hours.is_open("stocks", utc(2026, 7, 5, 15, 0)) is False

    @pytest.mark.parametrize("cls", ["crypto", "bonds"])
    def test_crypto_and_bonds_are_always_open(self, cls):
        assert market_hours.is_open(cls, utc(2026, 7, 4, 3, 0)) is True

    def test_an_unknown_class_is_not_gated(self):
        assert market_hours.is_open("forex", utc(2026, 7, 4, 3, 0)) is True

    def test_commodities_respect_the_globex_break(self):
        # 17:00-18:00 ET; in July (EDT) that is 21:00-22:00 UTC.
        assert market_hours.is_open("commodities", utc(2026, 7, 1, 21, 0)) is False
        assert market_hours.is_open("commodities", utc(2026, 7, 1, 22, 0)) is True
        # Sunday opens at the evening restart.
        assert market_hours.is_open("commodities", utc(2026, 7, 5, 21, 59)) is False
        assert market_hours.is_open("commodities", utc(2026, 7, 5, 22, 0)) is True


class TestTheScheduler:
    """These two predicates are the whole scheduler: they decide, from the
    `job_run` timestamp alone, whether a job runs on this tick."""

    def test_a_job_that_has_never_run_is_always_due(self):
        # This is how a fresh database bootstraps itself on first boot.
        assert _weekly(None, utc(2026, 7, 28)) is True
        assert _every(30)(None, utc(2026, 7, 28)) is True

    def test_weekly_fires_on_the_iso_week_rollover_not_after_seven_days(self):
        # Mon 2026-07-27 and Fri 2026-07-31 share ISO week 31.
        assert _weekly(utc(2026, 7, 27), utc(2026, 7, 31)) is False
        # Two days apart, but a new ISO week — this is the Monday run.
        assert _weekly(utc(2026, 7, 26, 23), utc(2026, 7, 27, 1)) is True

    def test_an_interval_job_waits_out_its_interval(self):
        now = utc(2026, 7, 28)
        assert _every(30)(now - timedelta(days=29, hours=23), now) is False
        assert _every(30)(now - timedelta(days=30), now) is True
