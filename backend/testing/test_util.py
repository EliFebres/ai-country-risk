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
from backend.llm import constants as ai_constants
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


class TestTheSeriesShapeMeter:
    """The function every discrimination verdict in `docs/payload-ab.md` rests
    on, and it had no test until the fourth experiment was about to use it."""

    def test_it_reproduces_the_published_a_prime_figures(self):
        """Pinned to the numbers the docs already argue from. If this drifts,
        either the meter changed or the write-ups are wrong, and both are worth
        a failing test."""
        path = (bakeoff.RESULTS_DIR / "US-2019" / "p2-rebaseline.json")
        if not path.exists():
            pytest.skip("arm file not present")
        rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
        shape = bakeoff.series_shape([r.get("llm_score") for r in rows])
        assert shape["n"] == 52
        assert shape["distinct"] == 8
        assert shape["round_share"] == pytest.approx(0.769, abs=0.001)
        assert shape["longest_run"] == 4

    def test_round_share_counts_multiples_of_five_on_the_hundred_scale(self):
        """0.45 is a round number and 0.47 is not; the prompt's instruction is
        about the integer the model emitted, not the float we store."""
        shape = bakeoff.series_shape([0.45, 0.47, 0.50, 0.63])
        assert shape["round_share"] == pytest.approx(0.5)

    def test_a_float_that_is_not_an_integer_score_is_never_round(self):
        """A computed composite lands between the integers. Counting 0.475 as a
        near-miss for 0.475*100 would make any averaged series look precise."""
        assert bakeoff.series_shape([0.475, 0.4625])["round_share"] == 0.0

    def test_distinct_counts_values_and_not_anchors(self):
        assert bakeoff.series_shape([0.5, 0.5, 0.5, 0.6])["distinct"] == 2

    def test_the_longest_run_is_of_identical_neighbours(self):
        assert bakeoff.series_shape([0.5, 0.5, 0.6, 0.5, 0.5, 0.5])["longest_run"] == 3

    def test_a_constant_series_has_no_autocorrelation_to_report(self):
        """None, not 0.0: a series that never moves has no correlation rather
        than an uncorrelated one, and 0.0 would read as the healthy answer."""
        assert bakeoff.series_shape([0.5] * 6)["lag1_autocorr"] is None

    def test_unscored_anchors_are_excluded_rather_than_counted_as_a_value(self):
        assert bakeoff.series_shape([0.5, None, 0.5, 0.6])["distinct"] == 2


class TestTheFirstMoveMeter:
    """Criterion (d) of three experiments, and until now never once code.

    Both previous attempts computed it by hand from the committed arm files and
    kept only the verdict, so the number deciding whether an intervention made
    the instrument *late* could not be recomputed or checked.
    """

    def test_it_reproduces_the_published_verdicts_on_the_determinate_window(self):
        """A-prime moves at 2018-02-05 and both trend arms a fortnight earlier;
        those three readings were arrived at independently and by hand."""
        expected = {"p2-rebaseline": "2018-02-05", "p3-context": "2018-02-05",
                    "trend-prompt": "2018-01-22", "p4-trend": "2018-01-22"}
        for candidate, when in expected.items():
            path = bakeoff.RESULTS_DIR / "TR-2018" / f"{candidate}.json"
            if not path.exists():
                pytest.skip(f"{candidate} not present")
            rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
            index = bakeoff.first_move([r.get("llm_score") for r in rows])
            assert rows[index]["as_of"] == when, candidate

    def test_a_flat_series_never_moves_and_says_so(self):
        """None is a real answer about a flat arm, not a missing measurement."""
        assert bakeoff.first_move([0.5] * 20, baseline_n=4) is None

    def test_the_baseline_is_the_opening_mean_and_not_the_opening_anchor(self):
        """A single first week is one draw from a series whose noise floor is a
        point or two. Attempt 1 recorded both readings because they disagreed
        about which arm moved first."""
        values = [0.48, 0.52, 0.50, 0.50, 0.54, 0.70]
        # Opening mean is 0.50, so 0.54 falls short and 0.70 clears. Read
        # against the opening *anchor* of 0.48 instead, 0.54 would clear it and
        # the arm would be reported as moving a week earlier than it did.
        assert bakeoff.first_move(values, baseline_n=4) == 5

    def test_a_series_shorter_than_its_baseline_returns_nothing(self):
        assert bakeoff.first_move([0.5, 0.9], baseline_n=13) is None

    def test_unscored_anchors_do_not_shift_the_index(self):
        """The index must address the row, not the position among scored rows --
        a verdict is reported as a date."""
        values = [0.50, None, 0.50, 0.50, 0.50, 0.80]
        assert bakeoff.first_move(values, baseline_n=4) == 5


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
                 "model_id": "gpt-4o-2024-08-06",
                 "input_tokens": 100, "output_tokens": 10, "cached_tokens": 0,
                 "utc_hour": 12},
                {"as_of": "2019-01-14", "status": "empty", "llm_score": None},
                {"as_of": "2019-01-21", "status": "failed", "spend_usd": 0.0}]
        got = bakeoff.cost_summary(rows)
        assert got["snapshots"] == 1
        assert got["per_snapshot_usd"] == pytest.approx(0.02)

    def test_a_run_whose_model_cannot_be_named_reports_no_dollars(self):
        """The complement of the test above, and the reason it needed a model.

        A cost figure derived from a model nobody can name is the same
        fabrication as one derived from a model with no price -- both come out
        of `usage._FALLBACK_PRICE`, which exists to stop a run rather than to
        describe one.
        """
        rows = [{"as_of": "2019-01-07", "status": "complete", "spend_usd": 0.02,
                 "input_tokens": 100, "output_tokens": 10, "cached_tokens": 0,
                 "utc_hour": 12}]
        got = bakeoff.cost_summary(rows)
        assert got["priced"] is False
        assert got["per_snapshot_usd"] is None
        assert got["input_tokens_per_snapshot"] == 100

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
        monkeypatch.delenv("SCORING_MODEL", raising=False)
        # A leftover from an earlier candidate. It must be cleared for the block
        # and restored after: the whole failure mode is one model's payload being
        # scored at another model's endpoint and filed under the wrong name.
        monkeypatch.setenv("SCORING_BASE_URL", "https://pre-existing.example/v1")

        with bakeoff.candidate_env("gpt-4.1-mini") as env:
            assert env["SCORING_MODEL"] == "gpt-4.1-mini-2025-04-14"
            assert os.environ["SCORING_MODEL"] == "gpt-4.1-mini-2025-04-14"
            assert "SCORING_BASE_URL" not in os.environ

        assert "SCORING_MODEL" not in os.environ
        assert os.environ["SCORING_BASE_URL"] == "https://pre-existing.example/v1"

    def test_the_incumbent_sets_no_endpoint_at_all(self):
        """gpt-4o is the reference; it must run on exactly the daily run's config."""
        with bakeoff.candidate_env("gpt-4o") as env:
            assert env == {}
            for key in ("SCORING_MODEL", "SCORING_BASE_URL", "SCORING_EXTRA_BODY"):
                assert key not in os.environ

    def test_a_missing_vendor_key_raises_before_anything_is_set(self, monkeypatch):
        """Tested through a synthetic candidate rather than a real one.

        Every current candidate is an OpenAI model and needs no `key_target`, so
        nothing in `CANDIDATES` exercises this path today — and the path is what
        stops a sweep spending on candidate two after candidate one's key turned
        out to be absent. Round 2 had three candidates that needed it and round 4
        may again.
        """
        monkeypatch.setitem(bakeoff.CANDIDATES, "synthetic", {
            "arm": "scoring", "env": {"SCORING_MODEL": "m"},
            "key_env": "NO_SUCH_VENDOR_KEY", "key_target": "SCORING_API_KEY"})
        monkeypatch.delenv("NO_SUCH_VENDOR_KEY", raising=False)
        monkeypatch.delenv("SCORING_MODEL", raising=False)

        with pytest.raises(bakeoff.MissingKey, match="NO_SUCH_VENDOR_KEY"):
            with bakeoff.candidate_env("synthetic"):
                pass
        assert "SCORING_MODEL" not in os.environ

    def test_every_reasoning_candidate_pins_the_effort_to_the_floor(self):
        """Reasoning tokens bill as output; an unpinned run prices a fiction.

        Measured, not assumed: unpinned, `gpt-5.6-luna` returned 1,834 output
        tokens of which 1,400 were reasoning, against 283 pinned — so leaving it
        on prices the model at roughly 1.7x a snapshot. `gpt-5.4-mini` was
        measured already defaulting to zero reasoning and is pinned anyway,
        because a default is not a guarantee and a vendor-side change would move
        cost and determinism at once, silently.

        This is the same trap MiniMax's thinking mode set in round 2, which is
        why it is a rule about a family rather than a note about one model.
        """
        for name in ("gpt-5.6-luna", "gpt-5.4-mini"):
            spec = bakeoff.CANDIDATES[name]
            body = next(v for k, v in spec["env"].items() if k.endswith("_EXTRA_BODY"))
            assert json.loads(body) == {"reasoning_effort": "none"}, name

    def test_no_candidate_carries_an_unpinned_reasoning_model(self):
        """The 5.x and o-series families reason by default; 4.x do not."""
        for name, spec in bakeoff.CANDIDATES.items():
            model = spec["env"].get("SCORING_MODEL", "")
            if model.startswith(("gpt-5", "o1", "o3", "o4")):
                assert any(k.endswith("_EXTRA_BODY") for k in spec["env"]), name


