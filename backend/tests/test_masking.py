"""The mask-integrity gate and the identifiability probe.

The gate is the important half. A masked snapshot that names its country is
not a degraded result, it is a mislabelled one, and once it is in a ten-year
series it looks exactly like a sound one. So `assert_clean` raises rather than
warns, and these tests pin that it raises for a leak anywhere in a nested
payload — not just in the field somebody remembered to check.

No network and no model: both model passes take an injected chat object.
"""

import json
from datetime import date

import pytest

from backend.utils.history import config
from backend.utils.masking import gazetteer, probe, rewrite


class FakeChat:
    """Stands in for `ai_client.build_digest_chat(...)`."""

    def __init__(self, result=None, raises=None):
        self._result, self._raises = result, raises
        self.prompts = []

    def with_structured_output(self, schema=None, strict=False):
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        if self._raises:
            raise self._raises
        return self._result


def item(**over):
    base = dict(
        title="Turkey's central bank holds rates as the lira slides",
        snippet="Ankara held rates on Wednesday.",
        text="The Central Bank of Turkey held rates at 24% on Wednesday in Ankara.",
        link="https://www.theguardian.com/world/2018/mar/14/turkey-lira-central-bank",
        _theme="order",
    )
    base.update(over)
    return base


class TestTheGazetteerPass:
    def test_every_text_field_is_masked(self):
        masked = rewrite.mask_item(item(), "TR")
        for field in ("title", "snippet", "text"):
            assert gazetteer.scan(masked[field], ["TR"]) == []

    def test_the_numbers_survive(self):
        assert "24%" in rewrite.mask_item(item(), "TR")["text"]

    def test_the_original_item_is_untouched(self):
        # The named run scores the same article; the two must differ only in
        # what the model was shown.
        original = item()
        rewrite.mask_item(original, "TR")
        assert "Turkey" in original["title"]

    def test_a_whole_snapshot_masks(self):
        masked = rewrite.mask_items([item(), item(title="Istanbul votes again")], "TR")
        assert len(masked) == 2
        assert all(gazetteer.scan(m["title"], ["TR"]) == [] for m in masked)


class TestTheGateRefusesToSend:
    def test_a_clean_payload_passes(self):
        rewrite.assert_clean({"articles": [{"title": "The central bank held rates."}]})

    def test_a_leak_at_the_top_level_raises(self):
        with pytest.raises(rewrite.MaskLeak):
            rewrite.assert_clean("Turkey held rates.")

    def test_a_leak_nested_deep_raises(self):
        # The leak is wherever nobody looked, so the scan walks the payload.
        payload = {"articles": [{"digest": {"bullets": ["rates held in Ankara"]}}]}
        with pytest.raises(rewrite.MaskLeak):
            rewrite.assert_clean(payload)

    def test_a_leak_in_a_list_of_strings_raises(self):
        with pytest.raises(rewrite.MaskLeak):
            rewrite.assert_clean({"evidence": ["fine", "Brazil devalued"]})

    def test_another_roster_country_is_also_a_leak(self):
        # Naming a different pilot country lets the probe rule countries out by
        # elimination — the same leak wearing a hat.
        with pytest.raises(rewrite.MaskLeak):
            rewrite.assert_clean({"text": "Unlike Portugal, it devalued."}, roster=config.PILOT_ROSTER)

    def test_the_error_names_what_leaked(self):
        with pytest.raises(rewrite.MaskLeak, match="Brazil"):
            rewrite.assert_clean("Brazil devalued.")

    def test_numbers_and_roles_do_not_trip_it(self):
        rewrite.assert_clean({"text": "The central bank raised rates to 24% in the capital."})


class TestTheModelRewrite:
    def test_it_returns_the_rewritten_text(self):
        chat = FakeChat({"rewritten": "The finance minister resigned."})
        assert rewrite.rewrite_body("X resigned.", "k", model_chat=chat) == \
            "The finance minister resigned."

    def test_the_prompt_insists_on_keeping_numbers(self):
        chat = FakeChat({"rewritten": "ok"})
        rewrite.rewrite_body("text", "k", model_chat=chat)
        assert "Keep every number exactly as written" in chat.prompts[0]

    def test_a_failed_rewrite_degrades_to_nothing_rather_than_leaking(self):
        # Fails closed. Being short one body costs a week some evidence; one
        # leaked name costs the comparison.
        chat = FakeChat(raises=RuntimeError("model down"))
        assert rewrite.rewrite_body("Turkey did a thing.", "k", model_chat=chat) == ""

    def test_a_malformed_response_degrades_too(self):
        assert rewrite.rewrite_body("x", "k", model_chat=FakeChat("not a dict")) == ""

    def test_empty_in_empty_out_without_a_call(self):
        chat = FakeChat({"rewritten": "should not be used"})
        assert rewrite.rewrite_body("", "k", model_chat=chat) == ""
        assert chat.prompts == []


