"""Tests for ``backend.utils.ai.policy`` — the enforcement layer.

These rules used to be prose inside the scoring prompt, which meant the only
way to check one was to spend an API call and hope. Now they are code, and this
is the file that proves a threshold does what the YAML says: floors raise both
horizons, the cap lowers one subscore, the sanctions gate overrides everything,
and none of it fabricates a score for a country the model failed to assess.

Non-mutation matters as much as the arithmetic: the caller persists the raw
dicts next to the gated ones, so a rule that edited its input in place would
silently destroy the raw record it was meant to preserve.
"""

from datetime import date

import pytest

from backend.utils.ai import policy


INFLATION = "Inflation (% y/y)"

# After Russia's effective_from (2022-06-06) in the real legal_restrictions.yaml.
GATED_DAY = date(2023, 1, 1)
# Any date works for the non-gate rules; DE is not a sanctioned country.
PLAIN_DAY = date(2026, 1, 1)


def subs(**overrides) -> dict:
    """A full set of mid-range subscores, so a floor visibly moves one."""
    base = {
        "conflict_war": 0.20,
        "political_stability": 0.30,
        "governance_corruption": 0.25,
        "macroeconomic_volatility": 0.30,
        "regulatory_uncertainty": 0.25,
    }
    base.update(overrides)
    return base


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


def run(iso2="DE", as_of=PLAIN_DAY, s12=0.40, s3=0.35,
        subscores=None, condition_flags=None, macro=None):
    return policy.apply_policy(
        iso2=iso2,
        as_of=as_of,
        raw_score_12m=s12,
        raw_score_3m=s3,
        raw_subscores=subs() if subscores is None else subscores,
        condition_flags=flags() if condition_flags is None else condition_flags,
        macro_facts={} if macro is None else macro,
    )


class TestNoRules:
    def test_scores_pass_through_untouched(self):
        got = run(s12=0.42, s3=0.37)
        assert got.score_12m == 0.42
        assert got.score_3m == 0.37
        assert got.applied_rules == []
        assert got.gate_note is None

    def test_subscores_pass_through(self):
        got = run()
        assert got.subscores == subs()


class TestWarOnTerritory:
    def test_floors_both_horizons(self):
        got = run(condition_flags=flags(war_on_territory=True))
        # A war is not a 12-month-only problem.
        assert got.score_12m == 0.90
        assert got.score_3m == 0.90

    def test_floors_conflict_war_subscore(self):
        got = run(condition_flags=flags(war_on_territory=True))
        assert got.subscores["conflict_war"] == 0.90

    def test_records_the_rule(self):
        got = run(condition_flags=flags(war_on_territory=True))
        assert "war_on_territory" in got.applied_rules

    def test_higher_raw_score_is_left_alone(self):
        # Floors raise; they never pull a genuinely worse assessment down.
        got = run(s12=0.97, s3=0.95, condition_flags=flags(war_on_territory=True))
        assert got.score_12m == 0.97
        assert got.score_3m == 0.95

    def test_untouched_subscores_stay_put(self):
        got = run(condition_flags=flags(war_on_territory=True))
        assert got.subscores["governance_corruption"] == 0.25


class TestInternalConflict:
    @pytest.mark.parametrize("level,overall,conflict", [
        ("A", 0.70, 0.80),
        ("B", 0.80, 0.88),
        # C sets no conflict_war floor of its own — B's is already 0.88 and C is
        # about nationwide effects, so the raw subscore stands.
        ("C", 0.90, 0.20),
    ])
    def test_levels_floor_correctly(self, level, overall, conflict):
        got = run(condition_flags=flags(internal_conflict_level=level))
        assert got.score_12m == overall
        assert got.score_3m == overall
        assert got.subscores["conflict_war"] == conflict
        assert f"internal_conflict_{level}" in got.applied_rules

    def test_c_beats_b_beats_a(self):
        a = run(condition_flags=flags(internal_conflict_level="A")).score_12m
        b = run(condition_flags=flags(internal_conflict_level="B")).score_12m
        c = run(condition_flags=flags(internal_conflict_level="C")).score_12m
        assert a < b < c

    def test_highest_floor_wins_when_combined_with_war(self):
        # War floors overall to 0.90 and conflict_war to 0.90; level A would
        # only reach 0.70/0.80. The stricter of the two must survive.
        got = run(condition_flags=flags(war_on_territory=True, internal_conflict_level="A"))
        assert got.score_12m == 0.90
        assert got.subscores["conflict_war"] == 0.90
        assert "war_on_territory" in got.applied_rules
        assert "internal_conflict_A" in got.applied_rules

    def test_none_level_is_inert(self):
        got = run(condition_flags=flags(internal_conflict_level="none"))
        assert got.applied_rules == []


class TestPoliticalStabilityCap:
    def test_caps_at_045(self):
        got = run(subscores=subs(political_stability=0.80))
        assert got.subscores["political_stability"] == 0.45
        assert "political_stability_cap" in got.applied_rules

    def test_below_the_cap_is_untouched(self):
        got = run(subscores=subs(political_stability=0.30))
        assert got.subscores["political_stability"] == 0.30
        assert got.applied_rules == []

    def test_released_by_emergency_rule(self):
        got = run(subscores=subs(political_stability=0.80),
                  condition_flags=flags(emergency_rule=True))
        assert got.subscores["political_stability"] == 0.80
        assert "political_stability_cap" not in got.applied_rules

    def test_released_by_sovereign_stress(self):
        got = run(subscores=subs(political_stability=0.80),
                  condition_flags=flags(sovereign_stress=True))
        assert got.subscores["political_stability"] == 0.80

    def test_cap_does_not_touch_the_overall_score(self):
        got = run(s12=0.60, subscores=subs(political_stability=0.80))
        assert got.score_12m == 0.60