from backend.util.pilot import reports


class TestAnArmThatScoredNothingSaysSo:
    """An exhausted API key produced 47 rows marked `complete`.

    The pipeline degrades a model failure into an empty result rather than
    raising, which is right for the daily run -- one country must not end the
    pass. Through the bake-off it meant `status: complete`, `llm_score: null`,
    `error: null`, `calls: 0`, and an arm that called itself finished having
    scored six anchors of fifty-three. `capture-baseline` would then have read
    it as a reference.
    """

    def test_a_result_with_no_score_is_not_complete(self):
        import inspect

        from backend.util.tools import bakeoff
        src = inspect.getsource(bakeoff.score_anchors)
        assert 'out.get("score") is None' in src, (
            "a returned dict is being taken as proof an anchor was scored")
        assert '"unscored"' in src


class TestAStalledHarvestIsNamed:
    """Eleven consecutive failures on one country produced no line anybody read.

    `_pace` counted them, and a total of eleven among a hundred windows reads
    as an unreliable API. The gap surfaced months later only because a purchase
    decision went looking for it. The run length is what separates flakiness
    from a country that is simply not being harvested.
    """

    def _rows(self, statuses, source="guardian", iso2="BR"):
        return [{"source": source, "country_iso2": iso2,
                 "as_of": _dt.date(2015 + i, 1, 1), "status": st,
                 "items": 0, "seconds": 20.0, "calls": 1}
                for i, st in enumerate(statuses)]

    def test_consecutive_failures_are_reported_as_a_run(self):
        out = reports._pace(self._rows(["failed"] * 11))
        assert out["per_source_country"]["guardian BR"]["longest_failure_run"] == 11
        assert out["stalled"] == ["guardian BR"]

    def test_scattered_failures_are_not_a_stall(self):
        """Six failures, never two together — an unreliable API, not a gap."""
        out = reports._pace(self._rows(
            ["failed", "done"] * 5 + ["failed"]))
        row = out["per_source_country"]["guardian BR"]
        assert row["failed"] == 6 and row["longest_failure_run"] == 1
        assert out["stalled"] == []

    def test_the_run_is_counted_by_window_not_by_retry_order(self):
        """`write_checkpoint` upserts `completed_at = now()` on every retry.

        So completed-at order is retry order: a window retried today sorts after
        one harvested last week, and a genuine run of failures reads as
        scattered ones. Rows arrive here in whatever order the SQL produced.
        """
        rows = self._rows(["done", "failed", "failed", "failed", "done"])
        assert (reports._pace(list(reversed(rows)))["per_source_country"]
                ["guardian BR"]["longest_failure_run"] == 3)

    def test_the_running_count_is_not_reported_as_a_result(self):
        """`streak` is loop state; only its high-water mark is a finding."""
        out = reports._pace(self._rows(["failed", "done"]))
        assert "streak" not in out["per_source_country"]["guardian BR"]


