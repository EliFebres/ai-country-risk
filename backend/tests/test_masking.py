"""The mask-integrity gate and the identifiability probe.

The gate is the important half. A masked snapshot that names its country is
not a degraded result, it is a mislabelled one, and once it is in a ten-year
series it looks exactly like a sound one. So `assert_clean` raises rather than
warns, and these tests pin that it raises for a leak anywhere in a nested
payload — not just in the field somebody remembered to check.

No network and no model: both model passes take an injected chat object.
"""

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


class TestTheProbe:
    def test_it_returns_a_guess(self):
        chat = FakeChat({"country": "TR", "confidence": 0.8, "evidence": "85% inflation"})
        got = probe.probe([item()], "k", model_chat=chat)
        assert got == {"country": "TR", "confidence": 0.8, "evidence": "85% inflation"}

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
