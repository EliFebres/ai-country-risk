"""Tests for ``backend.utils.ai.langchain_llm._from_100`` — the scale boundary.

The v2 prompt asks the model for integers 0-100 because that grid has the rank
resolution the roster needs; everything downstream — policy, the database, the
front-end — speaks 0-1. This one function is where the two meet, so if it is
wrong, every stored score is wrong by two orders of magnitude and the front-end
renders a risk of 8500%.

It must also never raise. A malformed impact on one article would otherwise
take down the whole country's scoring.
"""

import pytest

from backend.utils.ai import langchain_llm as llm


class TestFrom100:
    @pytest.mark.parametrize("raw,expected", [
        (0, 0.0),
        (37, 0.37),
        (62, 0.62),
        (100, 1.0),
    ])
    def test_integers(self, raw, expected):
        assert llm._from_100(raw) == pytest.approx(expected)

    def test_float_input(self):
        assert llm._from_100(62.5) == pytest.approx(0.625)

    def test_numeric_string(self):
        # strict=True guarantees the type, but the fallback path does not.
        assert llm._from_100("41") == pytest.approx(0.41)

    def test_none_stays_none(self):
        # A null subscore means "the evidence is silent", not zero risk.
        assert llm._from_100(None) is None

    def test_garbage_string_is_none(self):
        assert llm._from_100("n/a") is None

    def test_bool_is_not_a_number(self):
        # True would otherwise sail through float() as 0.01.
        assert llm._from_100(True) is None

    @pytest.mark.parametrize("raw,expected", [(140, 1.0), (-20, 0.0)])
    def test_out_of_range_is_clamped(self, raw, expected):
        # strict structured output enforces the schema's shape but not its
        # minimum/maximum, so the clamp is the only guard.
        assert llm._from_100(raw) == expected


class TestArticleImpactConversion:
    # Mirrors the comprehension in country_llm_score: every article keeps its
    # id and topic_group, and only impact changes scale.
    @staticmethod
    def convert(scores):
        return [{**a, "impact": llm._from_100(a.get("impact"))} for a in scores]

    def test_empty_list(self):
        assert self.convert([]) == []

    def test_preserves_id_and_topic_group(self):
        got = self.convert([{"id": "a1", "impact": 85, "topic_group": "ru_strikes"}])
        assert got == [{"id": "a1", "impact": pytest.approx(0.85), "topic_group": "ru_strikes"}]

    def test_malformed_impact_does_not_raise(self):
        got = self.convert([{"id": "a2", "impact": "oops", "topic_group": "t"}])
        assert got[0]["impact"] is None


class TestFailureResult:
    def test_has_every_key_a_caller_reads(self):
        # A failure path that returned a short dict would KeyError in the
        # upsert instead of cleanly skipping the country.
        got = llm._failure_result()
        assert got["score"] is None
        for key in ("bullet_summary", "subscores", "ledger_scores",
                    "news_article_scores", "score_3m",
                    "raw_score_12m", "raw_score_3m",
                    "subscore_evidence", "condition_flags", "evidence_coverage",
                    "applied_rules", "legal_gate", "non_investable",
                    "model_id", "prompt_version", "policy_version"):
            assert key in got

    def test_stamps_are_populated(self):
        got = llm._failure_result()
        assert got["model_id"] and got["prompt_version"] and got["policy_version"]