class TestEveryCandidateIsAScorer:
    """One variable, one axis. A digest candidate cannot share this meter.

    The digest cache is keyed on the digest model, so a candidate that moves
    stage 1 reads *different evidence* — and rank correlation stops isolating the
    scorer, which is the only thing it is for. Holding digests on `gpt-4o-mini`
    is what makes the number mean what the write-up says it means.
    """

    def test_every_candidate_varies_exactly_one_axis(self):
        """Two axes now — the scorer and the payload — and no candidate may
        straddle them. A scoring arm that also moved the payload, or a payload
        arm that also moved the model, would produce a number with two causes
        and no way to separate them."""
        for name, spec in bakeoff.CANDIDATES.items():
            assert spec["arm"] in ("scoring", "payload", "prompt", "crossed"), name
            assert not [k for k in spec["env"] if k.startswith("DIGEST_")], (
                f"{name} moves stage 1; it cannot be compared on this meter")
            assert spec.get("key_target", "SCORING_API_KEY") == "SCORING_API_KEY"
            moves_model = any(k.startswith("SCORING_") for k in spec["env"])
            moves_payload = "PAYLOAD_VARIANT" in spec["env"]
            moves_prompt = "PROMPT_VARIANT" in spec["env"]
            axes = sum((moves_model, moves_payload, moves_prompt))
            if spec["arm"] == "crossed":
                # The one exception, and it is not a relaxation. A crossed cell
                # exists to answer whether two changes are additive, which is
                # readable only because both single-cause corners were measured
                # separately -- so it must name them, they must exist, and
                # between them they must cover exactly the axes it moves.
                # Without that it is the two-cause number this test was written
                # against, wearing a label.
                assert axes > 1, f"{name} is crossed but varies one axis"
                # A crossed cell is registered before it can be filled in --
                # which variant it crosses is a result, not a preference. The
                # two unknowns must agree: an env slot left open iff the sibling
                # naming it is left open, so a half-filled cell cannot run.
                assert (None in spec["crosses"]) == (None in spec["env"].values()), (
                    f"{name} is half-resolved")
                covered = set()
                for sibling in spec["crosses"]:
                    if sibling is None:
                        continue
                    assert sibling in bakeoff.CANDIDATES, (
                        f"{name} crosses {sibling!r}, which is not a candidate")
                    sib = bakeoff.CANDIDATES[sibling]
                    assert sib["arm"] != "crossed", (
                        f"{name} crosses {sibling}, which is itself crossed")
                    covered |= set(sib["env"])
                moved = {k for k, v in spec["env"].items() if v is not None}
                assert moved <= covered, (
                    f"{name} moves {sorted(moved - covered)}, which no arm it "
                    f"crosses measured alone")
                continue
            assert axes <= 1, (
                f"{name} varies more than one axis, so its number has more "
                f"than one cause and no way to separate them")
            if spec["arm"] == "payload":
                assert moves_payload and not (moves_model or moves_prompt), name
            if spec["arm"] == "prompt":
                # The one arm that changes what the model is *told* and not what
                # it is given. It carries no new evidence by construction.
                assert moves_prompt and not (moves_model or moves_payload), name

    def test_the_payload_axis_is_scrubbed_between_arms(self):
        """Missing from `managed`, a p3 arm leaks into every arm scored after it
        in the same process, and the contamination looks like a finding."""
        import inspect
        src = inspect.getsource(bakeoff.candidate_env)
        assert "PAYLOAD_VARIANT" in src
        assert "PROMPT_VARIANT" in src, (
            "a leaked trend instruction is invisible in the result file")

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
        # `model_id` is on the row because production rows carry it and because
        # `cost_summary` now withholds dollars for a model it cannot price. The
        # parameter existed here and was never written into the row, so this
        # fixture was quietly testing an unpriceable run.
        return [{"as_of": f"2019-01-{7 + i:02d}", "status": "complete",
                 "model_id": model,
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


class TestDeterminismGatesScoresNotProse:
    """A gate the incumbent cannot pass disqualifies everyone for its own defect.

    Measured over six repeats of one prompt at temperature 0 and seed 42, gpt-4o
    returns identical scores, ledgers, flags and coverage, and varies its
    `bullet_summary` every time plus which item `subscore_evidence` cites. Gating
    on the whole payload therefore fails the reference, and a bake-off whose
    reference fails its own gate ends with no candidates and no finding.
    """

    def test_reworded_prose_alone_is_not_a_determinism_failure(self):
        a = {"score_12m": 50, "ledger_scores": {"friction": 40},
             "bullet_summary": "One phrasing."}
        b = {**a, "bullet_summary": "A different phrasing entirely."}
        assert bakeoff._scored_only(a) == bakeoff._scored_only(b)

    def test_a_different_citation_for_the_same_score_is_not_either(self):
        a = {"score_12m": 50, "subscore_evidence": {"information_capacity": ["a1"]}}
        b = {"score_12m": 50, "subscore_evidence": {"information_capacity": ["structural"]}}
        assert bakeoff._scored_only(a) == bakeoff._scored_only(b)

    def test_a_moved_score_is_still_a_failure(self):
        a = {"score_12m": 50, "bullet_summary": "same"}
        b = {"score_12m": 51, "bullet_summary": "same"}
        assert bakeoff._scored_only(a) != bakeoff._scored_only(b)

    def test_a_moved_ledger_or_flag_is_still_a_failure(self):
        base = {"score_12m": 50, "ledger_scores": {"friction": 40},
                "condition_flags": {"sovereign_stress": False}}
        moved_ledger = {**base, "ledger_scores": {"friction": 41}}
        moved_flag = {**base, "condition_flags": {"sovereign_stress": True}}
        assert bakeoff._scored_only(base) != bakeoff._scored_only(moved_ledger)
        assert bakeoff._scored_only(base) != bakeoff._scored_only(moved_flag)

    def test_only_prose_and_citations_are_ungated(self):
        """Anything added to this set stops being checked, so it is pinned.

        `band_placement` and `typical_week` joined it with the elicitation arms,
        on the evidence that decided `bullet_summary`: measured over repeats at
        temperature 0 and seed 42, gpt-4o rewords them while every number holds.
        """
        assert bakeoff._UNGATED_FIELDS == ("bullet_summary", "subscore_evidence",
                                           "band_placement", "typical_week")

    def test_the_decisions_the_variants_force_are_still_gated(self):
        """The prose describing a decision is ungated; the decision is not. A
        band that wanders between repeats while the score holds is a finding
        about the variant, and ungating it would hide exactly that."""
        for field in ("band", "delta_vs_typical"):
            assert field not in bakeoff._UNGATED_FIELDS
        base = {"score_12m": 50, "band": "Moderate", "delta_vs_typical": 3}
        assert bakeoff._scored_only(base) != bakeoff._scored_only(
            {**base, "band": "High"})
        assert bakeoff._scored_only(base) != bakeoff._scored_only(
            {**base, "delta_vs_typical": 9})


class TestTheNoiseFloorIsInTheDivergenceMeterUnits:
    """A determinism pass/fail does not say whether the failure matters.

    The candidate's own jitter is only meaningful against the signal it would be
    spent on, and the only substantive one measured is PT's masking divergence
    of 0.072 on the stored 0-1 scale. Reporting the two in different registers
    leaves the reader doing the arithmetic.
    """

    def test_the_share_is_the_spread_against_the_current_benchmark(self):
        """Derived from the constant, not from a number computed against an
        older value of it. These pinned 0.072's arithmetic and broke when gate 2
        recomputed the divergence at 0.075 — a test failing because a
        measurement improved is a test coupled to the wrong thing."""
        got = bakeoff.noise_floor(2)
        assert got["spread_0_1"] == 0.02
        assert got["share_of_divergence"] == pytest.approx(
            0.02 / bakeoff.PT_MASKING_DIVERGENCE, abs=0.001)

    def test_a_point_or_two_costs_a_meaningful_slice_of_the_signal(self):
        """The behavioural claim, which is what the number is for: single-digit
        jitter is not free against a signal this small."""
        assert 0.2 < bakeoff.noise_floor(2)["share_of_divergence"] < 0.35

    def test_half_a_point_is_effectively_invisible(self):
        assert bakeoff.noise_floor(0.5)["share_of_divergence"] < 0.10

    def test_jitter_can_exceed_the_whole_signal(self):
        """Measured on real candidates, so it is not a hypothetical: gpt-4.1-nano
        swings 20 points on identical input, and masking was worth 0.075."""
        assert bakeoff.noise_floor(15)["share_of_divergence"] >= 2.0
        assert bakeoff.noise_floor(20)["share_of_divergence"] > 2.5

    def test_a_perfectly_stable_candidate_scores_zero(self):
        assert bakeoff.noise_floor(0)["share_of_divergence"] == 0.0

    def test_unmeasured_is_none_and_never_zero(self):
        """"Not measured" and "perfectly stable" are opposite facts and must not
        render the same, per the em-dash rule the reports already follow."""
        got = bakeoff.noise_floor(None)
        assert got["share_of_divergence"] is None
        assert got["spread_0_1"] is None

    def test_the_benchmark_is_the_stored_scale_not_the_model_scale(self):
        """0.072 is masked-minus-named out of `risk_snapshot.score`, which is
        0-1. Reading it as 0-100 would understate every candidate 100-fold."""
        assert 0 < bakeoff.PT_MASKING_DIVERGENCE < 1


class TestTheBaselineUnwrapsTheStoredLedgerColumn:
    """`risk_snapshot.ledger_scores` holds two things; the candidate arm holds one.

    The column is a JSONB `{ledger_scores: {...}, subscore_evidence: {...}}`,
    while a candidate row carries the four scores flat off `llm_output`. Copying
    the column whole nests them a level too deep, every ledger lookup misses, and
    `_paired` drops a None rather than raising — so the comparison printed `n=0`
    and an em dash for all four ledgers and read like a metric nobody had filled
    in. The composite still matched, so the report looked healthy.
    """

    @staticmethod
    def _rows(ledgers):
        return [{"as_of": "2019-01-07", "status": "complete", "llm_score": 0.5,
                 "score_3m": 0.5, "ledger_scores": ledgers, "condition_flags": {},
                 "lint": []}]

    def test_the_four_ledgers_are_actually_paired(self):
        """The regression itself: n must not be 0 when both sides have ledgers."""
        stored = {"ledger_scores": {"friction": 0.38, "order_uncertainty": 0.40,
                                    "information_capacity": 0.25, "edge_vitality": 0.60},
                  "subscore_evidence": {"friction": ["a1"]}}
        flat = {"friction": 0.38, "order_uncertainty": 0.40,
                "information_capacity": 0.25, "edge_vitality": 0.60}
        unwrapped = (stored or {}).get("ledger_scores", stored) or {}
        got = bakeoff.compare_one(self._rows(unwrapped), self._rows(flat))
        for ledger in ("friction", "order_uncertainty",
                       "information_capacity", "edge_vitality"):
            assert got["metrics"][ledger]["n"] == 1, ledger

    def test_a_flat_column_still_works(self):
        """`.get(..., ledgers)` and not a bare index, so a future flat column
        does not silently start returning nothing."""
        flat = {"friction": 0.38}
        assert (flat or {}).get("ledger_scores", flat) == flat


class TestTheSeriesShapeMeters:
    """Four single-series meters, because every other one here is paired.

    Rank correlation cannot answer "does this series say anything at all" — it
    returns None below two distinct values, and a series with nine across
    fifty-two weeks is barely above that. These are what a payload change is
    supposed to move and a scorer change is not.
    """

    def test_a_constant_series_is_not_reported_as_uncorrelated(self):
        """"Not measurable" and "uncorrelated" are different facts — the em-dash
        rule the reports already follow."""
        got = bakeoff.series_shape([0.5] * 6)
        assert got["lag1_autocorr"] is None
        assert got["distinct"] == 1 and got["longest_run"] == 6

    def test_an_empty_series_reports_nothing_rather_than_zero(self):
        got = bakeoff.series_shape([])
        assert got == {"n": 0, "distinct": 0, "lag1_autocorr": None,
                       "longest_run": None, "round_share": None}

    def test_nones_are_dropped_not_counted(self):
        assert bakeoff.series_shape([0.5, None, 0.6])["n"] == 2

    def test_the_longest_run_counts_consecutive_identical_scores(self):
        assert bakeoff.series_shape([0.1, 0.2, 0.2, 0.2, 0.3])["longest_run"] == 3

    def test_round_share_is_on_the_models_own_scale(self):
        """The prompt bans multiples of 5 on the 0-100 grid; the store keeps
        0-1, so the check has to convert or it measures nothing."""
        assert bakeoff.series_shape([0.50, 0.55, 0.60])["round_share"] == 1.0
        assert bakeoff.series_shape([0.52, 0.37, 0.68])["round_share"] == 0.0

    def test_it_reproduces_the_measured_incumbent(self):
        """Pinned to what gpt-4o actually produced, so a change to the meter
        cannot quietly restate the finding it was built to test."""
        us = bakeoff.series_shape([0.45, 0.55, 0.52, 0.70, 0.62, 0.50, 0.52, 0.42])
        assert us["distinct"] == 7
        assert us["round_share"] == pytest.approx(0.5)


# --- a locally served scorer, and the three things that break first ---------
#
# `CANDIDATES` dispatches on environment variables, so adding a local model is a
# dict entry and that part was never in doubt. What breaks is downstream of it:
# the schema gate assumes a strict mode the endpoint does not have, and the cost
# table prices a model that has no price. Both are exercised here against a stub
# that behaves the way llama.cpp and vLLM behave -- refusing `json_schema`,
# serving `json_object`, and reporting token usage with no billing attached.
#
# Loopback only. No vendor is contacted and no key is read.

_VALID_ANSWER = {
    "condition_flags": {"war_on_territory": False, "internal_conflict_level": "none",
                        "emergency_rule": False, "sovereign_stress": False},
    "ledger_scores": {"friction": 40, "order_uncertainty": 35,
                      "information_capacity": 60, "edge_vitality": None},
    "subscore_evidence": {"friction": ["a1"], "order_uncertainty": ["a2"],
                          "information_capacity": ["structural"]},
    "news_article_scores": [{"id": "a1", "impact": 20, "topic_group": "monetary"},
                            {"id": "a2", "impact": 45, "topic_group": "politics"}],
    "score_3m": 41, "score_12m": 43, "evidence_coverage": 70,
    "bullet_summary": "A stub answer, shaped like the real one.",
}


class _LocalEndpoint:
    """An OpenAI-compatible server with no strict mode. Threaded, loopback, stdlib.

    Records every request body so a test can assert on what the harness actually
    sent rather than on what `client.py` looks like it sends.
    """

    def __init__(self, *, answer=None, serve_strict=False):
        import http.server
        import threading

        self.requests = []
        self.answer = _VALID_ANSWER if answer is None else answer
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):  # silence the default stderr spew
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(
                    int(self.headers["Content-Length"])).decode("utf-8"))
                outer.requests.append(body)
                fmt = (body.get("response_format") or {}).get("type")
                if fmt == "json_schema" and not serve_strict:
                    # What llama.cpp's server and most OpenAI-compatible servers
                    # do with OpenAI's strict `json_schema` block.
                    return self._send(400, {"error": {
                        "message": "response_format.type json_schema is not supported",
                        "type": "invalid_request_error"}})
                content = (json.dumps(outer.answer) if isinstance(outer.answer, dict)
                           else outer.answer)
                self._send(200, {
                    "id": "stub", "object": "chat.completion", "model": "local-stub",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": content}}],
                    # Tokens, and no price anywhere. That is the whole point.
                    "usage": {"prompt_tokens": 1200, "completion_tokens": 180,
                              "total_tokens": 1380},
                })

            def _send(self, code, payload):
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self):
        return "http://127.0.0.1:%d/v1" % self._server.server_address[1]

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def local_endpoint():
    server = _LocalEndpoint()
    try:
        yield server
    finally:
        server.close()


