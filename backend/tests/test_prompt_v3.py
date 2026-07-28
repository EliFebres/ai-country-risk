"""Characterization tests for the v3 scoring prompt and schema.

The prompt is product logic written in prose, which makes it the one part of
this system with no compiler. A deleted paragraph costs nothing at import time
and changes every score in the roster, so the sections that carry real
behavioral weight are pinned here by substring.

Two groups, and the second matters more than the first:

* **Required** — the framework's load-bearing instructions. If the three-door
  test or the edge protection quietly disappears in an edit, scores move and
  nothing else notices.
* **Forbidden** — the language the observe-only rewrite removed. Enforcement
  used to live in the prompt, then in a policy module, and now nowhere. A floor
  or a pinned score creeping back into the wording would restore the exact
  problem the deletion solved — a stored number that is part model judgement and
  part rule, with no way to tell which part is which — and it would do it
  without touching a line of Python.

No network, no model call: these read the template.
"""

import pytest

from backend.utils.ai import constants as ai_constants


PROMPT = ai_constants.AI_PROMPT_V3
SCHEMA = ai_constants.RISK_SCHEMA_V3


def lower() -> str:
    return PROMPT.lower()


def flat() -> str:
    """Lowercased, with every run of whitespace collapsed to a single space.

    The prompt is hard-wrapped at 79 columns, so any pinned phrase longer than a
    few words can straddle a newline and a plain substring check on `lower()`
    fails for a reason that has nothing to do with the wording. Sentence-length
    assertions read this instead; short ones can keep using `lower()`.
    """
    return " ".join(PROMPT.lower().split())


class TestFormatting:
    def test_formats_with_its_exact_placeholder_set(self):
        rendered = PROMPT.format(
            country="Portugal", as_of_date="2026-07-27",
            evidence_json="{}", articles_json="[]", full_text_block="(none)",
        )
        assert "Portugal" in rendered and "2026-07-27" in rendered
        assert "{" not in rendered.replace("{}", "")  # no unescaped braces survive

    def test_country_appears_in_the_localization_rule(self):
        rendered = PROMPT.format(
            country="Portugal", as_of_date="2026-07-27",
            evidence_json="{}", articles_json="[]", full_text_block="(none)",
        )
        assert "kinetic activity on Portugal's territory" in rendered


class TestThreeLedgers:
    @pytest.mark.parametrize("term", ["FRICTION", "ORDER-UNCERTAINTY", "INFORMATION"])
    def test_each_ledger_is_defined(self, term):
        assert term in PROMPT

    def test_friction_is_defined_as_the_wedge(self):
        assert "wedge" in lower()
        assert "judge the take by how it converts" in lower()

    def test_order_uncertainty_names_the_load_bearing_rules(self):
        text = lower()
        assert "load-bearing rules" in text
        for rule in ("contract", "currency", "statistic", "succession"):
            assert rule in text

    def test_information_sets_trust_and_drift(self):
        text = lower()
        assert "haircut" in text
        assert "market-observed" in text
        assert "compound" in text


class TestScoreDirectionIsStated:
    """Every ledger's direction is explicit, not inferable.

    `information_capacity` is the trap: the name says capacity but it is scored
    as risk, so a model left to infer could invert it and nothing downstream
    would notice — the number is in range, the summary reads sensibly, and the
    stored series silently flips sign.
    """

    def test_all_four_ledgers_declare_a_direction(self):
        for ledger in ("friction", "order_uncertainty",
                       "information_capacity", "edge_vitality"):
            assert f"{ledger} " in PROMPT or f"`{ledger}`" in PROMPT

    def test_information_capacity_is_explicitly_inverted(self):
        assert "higher = WORSE instruments" in PROMPT
        assert "strong statistical system\n                        scores LOW here" in PROMPT

    def test_edge_vitality_is_the_only_non_risk_score(self):
        assert "higher = MORE vitality" in PROMPT
        assert "only score where a high number is a good thing" in lower()


class TestEdgeProtection:
    def test_names_churn_formation_failure_and_human_capital(self):
        text = flat()
        for term in ("churn", "startup formation", "failure",
                     "human-capital formation", "learning outcomes"):
            assert term in text

    def test_states_it_must_not_raise_a_score(self):
        assert "MUST NOT raise any risk score" in PROMPT

    def test_says_reported_not_penalized(self):
        text = lower()
        assert "do not let a high value raise friction" in text

    def test_the_spend_versus_learning_gap_reads_as_friction(self):
        # The pair is carried in the payload without a computed wedge; this
        # sentence is the only place the model is told how to read the gap.
        text = flat()
        assert "wedge made visible inside a school system" in text
        assert "friction evidence, not as edge credit" in text


