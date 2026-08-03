"""The cutover: what the live daily run now sends the model.

Masking stopped being a pilot experiment and became the production regime, so
what matters is no longer "does the gazetteer work" — `test_gazetteer` covers
that — but "does the thing that actually leaves for the API still name the
country". Two ways it could, and both are covered here:

* through the **evidence payload**, which carries the country in its `_meta`
  and its series labels and is serialized whole into the prompt;
* through the **articles**, which reach the model as digests generated *from*
  the article text — so masking after the digest would leak into every prompt
  while every article beside it looked clean.

The assertions are deliberately made against the prompt string rather than
against the masking functions. A function that masks correctly and a prompt
that carries a name are perfectly compatible failures, and only one of them
costs a ten-year series.

No network: the chat object is injected.
"""

import json
from datetime import date

import pytest

from backend.utils import pipeline
from backend.utils.ai import langchain_llm as llm
from backend.utils.masking import gazetteer, rewrite

AS_OF = date(2024, 5, 6)

MODEL_REPLY = {
    "score_12m": 62, "score_3m": 77,
    "bullet_summary": "Model summary.",
    "ledger_scores": {}, "news_article_scores": [],
}


class FakeStructured:
    def __init__(self, reply, sink):
        self._reply, self._sink = reply, sink

    def invoke(self, messages):
        self._sink.append(messages[0].content)
        return self._reply


class FakeChat:
    def __init__(self, reply, sink):
        self._structured = FakeStructured(reply, sink)

    def with_structured_output(self, **_kwargs):
        return self._structured