# Three stand-in payloads for the three pinned anchors. `smoke` assembles the
# real ones from the corpus, which these tests have no access to and should not
# need: they are about the harness — routes, retries, seeds, where a gate result
# lands — and none of that depends on which week the evidence came from.
#
# A test double, deliberately not a production fallback. `_assemble_band` raises
# `SmokePayloadUnavailable` rather than reaching for anything like this, because
# a gate that silently runs small reports a pass for a request the candidate was
# never asked to satisfy. Stubbing it here is a test saying what it stands in
# for; a fallback in the module would be the module lying to its operator.
_STUB_BANDS = {
    "calm": ({"structural": {"gdp_growth_pct": 2.4, "gov_debt_pct_gdp": 41.2}},
             [{"id": "a1", "source": "a national daily",
               "published_at": "2019-06-03",
               "title": "Budget surplus widens on stronger receipts",
               "digest": {"what_happened": "Receipts beat forecasts.",
                          "actors": "the finance ministry", "numbers": "1.2% of GDP",
                          "transmission": "fiscal space",
                          "directly_about_country": True, "stage1_severity": 10}}]),
    "moderate": ({"structural": {"gdp_growth_pct": 2.1, "gov_debt_pct_gdp": 117.2}},
                 [{"id": "a1", "source": "a news agency",
                   "published_at": "2019-03-11",
                   "title": "Regulator approves cross-border rail concession",
                   "digest": {"what_happened": "A concession was approved.",
                              "actors": "the transport regulator",
                              "numbers": "30-year term",
                              "transmission": "infrastructure investment",
                              "directly_about_country": True,
                              "stage1_severity": 40}}]),
    "stressed": ({"structural": {"gdp_growth_pct": -4.8, "cpi_inflation_pct": 61.3}},
                 [{"id": "a1", "source": "a national daily",
                   "published_at": "2018-08-13",
                   "title": "Currency falls a further fifth as reserves are drawn down",
                   "digest": {"what_happened": "The currency fell sharply.",
                              "actors": "the central bank", "numbers": "-21%",
                              "transmission": "import costs",
                              "directly_about_country": True,
                              "stage1_severity": 88}}]),
}