class TestThreeDoorEventTest:
    def test_all_three_doors_are_present(self):
        assert "three doors" in lower()
        for door in ("F — it changes the wedge",
                     "U — it destabilizes the order",
                     "I — it changes the instruments"):
            assert door in PROMPT

    def test_door_f_covers_skilled_emigration(self):
        # There is deliberately no data series for departure, so the digests are
        # the only instrument — the prompt has to say which door they enter by.
        text = flat()
        assert "skilled departure" in text
        assert "grading the wedge with their feet" in text

    def test_names_the_noise_it_replaces(self):
        text = lower()
        for noise in ("natural disaster", "weapons demonstration", "celebrity politics"):
            assert noise in text

    def test_the_old_one_off_guardrail_is_gone(self):
        # Replaced entirely by the event test, per the framework.
        text = lower()
        assert "foiled plot" not in text
        assert "anti-overreaction" not in text


class TestSuppressedVolatility:
    def test_treats_calm_as_evidence_against(self):
        assert "evidence AGAINST" in PROMPT

    def test_uses_the_fuel_load_framing(self):
        assert "fuel load" in lower()

    def test_says_null_is_not_false(self):
        assert "that is not a false" in lower()


class TestCarriedOverMechanics:
    def test_integer_scale_and_no_multiples_of_five(self):
        assert "INTEGERS 0-100" in PROMPT
        assert "never round" in lower() and "multiples of 5" in PROMPT

    def test_five_bands(self):
        for band in ("5-20 Low", "20-40 Low-Moderate", "40-75 Moderate",
                     "75-90 High", "90-98 Extreme"):
            assert band in PROMPT

    @pytest.mark.parametrize("anchor", ["~12", "~38", "~58", "~85", "~95"])
    def test_all_five_calibration_anchors(self, anchor):
        assert anchor in PROMPT

    def test_localization_and_materiality(self):
        assert "Localization & Materiality" in PROMPT

    def test_topic_clustering_with_persistence_and_breadth(self):
        text = lower()
        assert "topic_group" in text
        assert "persistence" in text and "breadth" in text and "singularity" in text

    def test_evidence_coverage_is_explained(self):
        assert "evidence_coverage" in PROMPT
        assert "thin wire stories" in lower()


class TestHorizonsAndFlags:
    def test_two_horizons_scored_independently(self):
        assert "score_3m" in PROMPT and "score_12m" in PROMPT
        assert "Do not derive one from the other" in PROMPT

    def test_level_width_drift_split(self):
        text = lower()
        assert "sets the level" in text
        assert "sets the width" in text
        assert "sets the drift" in text

    def test_condition_flags_are_observations_only(self):
        assert "Condition flags: observations only" in PROMPT
        assert "Nothing downstream will alter your scores" in PROMPT
        assert "must not adjust them to anticipate any rule" in PROMPT

    @pytest.mark.parametrize("flag", ["war_on_territory", "internal_conflict_level",
                                      "emergency_rule", "sovereign_stress"])
    def test_all_four_flags_are_defined(self, flag):
        assert flag in PROMPT

    def test_as_of_discipline(self):
        assert "Treat {as_of_date} as today" in PROMPT
        assert "staleness_days" in PROMPT


