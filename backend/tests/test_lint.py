"""Characterization tests for ``backend.utils.lint`` — the advisory tripwires.

Lint replaced enforcement, so it inherits enforcement's job of noticing when a
model flags a war and then scores a 44 — but not its job of doing anything about
it. Two properties matter, and they pull in opposite directions:

* **It must fire** on the contradictions that used to be silently overwritten.
  A tripwire that never trips is worse than no tripwire, because it reads like
  evidence that nothing is wrong.
* **It must stay quiet** otherwise. A rule that fires on every country trains
  the operator to ignore the log, at which point the real findings are invisible
  too.

And underneath both: lint returns findings, never scores. The moment something
here can change a number, the whole rewrite is undone.

Pure function, no I/O — every input is passed in.
"""

import inspect
from datetime import date

import pytest

from backend.utils import lint


AS_OF = date(2026, 7, 27)


def flags(**overrides) -> dict:
    """The condition_flags object as the schema defines it, all-clear."""
    base = {
        "war_on_territory": False,
        "internal_conflict_level": "none",
        "emergency_rule": False,
        "sovereign_stress": False,
    }
    base.update(overrides)
    return base


def check(**overrides):
    """Run lint for PT with quiet defaults."""
    kwargs = {
        "country_iso2": "PT",
        "as_of": AS_OF,
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


class TestQuietByDefault:
    def test_a_clean_country_produces_nothing(self):
        assert check() == []

    def test_all_none_scores_produce_nothing(self):
        # A failed model call has no scores to contradict its flags.
        assert check(score_3m=None, score_12m=None, ledger_scores={}) == []

    def test_missing_condition_flags_produce_nothing(self):
        assert check(condition_flags=None) == []

    def test_malformed_flags_are_inert(self):
        assert check(condition_flags="not a dict") == []
        assert check(condition_flags={"war_on_territory": "yes"}) == []


class TestWarDivergence:
    def test_fires_below_the_tripwire(self):
        findings = check(condition_flags=flags(war_on_territory=True), score_12m=44)
        assert rules(findings) == ["flag_score_divergence"]
        divergences = findings[0]["detail"]["divergences"]
        assert len(divergences) == 1
        assert divergences[0]["flag"] == "war_on_territory"
        assert divergences[0]["observed_score"] == 44
        assert divergences[0]["horizon"] == "12m"
        assert divergences[0]["tripwire"] == lint.WAR_SCORE_FLOOR

    def test_quiet_at_and_above_the_tripwire(self):
        at = lint.WAR_SCORE_FLOOR
        assert check(condition_flags=flags(war_on_territory=True), score_12m=at) == []
        assert check(condition_flags=flags(war_on_territory=True), score_12m=at + 20) == []

    def test_quiet_without_the_flag(self):
        assert check(score_12m=10) == []

    def test_quiet_when_the_score_is_missing(self):
        assert check(condition_flags=flags(war_on_territory=True), score_12m=None) == []


class TestSovereignStressDivergence:
    def test_fires_on_the_three_month_horizon(self):
        findings = check(condition_flags=flags(sovereign_stress=True), score_3m=30)
        assert rules(findings) == ["flag_score_divergence"]
        divergence = findings[0]["detail"]["divergences"][0]
        assert divergence["flag"] == "sovereign_stress"
        assert divergence["observed_score"] == 30
        assert divergence["horizon"] == "3m"

    def test_quiet_at_the_tripwire(self):
        at = lint.SOVEREIGN_STRESS_SCORE_FLOOR
        assert check(condition_flags=flags(sovereign_stress=True), score_3m=at) == []

    def test_reads_the_three_month_score_not_the_twelve(self):
        # A high 12m with a low 3m is exactly the case this catches.
        findings = check(condition_flags=flags(sovereign_stress=True),
                         score_3m=30, score_12m=95)
        assert len(findings[0]["detail"]["divergences"]) == 1


class TestCalmTakenAtFaceValue:
    def test_fires_when_suppressed_calm_meets_low_order_uncertainty(self):
        findings = check(suppressed_vol_flag=True,
                         ledger_scores={"order_uncertainty": 22})
        assert rules(findings) == ["calm_taken_at_face_value"]
        assert findings[0]["detail"]["order_uncertainty"] == 22

    def test_quiet_at_the_tripwire(self):
        at = lint.SUPPRESSED_CALM_UNCERTAINTY_FLOOR
        assert check(suppressed_vol_flag=True, ledger_scores={"order_uncertainty": at}) == []

    def test_quiet_when_the_flag_is_false(self):
        assert check(suppressed_vol_flag=False,
                     ledger_scores={"order_uncertainty": 22}) == []

    def test_null_flag_is_not_a_true(self):
        # None means an input was missing, not that calm was detected.
        assert check(suppressed_vol_flag=None,
                     ledger_scores={"order_uncertainty": 22}) == []

    def test_quiet_when_the_ledger_score_is_missing(self):
        assert check(suppressed_vol_flag=True, ledger_scores={}) == []


class TestNonInvestableAuditTrail:
    def test_records_an_entry_for_every_badged_country_day(self):
        findings = check(non_investable=True)
        assert rules(findings) == ["non_investable"]

    def test_the_entry_states_the_score_was_not_adjusted(self):
        detail = check(non_investable=True, score_12m=62)[0]["detail"]
        assert detail["score_12m"] == 62
        assert "not adjusted" in detail["note"]

    def test_is_not_a_contradiction(self):
        # It is bookkeeping, so it must not be logged at warning level.
        assert lint.check(
            country_iso2="RU", as_of=AS_OF, non_investable=True,
        )[0]["rule"] == "non_investable"


class TestMultipleFindings:
    def test_every_tripped_rule_is_reported(self):
        findings = check(
            condition_flags=flags(war_on_territory=True, sovereign_stress=True),
            score_12m=44, score_3m=30,
            suppressed_vol_flag=True, ledger_scores={"order_uncertainty": 22},
            non_investable=True,
        )
        # One divergence row carrying both contradictions, not two rows: they
        # share a rule name and `risk_lint` is keyed (country, as_of, rule).
        assert rules(findings) == [
            "flag_score_divergence", "calm_taken_at_face_value", "non_investable",
        ]
        assert len(findings) == len(set(rules(findings)))
        assert [d["flag"] for d in findings[0]["detail"]["divergences"]] == [
            "war_on_territory", "sovereign_stress",
        ]

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

    def test_every_finding_carries_its_key(self):
        for finding in check(condition_flags=flags(war_on_territory=True), score_12m=10):
            assert finding["country_iso2"] == "PT"
            assert finding["as_of"] == AS_OF
            assert finding["rule"] and isinstance(finding["detail"], dict)


class TestLintCannotChangeAScore:
    def test_returns_findings_not_scores(self):
        for finding in check(condition_flags=flags(war_on_territory=True), score_12m=44):
            assert set(finding) == {"country_iso2", "as_of", "rule", "detail"}

    def test_module_has_no_mutator(self):
        source = inspect.getsource(lint)
        assert "def apply" not in source
        # The tripwire constants are compared against, never assigned from.
        for constant in ("WAR_SCORE_FLOOR", "SOVEREIGN_STRESS_SCORE_FLOOR",
                         "SUPPRESSED_CALM_UNCERTAINTY_FLOOR"):
            assert f"= {constant}" not in source

    def test_thresholds_are_documented_as_advisory(self):
        assert "advisory" in lint.__doc__.lower()
        assert "never worth correcting" in lint.__doc__.lower()

    def test_is_pure(self):
        source = inspect.getsource(lint)
        for banned in ("date.today", "datetime.now", "requests", "psycopg2"):
            assert banned not in source

    def test_repeated_calls_are_identical(self):
        first = check(condition_flags=flags(war_on_territory=True), score_12m=44)
        second = check(condition_flags=flags(war_on_territory=True), score_12m=44)
        assert first == second


class TestLogFindings:
    def test_logs_each_finding(self, caplog):
        findings = check(condition_flags=flags(war_on_territory=True), score_12m=44)
        with caplog.at_level("INFO"):
            lint.log_findings(findings)
        assert "flag_score_divergence" in caplog.text
        assert "PT" in caplog.text

    def test_contradictions_warn_and_bookkeeping_informs(self, caplog):
        with caplog.at_level("INFO"):
            lint.log_findings(check(condition_flags=flags(war_on_territory=True), score_12m=44))
            lint.log_findings(check(non_investable=True))
        def levels_for(rule):
            return {r.levelname for r in caplog.records if rule in r.getMessage()}

        # A disagreement is worth a warning; the badge audit trail is not.
        assert levels_for("flag_score_divergence") == {"WARNING"}
        assert levels_for("non_investable") == {"INFO"}

    def test_empty_findings_log_nothing(self, caplog):
        with caplog.at_level("INFO"):
            lint.log_findings([])
        assert caplog.text == ""
