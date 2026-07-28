"""The observe-only guarantee: no code path alters a model score.

This is the single most important property of the friction-framework rewrite,
and it is the easiest one to lose. Nothing about a re-added floor looks wrong:
the pipeline runs, the snapshot writes, the front-end renders a number. It is
only wrong in the sense that nobody can any longer tell which part of a stored
score came from the model and which came from a rule — and by the time that
matters, there is a year of history built on it.

So this file attacks the property from three angles:

1. **Behavioral** — a sanctioned country scores end to end and keeps its own
   number, gaining a badge instead of a 1.0. This is the fixture the old gate
   would have failed.
2. **Structural** — the only assignment to the ``score`` key in the whole
   backend is the one in ``langchain_llm`` that reads the model's ``score_12m``.
3. **Regression** — the deleted enforcement symbols stay deleted.

Fakes, not mocks, and no network: the model call is monkeypatched.
"""

import pathlib
import re
from datetime import date

import pytest

from backend.utils.ai import constants as ai_constants
from backend.utils.ai import langchain_llm as llm
from backend.utils.ai import policy


BACKEND = pathlib.Path(__file__).resolve().parents[1]

# After Russia's effective_from (2022-06-06) in the real legal_restrictions.yaml.
SANCTIONED_DAY = date(2023, 1, 1)


def model_output(**overrides) -> dict:
    """A complete, schema-shaped model reply."""
    base = {
        "condition_flags": {
            "war_on_territory": True,
            "internal_conflict_level": "none",
            "emergency_rule": False,
            "sovereign_stress": True,
        },
        "ledger_scores": {
            "friction": 71, "order_uncertainty": 83,
            "information_capacity": 29, "edge_vitality": 34,
        },
        "subscore_evidence": {
            "friction": ["Tax revenue (% GDP)"],
            "order_uncertainty": ["a1"],
            "information_capacity": ["Statistical performance (0–100)"],
        },
        "news_article_scores": [{"id": "a1", "impact": 88, "topic_group": "t"}],
        "score_3m": 77,
        "score_12m": 62,
        "evidence_coverage": 55,
        "bullet_summary": "Model summary.",
    }
    base.update(overrides)
    return base


class _FakeStructured:
    def __init__(self, reply):
        self.reply = reply

    def invoke(self, _messages):
        return self.reply


class _FakeChat:
    def __init__(self, structured):
        self._structured = structured

    def with_structured_output(self, **_kwargs):
        return self._structured


