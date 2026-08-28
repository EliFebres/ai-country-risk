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
import json
import os
import inspect
import math
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.main import _every, _weekly
from backend.llm import payload as dr
from backend.util import market_hours
from backend.util import lint, metrics
from backend.llm import usage
from backend.util.tools import bakeoff

AS_OF = _dt.date(2026, 7, 27)


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


def annual(code, values, *, start=2022, source="World Bank"):
    """Annual rows as `country_data_fetch.panel_rows` writes them: one per year,
    stamped 31 December of the year they describe."""
    return {code: [{"period": str(start + i), "freq": "A", "value": v,
                    "as_of": _dt.date(start + i, 12, 31), "source": source}
                   for i, v in enumerate(values)]}


def merge(*stores):
    """Concatenate per-code row lists.

    `{**a, **b}` replaces on a shared key rather than concatenating, which
    silently drops one side when both hold the same indicator — the exact
    mistake these tests are about.
    """
    out = {}
    for store in stores:
        for code, rows in store.items():
            out.setdefault(code, []).extend(rows)
    return out


def build(**kwargs):
    """Build a payload for PT with sensible empty defaults."""
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

    def test_a_monthly_print_beats_an_annual_one(self):
        # The annual row says 2.34 for 2025, the monthly says 3.8 for 2026-06.
        payload = build(series=merge(
            annual("CPI.YOY", [7.8, 4.3, 2.4, 2.34]),
            series_rows("CPI.YOY", [3.3, 3.5, 3.8], start="2026-04")))
        entry = payload["uncertainty_inputs"]["Inflation (% y/y)"]
        assert entry["value"] == 3.8
        assert entry["period"] == "2026-06" and entry["freq"] == "M"

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
        payload = build(series=merge(
            annual("CPI.YOY", [1.0, 2.0, 3.0, 4.0], start=2023),
            series_rows("CPI.YOY", [9.9], start="2024-01")))
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
        payload = build(series=merge(
            annual("CPI.YOY", [1.0, 2.0, 3.0, 4.0]),
            series_rows("IQ.SPI.OVRL", [64.0], freq="A", start="2024",
                        source="World Bank SPI"),
            series_rows("IC.BUS.NDNS.ZS", [5.1], freq="A", start="2024",
                        source="World Bank WDI")))
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


def bakeoff_rows(scores, *, flags=None, lint_rules=None, start="2019-01-07"):
    """Candidate-file rows over consecutive Mondays, one score each.

    The ledger scores track the composite so a test about `score` does not have
    to restate four more numbers; tests that care about the ledgers set them.
    """
    first = date.fromisoformat(start)
    rows = []
    for i, value in enumerate(scores):
        rows.append({
            "as_of": (first + timedelta(weeks=i)).isoformat(),
            "status": "complete",
            "llm_score": value,
            "score_3m": value,
            "ledger_scores": {"friction": value, "order_uncertainty": value,
                              "information_capacity": value, "edge_vitality": value},
            "condition_flags": dict(flags or {}),
            "lint": [{"rule": r} for r in (lint_rules or [])],
        })
    return rows