class TestTheDigestSweep:
    """The layer that was missing, and the one the probe kept quoting past.

    Digests are born masked, so the sweep only ever ran on the two or three
    full texts. But the digest *model* writes the digest, and it wrote names —
    a probe over twenty bundles identified fifteen, quoting "Jair Bolsonaro",
    "Erdoğan", "Park Geun-hye", "Temer", "Lula". All of those were in digests.
    """

    DIGEST = {"what_happened": "Bolsonaro dissolved the committee.",
              "actors": "Jair Bolsonaro, President",
              "numbers": "inflation 8.5%", "transmission": "fiscal",
              "directly_about_country": True, "stage1_severity": 60.0}

    def test_it_replaces_the_names_the_digest_model_kept(self):
        chat = FakeChat({"what_happened": "The president dissolved the committee.",
                         "actors": "the president", "numbers": "inflation 8.5%",
                         "transmission": "fiscal"})
        out = rewrite.sweep_digest(self.DIGEST, "k", model_chat=chat)
        assert "Bolsonaro" not in str(out)
        assert out["actors"] == "the president"

    def test_the_numbers_and_the_non_text_fields_survive(self):
        chat = FakeChat({"what_happened": "x", "actors": "the president",
                         "numbers": "inflation 8.5%", "transmission": "fiscal"})
        out = rewrite.sweep_digest(self.DIGEST, "k", model_chat=chat)
        assert out["numbers"] == "inflation 8.5%"
        assert out["stage1_severity"] == 60.0
        assert out["directly_about_country"] is True

    def test_the_prompt_insists_on_keeping_numbers(self):
        chat = FakeChat({"what_happened": "x", "actors": "y",
                         "numbers": "z", "transmission": "w"})
        rewrite.sweep_digest(self.DIGEST, "k", model_chat=chat)
        assert "Keep every number exactly as written" in chat.prompts[0]

    def test_a_failed_sweep_returns_none_rather_than_dropping_the_digest(self):
        # Unlike `rewrite_body` this does not fail closed: a digest is not sent
        # whole, and silently dropping it would cost the article for a call that
        # merely timed out. The caller keeps the unswept digest and the manifest
        # records the mode.
        chat = FakeChat(raises=RuntimeError("model down"))
        assert rewrite.sweep_digest(self.DIGEST, "k", model_chat=chat) is None

    def test_a_non_dict_is_refused(self):
        assert rewrite.sweep_digest("not a digest", "k") is None

    def test_the_headline_is_swept_in_the_same_call(self):
        """A title is sent for every article and had only ever been
        gazetteer-masked, so "Brazil Election: Jair Bolsonaro Heads to Runoff"
        reached the model as "the country Election: Jair Bolsonaro Heads to
        Runoff". Six of twenty titles in one measured bundle named the
        politician."""
        chat = FakeChat({"what_happened": "x", "actors": "the president",
                         "numbers": "n", "transmission": "t",
                         "headline": "the country election: far-right candidate heads to runoff"})
        out = rewrite.sweep_digest(
            self.DIGEST, "k", model_chat=chat,
            title="the country Election: Jair Bolsonaro Heads to Runoff")
        assert "Bolsonaro" not in out[rewrite._SWEPT_TITLE_KEY]
        assert "headline:" in chat.prompts[0]

    def test_no_title_means_no_swept_title_key(self):
        chat = FakeChat({"what_happened": "x", "actors": "y", "numbers": "n",
                         "transmission": "t", "headline": "should be ignored"})
        out = rewrite.sweep_digest(self.DIGEST, "k", model_chat=chat)
        assert rewrite._SWEPT_TITLE_KEY not in out