@pytest.fixture
def stub_bands(monkeypatch):
    """`_assemble_band` without a corpus behind it."""
    monkeypatch.setattr(
        bakeoff, "_assemble_band",
        lambda band: (_STUB_BANDS[band][0], _STUB_BANDS[band][1], ()))
    return _STUB_BANDS


@pytest.fixture
def local_candidate(monkeypatch, local_endpoint, stub_bands):
    """`local-template` pointed at the stub, registered the way a real one would be."""
    monkeypatch.setenv("SCORING_LOCAL_KEY", "not-a-real-key")
    monkeypatch.setitem(bakeoff.CANDIDATES, "local-stub", {
        "arm": "scoring",
        "note": "the stub",
        "env": {"SCORING_MODEL": "local-stub",
                "SCORING_BASE_URL": local_endpoint.base_url},
        "key_env": "SCORING_LOCAL_KEY",
        "key_target": "SCORING_API_KEY",
    })
    return local_endpoint


class TestALocalEndpointIsJustACandidateEntry:

    def test_the_template_carries_an_unreachable_url_on_purpose(self):
        """A template that resolves is a template somebody runs by accident."""
        spec = bakeoff.CANDIDATES["local-template"]
        assert spec["env"]["SCORING_BASE_URL"] == "http://127.0.0.1:1/v1"
        assert spec["env"]["SCORING_MODEL"] == "REPLACE-ME"
        # It must not borrow the vendor key: a local run has to work when
        # OPENAI_API_KEY is absent, which is the whole situation it is for.
        assert spec["key_env"] != "OPENAI_API_KEY"
        assert spec["key_target"] == "SCORING_API_KEY"

    def test_the_endpoint_and_its_dummy_key_both_resolve(self, local_candidate):
        with bakeoff.candidate_env("local-stub") as env:
            assert env["SCORING_BASE_URL"] == local_candidate.base_url
            assert os.environ["SCORING_API_KEY"] == "not-a-real-key"
            assert os.environ["SCORING_MODEL"] == "local-stub"
        assert "SCORING_BASE_URL" not in os.environ
        assert "SCORING_API_KEY" not in os.environ


class TestTheSchemaGateAgainstAnEndpointWithNoStrictMode:
    """The gate hard-coded `strict=True`, so every local model failed it.

    Round 2 measured DeepSeek and MiniMax through `json_object` with local
    validation and that code was never committed, so the next endpoint without
    strict mode would have had it written a third time.
    """

    def test_it_falls_back_and_says_which_route_answered(self, local_candidate):
        got = bakeoff.smoke("local-stub", repeats=2, bands=("moderate",))
        assert got["schema"]["passed"] is True
        assert got["schema"]["route"] == "json_object"
        # A pass through the wider route is not the production contract, and the
        # verdict has to carry that or the two read alike.
        assert "strict mode unavailable" in got["schema"]["error"]
        assert any(r.get("response_format", {}).get("type") == "json_schema"
                   for r in local_candidate.requests), "strict was never attempted"
        assert any(r.get("response_format", {}).get("type") == "json_object"
                   for r in local_candidate.requests)

    def test_an_answer_the_grammar_would_accept_and_the_schema_does_not(
            self, local_candidate):
        """The reason local validation runs even under guided decoding.

        No grammar backend enforces `minimum`/`maximum` -- a context-free grammar
        cannot express a numeric range -- so a guided-JSON endpoint will happily
        emit `score_12m: 250` and call it schema-compliant.
        """
        local_candidate.answer = dict(_VALID_ANSWER, score_12m=250)
        got = bakeoff.smoke("local-stub", repeats=2, bands=("moderate",))
        assert got["schema"]["passed"] is False
        assert "250" in got["schema"]["error"] or "maximum" in got["schema"]["error"]

    def test_a_missing_required_field_is_caught(self, local_candidate):
        local_candidate.answer = {k: v for k, v in _VALID_ANSWER.items()
                                  if k != "ledger_scores"}
        got = bakeoff.smoke("local-stub", repeats=2, bands=("moderate",))
        assert got["schema"]["passed"] is False
        assert got["determinism"]["scored_match_rate"] is None

    def test_seed_and_temperature_reach_the_endpoint_unchanged(self, local_candidate):
        bakeoff.smoke("local-stub", repeats=2, bands=("moderate",))
        sent = local_candidate.requests[-1]
        assert sent["temperature"] == 0.0
        assert sent["seed"] == 42
        assert sent["model"] == "local-stub"

    def test_all_three_bands_run_and_are_kept_separately(self, local_candidate):
        got = bakeoff.smoke("local-stub", repeats=2)
        assert set(got["determinism"]["by_band"]) == set(bakeoff._SMOKE_ANCHORS)
        assert got["determinism"]["bands"] == 3
        # Per-repeat scores, not just a spread. A canary has to say what moved.
        for band in got["determinism"]["by_band"].values():
            assert band["scores"] == [43, 43]

    def test_the_three_payloads_are_different_evidence(self, stub_bands):
        """Three payloads that scored alike would measure one band three times."""
        rendered = {b: bakeoff.smoke_prompt(b) for b in bakeoff._SMOKE_ANCHORS}
        assert len(set(rendered.values())) == 3
        assert "61.3" in rendered["stressed"]      # the stressed inflation print
        assert "41.2" in rendered["calm"]          # the calm debt ratio

    def test_the_gate_records_how_big_its_own_request_was(self, local_candidate):
        """The number whose absence let a 2,980-token gate certify a 13,459-token
        run for months. Neither figure was ever written beside the other."""
        got = bakeoff.smoke("local-stub", repeats=1, bands=("moderate",))
        report = got["payload"]
        assert set(report) == set(bakeoff._SMOKE_ANCHORS), (
            "the report covers every band, not only the ones just run — a band "
            "that was skipped still has a size worth stating")
        moderate = report["moderate"]
        assert moderate["anchor"] == "US 2019-03-11"
        assert moderate["why"], "an anchor with no stated reason invites a swap"
        assert moderate["prompt_tokens"] > 0
        assert moderate["counted_by"] in ("tiktoken", "chars/4 estimate")