class TestRankCorrelationIsTheMeter:
    """A constant offset is survivable; a reordering is not.

    The whole bake-off turns on telling those apart, and a mean shift cannot:
    a candidate that adds 0.1 to every week and one that shuffles the year
    both report "different from the baseline" under a difference of means. So
    the two cases are asserted against each other rather than in isolation.
    """

    def test_a_constant_offset_keeps_perfect_rank_and_shows_a_level_shift(self):
        base = bakeoff_rows([0.10, 0.25, 0.40, 0.55, 0.70])
        offset = bakeoff_rows([0.20, 0.35, 0.50, 0.65, 0.80])
        left = bakeoff._series(base, "llm_score")
        right = bakeoff._series(offset, "llm_score")

        assert bakeoff.rank_correlation(left, right)["spearman"] == 1.0
        assert bakeoff.rank_correlation(left, right)["kendall"] == 1.0
        moved = bakeoff.shift(left, right)
        assert moved["signed_mean"] == pytest.approx(0.10)
        assert moved["abs_mean"] == pytest.approx(0.10)

    def test_a_reversed_series_reorders_completely(self):
        base = bakeoff_rows([0.10, 0.25, 0.40, 0.55, 0.70])
        reversed_ = bakeoff_rows([0.70, 0.55, 0.40, 0.25, 0.10])
        got = bakeoff.rank_correlation(bakeoff._series(base, "llm_score"),
                                       bakeoff._series(reversed_, "llm_score"))
        assert got["spearman"] == -1.0
        assert got["kendall"] == -1.0
        # And the level says nothing: the mean is identical either way, which is
        # exactly why rank correlation is read first.
        moved = bakeoff.shift(bakeoff._series(base, "llm_score"),
                              bakeoff._series(reversed_, "llm_score"))
        assert moved["signed_mean"] == pytest.approx(0.0)
        assert moved["abs_mean"] > 0.2

    def test_opposite_weeks_cannot_average_into_a_clean_zero(self):
        """`reports.divergence` makes the same argument in the same words."""
        base = bakeoff_rows([0.40, 0.40, 0.40, 0.40])
        mixed = bakeoff_rows([0.60, 0.20, 0.60, 0.20])
        moved = bakeoff.shift(bakeoff._series(base, "llm_score"),
                              bakeoff._series(mixed, "llm_score"))
        assert moved["signed_mean"] == pytest.approx(0.0)
        assert moved["abs_mean"] == pytest.approx(0.20)
        assert moved["max_abs"] == pytest.approx(0.20)

    def test_an_unmeasurable_correlation_is_none_and_never_nan(self):
        """pandas returns NaN for a constant series, and NaN formats as a number."""
        flat = bakeoff_rows([0.4, 0.4, 0.4, 0.4])
        varied = bakeoff_rows([0.1, 0.2, 0.3, 0.4])
        got = bakeoff.rank_correlation(bakeoff._series(flat, "llm_score"),
                                       bakeoff._series(varied, "llm_score"))
        assert got["spearman"] is None and got["kendall"] is None
        assert got["n"] == 4

        two = bakeoff.rank_correlation({"a": 0.1, "b": 0.2}, {"a": 0.3, "b": 0.4})
        assert two["spearman"] is None
        assert two["n"] == 2

    def test_an_anchor_only_one_side_scored_is_dropped_from_the_pair(self):
        left = {"a": 0.1, "b": 0.2, "c": None, "d": 0.4}
        right = {"a": 0.2, "b": 0.3, "c": 0.5, "e": 0.9}
        assert bakeoff.rank_correlation(left, right)["n"] == 2
        assert bakeoff.shift(left, right)["n"] == 2

    def test_kendall_is_tie_corrected(self):
        """tau-b, not tau-a: the ledgers are integers on a 0-100 grid.

        Under tau-a a tie counts against the numerator's denominator and two
        series that agree perfectly report a ceiling below 1.0 — which would
        read as disagreement on exactly the metric with the coarsest scale.
        """
        # C=5, D=0, one tie on the left: 5 / sqrt(6*5).
        assert bakeoff._kendall_tau_b([1, 2, 2, 3], [1, 2, 3, 4]) == \
            pytest.approx(5 / (30 ** 0.5))
        assert bakeoff._kendall_tau_b([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)
        assert bakeoff._kendall_tau_b([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
        # Tied on both sides everywhere: nothing to correlate, and None says so.
        assert bakeoff._kendall_tau_b([1, 1, 1], [2, 2, 2]) is None

class TestTheBandsAreThePromptsOwn:
    """Calibration is judged on the bands the model was told about."""

    @pytest.mark.parametrize("score, expected", [
        (0.00, "Low"), (0.12, "Low"), (0.199, "Low"),
        (0.20, "Low-Moderate"), (0.399, "Low-Moderate"),
        (0.40, "Moderate"), (0.58, "Moderate"), (0.749, "Moderate"),
        (0.75, "High"), (0.899, "High"),
        (0.90, "Extreme"), (1.00, "Extreme"),
    ])
    def test_the_boundaries_are_lower_inclusive(self, score, expected):
        assert bakeoff.band(score) == expected

    def test_an_unscored_anchor_has_no_band(self):
        assert bakeoff.band(None) is None

    def test_the_matrix_keeps_its_shape_when_a_band_is_empty(self):
        """A 5x5 that silently becomes 3x3 is a different plot every run."""
        base = bakeoff_rows([0.10, 0.30, 0.50])
        same = bakeoff_rows([0.10, 0.30, 0.50])
        matrix = bakeoff.band_matrix(bakeoff._series(base, "llm_score"),
                                     bakeoff._series(same, "llm_score"))
        labels = [b[0] for b in bakeoff.BANDS]
        assert list(matrix) == labels
        assert all(list(row) == labels for row in matrix.values())
        assert matrix["Low"]["Low"] == 1
        assert matrix["Moderate"]["Moderate"] == 1
        assert matrix["Extreme"]["Extreme"] == 0

    def test_an_offset_lands_off_the_diagonal_in_one_direction(self):
        base = bakeoff_rows([0.10, 0.15, 0.18])
        hotter = bakeoff_rows([0.25, 0.30, 0.35])
        matrix = bakeoff.band_matrix(bakeoff._series(base, "llm_score"),
                                     bakeoff._series(hotter, "llm_score"))
        assert matrix["Low"]["Low-Moderate"] == 3
        assert matrix["Low"]["Low"] == 0


class TestTheObservationOnlyFlags:
    """Per flag, because the flags are not equivalent.

    `war_on_territory` is false on every PT week in 2019 and agreeing about it
    is nearly free; `sovereign_stress` is the one a model reading the prompt as
    instructions would start moving. A single mean hides which happened.
    """

    def test_agreement_is_reported_per_flag(self):
        base = {"d1": {"war_on_territory": False, "sovereign_stress": False},
                "d2": {"war_on_territory": False, "sovereign_stress": False}}
        cand = {"d1": {"war_on_territory": False, "sovereign_stress": True},
                "d2": {"war_on_territory": False, "sovereign_stress": False}}
        got = bakeoff.flag_agreement(base, cand)
        assert got["war_on_territory"]["agreement"] == 1.0
        assert got["sovereign_stress"]["agreement"] == 0.5

    def test_a_flag_neither_side_reported_is_none_not_perfect(self):
        got = bakeoff.flag_agreement({"d1": {}}, {"d1": {}})
        assert got["emergency_rule"] == {"n": 0, "agreement": None}


class TestTheCostSummary:
    """A provider that reports no cache detail must not read as a 0% hit rate."""

    def test_an_unreported_cache_share_is_none(self):
        rows = [{"as_of": "2019-01-07", "status": "complete", "spend_usd": 0.01,
                 "input_tokens": 1000, "output_tokens": 100, "cached_tokens": None,
                 "utc_hour": 12}]
        assert bakeoff.cost_summary(rows)["cache_share"] is None

    def test_a_measured_cache_share_is_a_fraction_of_input(self):
        rows = [{"as_of": "2019-01-07", "status": "complete", "spend_usd": 0.01,
                 "input_tokens": 1000, "output_tokens": 100, "cached_tokens": 800,
                 "utc_hour": 12}]
        assert bakeoff.cost_summary(rows)["cache_share"] == 0.8

    def test_empty_and_failed_anchors_do_not_dilute_the_per_snapshot_cost(self):
        rows = [{"as_of": "2019-01-07", "status": "complete", "spend_usd": 0.02,
                 "input_tokens": 100, "output_tokens": 10, "cached_tokens": 0,
                 "utc_hour": 12},
                {"as_of": "2019-01-14", "status": "empty", "llm_score": None},
                {"as_of": "2019-01-21", "status": "failed", "spend_usd": 0.0}]
        got = bakeoff.cost_summary(rows)
        assert got["snapshots"] == 1
        assert got["per_snapshot_usd"] == pytest.approx(0.02)

    def test_a_projection_says_nothing_when_nothing_was_measured(self):
        assert bakeoff.projection(None) == {"pilot_usd": None, "backfill_usd": None}
        assert bakeoff.projection(0.01)["pilot_usd"] == pytest.approx(20.92)


class TestTheComparisonNamesWhatItCouldNotMatch:
    """"Not measured" and "no change" are different answers, per `probe.compare`."""

    def test_anchors_on_only_one_side_are_listed_rather_than_dropped(self):
        base = bakeoff_rows([0.1, 0.2, 0.3])
        short = bakeoff_rows([0.1, 0.2])
        got = bakeoff.compare_one(base, short)
        assert got["n_baseline"] == 3 and got["n_candidate"] == 2
        assert got["only_baseline"] == ["2019-01-21"]
        assert got["only_candidate"] == []
        assert got["metrics"]["llm_score"]["n"] == 2

    def test_every_metric_is_reported_including_the_counter_intuitive_ledgers(self):
        got = bakeoff.compare_one(bakeoff_rows([0.1, 0.2, 0.3]),
                                  bakeoff_rows([0.1, 0.2, 0.3]))
        assert set(got["metrics"]) == set(bakeoff.METRICS)
        assert "information_capacity" in got["metrics"]
        assert "edge_vitality" in got["metrics"]

    def test_a_rule_firing_on_one_side_only_is_visible(self):
        base = bakeoff_rows([0.1, 0.2], lint_rules=["war_flag_without_score_floor"])
        cand = bakeoff_rows([0.1, 0.2])
        got = bakeoff.compare_one(base, cand)
        assert got["lint"]["war_flag_without_score_floor"] == {"baseline": 2,
                                                              "candidate": 0}


class TestTheEnvironmentIsRestored:
    """A sweep must not let the second candidate inherit the first's endpoint."""

    def test_a_candidates_endpoint_is_set_and_then_put_back(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "mm-key")
        monkeypatch.delenv("SCORING_MODEL", raising=False)
        monkeypatch.setenv("SCORING_BASE_URL", "https://pre-existing.example/v1")

        with bakeoff.candidate_env("minimax-m3") as env:
            assert env["SCORING_MODEL"] == "MiniMax-M3"
            assert os.environ["SCORING_API_KEY"] == "mm-key"
            assert os.environ["SCORING_BASE_URL"] == "https://api.minimax.io/v1"

        assert "SCORING_MODEL" not in os.environ
        assert "SCORING_API_KEY" not in os.environ
        assert os.environ["SCORING_BASE_URL"] == "https://pre-existing.example/v1"

    def test_a_missing_vendor_key_raises_before_anything_is_set(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("SCORING_MODEL", raising=False)
        with pytest.raises(bakeoff.MissingKey, match="DEEPSEEK_API_KEY"):
            with bakeoff.candidate_env("deepseek-v4-pro"):
                pass
        assert "SCORING_MODEL" not in os.environ

    def test_the_deepseek_candidates_pin_thinking_off(self):
        """Reasoning tokens bill as output; an unpinned run prices a fiction."""
        for name in ("deepseek-v4-pro", "deepseek-v4-flash"):
            spec = bakeoff.CANDIDATES[name]
            body = next(v for k, v in spec["env"].items() if k.endswith("_EXTRA_BODY"))
            assert json.loads(body) == {"thinking": {"type": "disabled"}}


class TestEveryCandidateIsAScorer:
    """One variable, one axis. A digest candidate cannot share this meter.

    The digest cache is keyed on the digest model, so a candidate that moves
    stage 1 reads *different evidence* — and rank correlation stops isolating the
    scorer, which is the only thing it is for. Holding digests on `gpt-4o-mini`
    is what makes the number mean what the write-up says it means.
    """

    def test_no_candidate_moves_the_digest_endpoint(self):
        for name, spec in bakeoff.CANDIDATES.items():
            assert spec["arm"] == "scoring", f"{name} is not on the scoring axis"
            assert not [k for k in spec["env"] if k.startswith("DIGEST_")], (
                f"{name} moves stage 1; it cannot be compared on this meter")
            assert spec.get("key_target", "SCORING_API_KEY") == "SCORING_API_KEY"

    def test_the_unrun_groq_candidate_is_gone(self):
        """An unrun candidate in a config file is one that gets run by accident."""
        assert "gpt-oss-120b" not in bakeoff.CANDIDATES


class TestDeepSeekIsPricedAtItsPeak:
    """A governor quoting the cheap half of a run it has not finished is the
    failure this pricing table exists to prevent."""

    def test_the_offpeak_figure_is_exactly_half_and_reporting_only(self):
        peak = usage.price("deepseek-v4-pro", 10_000, 1_000)
        assert usage.offpeak_price("deepseek-v4-pro", 10_000, 1_000) == peak / 2.0

    def test_a_non_deepseek_model_has_no_offpeak_half(self):
        # None, not the same number: "this vendor does not do time-of-day" and
        # "we measured no discount" are different facts.
        assert usage.offpeak_price("MiniMax-M3", 10_000, 1_000) is None
        assert usage.offpeak_price("gpt-4o-2024-08-06", 10_000, 1_000) is None

    def test_the_peak_hours_are_the_two_windows_deepseek_bills_double(self):
        assert usage.DEEPSEEK_PEAK_HOURS_UTC == frozenset({1, 2, 3, 6, 7, 8, 9})

    def test_cached_input_is_billed_at_the_cache_rate_not_the_miss_rate(self):
        """Prompt v4 is a long constant prefix; pricing it all at the miss rate
        overstates each vendor by a different amount and so ranks them wrongly."""
        rate_in, rate_cached, _ = usage.PRICES_USD_PER_1M["MiniMax-M3"]
        assert rate_cached < rate_in
        all_missed = usage.price("MiniMax-M3", 10_000, 0, cached_tokens=0)
        all_cached = usage.price("MiniMax-M3", 10_000, 0, cached_tokens=10_000)
        assert all_cached == pytest.approx(all_missed * rate_cached / rate_in)

    def test_more_cached_than_input_cannot_price_below_the_honest_floor(self):
        """A provider reporting nonsense must not be read the cheap way."""
        clamped = usage.price("MiniMax-M3", 1_000, 0, cached_tokens=99_999)
        assert clamped == usage.price("MiniMax-M3", 1_000, 0, cached_tokens=1_000)


class TestTheCostSummaryCarriesTheBillingWindow:
    """DeepSeek's price depends on the hour, so the hour is part of the result."""

    @staticmethod
    def _rows(hours, model="deepseek-v4-pro"):
        return [{"as_of": f"2019-01-{7 + i:02d}", "status": "complete",
                 "spend_usd": 0.02, "offpeak_usd": 0.01, "input_tokens": 1000,
                 "output_tokens": 100, "cached_tokens": 0, "utc_hour": h}
                for i, h in enumerate(hours)]

    def test_the_hours_a_run_occupied_are_recorded_not_just_printed(self):
        got = bakeoff.cost_summary(self._rows([13, 14, 13]))
        assert got["utc_hours"] == [13, 14]

    def test_the_offpeak_figure_rides_alongside_rather_than_replacing(self):
        got = bakeoff.cost_summary(self._rows([13, 14]))
        assert got["per_snapshot_usd"] == pytest.approx(0.02)
        assert got["offpeak_per_snapshot_usd"] == pytest.approx(0.01)

    def test_a_vendor_without_a_billing_window_reports_no_offpeak_figure(self):
        rows = [{"as_of": "2019-01-07", "status": "complete", "spend_usd": 0.01,
                 "offpeak_usd": None, "input_tokens": 1000, "output_tokens": 100,
                 "cached_tokens": 0, "utc_hour": 13}]
        assert bakeoff.cost_summary(rows)["offpeak_per_snapshot_usd"] is None