class TestTheProbe:
    def test_it_returns_a_guess(self):
        chat = FakeChat({"country": "TR", "confidence": 0.8, "evidence": "85% inflation",
                         "alternatives": [{"country": "TR", "probability": 0.7},
                                          {"country": "AR", "probability": 0.2}],
                         "insufficient_information": False})
        got = probe.probe([item()], "k", model_chat=chat)
        assert got["country"] == "TR" and got["confidence"] == 0.8
        assert got["evidence"] == "85% inflation"
        assert got["alternatives"][0] == {"country": "TR", "probability": 0.7}
        assert got["insufficient_information"] is False

    def test_a_model_that_omits_the_distribution_still_parses(self):
        """The distribution is new; a stray response without it must degrade to
        an empty list rather than taking the probe down."""
        chat = FakeChat({"country": "TR", "confidence": 0.8, "evidence": "x"})
        got = probe.probe([item()], "k", model_chat=chat)
        assert got["alternatives"] == [] and got["insufficient_information"] is False

    def test_a_malformed_alternative_is_dropped_not_fatal(self):
        chat = FakeChat({"country": "TR", "confidence": 0.5, "evidence": "x",
                         "alternatives": [{"country": "TR", "probability": "high"},
                                          {"country": "BR", "probability": 0.3},
                                          "nonsense"]})
        got = probe.probe([item()], "k", model_chat=chat)
        assert got["alternatives"] == [{"country": "BR", "probability": 0.3}]

    def test_insufficient_information_is_carried_through(self):
        """The answer that lets a prior admit it is a prior. Losing it would put
        the meter back to reporting base rates as identifications."""
        chat = FakeChat({"country": "US", "confidence": 0.4, "evidence": "base rates",
                         "insufficient_information": True})
        assert probe.probe([item()], "k", model_chat=chat)["insufficient_information"]

    def test_the_bundle_never_contains_urls(self):
        # ".../2018/mar/14/turkey-lira-central-bank" would hand over the answer.
        chat = FakeChat({"country": "ZZ", "confidence": 0.1, "evidence": "-"})
        probe.probe([item()], "k", model_chat=chat)
        assert "theguardian.com" not in chat.prompts[0]

    def test_a_failed_probe_is_not_recorded_as_an_identification(self):
        # The opposite of the leakage scan's fail-closed: this is a
        # measurement, and a failed measurement must not read as a hit.
        chat = FakeChat(raises=RuntimeError("model down"))
        got = probe.probe([item()], "k", model_chat=chat)
        assert got["country"] == "ZZ" and got["confidence"] == 0.0

    def test_an_empty_bundle_is_not_a_guess(self):
        assert probe.probe([], "k")["country"] == "ZZ"


class TestTheProbeSummary:
    def test_hit_rates_are_per_country(self):
        got = probe.summarize([
            {"country_iso2": "US", "guess": {"country": "US", "confidence": 0.9}},
            {"country_iso2": "US", "guess": {"country": "US", "confidence": 0.9}},
            {"country_iso2": "PT", "guess": {"country": "ZZ", "confidence": 0.1}},
            {"country_iso2": "PT", "guess": {"country": "ES", "confidence": 0.3}},
        ])
        assert got["per_country"]["US"]["rate"] == 1.0
        assert got["per_country"]["PT"]["rate"] == 0.0

    def test_the_spread_is_the_meter_not_any_single_rate(self):
        # The US is expected at the ceiling. If every country sits up there
        # with it, masking is not working.
        got = probe.summarize([
            {"country_iso2": "US", "guess": {"country": "US", "confidence": 0.9}},
            {"country_iso2": "PT", "guess": {"country": "ZZ", "confidence": 0.1}},
        ])
        assert got["ceiling"] == 1.0
        assert got["spread"] == 1.0

    def test_no_results_is_not_a_crash(self):
        assert probe.summarize([])["spread"] == 0.0