class TestTheGateAssemblesARealPayload:
    """The gate sends what the run sends, or it does not run.

    Measured 2026-08-30: the canned payloads rendered at 2,962 / 2,980 / 2,987
    tokens against a dispatched prompt of 11,264 / 12,734 / 13,459. Gates 1 and
    2 are the two the acceptance bar says to stop on, so a candidate was being
    admitted or rejected on a request a fifth the size of the real one — and for
    a self-served model that is the wrong end of the range to test.
    """

    def test_the_anchors_are_pinned_and_each_says_why(self):
        """They are part of the gate's meaning, not an implementation detail."""
        assert set(bakeoff._SMOKE_ANCHORS) == {"calm", "moderate", "stressed"}
        for band, (iso2, as_of, why) in bakeoff._SMOKE_ANCHORS.items():
            assert len(iso2) == 2 and iso2.isupper(), band
            assert isinstance(as_of, _dt.date), band
            assert why and len(why) > 20, f"{band} does not say why it was chosen"
        # Three countries, so the gate does not report one country's evidence
        # texture as the instrument's.
        assert len({a[0] for a in bakeoff._SMOKE_ANCHORS.values()}) == 3

    def test_an_unavailable_payload_refuses_rather_than_running_small(
            self, monkeypatch):
        """No canned fallback. A gate that quietly runs small reports a pass for
        a request the candidate was never asked to satisfy, and on disk that
        pass is indistinguishable from a real one."""
        monkeypatch.setattr(bakeoff, "_assemble_band",
                            lambda band: (_ for _ in ()).throw(
                                bakeoff.SmokePayloadUnavailable("no corpus")))
        with pytest.raises(bakeoff.SmokePayloadUnavailable):
            bakeoff.smoke_prompt("moderate")

    def test_the_full_text_block_is_rendered_not_stubbed_out(self, monkeypatch):
        """Three quarters of the gap was this one line."""
        article = {"id": "a1", "source": "x", "published_at": "2019-03-11",
                   "title": "A title",
                   "text": "BODYMARKER " * 400,
                   "digest": {"what_happened": "something", "actors": "someone",
                              "numbers": "1", "transmission": "a channel",
                              "directly_about_country": True,
                              "stage1_severity": 10}}
        monkeypatch.setattr(bakeoff, "_assemble_band",
                            lambda band: ({"structural": {}}, [article], ("a1",)))
        rendered = bakeoff.smoke_prompt("moderate")
        assert "BODYMARKER" in rendered, "the full text never reached the prompt"
        assert "(no full-text articles supplied)" not in rendered

    def test_the_prompt_is_masked_the_way_the_run_masks_it(self, monkeypatch):
        """A gate rendering unmasked prose measures a prompt production never
        sends -- and `assert_clean` is the gate the run itself relies on."""
        leaky = {"id": "a1", "source": "x", "published_at": "2018-08-13",
                 "title": "Turkey raises rates as the lira falls",
                 "digest": {"what_happened": "Ankara acted", "actors": "Erdogan",
                            "numbers": "1", "transmission": "a channel",
                            "directly_about_country": True, "stage1_severity": 50}}
        monkeypatch.setattr(bakeoff, "_assemble_band",
                            lambda band: ({"structural": {}}, [leaky], ()))
        monkeypatch.setitem(bakeoff._SMOKE_ANCHORS, "stressed",
                            ("TR", _dt.date(2018, 8, 13), "the masking check"))
        rendered = bakeoff.smoke_prompt("stressed")
        for term in ("Turkey", "Ankara", "lira"):
            assert term not in rendered, f"{term} survived into the gate prompt"


class TestAnUnpricedModelReportsTokensAndNotDollars:
    """`usage.price` bills an unknown id at gpt-4o's rate, by design.

    Right for a governor -- an unknown model should stop the run early rather
    than spend freely. Wrong for a comparison: it turns "nobody knows what this
    costs" into a confident dollar figure, which is the shape of the criterion
    that was measuring the prompt cache.
    """

    def test_is_priced_separates_a_real_rate_from_the_fallback(self):
        assert usage.is_priced("gpt-4o-2024-08-06") is True
        assert usage.is_priced("local-stub") is False
        assert usage.is_priced("") is False

    def test_the_fallback_rate_still_applies_to_the_budget(self):
        """The governor must keep stopping early. Only the reporting changes."""
        assert usage.price("local-stub", 1_000_000, 0) == pytest.approx(2.50)

    def test_cost_summary_withholds_dollars_and_keeps_tokens(self):
        rows = [{"status": "complete", "model_id": "local-stub", "calls": 1,
                 "spend_usd": 0.031, "input_tokens": 1200, "output_tokens": 180,
                 "cached_tokens": 0, "seconds": 4.5, "utc_hour": 3}]
        got = bakeoff.cost_summary(rows)
        assert got["priced"] is False
        assert got["spend_usd"] is None
        assert got["per_snapshot_usd"] is None
        assert got["cache_neutral_per_snapshot_usd"] is None
        # What is real is still reported.
        assert got["input_tokens_per_snapshot"] == 1200
        assert got["output_tokens_per_snapshot"] == 180
        assert got["seconds_per_snapshot"] == 4.5

    def test_a_priced_model_is_unaffected(self):
        rows = [{"status": "complete", "model_id": "gpt-4o-2024-08-06", "calls": 1,
                 "spend_usd": 0.031, "input_tokens": 1200, "output_tokens": 180,
                 "cached_tokens": 0, "seconds": 4.5, "utc_hour": 3}]
        got = bakeoff.cost_summary(rows)
        assert got["priced"] is True
        assert got["spend_usd"] == 0.031
        assert got["cache_neutral_per_snapshot_usd"] > 0