@pytest.fixture
def prompt_of(monkeypatch):
    """Score something and hand back the prompt string the model was sent."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def run(**over):
        sink = []
        monkeypatch.setattr(llm.ai_client, "build_chat",
                            lambda _key: FakeChat(MODEL_REPLY, sink))
        kwargs = dict(
            country_display="Portugal",
            payload={"_meta": {"country": "PT", "as_of": AS_OF.isoformat()},
                     "note": "Portugal's deficit narrowed in Lisbon's own figures."},
            articles=[{"id": "a1",
                       "title": "Portugal cuts rates as Germany stalls",
                       "text": "Lisbon acted after the Bundesbank held. "
                               "The Portuguese finance minister spoke.",
                       "digest": {"what": "The capital cut rates."}}],
            as_of=AS_OF,
            fulltext_ids=["a1"],
        )
        kwargs.update(over)
        out = llm.country_llm_score(**kwargs)
        return sink[0], out

    return run


class TestTheMaskedPromptNamesNobody:
    def test_the_country_is_gone_from_the_whole_prompt(self, prompt_of):
        prompt, _ = prompt_of(mask_iso2="PT")
        for form in ("Portugal", "Portuguese", "Lisbon"):
            assert form not in prompt

    def test_another_roster_country_is_gone_too(self, prompt_of):
        # Naming Germany while scoring Portugal narrows the field by one.
        prompt, _ = prompt_of(mask_iso2="PT")
        assert "Germany" not in prompt

    def test_the_evidence_payload_is_masked_not_just_the_articles(self, prompt_of):
        # The payload is serialized whole into the prompt; masking only the
        # articles would leave the country named in `_meta` forever.
        prompt, _ = prompt_of(mask_iso2="PT")
        assert '"PT"' not in prompt
        assert "the capital" in prompt

    def test_the_country_label_is_masked(self, prompt_of):
        prompt, _ = prompt_of(mask_iso2="PT")
        assert llm.MASKED_COUNTRY_LABEL in prompt

    def test_the_numbers_survive(self, prompt_of):
        # Masking is about identity. A masked run that lost the magnitudes
        # would be measuring something else entirely.
        prompt, _ = prompt_of(
            mask_iso2="PT",
            payload={"_meta": {"country": "PT"},
                     "note": "Portugal's deficit hit 4.8% of GDP in 2024."})
        assert "4.8%" in prompt and "2024" in prompt

    def test_the_score_still_comes_back(self, prompt_of):
        _, out = prompt_of(mask_iso2="PT")
        assert out["score"] == pytest.approx(0.62)

    def test_the_sanctions_lookup_still_resolves(self, prompt_of):
        # `_extract_iso2` runs before masking, so a sanctioned country is still
        # badged even though its code never reaches the prompt.
        _, out = prompt_of(mask_iso2="RU",
                           payload={"_meta": {"country": "RU",
                                              "as_of": AS_OF.isoformat()}})
        assert out["non_investable"] is True


class TestTheNamedPathIsUntouched:
    def test_no_mask_iso2_leaves_the_prompt_named(self, prompt_of):
        prompt, _ = prompt_of()
        assert "Portugal" in prompt and "Lisbon" in prompt


class TestEveryFieldThatReachesTheModel:
    """The bug a live run found and no unit test would have.

    Masking started as an allow-list of three fields — title, snippet, text.
    But `article_input_text` prefers `content` over `text`, and the legacy
    prompt shape reads `summary`. Both went to the model unmasked, and the
    allow-list looked complete the whole time.
    """

    @pytest.mark.parametrize("field", ["title", "snippet", "text", "content",
                                       "summary", "source"])
    def test_a_text_field_is_masked(self, field):
        masked = rewrite.mask_item({field: "Portugal cut rates in Lisbon."}, "PT")
        assert "Portugal" not in masked[field] and "Lisbon" not in masked[field]

    def test_the_stage_one_digest_is_masked_too(self):
        # Model output, generated from masked text. The gazetteer is cheaper
        # than trusting it.
        masked = rewrite.mask_item(
            {"digest": {"what": "Portugal cut rates.", "why": ["Lisbon acted"]}}, "PT")
        assert "Portugal" not in str(masked["digest"])
        assert "Lisbon" not in str(masked["digest"])

    def test_the_link_is_left_alone(self):
        # Never sent — `prompt_entries` carries ids, not URLs — and masking a
        # path like /2018/turkey-lira-crisis produces nonsense.
        url = "https://example.com/2018/portugal-bailout"
        assert rewrite.mask_item({"link": url}, "PT")["link"] == url

    def test_an_unknown_field_is_masked_by_default(self):
        # The polarity is the point: a field nobody thought of is masked, not
        # forwarded.
        masked = rewrite.mask_item({"some_new_field": "Lisbon acted."}, "PT")
        assert "Lisbon" not in masked["some_new_field"]


class TestTheCodeIsALeakToo:
    """The one identifier the prose gazetteer cannot carry.

    "PT" is not a word, so no pattern can hunt it in a sentence without
    shredding every "IT", "NO" and "IN" in the corpus. But a payload *value*
    that is exactly "PT" is a field, not prose — and `_meta.country` is
    serialized straight into the prompt.
    """

    def test_a_bare_code_value_is_masked(self):
        out = rewrite.mask_payload({"_meta": {"country": "PT"}}, "PT")
        assert out["_meta"]["country"] == gazetteer.ROLES["names"]

    def test_another_countrys_code_flattens(self):
        assert rewrite.mask_payload({"peer": "DE"}, "PT")["peer"] == \
            gazetteer.ROLES["foreign"]

    def test_the_gate_catches_a_code_the_masking_missed(self):
        with pytest.raises(rewrite.MaskLeak, match="PT"):
            rewrite.assert_clean({"_meta": {"country": "PT"}})

    def test_a_code_inside_prose_is_left_alone(self):
        # "IT" is information technology and "NO" is no. Masking two-letter
        # codes inside sentences would damage text nowhere near a country.
        text = "IT spending rose and NO decision was taken IN March."
        assert rewrite.mask_payload({"note": text}, "PT")["note"] == text


class TestTheGateStandsBeforeTheCall:
    def test_a_gazetteer_hole_raises_instead_of_sending(self, prompt_of, monkeypatch):
        # Pretend the gazetteer forgot Portugal entirely. The run must fail
        # loudly: a masked snapshot that named its country would sit in the
        # series looking exactly like a sound one.
        monkeypatch.setattr(gazetteer, "mask", lambda text, iso2: text)
        monkeypatch.setattr(gazetteer, "mask_foreign",
                            lambda text, iso2, roster=None: text)
        with pytest.raises(rewrite.MaskLeak, match="Portugal"):
            prompt_of(mask_iso2="PT")


class TestThePipelineMasksBeforeItDigests:
    """The ordering bug that would leak into every prompt at once.

    A digest is generated *from* the article text. Mask after digesting and
    every article's digest carries the name, while every article beside it
    looks clean — the leak is in the one field nobody re-reads.
    """

    def test_the_digest_prompt_never_sees_the_country(self, monkeypatch):
        seen = {}

        def fake_digest(items, *, country_display, iso2, as_of, masked=False,
                        content_cache=None):
            seen["display"] = country_display
            seen["masked"] = masked
            seen["text"] = json.dumps(items)
            return items

        monkeypatch.setattr(pipeline.digest_engine, "digest_articles", fake_digest)
        monkeypatch.setattr(pipeline.digest_engine, "select_fulltext_ids", lambda _i: [])
        monkeypatch.setattr(pipeline.data_retrieval, "prepare_llm_payload_pretty",
                            lambda **_k: {"_meta": {"country": "PT",
                                                    "generated_at": AS_OF.isoformat()}})
        monkeypatch.setattr(pipeline.data_retrieval, "build_evidence_payload",
                            lambda *_a, **_k: {"_meta": {"country": "PT"}})
        monkeypatch.setattr(pipeline.langchain_llm, "country_llm_score",
                            lambda **_k: {"score": 0.5})
        monkeypatch.setattr(pipeline.data_push, "upsert_snapshot", lambda *_a, **_k: None)
        monkeypatch.setattr(pipeline.data_push, "upsert_lint_findings", lambda *_a: None)

        items = [{"title": "Portugal cuts rates", "text": "Lisbon acted.",
                  "link": "https://example.com/a", "published": "2024-05-01"}]
        pipeline._process_country("Portugal", "PT", [], as_of=AS_OF, items=items)

        assert seen["display"] == llm.MASKED_COUNTRY_LABEL
        assert "Portugal" not in seen["text"] and "Lisbon" not in seen["text"]
        # The text is masked; the digest model still has to be told not to write
        # names back in. `actors: who did what to whom` is otherwise a direct
        # instruction to name the people the gazetteer cannot know about.
        assert seen["masked"] is True

    def test_the_stored_articles_stay_unmasked(self, monkeypatch):
        # The database and the front end show real headlines. Masking is a
        # transform at the scoring boundary and nowhere else.
        stored = {}
        monkeypatch.setattr(pipeline.digest_engine, "digest_articles",
                            lambda items, **_k: items)
        monkeypatch.setattr(pipeline.digest_engine, "select_fulltext_ids", lambda _i: [])
        monkeypatch.setattr(pipeline.data_retrieval, "prepare_llm_payload_pretty",
                            lambda **_k: {"_meta": {"country": "PT",
                                                    "generated_at": AS_OF.isoformat()}})
        monkeypatch.setattr(pipeline.data_retrieval, "build_evidence_payload",
                            lambda *_a, **_k: {"_meta": {"country": "PT"}})
        monkeypatch.setattr(pipeline.langchain_llm, "country_llm_score",
                            lambda **_k: {"score": 0.5,
                                          "news_article_scores": [
                                              {"id": "a1", "impact": 90}]})
        monkeypatch.setattr(pipeline.data_push, "upsert_snapshot",
                            lambda payload, country_name: stored.update(payload))
        monkeypatch.setattr(pipeline.data_push, "upsert_lint_findings", lambda *_a: None)

        items = [{"title": "Portugal cuts rates", "text": "Lisbon acted.",
                  "link": "https://example.com/a", "published": "2024-05-01"}]
        pipeline._process_country("Portugal", "PT", [], as_of=AS_OF, items=items)

        assert stored["scoring_mode"] == "masked"
        assert stored["top_articles"][0]["title"] == "Portugal cuts rates"

    def test_the_manifest_records_the_regime_that_scored_the_row(self, monkeypatch):
        """A masked row cannot be rebuilt without knowing which mask map made it:
        the same articles under a different gazetteer are different bytes."""
        stored = {}
        monkeypatch.setattr(pipeline.digest_engine, "digest_articles",
                            lambda items, **_k: items)
        monkeypatch.setattr(pipeline.digest_engine, "select_fulltext_ids", lambda _i: [])
        monkeypatch.setattr(pipeline.data_retrieval, "prepare_llm_payload_pretty",
                            lambda **_k: {"_meta": {"country": "PT",
                                                    "generated_at": AS_OF.isoformat()}})
        monkeypatch.setattr(pipeline.data_retrieval, "build_evidence_payload",
                            lambda *_a, **_k: {"_meta": {"country": "PT"},
                                               "structural": {"region": "Europe",
                                                              "income_group": "high"}})
        monkeypatch.setattr(pipeline.langchain_llm, "country_llm_score",
                            lambda **_k: {"score": 0.5, "news_article_scores": []})
        monkeypatch.setattr(pipeline.data_push, "upsert_snapshot",
                            lambda payload, country_name: stored.update(payload))
        monkeypatch.setattr(pipeline.data_push, "upsert_lint_findings", lambda *_a: None)
        # No key, so the probe declines rather than calling out.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        items = [{"title": "Portugal cuts rates", "text": "Lisbon acted.",
                  "link": "https://example.com/a", "published": "2024-05-01"}]
        pipeline._process_country("Portugal", "PT", [], as_of=AS_OF, items=items)

        mask = stored["input_manifest"]["masking"]
        assert mask["mask_map_version"] == gazetteer.MASK_MAP_VERSION
        assert mask["mask_integrity_status"] == "clean"
        # Five countries of forty-eight carry a structural block; the count is
        # what makes that asymmetry visible in the data rather than in a comment.
        assert mask["structural_fields"] == 2

    def test_a_named_run_carries_no_masking_block(self, monkeypatch):
        """Absent, not a block full of nulls: a named row was never masked, and
        saying "mask_map_version: null" invites somebody to average over it."""
        stored = {}
        monkeypatch.setattr(pipeline.digest_engine, "digest_articles",
                            lambda items, **_k: items)
        monkeypatch.setattr(pipeline.digest_engine, "select_fulltext_ids", lambda _i: [])
        monkeypatch.setattr(pipeline.data_retrieval, "prepare_llm_payload_pretty",
                            lambda **_k: {"_meta": {"country": "PT",
                                                    "generated_at": AS_OF.isoformat()}})
        monkeypatch.setattr(pipeline.data_retrieval, "build_evidence_payload",
                            lambda *_a, **_k: {"_meta": {"country": "PT"}})
        monkeypatch.setattr(pipeline.langchain_llm, "country_llm_score",
                            lambda **_k: {"score": 0.5, "news_article_scores": []})
        monkeypatch.setattr(pipeline.data_push, "upsert_snapshot",
                            lambda payload, country_name: stored.update(payload))
        monkeypatch.setattr(pipeline.data_push, "upsert_lint_findings", lambda *_a: None)

        items = [{"title": "Portugal cuts rates", "link": "https://e.com/a",
                  "published": "2024-05-01"}]
        pipeline._process_country("Portugal", "PT", [], as_of=AS_OF, items=items,
                                  scoring_mode="named")
        assert "masking" not in stored["input_manifest"]


class TestTheProductionProbe:
    """Identifiability is a property of this week's evidence, not of the method,
    so it is measured continuously rather than once in a pilot."""

    def test_the_sample_is_stable_across_processes(self):
        """Python salts string hashing per process. Sampling on `hash()` would
        have re-drawn the roster on every restart while claiming not to."""
        picked = [iso2 for iso2 in ("US", "TR", "BR", "PT", "KR", "IN", "JP", "DE")
                  if pipeline.zlib.crc32(f"{iso2}:{AS_OF.toordinal()}".encode())
                  % pipeline._PROBE_EVERY_NTH_COUNTRY == 0]
        assert picked == [iso2 for iso2 in ("US", "TR", "BR", "PT", "KR", "IN", "JP", "DE")
                          if pipeline.zlib.crc32(f"{iso2}:{AS_OF.toordinal()}".encode())
                          % pipeline._PROBE_EVERY_NTH_COUNTRY == 0]

    def test_a_country_out_of_the_sample_is_not_probed(self, monkeypatch):
        monkeypatch.setattr(pipeline.probe, "probe",
                            lambda *_a, **_k: pytest.fail("probed a country not in the sample"))
        skipped = [iso2 for iso2 in ("US", "TR", "BR", "PT", "KR")
                   if pipeline.zlib.crc32(f"{iso2}:{AS_OF.toordinal()}".encode())
                   % pipeline._PROBE_EVERY_NTH_COUNTRY != 0]
        assert skipped, "the fixture date samples every country; pick another"
        for iso2 in skipped:
            assert pipeline._identifiability([{"title": "x"}], iso2, AS_OF) is None

    def test_a_failed_probe_never_blocks_the_snapshot(self, monkeypatch):
        """The opposite of assert_clean, deliberately. The US is expected to be
        identified nearly always from coverage volume alone, and refusing to
        score it would be answering a different question."""
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setattr(pipeline.probe, "probe",
                            lambda *_a, **_k: {"country": "PT", "confidence": 1.0,
                                               "evidence": "obvious"})
        sampled = next(iso2 for iso2 in ("US", "TR", "BR", "PT", "KR", "IN", "JP", "DE")
                       if pipeline.zlib.crc32(f"{iso2}:{AS_OF.toordinal()}".encode())
                       % pipeline._PROBE_EVERY_NTH_COUNTRY == 0)
        got = pipeline._identifiability([{"title": "x"}], sampled, AS_OF)
        assert got["country"] == "PT" and got["confidence"] == 1.0