class TestTheControlArm:
    """Every identifiability number is unreadable without this.

    A probe forced to name a country names the one its prior favours, and on a
    roster containing the United States that is the United States — so "US
    identified at 0.85" and "the model always says US" produce identical output.
    The null bundle is the only thing that separates them.
    """

    def test_the_null_bundle_names_no_country(self):
        blob = json.dumps(probe.null_bundle(20), ensure_ascii=False)
        assert gazetteer.scan(blob, list(gazetteer.DEFAULT_ROSTER)) == []

    def test_it_matches_a_real_snapshots_size(self):
        """A six-article bundle and a twenty-article one are not the same test:
        volume is itself a signal the probe uses."""
        assert len(probe.null_bundle(20)) == 20
        assert len(probe.null_bundle(7)) == 7

    def test_it_keeps_numbers_because_magnitudes_are_the_signal(self):
        """Stripping the numbers would make the control easier than the thing it
        is a control for — the probe cites magnitudes when it names the US."""
        assert any(any(ch.isdigit() for ch in it["digest"]["numbers"])
                   for it in probe.null_bundle(6))

    def test_it_has_the_shape_the_prompt_builder_expects(self):
        entries = probe.bundle_text(probe.null_bundle(4))
        assert entries and "central bank" in entries

    def test_the_distribution_exposes_an_over_named_country(self):
        results = [{"country_iso2": c, "guess": {"country": "US", "confidence": 0.9}}
                   for c in ("US", "TR", "BR", "PT")]
        got = probe.distribution(results)
        assert got["guessed"]["US"] == 4
        # Named in 4 of 4 while being the truth in 1 of 4: the prior, visible.
        assert got["over_representation"]["US"] == 0.75

    def test_a_calibrated_probe_shows_no_over_representation(self):
        results = [{"country_iso2": c, "guess": {"country": c, "confidence": 0.9}}
                   for c in ("US", "TR", "BR", "PT")]
        assert set(probe.distribution(results)["over_representation"].values()) == {0.0}

    def test_insufficient_information_is_counted(self):
        results = [{"country_iso2": "PT",
                    "guess": {"country": "US", "insufficient_information": True}},
                   {"country_iso2": "PT",
                    "guess": {"country": "PT", "insufficient_information": False}}]
        assert probe.distribution(results)["insufficient_information"] == 1


class TestComparingTwoMaskingBehaviours:
    """The consumer of `probe_result`, and the reason the table exists.

    Twenty bundles were probed on 2026-08-03 and the result — fifteen
    identified, with the leaking names quoted — lives in a commit message. The
    sweep written to fix it therefore cannot be measured against the run that
    motivated it. Storing the rows is only half the fix; this is the half that
    reads them.
    """

    def row(self, iso2, day, guess, confidence, identified, evidence=""):
        return {"country_iso2": iso2, "as_of": day, "guess": guess,
                "confidence": confidence, "identified": identified,
                "evidence": evidence}

    def test_a_bundle_the_sweep_fixed_is_reported_as_fixed(self):
        got = probe.compare(
            [self.row("TR", date(2018, 8, 18), "TR", 0.9, True, "Erdoğan")],
            [self.row("TR", date(2018, 8, 18), "ZZ", 0.2, False)])
        assert len(got) == 1
        assert got[0]["fixed"] is True and got[0]["regressed"] is False
        assert got[0]["was_guess"] == "TR" and got[0]["now_guess"] == "ZZ"

    def test_a_bundle_that_got_worse_is_reported_as_regressed(self):
        got = probe.compare(
            [self.row("PT", date(2019, 6, 3), "ZZ", 0.1, False)],
            [self.row("PT", date(2019, 6, 3), "PT", 0.8, True, "the Douro")])
        assert got[0]["regressed"] is True and got[0]["fixed"] is False
        # The text is carried through, because "why did masking fail here" is
        # only ever answered by what the probe quoted back.
        assert got[0]["now_evidence"] == "the Douro"

    def test_a_bundle_only_one_run_covered_is_kept_not_dropped(self):
        """Twenty bundles leaving six traces is exactly this, silently."""
        got = probe.compare(
            [], [self.row("US", date(2017, 3, 11), "US", 0.95, True)])
        assert len(got) == 1
        assert got[0]["was_guess"] is None
        # Not False: "not measured" and "no change" are different answers.
        assert got[0]["fixed"] is None and got[0]["regressed"] is None

    def test_both_sides_empty_is_not_a_crash(self):
        assert probe.compare([], []) == []