class TestAGateResultIsPersisted:
    """The consumer-side test for the writer that failed silently.

    24 of the 26 committed bake-off files carry `gates: {}`. The gate is a
    property of the candidate -- `smoke` runs on a canned payload with no
    country in it -- but it was written into a window-scoped file by one branch
    of the CLI, so smoking under one window left the other window's file saying
    the gate had never run, and any writer that was not that branch overwrote it
    with the empty default from `_wrap`.
    """

    @pytest.fixture
    def results_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bakeoff, "RESULTS_DIR", tmp_path)
        return tmp_path

    def test_save_carries_forward_gates_the_payload_does_not_have(self, results_dir):
        bakeoff.save("cand", {"candidate": "cand", "gates": {"schema": {"passed": True}},
                              "rows": []})
        # A fresh payload from `_wrap`, exactly what `score_anchors` produces.
        bakeoff.save("cand", {"candidate": "cand", "gates": {}, "rows": [1, 2]})
        got = bakeoff.load("cand")
        assert got["gates"] == {"schema": {"passed": True}}, "the gate was dropped"
        assert got["rows"] == [1, 2], "the new rows were lost"

    def test_a_caller_that_means_to_clear_them_still_can(self, results_dir):
        bakeoff.save("cand", {"candidate": "cand", "gates": {"schema": {}},
                              "rows": []})
        bakeoff.save("cand", {"candidate": "cand", "gates": None, "rows": []})
        assert bakeoff.load("cand")["gates"] is None

    def test_gates_land_in_every_window_the_candidate_has_a_file_in(
            self, results_dir, monkeypatch):
        for window in ("US-2019", "TR-2018"):
            (results_dir / window).mkdir()
            (results_dir / window / "cand.json").write_text(
                json.dumps({"candidate": "cand", "gates": {}, "rows": []}),
                encoding="utf-8")
        monkeypatch.setattr(bakeoff, "COUNTRY", "US")
        monkeypatch.setattr(bakeoff, "SINCE", _dt.date(2019, 1, 1))

        bakeoff.save_gates("cand", {"schema": {"passed": True, "route": "strict"}})

        for window in ("US-2019", "TR-2018"):
            got = json.loads((results_dir / window / "cand.json").read_text("utf-8"))
            assert got["gates"]["schema"]["passed"] is True, window

    def test_a_smoke_run_writes_a_non_empty_gates_block(self, results_dir,
                                                        local_candidate, monkeypatch):
        """End to end: run the gate, and assert it is on disk afterwards.

        This is the test whose absence let the defect stay invisible. It asserts
        the thing a reader of the committed files actually depends on.
        """
        monkeypatch.setattr(bakeoff, "COUNTRY", "LOCAL")
        monkeypatch.setattr(bakeoff, "SINCE", _dt.date(2019, 1, 1))
        result = bakeoff.smoke("local-stub", repeats=2, bands=("moderate",))
        bakeoff.save_gates("local-stub",
                           {k: result[k] for k in ("schema", "determinism", "cost")})

        stored = json.loads(
            (results_dir / "LOCAL-2019" / "local-stub.json").read_text("utf-8"))
        assert stored["gates"], "a smoke run left an empty gates block"
        assert stored["gates"]["schema"]["route"] == "json_object"
        assert stored["gates"]["determinism"]["scores"] == [43, 43]
        assert stored["gates"]["cost"]["priced"] is False


class TestCapturedUnderIsWrittenOnce:
    """The stamp records what produced the rows, so only a run that produced
    rows may write it.

    `b128aad` added a gates block to `gpt-4.1.json` and `gpt-4o.json` without
    re-scoring a single anchor, and carried `git_sha` from the 08-27 trees that
    did score them to the 08-29 tree that did not. The vintage fix landed in
    between, so both files came to claim a post-fix tree for pre-fix rows — and
    the field that would have exposed that was the field the write destroyed.
    """

    @pytest.fixture
    def results_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bakeoff, "RESULTS_DIR", tmp_path)
        return tmp_path

    def test_a_second_write_cannot_alter_an_existing_value(self, results_dir):
        bakeoff.save("cand", {"candidate": "cand", "rows": [1],
                              "captured_under": {"git_sha": "aaa",
                                                 "PAYLOAD_VERSION": "p2"}})
        bakeoff.save("cand", {"candidate": "cand", "rows": [1],
                              "captured_under": {"git_sha": "bbb",
                                                 "PAYLOAD_VERSION": "p9"}})
        stored = bakeoff.load("cand")["captured_under"]
        assert stored["git_sha"] == "aaa", "the scoring tree was overwritten"
        assert stored["PAYLOAD_VERSION"] == "p2"

    def test_a_new_field_may_still_be_added_alongside_it(self, results_dir):
        bakeoff.save("cand", {"candidate": "cand", "rows": [1],
                              "captured_under": {"git_sha": "aaa"}})
        bakeoff.save("cand", {"candidate": "cand", "rows": [1],
                              "captured_under": {"git_sha": "bbb",
                                                 "PAYLOAD_FINGERPRINT": "f00d"}})
        stored = bakeoff.load("cand")["captured_under"]
        assert stored["git_sha"] == "aaa"
        assert stored["PAYLOAD_FINGERPRINT"] == "f00d", "a new key was dropped"

    def test_the_first_write_stamps_freely(self, results_dir):
        bakeoff.save("cand", {"candidate": "cand", "rows": [],
                              "captured_under": {"git_sha": "aaa"}})
        assert bakeoff.load("cand")["captured_under"]["git_sha"] == "aaa"

    def test_the_gates_writer_cannot_restamp_either(self, results_dir,
                                                    monkeypatch):
        """`save_gates` and the smoke branch of `main` both bypassed `save`.

        The one that overwrote the reference arms was the third writer, so a
        guard on `save` alone would have left the defect exactly where it was.
        """
        for window in ("US-2019", "TR-2018"):
            (results_dir / window).mkdir()
            (results_dir / window / "cand.json").write_text(
                json.dumps({"candidate": "cand", "gates": {}, "rows": [1],
                            "captured_under": {"git_sha": "aaa"}}),
                encoding="utf-8")
        monkeypatch.setattr(bakeoff, "COUNTRY", "US")
        monkeypatch.setattr(bakeoff, "SINCE", _dt.date(2019, 1, 1))

        bakeoff.save_gates("cand", {"schema": {"passed": True}})

        for window in ("US-2019", "TR-2018"):
            got = json.loads((results_dir / window / "cand.json").read_text("utf-8"))
            assert got["gates"]["schema"]["passed"] is True, window
            assert got["captured_under"]["git_sha"] == "aaa", window

    def test_the_committed_reference_arms_name_the_tree_that_scored_them(self):
        """The four files, corrected, against the SHAs recovered from git.

        Not a round-trip through the guard — the guard cannot undo a write that
        already happened. This asserts the correction itself, so a future
        restamp of these particular files fails here.
        """
        expected = {("US-2019", "gpt-4.1"): "d063fc4fc9a57bf79ae4ba89a288d1e6df06a1ee",
                    ("US-2019", "gpt-4o"): "d063fc4fc9a57bf79ae4ba89a288d1e6df06a1ee",
                    ("TR-2018", "gpt-4.1"): "30e07ef90801536a3659c5c99226f773354f1db0",
                    ("TR-2018", "gpt-4o"): "30e07ef90801536a3659c5c99226f773354f1db0"}
        for (window, name), sha in expected.items():
            path = bakeoff.RESULTS_DIR / window / f"{name}.json"
            if not path.exists():   # a checkout without the committed arms
                continue
            arm = json.loads(path.read_text(encoding="utf-8"))
            assert arm["captured_under"]["git_sha"] == sha, f"{window}/{name}"
            assert arm.get("captured_under_note"), (
                f"{window}/{name} was corrected and does not say so")