class TestForbiddenLanguage:
    """The enforcement vocabulary that must never come back.

    Phrased precisely rather than by bare substring. A crude ban on "cap" also
    forbids "capability", "capacity" and "capital controls", and one on
    "sanction" forbids the impact band's legitimate "binding sanctions" — which
    is *evidence about an event*, not an instruction to adjust a score. Banning
    those would make the test fire on correct prompts and get deleted, which is
    worse than not having it.
    """

    @pytest.mark.parametrize("phrase", [
        # Floors and minimums
        "must be at least", "at least 0.", "no lower than", "minimum score",
        "floor",
        # Caps and maximums
        "cap the", "capped at", "at most 55", "no higher than", "maximum score",
        # Pinned values
        "pin the", "pinned", "set the score to 1", "forced to 1.0",
        "score of exactly",
        # The framing v3 replaced
        "no post-processing will alter your score",
        "enforcement happens downstream",
        # The edge metrics v3.1 replaced. Both flip meaning with strategic
        # context — see the edge block in utils/constants.py.
        "patent",
        "high-technology exports",
    ])
    def test_absent(self, phrase):
        assert phrase not in lower(), f"prompt must not contain {phrase!r}"

    def test_no_investability_instruction(self):
        # "binding sanctions" as an event is legitimate evidence and stays. What
        # must not appear is any instruction about legal investability — that is
        # a badge applied in code, and a model told to anticipate it would fold
        # a legal fact into a risk judgement.
        text = lower()
        for phrase in ("non_investable", "uninvestable", "non-investable",
                       "legally cannot", "restricted country", "sanctions gate"):
            assert phrase not in text, f"prompt must not instruct on {phrase!r}"

    def test_no_reference_to_a_previous_score(self):
        text = lower()
        for phrase in ("yesterday", "previous score", "prior day", "last score",
                       "prior score", "previous day"):
            assert phrase not in text

    def test_no_edge_penalty(self):
        # Every mention of the edge metrics must be protective. If "penalize"
        # appears at all, it must be inside the prohibition.
        text = lower()
        assert "high churn increases" not in text
        assert "churn raises" not in text
        for sentence in text.split("."):
            if "penaliz" in sentence:
                assert "not penalized" in sentence or "never" in sentence, sentence


class TestSchemaV3:
    def test_is_strict(self):
        assert SCHEMA["additionalProperties"] is False
        assert set(SCHEMA["required"]) == set(SCHEMA["properties"])

    def test_four_ledger_scores(self):
        ledgers = SCHEMA["properties"]["ledger_scores"]
        assert set(ledgers["properties"]) == {
            "friction", "order_uncertainty", "information_capacity", "edge_vitality",
        }
        assert ledgers["additionalProperties"] is False

    def test_edge_vitality_has_no_evidence_list(self):
        # It is reported, never cited against the country.
        evidence = SCHEMA["properties"]["subscore_evidence"]["properties"]
        assert "edge_vitality" not in evidence
        assert set(evidence) == {"friction", "order_uncertainty", "information_capacity"}

    def test_old_five_subfactors_are_gone(self):
        assert "subscores" not in SCHEMA["properties"]
        flat = str(SCHEMA)
        for old in ("conflict_war", "political_stability", "governance_corruption",
                    "macroeconomic_volatility", "regulatory_uncertainty"):
            assert old not in flat

    @pytest.mark.parametrize("field", ["score_3m", "score_12m", "evidence_coverage"])
    def test_scores_are_bounded_integers(self, field):
        spec = SCHEMA["properties"][field]
        assert spec["type"] == "integer"
        assert spec["minimum"] == 0 and spec["maximum"] == 100

    def test_ledger_scores_allow_null_for_silent_evidence(self):
        for spec in SCHEMA["properties"]["ledger_scores"]["properties"].values():
            assert spec["type"] == ["integer", "null"]

    def test_condition_flags_shape(self):
        flags = SCHEMA["properties"]["condition_flags"]["properties"]
        assert flags["internal_conflict_level"]["enum"] == ["none", "A", "B", "C"]
        for boolean in ("war_on_territory", "emergency_rule", "sovereign_stress"):
            assert flags[boolean]["type"] == "boolean"

    def test_article_impacts_are_integers(self):
        item = SCHEMA["properties"]["news_article_scores"]["items"]["properties"]
        assert item["impact"]["type"] == "integer"
        assert item["impact"]["maximum"] == 100

    def test_summary_cap_matches_the_truncation_constant(self):
        from backend.utils.ai import langchain_llm
        assert SCHEMA["properties"]["bullet_summary"]["maxLength"] == langchain_llm._MAX_SUMMARY_CHARS


class TestVersionStamp:
    def test_prompt_version_is_the_human_capital_revision(self):
        # Bumped from "v3.0-friction-framework" when patents left the edge
        # ledger: a snapshot had already been scored under that wording.
        assert ai_constants.PROMPT_VERSION == "v3.1"

    def test_deleted_generations_are_really_gone(self):
        for name in ("AI_PROMPT", "RISK_SCHEMA", "AI_PROMPT_V2", "RISK_SCHEMA_V2"):
            assert not hasattr(ai_constants, name), f"{name} should have been deleted"

    def test_the_tag_preserving_old_prompts_is_referenced(self):
        # Stored prompt_version strings must stay resolvable to exact text.
        import inspect
        assert "prompts-pre-v3" in inspect.getsource(ai_constants)[:6000]