class TestInflationFloors:
    def test_below_lowest_tier_does_nothing(self):
        got = run(macro={INFLATION: 24.0})
        assert got.score_12m == 0.40
        assert got.applied_rules == []

    def test_boundary_is_inclusive(self):
        got = run(macro={INFLATION: 25.0})
        assert got.score_12m == 0.55
        assert got.score_3m == 0.55
        assert got.subscores["macroeconomic_volatility"] == 0.70
        assert "inflation>=25" in got.applied_rules

    def test_middle_tier(self):
        got = run(macro={INFLATION: 41.2})
        assert got.score_12m == 0.65
        assert got.subscores["macroeconomic_volatility"] == 0.80

    def test_top_tier(self):
        got = run(macro={INFLATION: 85.0})
        assert got.score_12m == 0.80
        # The top tier sets no macro_vol floor, so the raw subscore stands.
        assert got.subscores["macroeconomic_volatility"] == 0.30

    def test_only_one_tier_fires(self):
        got = run(macro={INFLATION: 85.0})
        infl_rules = [r for r in got.applied_rules if r.startswith("inflation")]
        assert infl_rules == ["inflation>=80"]

    def test_missing_indicator_is_inert(self):
        got = run(macro={"GDP growth (%)": 3.1})
        assert got.applied_rules == []

    def test_non_numeric_value_is_inert(self):
        got = run(macro={INFLATION: "n/a"})
        assert got.applied_rules == []


class TestSanctionsGate:
    def test_overrides_both_horizons(self):
        got = run(iso2="RU", as_of=GATED_DAY, s12=0.30, s3=0.20)
        assert got.score_12m == 1.0
        assert got.score_3m == 1.0

    def test_populates_gate_note(self):
        got = run(iso2="RU", as_of=GATED_DAY)
        assert got.gate_note is not None
        assert "Russia" in got.gate_note
        assert any(r.startswith("sanctions_gate:") for r in got.applied_rules)

    def test_not_yet_in_force_does_not_fire(self):
        # effective_from is 2022-06-06.
        got = run(iso2="RU", as_of=date(2022, 1, 1), s12=0.30)
        assert got.score_12m == 0.30
        assert got.gate_note is None

    def test_unsanctioned_country_unaffected(self):
        got = run(iso2="DE", as_of=GATED_DAY, s12=0.30)
        assert got.score_12m == 0.30
        assert got.gate_note is None

    def test_beats_everything_below_it(self):
        got = run(iso2="RU", as_of=GATED_DAY, s12=0.10, s3=0.10,
                  condition_flags=flags(internal_conflict_level="A"),
                  macro={INFLATION: 90.0})
        assert got.score_12m == 1.0
        assert got.score_3m == 1.0

    def test_does_not_mutate_the_callers_dicts(self):
        # The caller persists these raw dicts next to the gated ones; a rule
        # that edited them in place would destroy the record it exists to keep.
        raw_subscores = subs(political_stability=0.80)
        condition_flags = flags(war_on_territory=True)
        macro_facts = {INFLATION: 90.0}
        before_subs = dict(raw_subscores)
        before_flags = dict(condition_flags)
        before_macro = dict(macro_facts)

        policy.apply_policy(
            iso2="RU",
            as_of=GATED_DAY,
            raw_score_12m=0.30,
            raw_score_3m=0.25,
            raw_subscores=raw_subscores,
            condition_flags=condition_flags,
            macro_facts=macro_facts,
        )

        assert raw_subscores == before_subs
        assert condition_flags == before_flags
        assert macro_facts == before_macro


class TestFailedCallPassesThrough:
    def test_none_scores_stay_none(self):
        got = run(s12=None, s3=None, condition_flags=flags(war_on_territory=True),
                  macro={INFLATION: 90.0})
        assert got.score_12m is None
        assert got.score_3m is None

    def test_gate_does_not_resurrect_a_failed_call(self):
        # A None score means the caller skips the country; forcing it to 1.0
        # would write a score with no assessment behind it.
        got = run(iso2="RU", as_of=GATED_DAY, s12=None, s3=None)
        assert got.score_12m is None
        assert got.score_3m is None

    def test_none_subscore_is_not_manufactured_by_a_floor(self):
        got = run(subscores=subs(conflict_war=None),
                  condition_flags=flags(war_on_territory=True))
        assert got.subscores["conflict_war"] is None


class TestMalformedInput:
    def test_missing_flag_keys(self):
        got = run(condition_flags={})
        assert got.applied_rules == []
        assert got.score_12m == 0.40

    def test_wrong_types_are_inert(self):
        # "true" is not True: only a real boolean fires a flag.
        got = run(condition_flags={"war_on_territory": "true",
                                   "internal_conflict_level": 7,
                                   "emergency_rule": None,
                                   "sovereign_stress": []})
        assert got.applied_rules == []

    def test_unknown_level_string_is_inert(self):
        got = run(condition_flags=flags(internal_conflict_level="Z"))
        assert got.applied_rules == []
        assert got.score_12m == 0.40

    def test_condition_flags_not_a_dict(self):
        got = run(condition_flags="nope")
        assert got.applied_rules == []

    def test_empty_subscores(self):
        got = run(subscores={}, condition_flags=flags(war_on_territory=True))
        # Nothing to floor, but the overall floors still apply.
        assert got.score_12m == 0.90
        assert got.subscores["conflict_war"] is None


class TestPolicyVersion:
    def test_is_stamped_from_the_yaml(self):
        # Stamped on every snapshot, so it must never silently become "unknown".
        assert policy.POLICY_VERSION.startswith("p")
