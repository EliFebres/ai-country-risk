"""Characterization tests for ``backend.utils.ai.policy`` — legal investability.

This file used to prove that floors raised scores, that a cap lowered a
subscore, and that a sanctions gate overrode everything. None of those rules
exist any more; the enforcement layer was deleted, and what is left is a lookup
that answers one question: may an investor lawfully hold this country's
securities on this date?

So the load-bearing property inverted, and it is the first thing tested below:
**this module cannot change a score.** There is no score in its inputs and none
in its outputs. Everything else here is the date arithmetic that decides when a
rule is in force, plus the malformed-input behavior that keeps a bad YAML file
from taking down a run.

No network, no database, no clock — ``as_of`` is always passed in, which is what
makes a backfill of an old date get that date's rules.
"""

import inspect
from datetime import date

import pytest

from backend.utils.ai import policy


# After Russia's effective_from (2022-06-06) in the real legal_restrictions.yaml.
GATED_DAY = date(2023, 1, 1)
# Before it.
PRE_GATE_DAY = date(2022, 1, 1)
# DE is not a sanctioned country.
PLAIN_DAY = date(2026, 1, 1)


class TestCannotTouchAScore:
    """The property the whole rewrite exists to guarantee."""

    def test_result_carries_no_score(self):
        result = policy.assess_investability(iso2="RU", as_of=GATED_DAY)
        assert set(result._fields) == {"non_investable", "legal_gate", "note", "applied_rules"}

    def test_takes_no_score_argument(self):
        # A score it cannot receive is a score it cannot change.
        params = set(inspect.signature(policy.assess_investability).parameters)
        assert params == {"iso2", "as_of"}

    def test_enforcement_symbols_are_gone(self):
        # Their absence is the contract; a re-added apply_policy would restore
        # the exact ambiguity (model number or rule number?) this removed.
        for name in ("apply_policy", "PolicyResult", "_floor", "_conflict_level",
                     "POLICY_PATH", "_load_policy"):
            assert not hasattr(policy, name), f"{name} should not exist any more"

    def test_risk_policy_yaml_is_gone(self):
        # The thresholds file went with the rules it configured. A stray copy
        # would read like live configuration to the next person editing it.
        assert not (policy.LEGAL_RULES_PATH.with_name("risk_policy.yaml")).exists()


class TestSanctionsBadge:
    def test_fires_for_a_sanctioned_country_in_force(self):
        result = policy.assess_investability(iso2="RU", as_of=GATED_DAY)
        assert result.non_investable is True
        assert result.legal_gate is not None
        assert set(result.legal_gate) == {"name", "rule", "sources"}

    def test_does_not_fire_before_effective_from(self):
        # effective_from is 2022-06-06.
        result = policy.assess_investability(iso2="RU", as_of=PRE_GATE_DAY)
        assert result.non_investable is False
        assert result.legal_gate is None
        assert result.note is None
        assert result.applied_rules == []

    def test_unsanctioned_country_is_clean(self):
        result = policy.assess_investability(iso2="DE", as_of=PLAIN_DAY)
        assert result.non_investable is False
        assert result.legal_gate is None

    def test_none_iso2_never_fires(self):
        assert policy.assess_investability(iso2=None, as_of=GATED_DAY).non_investable is False

    def test_lowercase_iso2_still_matches(self):
        assert policy.assess_investability(iso2="ru", as_of=GATED_DAY).non_investable is True

    def test_applied_rules_is_an_observation(self):
        result = policy.assess_investability(iso2="RU", as_of=GATED_DAY)
        assert result.applied_rules == [f"sanctions_badge:{result.legal_gate['name']}"]

    def test_note_states_the_score_is_unadjusted(self):
        # The badge shows in the sidebar, but the map tooltip and the rail show
        # only score + summary. The note is what carries the fact there — and it
        # must not repeat the old "forced to 1.0" claim, which is no longer true.
        note = policy.assess_investability(iso2="RU", as_of=GATED_DAY).note
        assert note and "not adjusted" in note
        assert "1.0" not in note
        assert "forced" not in note


class TestEffectiveFromParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2022-06-06", date(2022, 6, 6)),
            ("2022-06-06T00:00:00Z", date(2022, 6, 6)),
        ],
    )
    def test_parses_iso_dates(self, raw, expected):
        assert policy._parse_iso_date(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "not-a-date", "06/06/2022"])
    def test_unparseable_is_always_in_force(self, raw):
        # Fail-closed: a rule with a broken date is treated as already active,
        # because failing open would understate a legal restriction.
        assert policy._parse_iso_date(raw) == date.min


class TestMalformedRulesFile:
    def test_unreadable_file_degrades_to_no_badge(self, monkeypatch, tmp_path):
        monkeypatch.setattr(policy, "LEGAL_RULES_PATH", tmp_path / "missing.yaml")
        policy._load_legal_rules_index.cache_clear()
        try:
            assert policy._load_legal_rules_index() == {}
            assert policy.assess_investability(iso2="RU", as_of=GATED_DAY).non_investable is False
        finally:
            policy._load_legal_rules_index.cache_clear()

    def test_entry_without_the_trigger_does_not_fire(self, monkeypatch, tmp_path):
        path = tmp_path / "legal.yaml"
        path.write_text(
            "entries:\n"
            "  - iso2: XX\n"
            "    name: Example\n"
            "    rule: Something short of a prohibition\n"
            "    trigger: {set_score_1_0: false}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(policy, "LEGAL_RULES_PATH", path)
        policy._load_legal_rules_index.cache_clear()
        try:
            assert policy.assess_investability(iso2="XX", as_of=PLAIN_DAY).non_investable is False
        finally:
            policy._load_legal_rules_index.cache_clear()


class TestVersionStamp:
    def test_policy_version_marks_the_regime_change(self):
        # Stored rows are split on this: a p1.0 score went through floors and a
        # sanctions override, a p2.0 score is the model's own.
        assert policy.POLICY_VERSION == "p2.0-observe-only"

    def test_is_a_plain_constant_not_read_from_yaml(self):
        # risk_policy.yaml was deleted with the rules it configured; reading a
        # version out of a file that no longer exists would silently stamp
        # every row "unknown".
        assert isinstance(policy.POLICY_VERSION, str)
        assert "_load_policy" not in inspect.getsource(policy)