class TestTheFourOutcomes:
    """Two buckets misread this corpus in both directions.

    PT on a quiet week came back "GB at 0.70". Counting only correct hits calls
    that a clean miss and understates what the bundle carried — the text was
    legible enough to place confidently in Western Europe. Counting confidence
    alone calls it a leak and overstates it — masking held; the model named the
    wrong country.
    """

    def test_a_correct_confident_guess_is_identified(self):
        assert probe.classify("TR", {"country": "TR", "confidence": 0.85}) == "identified"

    def test_a_wrong_confident_guess_is_its_own_category(self):
        """PT 2021-07-05, exactly."""
        assert probe.classify("PT", {"country": "GB", "confidence": 0.70}) == "wrong"

    def test_a_declined_guess_is_no_guess(self):
        assert probe.classify("PT", {"country": "ZZ", "confidence": 0.0}) == "no_guess"

    def test_insufficient_information_is_no_guess_even_when_named(self):
        """The model may name a country and say it is guessing from base rates.
        That is not an identification and must not be counted as one."""
        assert probe.classify("PT", {"country": "US", "confidence": 0.4,
                                     "insufficient_information": True}) == "no_guess"

    def test_a_low_confidence_correct_guess_is_uncertain_not_identified(self):
        assert probe.classify("KR", {"country": "KR", "confidence": 0.2}) == "uncertain"

    def test_the_summary_carries_all_four(self):
        got = probe.summarize([
            {"country_iso2": "PT", "guess": {"country": "GB", "confidence": 0.7}},
            {"country_iso2": "PT", "guess": {"country": "ZZ", "confidence": 0.0}},
            {"country_iso2": "TR", "guess": {"country": "TR", "confidence": 0.9}},
        ])
        assert got["totals"] == {"identified": 1, "wrong": 1,
                                 "uncertain": 0, "no_guess": 1}
        # PT was never identified and was placed once: two different facts, and
        # the old single rate could express only the first.
        assert got["per_country"]["PT"]["rate"] == 0.0
        assert got["per_country"]["PT"]["placed_rate"] == 0.5


class TestOutletFingerprinting:
    """Whether the probe is reading the evidence or the newspaper.

    The corpus is two outlets with very different footprints. A model that has
    read both can recognise house style, and if bundles with a higher NYT share
    are more identifiable then part of the identifiability number is the outlet
    rather than the country.
    """

    def row(self, iso2, guess_country, confidence, guardian, nyt):
        return {"country_iso2": iso2,
                "guess": {"country": guess_country, "confidence": confidence},
                "sources": {"guardian": guardian, "nyt": nyt}}

    def test_it_reports_the_mix_per_outcome(self):
        got = probe.source_mix_correlation([
            self.row("US", "US", 0.9, 5, 15),
            self.row("PT", "ZZ", 0.0, 19, 1),
        ])
        assert got["by_outcome"]["identified"]["mean_nyt_share"] == 0.75
        assert got["by_outcome"]["no_guess"]["mean_nyt_share"] == 0.05

    def test_a_positive_gap_is_the_fingerprinting_shape(self):
        got = probe.source_mix_correlation([
            self.row("US", "US", 0.9, 4, 16),
            self.row("TR", "TR", 0.9, 6, 14),
            self.row("PT", "ZZ", 0.0, 19, 1),
            self.row("KR", "ZZ", 0.0, 18, 2),
        ])
        assert got["nyt_share_gap"] > 0.5

    def test_a_wrong_guess_counts_as_placed(self):
        """It is still the bundle carrying enough to commit to an answer."""
        got = probe.source_mix_correlation([
            self.row("PT", "GB", 0.7, 10, 10),
            self.row("PT", "ZZ", 0.0, 20, 0),
        ])
        assert got["by_outcome"]["wrong"]["n"] == 1
        assert got["nyt_share_gap"] == 0.5

    def test_no_gap_when_one_side_is_empty(self):
        """Two bundles that both declined say nothing about fingerprinting."""
        got = probe.source_mix_correlation([
            self.row("PT", "ZZ", 0.0, 20, 0),
            self.row("KR", "ZZ", 0.0, 10, 10),
        ])
        assert got["nyt_share_gap"] is None


class TestThePromptCoversPeopleWithoutOffices:
    """A probe named "Neymar" as its reason for identifying Brazil.

    Rule 2 mapped a named person to their *office*, and an athlete has not got
    one — so a footballer fell through a rule that looked complete. It is a gap
    rather than a ceiling: unlike a failed coup or a World Cup match, a person's
    name carries no evidence the roles cannot carry.
    """

    def test_both_passes_name_the_officeless_case(self):
        for prompt in (rewrite._DIGEST_SWEEP_PROMPT, rewrite._REWRITE_PROMPT):
            assert "no office" in prompt
            assert "footballer" in prompt

    def test_it_does_not_reintroduce_the_country_through_the_role(self):
        """"the national team's striker" would put back exactly what the name
        gave away, which is how a mask rule turns into a leak."""
        assert "never a role that implies where they play" in rewrite._MASK_RULES

    def test_the_rule_is_shared_rather_than_duplicated(self):
        assert rewrite._MASK_RULES in rewrite._DIGEST_SWEEP_PROMPT
        assert rewrite._MASK_RULES in rewrite._REWRITE_PROMPT