class TestWhatAGrammarWillNotEnforce:
    """The one risk the loopback stub cannot discover, checked statically.

    A stub can prove the harness handles an endpoint with no strict mode. It
    cannot prove anything about how a real `guided_json` / GBNF backend compiles
    `RISK_SCHEMA_V3`, because it does not compile it. This reads the schema
    instead and names what no context-free grammar can express.
    """

    def test_the_bounds_the_scores_depend_on_are_all_flagged(self):
        risks = bakeoff.grammar_risks(ai_constants.RISK_SCHEMA_V3)
        joined = "\n".join(risks)
        for field in ("score_12m", "score_3m", "evidence_coverage"):
            assert f"$.{field}: minimum=0" in joined
            assert f"$.{field}: maximum=100" in joined
        assert "$.news_article_scores[].impact: maximum=100" in joined
        assert "$.bullet_summary: maxLength=800" in joined

    def test_the_four_union_types_are_reported_as_a_determinism_risk(self):
        """Rewriting them as `anyOf` made gpt-4o non-deterministic on demand.

        Same meaning, different grammar, 50x9 became 52x7 + 50x2. So how a
        backend compiles a union is load-bearing, and a local candidate cannot
        inherit the incumbent's determinism result across a different compiler.
        """
        risks = [r for r in bakeoff.grammar_risks(ai_constants.RISK_SCHEMA_V3)
                 if "union type" in r]
        assert len(risks) == 4
        assert all(r.startswith("$.ledger_scores.") for r in risks)

    def test_a_schema_with_nothing_a_grammar_misses_reports_nothing(self):
        """It must not simply always complain, or it says nothing."""
        assert bakeoff.grammar_risks({
            "type": "object",
            "properties": {"a": {"type": "string"},
                           "b": {"type": "array", "items": {"type": "boolean"}}},
            "required": ["a", "b"], "additionalProperties": False,
        }) == []

    def test_production_forwards_the_bounds_openai_does_not_enforce(self):
        """Named here because it is easy to assume strict mode covers them.

        LangChain passes `minimum`/`maximum` through verbatim under
        `strict: true`. They are not in OpenAI's enforced subset, so the
        production call has the same hole a local grammar would -- which is why
        `langchain_llm._from_100` clamps, and why that clamp is load-bearing
        rather than defensive decoration.
        """
        from backend.llm import langchain_llm

        assert langchain_llm._from_100(250) == 1.0
        assert langchain_llm._from_100(-40) == 0.0
        assert langchain_llm._from_100(None) is None


class TestTheRhoGateCatchesInversionsAndNotDisagreement:
    """Rank agreement demoted from ranking criterion to disaster detector.

    Agreement with the incumbent rewards a candidate for reproducing the
    incumbent's judgement including where it is wrong, and the reason to screen
    a candidate at all is that the incumbent might be. So the only thing gated
    is a candidate ranking the year backwards.
    """

    @staticmethod
    def _rows(scores, ledger=None, key="friction"):
        rows = []
        for i, s in enumerate(scores):
            led = {"friction": None, "order_uncertainty": None,
                   "information_capacity": None, "edge_vitality": None}
            if ledger is not None:
                led[key] = ledger[i]
            rows.append({"as_of": f"2019-01-{7 + i:02d}", "status": "complete",
                         "llm_score": s, "score_3m": s, "ledger_scores": led,
                         "condition_flags": {}, "lint": []})
        return rows

    def test_a_backwards_ranking_fails(self):
        forward = self._rows([0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
        backward = self._rows([0.6, 0.5, 0.4, 0.3, 0.2, 0.1])
        got = bakeoff.rho_gate(forward, backward)
        assert got["passed"] is False
        assert got["failures"]["llm_score"] < bakeoff.RHO_GATE_FLOOR

    def test_mere_disagreement_passes(self):
        """rho of 0.3 is not a finding. That is the whole demotion."""
        base = self._rows([0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
        noisy = self._rows([0.2, 0.1, 0.4, 0.3, 0.6, 0.5])
        got = bakeoff.rho_gate(base, noisy)
        assert got["passed"] is True
        assert 0.0 < got["worst_gated"] < 1.0

    def test_a_ledger_too_coarse_to_rank_is_excluded_and_named(self):
        """`edge_vitality` takes two values across all 52 US 2019 anchors.

        A rank correlation over two values is not a rank correlation, and
        gating on one fails gpt-4o against itself at -0.287 -- the same shape as
        the determinism gate that failed the reference and had to be rescued.
        """
        base = self._rows([0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
                          ledger=[0.5, 0.5, 0.5, 0.9, 0.9, 0.9])
        cand = self._rows([0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
                          ledger=[0.9, 0.9, 0.9, 0.5, 0.5, 0.5])
        got = bakeoff.rho_gate(base, cand)
        assert got["passed"] is True, "a two-value ledger must not disqualify"
        assert "friction" in got["excluded_as_too_coarse"]
        # Excluded, not silently dropped: the coarseness is itself a finding.
        assert got["excluded_as_too_coarse"]["friction"]["distinct"] == 2

    def test_a_ledger_with_enough_range_is_still_gated(self):
        base = self._rows([0.1] * 6, ledger=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
        cand = self._rows([0.1] * 6, ledger=[0.6, 0.5, 0.4, 0.3, 0.2, 0.1])
        got = bakeoff.rho_gate(base, cand)
        assert got["passed"] is False
        assert "friction" in got["failures"]


@pytest.mark.skipif(not (bakeoff.RESULTS_DIR / "US-2019" / "gpt-4o.json").exists(),
                    reason="the committed bake-off arms are not present")
class TestTheRhoGateAgainstTheArmsItWasCalibratedOn:
    """A gate is only allowed to disqualify anybody if the reference passes it.

    Committed data, no network. These are the numbers docs/scorer-acceptance.md
    quotes, so a change to either has to break this.
    """

    @staticmethod
    def _arm(window, name):
        return json.loads((bakeoff.RESULTS_DIR / window / f"{name}.json")
                          .read_text(encoding="utf-8"))["rows"]

    def test_the_reference_clears_its_own_gate_on_both_windows(self):
        for window in ("US-2019", "TR-2018"):
            got = bakeoff.rho_gate(self._arm(window, "p2-rebaseline"),
                                   self._arm(window, "gpt-4o"))
            assert got["passed"], f"{window}: the reference failed its own gate"

    def test_it_catches_the_inversion_it_exists_for(self):
        """gpt-4.1-nano's friction ledger, ordered backwards against gpt-4o."""
        got = bakeoff.rho_gate(self._arm("US-2019", "gpt-4o"),
                               self._arm("US-2019", "gpt-4.1-nano"))
        assert got["passed"] is False
        assert got["failures"]["friction"] == pytest.approx(-0.2283, abs=1e-3)

    def test_three_of_four_us_ledgers_are_too_coarse_to_rank(self):
        """Reported because it is a finding about the instrument, not the gate.

        Only `order_uncertainty` has enough range to rank on the ambiguous
        window. See deferred.md §3 -- `information` scores on one indicator and
        `edge` on 2.7 of four.
        """
        got = bakeoff.rho_gate(self._arm("US-2019", "p2-rebaseline"),
                               self._arm("US-2019", "gpt-4.1"))
        assert set(got["excluded_as_too_coarse"]) == {
            "friction", "information_capacity", "edge_vitality"}
        assert "order_uncertainty" in got["gated"]