@pytest.fixture
def scored(monkeypatch):
    """Score a country against a canned model reply. Returns a callable."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def run(iso2="RU", as_of=SANCTIONED_DAY, **overrides):
        reply = model_output(**overrides)
        monkeypatch.setattr(
            llm.ai_client, "build_chat", lambda _key: _FakeChat(_FakeStructured(reply)),
        )
        payload = {"_meta": {"country": iso2, "as_of": as_of.isoformat()}}
        return llm.country_llm_score(
            country_display="Test Country", payload=payload,
            articles=[{"id": "a1", "title": "T", "text": "body"}],
            as_of=as_of, fulltext_ids=["a1"],
        )

    return run


class TestSanctionedCountryKeepsItsScore:
    """The fixture the deleted gate would have failed."""

    def test_score_is_the_models_own(self, scored):
        out = scored(iso2="RU")
        assert out["score"] == pytest.approx(0.62)   # 62/100, not 1.0
        assert out["score_3m"] == pytest.approx(0.77)

    def test_gains_the_badge_instead(self, scored):
        out = scored(iso2="RU")
        assert out["non_investable"] is True
        assert out["legal_gate"]["name"]

    def test_the_old_forced_value_never_appears(self, scored):
        out = scored(iso2="RU")
        for key in ("score", "score_3m", "raw_score_12m", "raw_score_3m"):
            assert out[key] != 1.0

    def test_applied_rules_records_a_badge_not_a_gate(self, scored):
        out = scored(iso2="RU")
        assert out["applied_rules"] == [f"sanctions_badge:{out['legal_gate']['name']}"]
        assert not any("gate:" in r for r in out["applied_rules"])

    def test_summary_carries_the_restriction_without_claiming_a_forced_score(self, scored):
        summary = scored(iso2="RU")["bullet_summary"]
        assert "Legally non-investable" in summary
        assert "not adjusted" in summary
        assert "Model summary." in summary
        assert "forced" not in summary

    def test_unsanctioned_country_is_untouched_and_unbadged(self, scored):
        out = scored(iso2="DE")
        assert out["score"] == pytest.approx(0.62)
        assert out["non_investable"] is False
        assert out["legal_gate"] is None
        assert out["bullet_summary"] == "Model summary."

    def test_a_failed_call_is_not_resurrected_by_the_badge(self, monkeypatch):
        # A sanctioned country whose model call failed must stay unscored. The
        # old gate would have written a 1.0 with no assessment behind it.
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        def boom(_key):
            raise RuntimeError("model down")

        monkeypatch.setattr(llm.ai_client, "build_chat", boom)
        out = llm.country_llm_score(
            country_display="Russia", payload={"_meta": {"country": "RU"}},
            articles=[], as_of=SANCTIONED_DAY,
        )
        assert out["score"] is None
        assert out["non_investable"] is False


class TestFlagsDoNotMoveScores:
    def test_war_flag_beside_a_low_score_survives_untouched(self, scored):
        # The old policy layer floored this to 0.90. Now the disagreement is
        # preserved and reported by lint instead.
        out = scored(iso2="DE", score_12m=44, score_3m=41)
        assert out["score"] == pytest.approx(0.44)
        assert out["condition_flags"]["war_on_territory"] is True

    def test_raw_and_gated_are_now_identical(self, scored):
        out = scored(iso2="RU")
        assert out["raw_score_12m"] == out["score"]
        assert out["raw_score_3m"] == out["score_3m"]

    def test_ledger_scores_are_passed_through_unrounded(self, scored):
        out = scored(iso2="RU")
        assert out["ledger_scores"] == {
            "friction": pytest.approx(0.71),
            "order_uncertainty": pytest.approx(0.83),
            "information_capacity": pytest.approx(0.29),
            "edge_vitality": pytest.approx(0.34),
        }
        # `subscores` keeps its name for the DB column and existing readers.
        assert out["subscores"] == out["ledger_scores"]

    def test_stamps_record_the_regime(self, scored):
        out = scored(iso2="RU")
        assert out["policy_version"] == "p2.0-observe-only"
        assert out["prompt_version"] == ai_constants.PROMPT_VERSION


class TestNothingElseAssignsAScore:
    """Structural: grep the backend for a second writer of `score`."""

    def _python_sources(self):
        for path in BACKEND.rglob("*.py"):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            yield path, path.read_text(encoding="utf-8")

    def test_only_langchain_llm_writes_the_score_key(self):
        # Any other module building a dict with a "score" key is either a second
        # scorer or a mutation of this one's output.
        pattern = re.compile(r'"score"\s*:')
        offenders = [
            str(path.relative_to(BACKEND))
            for path, source in self._python_sources()
            if pattern.search(source) and path.name != "langchain_llm.py"
        ]
        assert offenders == [], f"unexpected writers of the score key: {offenders}"

    def test_langchain_llm_assigns_score_exactly_once(self):
        source = (BACKEND / "utils" / "ai" / "langchain_llm.py").read_text(encoding="utf-8")
        # Once in the success path, once in _failure_result (which writes None).
        assert len(re.findall(r'"score"\s*:', source)) == 2

    def test_no_module_mutates_an_llm_output_score(self):
        pattern = re.compile(r'\[\s*["\']score["\']\s*\]\s*=')
        offenders = [
            str(path.relative_to(BACKEND))
            for path, source in self._python_sources()
            if pattern.search(source)
        ]
        assert offenders == [], f"score is assigned by subscript in: {offenders}"

    def test_enforcement_symbols_are_gone_from_the_whole_backend(self):
        for symbol in ("apply_policy", "macro_latest_facts", "AI_PROMPT_V2",
                       "RISK_SCHEMA_V2", "risk_policy"):
            offenders = [
                str(path.relative_to(BACKEND))
                for path, source in self._python_sources()
                # policy.py's changelog comment names risk_policy.yaml on purpose.
                if symbol in source and path.name != "policy.py"
            ]
            assert offenders == [], f"{symbol} still referenced in {offenders}"

    def test_risk_policy_yaml_is_deleted(self):
        assert not (BACKEND / "utils" / "ai" / "risk_policy.yaml").exists()

    def test_policy_module_exposes_no_score_mutator(self):
        assert not hasattr(policy, "apply_policy")
        assert hasattr(policy, "assess_investability")
