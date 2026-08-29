"""Everything that decides what leaves for the API, and what comes back.

Prompts, schemas, masking, the scoring call and the observe-only guarantee.

The assertions about masking are deliberately made against the **prompt string**
rather than against the masking functions. A function that masks correctly and a
prompt that carries a name are perfectly compatible failures, and only one of
them costs a ten-year series.

The observe-only property — no code path alters a model score — is attacked from
three angles, because nothing about a re-added floor looks wrong: the pipeline
runs, the snapshot writes, the front end renders a number. It is only wrong in
the sense that nobody can tell which part of a stored score came from the model
and which came from a rule, and by then there is a year of history built on it.

No network: every chat object is injected.
"""

import importlib
import inspect
import json
import pathlib
import re
from datetime import date

import pytest
import yaml

from backend.util import constants
from backend.util import pipeline
from backend.llm import constants as ai_constants
from backend.llm import langchain_llm as llm
from backend.util import policy
from backend.data_fetching import curated_loader
from backend.util import config
from backend.llm import client as ai_client
from backend.llm import context as llm_context
from backend.llm import digest_engine, gazetteer, probe, rewrite
from backend.llm import payload as payload_mod
from backend.util import provenance
import datetime as _dt

AS_OF = date(2024, 5, 6)
SANCTIONED_DAY = date(2023, 1, 1)   # after Russia's effective_from (2022-06-06)
BACKEND = pathlib.Path(__file__).resolve().parents[1]
ROSTER = [c["iso2"] for c in constants.COUNTRY_ROSTER]

PROMPT = ai_constants.AI_PROMPT_V3
SCHEMA = ai_constants.RISK_SCHEMA_V3

MODEL_REPLY = {
    "score_12m": 62, "score_3m": 77,
    "bullet_summary": "Model summary.",
    "ledger_scores": {}, "news_article_scores": [],
}


def lower() -> str:
    return PROMPT.lower()


class FakeStructured:
    def __init__(self, reply, sink=None):
        self._reply, self._sink = reply, sink

    def invoke(self, messages):
        if self._sink is not None:
            self._sink.append(messages[0].content)
        return self._reply


class FakeChat:
    def __init__(self, reply, sink=None):
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


# ---------------------------------------------------------------------------
# What actually leaves for the API
# ---------------------------------------------------------------------------

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


class TestTheGateRefusesToSend:
    """A masked snapshot that names its country is not a degraded result, it is
    a mislabelled one, and once it is in a ten-year series it looks exactly like
    a sound one. So `assert_clean` raises rather than warns — for a leak
    anywhere in a nested payload, not just where somebody remembered to look."""

    def test_a_clean_payload_passes(self):
        rewrite.assert_clean({"articles": [{"title": "The central bank held rates."}]})

    def test_a_leak_at_the_top_level_raises(self):
        with pytest.raises(rewrite.MaskLeak):
            rewrite.assert_clean("Turkey held rates.")

    def test_a_leak_nested_deep_raises(self):
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
            rewrite.assert_clean({"text": "Unlike Portugal, it devalued."},
                                 roster=config.PILOT_ROSTER)

    def test_the_error_names_what_leaked(self):
        with pytest.raises(rewrite.MaskLeak, match="Brazil"):
            rewrite.assert_clean("Brazil devalued.")

    def test_numbers_and_roles_do_not_trip_it(self):
        rewrite.assert_clean(
            {"text": "The central bank raised rates to 24% in the capital."})

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

    @staticmethod
    def _wire(monkeypatch, *, evidence=None, score=None, upsert=None):
        monkeypatch.setattr(pipeline.digest_engine, "select_fulltext_ids", lambda _i: [])
        monkeypatch.setattr(pipeline.llm_payload, "prepare_llm_payload_pretty",
                            lambda **_k: {"_meta": {"country": "PT",
                                                    "generated_at": AS_OF.isoformat()}})
        monkeypatch.setattr(pipeline.llm_payload, "build_evidence_payload",
                            lambda *_a, **_k: evidence or {"_meta": {"country": "PT"}})
        monkeypatch.setattr(pipeline.langchain_llm, "country_llm_score",
                            lambda **_k: score or {"score": 0.5})
        monkeypatch.setattr(pipeline.data_push, "upsert_snapshot",
                            upsert or (lambda *_a, **_k: None))
        monkeypatch.setattr(pipeline.data_push, "upsert_lint_findings", lambda *_a: None)

    ITEMS = [{"title": "Portugal cuts rates", "text": "Lisbon acted.",
              "link": "https://example.com/a", "published": "2024-05-01"}]

    def test_the_digest_prompt_never_sees_the_country(self, monkeypatch):
        seen = {}

        def fake_digest(items, *, country_display, iso2, as_of, masked=False,
                        content_cache=None):
            seen["display"] = country_display
            seen["masked"] = masked
            seen["text"] = json.dumps(items)
            return items

        monkeypatch.setattr(pipeline.digest_engine, "digest_articles", fake_digest)
        self._wire(monkeypatch)
        pipeline._process_country("Portugal", "PT", [], as_of=AS_OF, items=self.ITEMS)

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
        self._wire(monkeypatch,
                   score={"score": 0.5,
                          "news_article_scores": [{"id": "a1", "impact": 90}]},
                   upsert=lambda payload, country_name: stored.update(payload))
        pipeline._process_country("Portugal", "PT", [], as_of=AS_OF, items=self.ITEMS)

        assert stored["scoring_mode"] == "masked"
        assert stored["top_articles"][0]["title"] == "Portugal cuts rates"

    def test_the_manifest_records_the_regime_that_scored_the_row(self, monkeypatch):
        """A masked row cannot be rebuilt without knowing which mask map made it:
        the same articles under a different gazetteer are different bytes."""
        stored = {}
        monkeypatch.setattr(pipeline.digest_engine, "digest_articles",
                            lambda items, **_k: items)
        self._wire(monkeypatch,
                   evidence={"_meta": {"country": "PT"},
                             "structural": {"region": "Europe",
                                            "income_group": "high"}},
                   score={"score": 0.5, "news_article_scores": []},
                   upsert=lambda payload, country_name: stored.update(payload))
        # No key, so the probe declines rather than calling out.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        pipeline._process_country("Portugal", "PT", [], as_of=AS_OF, items=self.ITEMS)

        mask = stored["input_manifest"]["masking"]
        assert mask["mask_map_version"] == gazetteer.MASK_MAP_VERSION
        # The gazetteer is half of masking. The sweep changed twice on
        # 2026-08-03 while `mask_map_version` sat still, so a row stamped with
        # the map alone cannot say which behaviour produced it.
        assert mask["sweep_version"] == rewrite.SWEEP_VERSION
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
        self._wire(monkeypatch,
                   score={"score": 0.5, "news_article_scores": []},
                   upsert=lambda payload, country_name: stored.update(payload))
        pipeline._process_country("Portugal", "PT", [], as_of=AS_OF,
                                  items=[{"title": "Portugal cuts rates",
                                          "link": "https://e.com/a",
                                          "published": "2024-05-01"}],
                                  scoring_mode="named")
        assert "masking" not in stored["input_manifest"]


class TestTheFullTextRewriteCache:
    """The three articles the scorer weights most heavily were the three it
    could not reproduce.

    `input_manifest` hashes the bytes the model read. For the ids in
    `fulltext_ids` those bytes are model-generated prose that was kept nowhere,
    so a rebuild wrote a different sentence and a different hash — and the
    manifest's whole promise failed on exactly the evidence that mattered most.
    The cache is what turns that from a caveat into a property.
    """

    class _Cache:
        def __init__(self, seeded=None):
            self.rows = dict(seeded or {})
            self.reads, self.writes = [], []

        def read_rewrite_cache(self, hashes, version, mode):
            self.reads.append((sorted(hashes), version, mode))
            return {h: self.rows[h] for h in hashes if h in self.rows}

        def write_rewrite_cache(self, rows, version, mode):
            self.writes.append((rows, version, mode))
            for r in rows:
                self.rows[r["content_sha256"]] = r["rewritten"]
            return len(rows)

    def items(self):
        return [{"id": "a1", "text": "The minister resigned in the capital."}]

    def test_a_cached_body_is_reused_instead_of_re_rewritten(self, monkeypatch):
        from backend.util import provenance

        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setattr(pipeline.rewrite, "rewrite_body",
                            lambda *_a, **_k: pytest.fail("re-rewrote a cached body"))
        items = self.items()
        sha = provenance.text_sha256(items[0]["text"])
        cache = self._Cache({sha: "the minister resigned in the capital."})
        pipeline._rewrite_fulltext(items, ["a1"], "PT", cache=cache)
        assert items[0]["text"] == "the minister resigned in the capital."

    def test_a_fresh_rewrite_is_written_back(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setattr(pipeline.rewrite, "rewrite_body",
                            lambda *_a, **_k: "the minister resigned")
        cache = self._Cache()
        pipeline._rewrite_fulltext(self.items(), ["a1"], "PT", cache=cache)
        rows, version, mode = cache.writes[0]
        assert rows[0]["rewritten"] == "the minister resigned"
        assert version == rewrite.REWRITE_VERSION and mode == "masked"

    def test_the_same_body_next_week_costs_nothing(self, monkeypatch):
        """Weekly anchors over a 30-day window put one article in about four
        consecutive snapshots, and a top-severity article stays top-severity."""
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        calls = []
        monkeypatch.setattr(pipeline.rewrite, "rewrite_body",
                            lambda *a, **k: calls.append(1) or "masked body")
        cache = self._Cache()
        for _ in range(4):
            pipeline._rewrite_fulltext(self.items(), ["a1"], "PT", cache=cache)
        assert len(calls) == 1, f"paid {len(calls)} times for one body"

    def test_a_failed_rewrite_is_not_cached(self, monkeypatch):
        """It fails closed to title-only. Caching that would degrade the article
        on every future snapshot instead of letting the next run try again."""
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setattr(pipeline.rewrite, "rewrite_body", lambda *_a, **_k: "")
        cache = self._Cache()
        items = self.items()
        pipeline._rewrite_fulltext(items, ["a1"], "PT", cache=cache)
        assert items[0]["text"] == ""
        assert cache.rows == {} and not cache.writes

    def test_no_cache_behaves_exactly_as_before(self, monkeypatch):
        """The daily run passes None and must be untouched by any of this."""
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setattr(pipeline.rewrite, "rewrite_body",
                            lambda *_a, **_k: "masked body")
        items = self.items()
        pipeline._rewrite_fulltext(items, ["a1"], "PT")
        assert items[0]["text"] == "masked body"

    def test_a_broken_cache_degrades_to_rewriting(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setattr(pipeline.rewrite, "rewrite_body",
                            lambda *_a, **_k: "masked body")

        class _Boom:
            def read_rewrite_cache(self, *a, **k): raise RuntimeError("down")
            def write_rewrite_cache(self, *a, **k): raise RuntimeError("down")

        items = self.items()
        pipeline._rewrite_fulltext(items, ["a1"], "PT", cache=_Boom())
        assert items[0]["text"] == "masked body"

    def test_a_fully_cached_snapshot_needs_no_api_key(self, monkeypatch):
        """What makes `rebuild_snapshot` free. A rebuild that had to call the
        model would be paying to compare a fresh non-deterministic value against
        a stored one, which is not a comparison."""
        from backend.util import provenance

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(pipeline.rewrite, "rewrite_body",
                            lambda *_a, **_k: pytest.fail("called the model"))
        items = self.items()
        sha = provenance.text_sha256(items[0]["text"])
        cache = self._Cache({sha: "the minister resigned"})
        pipeline._rewrite_fulltext(items, ["a1"], "PT", cache=cache)
        assert items[0]["text"] == "the minister resigned"

    def test_a_miss_with_no_key_fails_closed(self, monkeypatch):
        """The rule the key check used to enforce by returning early, kept: an
        unmasked body must not reach the scorer because there was no key."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        items = self.items()
        pipeline._rewrite_fulltext(items, ["a1"], "PT", cache=self._Cache())
        assert items[0]["text"] == ""


# ---------------------------------------------------------------------------
# The structural block, which is written to be read by the masked model
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def facts():
    return curated_loader.load_structural_facts()


class TestStructuralFactsSurviveTheMask:
    """Masking strips the country's name and every prior that came with it,
    including legitimate ones — that a debt is issued in a currency the borrower
    prints, or that a euro-area member's is not. Those are supplied back as
    stated evidence, which creates a trap the rest of the project does not have:
    the block goes through the same gazetteer as the articles, and the World
    Bank's own region names do not survive the trip."""

    @pytest.mark.parametrize("iso2", ["US", "TR", "BR", "PT", "KR"])
    def test_a_filled_block_passes_the_gate(self, facts, iso2):
        rewrite.assert_clean(rewrite.mask_payload({"structural": facts[iso2]}, iso2))

    @pytest.mark.parametrize("iso2", ["US", "TR", "BR", "PT", "KR"])
    def test_no_field_is_mangled_on_the_way_through(self, facts, iso2):
        """Not just "no leak" — no *damage*. "Latin America and Caribbean"
        masking to "another country and Caribbean" passes the gate and is still
        wrong: it reads as though a country were named, and it teaches the
        scorer that the text has been tampered with."""
        masked = rewrite.mask_payload({"structural": facts[iso2]}, iso2)
        assert masked["structural"] == facts[iso2]

    @pytest.mark.parametrize("region",
                             sorted(curated_loader._STRUCTURAL_VOCAB["region"]))
    def test_every_allowed_region_survives_for_every_roster_country(self, region):
        """The guard for the forty-three countries not yet filled in.

        A region value is only safe if it survives being masked *as* every
        country, because the foreign pass flattens every roster country but the
        one being scored — which is how "Latin America" became "another country"
        while scoring Brazil rather than while scoring the US.
        """
        for iso2 in ROSTER:
            assert rewrite.mask_text(region, iso2) == region, f"{region!r} mangled for {iso2}"
            assert not gazetteer.scan(region, ROSTER), f"{region!r} scans as a roster term"

    def test_every_value_carries_a_source_and_a_date(self):
        """The point of the file is that a fact can be checked, not that it is
        present. A bare scalar is a value somebody pasted without saying where
        from, which is the fabrication this is supposed to prevent."""
        raw = yaml.safe_load(
            curated_loader.STRUCTURAL_FACTS.read_text(encoding="utf-8"))
        for iso2, block in raw.items():
            for field, entry in block.items():
                assert isinstance(entry, dict), f"{iso2}.{field} is not a cited entry"
                assert entry.get("source"), f"{iso2}.{field} has no source"
                assert entry.get("retrieved"), f"{iso2}.{field} has no retrieval date"

    def test_an_uncited_value_is_dropped_rather_than_trusted(self, tmp_path):
        path = tmp_path / "f.yaml"
        path.write_text("PT:\n  income_group: high\n", encoding="utf-8")
        assert curated_loader.load_structural_facts(path) == {}

    def test_an_unparseable_file_costs_the_block_not_the_run(self, tmp_path):
        """Read on the live path for every country: one bad file must not cost
        forty-seven countries their scores."""
        path = tmp_path / "f.yaml"
        path.write_text("PT: [unclosed\n", encoding="utf-8")
        assert curated_loader.load_structural_facts(path) == {}


# ---------------------------------------------------------------------------
# Observe-only: no code path alters a model score
# ---------------------------------------------------------------------------

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


@pytest.fixture
def scored(monkeypatch):
    """Score a country against a canned model reply. Returns a callable."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def run(iso2="RU", as_of=SANCTIONED_DAY, **overrides):
        reply = model_output(**overrides)
        monkeypatch.setattr(llm.ai_client, "build_chat",
                            lambda _key: FakeChat(reply))
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
        assert "not adjusted" in summary and "forced" not in summary

    def test_unsanctioned_country_is_untouched_and_unbadged(self, scored):
        out = scored(iso2="DE")
        assert out["score"] == pytest.approx(0.62)
        assert out["non_investable"] is False and out["legal_gate"] is None

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
        assert out["score"] is None and out["non_investable"] is False

    def test_a_war_flag_beside_a_low_score_survives_untouched(self, scored):
        # The old policy layer floored this to 0.90. Now the disagreement is
        # preserved and reported by lint instead.
        out = scored(iso2="DE", score_12m=44, score_3m=41)
        assert out["score"] == pytest.approx(0.44)
        assert out["condition_flags"]["war_on_territory"] is True

    def test_raw_and_gated_are_now_identical(self, scored):
        out = scored(iso2="RU")
        assert out["raw_score_12m"] == out["score"]
        assert out["raw_score_3m"] == out["score_3m"]


class TestNothingElseAssignsAScore:
    """Structural: grep the backend for a second writer of `score`."""

    def _python_sources(self):
        for path in BACKEND.rglob("*.py"):
            # "testing" as well as "tests": the folder is renamed in the
            # restructure, and a filter that stopped matching would quietly
            # start reporting this suite's own fixtures as offenders.
            if {"tests", "testing"} & set(path.parts) or "__pycache__" in path.parts:
                continue
            yield path, path.read_text(encoding="utf-8")

    def test_only_langchain_llm_writes_the_score_key(self):
        # Any other module building a dict with a "score" key is either a second
        # scorer or a mutation of this one's output. Not preceded by `==`:
        # `elif args.command == "score":` is a comparison, not a dict literal.
        pattern = re.compile(r'(?<!== )"score"\s*:')
        offenders = [
            str(path.relative_to(BACKEND))
            for path, source in self._python_sources()
            if pattern.search(source) and path.name != "langchain_llm.py"
        ]
        assert offenders == [], f"unexpected writers of the score key: {offenders}"

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


class TestPolicyCannotTouchAScore:
    """The property the whole rewrite exists to guarantee. There is no score in
    this module's inputs and none in its outputs."""

    def test_result_carries_no_score(self):
        result = policy.assess_investability(iso2="RU", as_of=SANCTIONED_DAY)
        assert set(result._fields) == {"non_investable", "legal_gate", "note",
                                       "applied_rules"}

    def test_takes_no_score_argument(self):
        # A score it cannot receive is a score it cannot change.
        assert set(inspect.signature(policy.assess_investability).parameters) == \
            {"iso2", "as_of"}

    def test_enforcement_symbols_are_gone(self):
        # Their absence is the contract; a re-added apply_policy would restore
        # the exact ambiguity (model number or rule number?) this removed.
        for name in ("apply_policy", "PolicyResult", "_floor", "_conflict_level",
                     "POLICY_PATH", "_load_policy"):
            assert not hasattr(policy, name), f"{name} should not exist any more"

    def test_does_not_fire_before_effective_from(self):
        result = policy.assess_investability(iso2="RU", as_of=date(2022, 1, 1))
        assert result.non_investable is False and result.legal_gate is None
        assert result.applied_rules == []

    def test_an_unparseable_date_is_always_in_force(self):
        # Fail-closed: a rule with a broken date is treated as already active,
        # because failing open would understate a legal restriction.
        assert policy._parse_iso_date("06/06/2022") == date.min

    def test_an_unreadable_file_degrades_to_no_badge(self, monkeypatch, tmp_path):
        monkeypatch.setattr(policy, "LEGAL_RULES_PATH", tmp_path / "missing.yaml")
        policy._load_legal_rules_index.cache_clear()
        try:
            assert policy._load_legal_rules_index() == {}
            assert policy.assess_investability(
                iso2="RU", as_of=SANCTIONED_DAY).non_investable is False
        finally:
            policy._load_legal_rules_index.cache_clear()

    def test_the_version_marks_the_regime_change(self):
        # Stored rows are split on this: a p1.0 score went through floors and a
        # sanctions override, a p2.0 score is the model's own.
        assert policy.POLICY_VERSION == "p2.0-observe-only"


class TestTheScaleBoundary:
    """`_from_100` is where the model's 0-100 grid meets the 0-1 everything
    downstream speaks. If it is wrong every stored score is wrong by two orders
    of magnitude and the front end renders a risk of 8500%."""

    @pytest.mark.parametrize("raw,expected", [(0, 0.0), (37, 0.37), (100, 1.0)])
    def test_integers(self, raw, expected):
        assert llm._from_100(raw) == pytest.approx(expected)

    def test_none_stays_none(self):
        # A null subscore means "the evidence is silent", not zero risk.
        assert llm._from_100(None) is None

    def test_bool_is_not_a_number(self):
        # True would otherwise sail through float() as 0.01.
        assert llm._from_100(True) is None

    def test_garbage_string_is_none(self):
        assert llm._from_100("n/a") is None

    @pytest.mark.parametrize("raw,expected", [(140, 1.0), (-20, 0.0)])
    def test_out_of_range_is_clamped(self, raw, expected):
        # Strict structured output enforces the schema's shape but not its
        # minimum/maximum, so the clamp is the only guard.
        assert llm._from_100(raw) == expected

    def test_the_failure_result_has_every_key_a_caller_reads(self):
        # A failure path that returned a short dict would KeyError in the
        # upsert instead of cleanly skipping the country.
        got = llm._failure_result()
        assert got["score"] is None
        for key in ("bullet_summary", "subscores", "ledger_scores",
                    "news_article_scores", "score_3m", "raw_score_12m",
                    "raw_score_3m", "subscore_evidence", "condition_flags",
                    "evidence_coverage", "applied_rules", "legal_gate",
                    "non_investable", "model_id", "prompt_version",
                    "policy_version"):
            assert key in got
        assert got["model_id"] and got["prompt_version"] and got["policy_version"]


# ---------------------------------------------------------------------------
# The prompt, which is product logic with no compiler
# ---------------------------------------------------------------------------

class TestForbiddenLanguage:
    """The enforcement vocabulary that must never come back.

    Enforcement used to live in the prompt, then in a policy module, and now
    nowhere. A floor or a pinned score creeping back into the wording would
    restore the exact problem the deletion solved — a stored number that is part
    model judgement and part rule, with no way to tell which — and it would do
    it without touching a line of Python.

    Phrased precisely rather than by bare substring. A crude ban on "cap" also
    forbids "capability", "capacity" and "capital controls", and one on
    "sanction" forbids the impact band's legitimate "binding sanctions", which
    is *evidence about an event*. Banning those would make the test fire on
    correct prompts and get deleted, which is worse than not having it.
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
        assert "high churn increases" not in text and "churn raises" not in text
        for sentence in text.split("."):
            if "penaliz" in sentence:
                assert "not penalized" in sentence or "never" in sentence, sentence

    def test_condition_flags_are_observations_only(self):
        assert "Condition flags: observations only" in PROMPT
        assert "Nothing downstream will alter your scores" in PROMPT
        assert "must not adjust them to anticipate any rule" in PROMPT

    def test_edge_protection_must_not_raise_a_score(self):
        assert "MUST NOT raise any risk score" in PROMPT
        assert "do not let a high value raise friction" in lower()


class TestTheSchemaIsStrict:
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
        assert set(evidence) == {"friction", "order_uncertainty",
                                 "information_capacity"}

    @pytest.mark.parametrize("field", ["score_3m", "score_12m", "evidence_coverage"])
    def test_scores_are_bounded_integers(self, field):
        spec = SCHEMA["properties"][field]
        assert spec["type"] == "integer"
        assert spec["minimum"] == 0 and spec["maximum"] == 100

    def test_ledger_scores_allow_null_for_silent_evidence(self):
        for spec in SCHEMA["properties"]["ledger_scores"]["properties"].values():
            assert spec["type"] == ["integer", "null"]

    def test_summary_cap_matches_the_truncation_constant(self):
        assert SCHEMA["properties"]["bullet_summary"]["maxLength"] == \
            llm._MAX_SUMMARY_CHARS

    def test_the_digest_schema_is_strict_too(self):
        s = ai_constants.DIGEST_SCHEMA
        assert s["additionalProperties"] is False
        assert set(s["required"]) == set(s["properties"])
        sev = s["properties"]["stage1_severity"]
        assert sev["minimum"] == 0 and sev["maximum"] == 100

    def test_the_masked_digest_prompt_targets_actors_by_name(self):
        """`actors: who did what to whom` is where names get written back in,
        so the rule has to name that field rather than gesture at the output."""
        prompt = ai_constants.DIGEST_PROMPT.format(
            country="the country", article_text="x",
            mask_rule=ai_constants.DIGEST_MASK_RULE)
        assert "This applies to `actors` above all" in prompt
        assert "never the president" in prompt
        # The numbers are the evidence; a masked digest that lost them would be
        # measuring something else entirely.
        assert "Keep every NUMBER exactly as written" in prompt

    def test_the_version_stamps_the_masked_production_regime(self):
        # v3.0-friction-framework -> v3.1 -> v4.0-masked-production. Every one
        # had snapshots scored under it, so each gets its own stamp rather than
        # rewriting history under the previous wording.
        assert ai_constants.PROMPT_VERSION == "v4.0-masked-production"


# ---------------------------------------------------------------------------
# The identifiability probe — the meter that says whether masking held
# ---------------------------------------------------------------------------
#
# Everything above asserts that masking *ran*. This asserts that the instrument
# measuring whether it *worked* is itself sound. A probe that scores leniently
# reports a clean corpus that is leaking, and nothing else in the suite catches
# that — which is why it survived the cut ahead of everything else.


class _ProbeChat:
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


def _probe_item(**over):
    base = dict(
        title="Turkey's central bank holds rates as the lira slides",
        snippet="Ankara held rates on Wednesday.",
        text="The Central Bank of Turkey held rates at 24% on Wednesday in Ankara.",
        link="https://www.theguardian.com/world/2018/mar/14/turkey-lira-central-bank",
        _theme="order",
    )
    base.update(over)
    return base


class TestTheProbeReadsAnAnswer:
    def test_it_returns_a_guess(self):
        chat = _ProbeChat({"country": "TR", "confidence": 0.8,
                           "evidence": "85% inflation",
                           "alternatives": [{"country": "TR", "probability": 0.7},
                                            {"country": "AR", "probability": 0.2}],
                           "insufficient_information": False})
        got = probe.probe([_probe_item()], "k", model_chat=chat)
        assert got["country"] == "TR" and got["confidence"] == 0.8
        assert got["alternatives"][0] == {"country": "TR", "probability": 0.7}
        assert got["insufficient_information"] is False

    def test_a_model_that_omits_the_distribution_still_parses(self):
        """The distribution is new; a stray response without it must degrade to
        an empty list rather than taking the probe down."""
        chat = _ProbeChat({"country": "TR", "confidence": 0.8, "evidence": "x"})
        got = probe.probe([_probe_item()], "k", model_chat=chat)
        assert got["alternatives"] == [] and got["insufficient_information"] is False

    def test_a_malformed_alternative_is_dropped_not_fatal(self):
        chat = _ProbeChat({"country": "TR", "confidence": 0.5, "evidence": "x",
                           "alternatives": [{"country": "TR", "probability": "high"},
                                            {"country": "BR", "probability": 0.3},
                                            "nonsense"]})
        got = probe.probe([_probe_item()], "k", model_chat=chat)
        assert got["alternatives"] == [{"country": "BR", "probability": 0.3}]

    def test_insufficient_information_is_carried_through(self):
        """The answer that lets a prior admit it is a prior. Losing it would put
        the meter back to reporting base rates as identifications."""
        chat = _ProbeChat({"country": "US", "confidence": 0.4,
                           "evidence": "base rates", "insufficient_information": True})
        assert probe.probe([_probe_item()], "k",
                           model_chat=chat)["insufficient_information"]

    def test_the_bundle_never_contains_urls(self):
        # ".../2018/mar/14/turkey-lira-central-bank" would hand over the answer.
        chat = _ProbeChat({"country": "ZZ", "confidence": 0.1, "evidence": "-"})
        probe.probe([_probe_item()], "k", model_chat=chat)
        assert "theguardian.com" not in chat.prompts[0]

    def test_a_failed_probe_is_not_recorded_as_an_identification(self):
        # The opposite of the leakage scan's fail-closed: this is a
        # measurement, and a failed measurement must not read as a hit.
        got = probe.probe([_probe_item()], "k",
                          model_chat=_ProbeChat(raises=RuntimeError("model down")))
        assert got["country"] == "ZZ" and got["confidence"] == 0.0

    def test_an_empty_bundle_is_not_a_guess(self):
        assert probe.probe([], "k")["country"] == "ZZ"


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


class TestTheSpreadIsTheMeter:
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
        assert got["ceiling"] == 1.0 and got["spread"] == 1.0

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


class TestTheInstrumentIsConfigurableAndVersioned:
    """The scorer can be pointed elsewhere, and nothing can do it quietly.

    Two properties, and the second is the one that matters. Pointing the client
    at another vendor is a two-line change anybody could have made by editing a
    constant; what did not exist was anything that *noticed*. The model was
    absent from `FROZEN_FIELDS`, absent from both cache keys, and stamped into
    every manifest from the literal rather than from the call — so a swapped
    scorer resumed over the old rows, read the old rewrites, and signed them
    with the old name.

    The unset-environment case is asserted first and hardest, because the daily
    run is owed byte-identical behaviour: nobody running `main.py` should be
    able to tell that a comparison harness exists.
    """

    def test_unset_environment_is_todays_configuration(self, monkeypatch):
        for name in ("SCORING_MODEL", "SCORING_BASE_URL", "SCORING_API_KEY",
                     "SCORING_EXTRA_BODY", "DIGEST_MODEL", "DIGEST_BASE_URL",
                     "DIGEST_API_KEY", "DIGEST_EXTRA_BODY"):
            monkeypatch.delenv(name, raising=False)

        scoring = ai_client.build_chat("a-key")
        assert scoring.model_name == ai_client.MODEL_NAME
        assert scoring.temperature == 0.0
        assert scoring.max_retries == 0
        assert scoring.seed == ai_client.SEED
        assert scoring.openai_api_base is None

        for build in (ai_client.build_digest_chat, ai_client.build_stage1_chat):
            stage1 = build("a-key")
            assert stage1.model_name == ai_client.DIGEST_MODEL_NAME
            assert stage1.openai_api_base is None
            assert stage1.seed == ai_client.SEED

    def test_the_scoring_endpoint_follows_its_own_environment(self, monkeypatch):
        monkeypatch.setenv("SCORING_MODEL", "some-candidate")
        monkeypatch.setenv("SCORING_BASE_URL", "https://elsewhere.example/v1")
        monkeypatch.setenv("SCORING_API_KEY", "candidate-key")
        chat = ai_client.build_chat("openai-key")
        assert chat.model_name == "some-candidate"
        assert str(chat.openai_api_base) == "https://elsewhere.example/v1"
        assert ai_client.scoring_model() == "some-candidate"

    def test_a_digest_override_cannot_move_the_masking_instrument(self, monkeypatch):
        """The whole reason `build_digest_chat` and `build_stage1_chat` are two.

        One builder served the article digest *and* the body rewrite, the digest
        sweep, the identifiability probe and the leakage scan. Measuring a
        cheaper digest model through that builder would have swapped the four
        masking passes as a side effect — and masking is the claim the pilot
        rests on, not a line item.
        """
        monkeypatch.setenv("DIGEST_MODEL", "some-cheap-digest")
        monkeypatch.setenv("DIGEST_BASE_URL", "https://elsewhere.example/v1")
        monkeypatch.setenv("DIGEST_API_KEY", "candidate-key")

        assert ai_client.build_stage1_chat("openai-key").model_name == "some-cheap-digest"
        masking = ai_client.build_digest_chat("openai-key")
        assert masking.model_name == ai_client.DIGEST_MODEL_NAME
        assert masking.openai_api_base is None

    def test_a_malformed_extra_body_raises_rather_than_being_dropped(self, monkeypatch):
        """A thinking pin that silently fails costs money and misreports it.

        DeepSeek bills reasoning tokens as output. A dropped
        `{"thinking": {"type": "disabled"}}` does not error — it just returns a
        correct answer at several times the quoted price, and the bake-off
        reports that price as the candidate's.
        """
        monkeypatch.setenv("SCORING_EXTRA_BODY", "{not json")
        with pytest.raises(ValueError, match="not valid JSON"):
            ai_client.build_chat("a-key")

        monkeypatch.setenv("SCORING_EXTRA_BODY", '["a", "list"]')
        with pytest.raises(ValueError, match="must be a JSON object"):
            ai_client.build_chat("a-key")

    def test_extra_body_reaches_the_client(self, monkeypatch):
        monkeypatch.setenv("SCORING_EXTRA_BODY", '{"thinking": {"type": "disabled"}}')
        chat = ai_client.build_chat("a-key")
        assert chat.extra_body == {"thinking": {"type": "disabled"}}

    def test_both_masking_cache_versions_move_with_the_model(self, monkeypatch):
        """The cache key said which instructions produced a row, never which model.

        Both versions hashed their own prompt and schema and stopped there, so a
        stage-1 model swap served every previously rewritten body back as a hit,
        produced by the old model, with the manifest reporting the same
        `rewrite_version` either way. Two masking behaviours under one label —
        the exact defect the comment above those constants was written about,
        surviving only because the model had never moved.
        """
        before = (rewrite.SWEEP_VERSION, rewrite.REWRITE_VERSION)
        monkeypatch.setattr(ai_client, "DIGEST_MODEL_NAME", "a-different-model")
        try:
            reloaded = importlib.reload(rewrite)
            assert reloaded.SWEEP_VERSION != before[0]
            assert reloaded.REWRITE_VERSION != before[1]
        finally:
            monkeypatch.undo()
            importlib.reload(rewrite)
        assert (rewrite.SWEEP_VERSION, rewrite.REWRITE_VERSION) == before

    def test_the_freeze_carries_the_model_and_the_seed(self, monkeypatch):
        """`FROZEN_FIELDS` versioned the evidence and never the instrument."""
        from backend.util.pilot import score as pilot_score

        assert {"SCORING_MODEL", "DIGEST_MODEL", "SEED"} <= set(pilot_score.FROZEN_FIELDS)

        monkeypatch.setenv("SCORING_MODEL", "some-candidate")
        current = pilot_score.versions()
        assert current["SCORING_MODEL"] == "some-candidate"
        assert current["SEED"] == str(ai_client.SEED)

        frozen = dict(current, SCORING_MODEL=ai_client.MODEL_NAME)
        moved = pilot_score.drift(frozen, current)
        assert moved == {"SCORING_MODEL": (ai_client.MODEL_NAME, "some-candidate")}

    def test_the_manifest_stamps_the_model_that_answered(self, monkeypatch):
        """Not the one the file names. `_failure_result` had the same bug."""
        monkeypatch.setenv("SCORING_MODEL", "some-candidate")
        assert llm._failure_result()["model_id"] == "some-candidate"


class TestOnlyProductionWritesLint:
    """Lint findings are keyed `(country, as_of, rule)` — no scoring mode.

    So every arm that shares `(country, as_of)` with the masked twin writes over
    production's rows on its own primary key: the two diagnostic modes, and every
    bake-off candidate. Invisibly, too — the bake-off reads lint back out of the
    in-memory manifest while `reports.lint_findings` reads the table, so the two
    disagree with nothing to say so. The write follows `upsert` for exactly the
    reason the snapshot does.
    """

    ITEMS = [{"title": "Portugal cuts rates", "text": "Lisbon acted.",
              "link": "https://example.com/a", "published": "2024-05-01"}]

    @staticmethod
    def _wire(monkeypatch, written):
        monkeypatch.setattr(pipeline.digest_engine, "select_fulltext_ids", lambda _i: [])
        monkeypatch.setattr(pipeline.digest_engine, "digest_articles",
                            lambda items, **_k: items)
        monkeypatch.setattr(pipeline.llm_payload, "prepare_llm_payload_pretty",
                            lambda **_k: {"_meta": {"country": "PT",
                                                    "generated_at": AS_OF.isoformat()}})
        monkeypatch.setattr(pipeline.llm_payload, "build_evidence_payload",
                            lambda *_a, **_k: {"_meta": {"country": "PT"}})
        # A war flag beside a low score: a finding that genuinely fires, so an
        # empty `written` means the write was skipped and not that lint was quiet.
        monkeypatch.setattr(pipeline.langchain_llm, "country_llm_score",
                            lambda **_k: {"score": 0.05,
                                          "condition_flags": {"war_on_territory": True}})
        monkeypatch.setattr(pipeline.data_push, "upsert_snapshot", lambda *_a, **_k: None)
        monkeypatch.setattr(pipeline.data_push, "upsert_lint_findings",
                            lambda findings: written.extend(findings))

    def test_the_production_arm_still_records_its_findings(self, monkeypatch):
        written = []
        self._wire(monkeypatch, written)
        pipeline._process_country("Portugal", "PT", [], as_of=AS_OF,
                                  items=self.ITEMS, upsert=True)
        assert written, "the masked production arm must still write lint"

    def test_a_non_production_arm_records_none(self, monkeypatch):
        written = []
        self._wire(monkeypatch, written)
        pipeline._process_country("Portugal", "PT", [], as_of=AS_OF,
                                  items=self.ITEMS, upsert=False)
        assert written == []


class TestTheDigestCacheKeyFollowsTheDigestModel:
    """The key and the chat must resolve through the same accessor.

    If the cache keys on the pinned constant while `build_stage1_chat` honours
    `DIGEST_MODEL`, a bake-off serves the incumbent's digests back to a candidate
    under the candidate's name — the two disagree and nothing says so. That is the
    same defect as a version constant nobody bumps, and it is the one thing the
    scoping in `client.py` cannot catch on its own.
    """

    def test_the_cache_reads_and_writes_under_the_effective_model(self, monkeypatch):
        monkeypatch.setenv("DIGEST_MODEL", "some-candidate")
        seen = {"read": [], "write": []}

        class Cache:
            def read_digest_cache(self, hashes, digest_model, mode):
                seen["read"].append(digest_model)
                return {}

            def write_digest_cache(self, rows, digest_model, mode):
                seen["write"].append(digest_model)

        class Structured:
            def batch(self, prompts, **_k):
                return [{"what_happened": "a thing", "actors": "someone",
                         "numbers": "1", "stage1_severity": 10}
                        for _ in prompts]

        class Chat:
            def with_structured_output(self, **_k):
                return Structured()

        monkeypatch.setattr(digest_engine.ai_client, "build_stage1_chat",
                            lambda *_a, **_k: Chat())
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        digest_engine.digest_articles(
            [{"id": "a1", "title": "T", "text": "body text here"}],
            country_display="Country A", iso2="PT", as_of=AS_OF,
            content_cache=Cache())

        assert seen["read"] == ["some-candidate"], seen
        assert seen["write"] == ["some-candidate"], seen
        # And the same accessor the chat was built from, so they cannot drift.
        assert ai_client.digest_model() == "some-candidate"


class TestTheContextBlockIsOptOnly:
    """p3 must not be able to change what the daily run does by existing.

    The same contract `client.scoring_model()` holds for the model: an unset
    environment is byte-identical to the behaviour before any of this was
    written. An A/B that quietly moved the production payload would be measuring
    itself.
    """

    def test_an_unset_variant_is_todays_contract(self, monkeypatch):
        monkeypatch.delenv("PAYLOAD_VARIANT", raising=False)
        assert provenance.payload_variant() == "p2"
        assert provenance.payload_version() == provenance.PAYLOAD_VERSION

    def test_a_nonsense_variant_raises_rather_than_defaulting(self, monkeypatch):
        """Silently falling back to p2 would run an A/B whose arms are identical
        and report it as a null result."""
        monkeypatch.setenv("PAYLOAD_VARIANT", "p9")
        with pytest.raises(ValueError, match="PAYLOAD_VARIANT"):
            provenance.payload_variant()

    def test_a_payload_without_the_block_renders_the_old_prompt_exactly(self):
        """The instruction follows the data. No block, no instruction, and the
        stamped prompt version does not move."""
        base = {"_meta": {"country": "PT"}}
        assert not base.get("trailing_context")
        rendered = ai_constants.AI_PROMPT_V3.format(
            country="the country", as_of_date="2019-06-03",
            evidence_json=json.dumps(base), articles_json="[]",
            full_text_block="(no full-text articles supplied)")
        assert ai_constants.TRAILING_CONTEXT_RULE not in rendered

    def test_the_block_lands_inside_the_evidence_payload(self):
        """Not a prompt placeholder — inside the dict, where `mask_payload` and
        `assert_clean` already run over everything whole."""
        out = payload_mod.build_evidence_payload(
            "PT", as_of=_dt.date(2019, 6, 3),
            trailing_context=[{"quarter": "2018Q4", "summary": "A thing happened."}])
        assert out["trailing_context"]["quarters"][0]["quarter"] == "2018Q4"
        assert "trailing_context" in json.dumps(out)

    def test_an_empty_block_is_omitted_rather_than_sent_empty(self):
        """Same reasoning as `structural`: an empty block reads to the model as
        'this country has no history', which is false and worse than silence."""
        out = payload_mod.build_evidence_payload(
            "PT", as_of=_dt.date(2019, 6, 3), trailing_context=[])
        assert "trailing_context" not in out


class TestTheTrendRuleIsToldAndNotGiven:
    """Arm C carries no evidence, which is the only reason it isolates anything.

    `trend_1y` and `trend_5y` have been on every indicator since p1 and are
    serialized into every prompt. So the trend arm cannot follow the data the
    way the context arm does -- there is no new data to follow, and the fields
    are present in every arm whether it was told about them or not. The variant
    is therefore environment-selected, and these pin the two properties that
    makes safe: it adds no bytes of evidence, and it stamps a version so "told"
    and "not told" are distinguishable afterwards.
    """

    def test_an_unset_variant_renders_the_prompt_unchanged(self, monkeypatch):
        monkeypatch.delenv("PROMPT_VARIANT", raising=False)
        assert provenance.prompt_variant() == ""
        assert provenance.prompt_version() == ai_constants.PROMPT_VERSION

    def test_a_nonsense_variant_raises_rather_than_defaulting(self, monkeypatch):
        """Falling back to the base prompt would run an A/B whose arms are
        identical and report it as a null result."""
        monkeypatch.setenv("PROMPT_VARIANT", "trendy")
        with pytest.raises(ValueError, match="PROMPT_VARIANT"):
            provenance.prompt_variant()

    def test_the_freeze_reads_the_prompt_it_would_actually_render(self, monkeypatch):
        """The defect that put `v4.0` in a committed p3 result file whose own
        rows all say `v4.1`."""
        from backend.util.pilot import score as pilot_score

        monkeypatch.setenv("PROMPT_VARIANT", "trend")
        assert pilot_score.versions()["PROMPT_VERSION"] ==             ai_constants.PROMPT_VERSION_TREND
        monkeypatch.delenv("PROMPT_VARIANT")
        assert pilot_score.versions()["PROMPT_VERSION"] ==             ai_constants.PROMPT_VERSION

    def test_the_rule_names_the_fields_and_what_their_sign_means(self):
        """Naming them is not enough: rising is worse for debt and better for
        growth, and a model left to infer that per indicator infers it
        inconsistently."""
        rule = ai_constants.TREND_FIELDS_RULE
        assert "trend_1y" in rule and "trend_5y" in rule
        assert "sign" in rule.lower()
        # Unknown must not read as flat -- the distinction the whole census
        # exists to preserve.
        assert "unknown, not flat" in rule

    def test_the_trend_arm_carries_no_evidence_the_others_lack(self):
        """The payload is identical with the variant set and unset. If this ever
        fails, arm C has stopped being a prompt arm and its result means nothing
        it claims to mean."""
        import os

        args = dict(as_of=_dt.date(2019, 6, 3), series={}, vintage_as_of=None)
        os.environ.pop("PROMPT_VARIANT", None)
        without = json.dumps(payload_mod.build_evidence_payload("PT", **args))
        os.environ["PROMPT_VARIANT"] = "trend"
        try:
            with_variant = json.dumps(payload_mod.build_evidence_payload("PT", **args))
        finally:
            os.environ.pop("PROMPT_VARIANT", None)
        assert without == with_variant


class TestContextIsMaskedBeforeItIsCached:
    """A leak cached is a leak served to every anchor in the quarter."""

    ITEMS = [{"title": "Portugal ruling", "text": "Lisbon acted.",
              "published": "2018-05-02", "link": "u1"}]

    def _build(self, monkeypatch, paragraph, sweep=None):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setattr(llm_context, "_paragraph",
                            lambda *_a, **_k: paragraph)
        monkeypatch.setattr(llm_context.rewrite, "sweep_digest",
                            lambda d, key, **k: sweep)
        written = []

        class Cache:
            def read_context_cache(self, keys, version, mode):
                return {}

            def write_context_cache(self, rows, version, mode):
                written.extend(rows)
                return len(rows)

        out = llm_context.build("PT", _dt.date(2019, 1, 7), cache=Cache(),
                                select=lambda i, a, n, bounds=None: list(self.ITEMS))
        return out, written

    def test_a_leaking_paragraph_is_dropped_and_never_cached(self, monkeypatch):
        out, written = self._build(monkeypatch, "Portugal did a thing.", sweep=None)
        assert out == [], "a paragraph naming the country reached the payload"
        assert written == [], "a leaking paragraph was cached"

    def test_a_clean_paragraph_survives_and_is_cached(self, monkeypatch):
        out, written = self._build(monkeypatch, "The country did a thing.")
        assert len(out) == llm_context.QUARTERS
        assert out[0]["summary"] == "The country did a thing."
        assert len(written) == llm_context.QUARTERS

    def test_the_sweep_result_is_preferred_when_it_returns_one(self, monkeypatch):
        """The paragraph rides in `what_happened` so no field has to be added to
        `_DIGEST_SWEEP_FIELDS`, which would move SWEEP_VERSION and invalidate
        every cached masked digest to rename a key the prompt never reads."""
        out, _ = self._build(monkeypatch, "The country did a thing.",
                             sweep={"what_happened": "A swept sentence."})
        assert out[0]["summary"] == "A swept sentence."

    def test_the_cache_version_carries_the_masking_versions(self, monkeypatch):
        """A masking change must invalidate context for the same reason it
        invalidates digests."""
        seen = {}

        class Cache:
            def read_context_cache(self, keys, version, mode):
                seen["version"] = version
                return {}

            def write_context_cache(self, rows, version, mode):
                return 0

        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setattr(llm_context, "_paragraph", lambda *_a, **_k: None)
        llm_context.build("PT", _dt.date(2019, 1, 7), cache=Cache(),
                          select=lambda i, a, n, bounds=None: list(self.ITEMS))
        assert seen["version"].startswith(llm_context.CONTEXT_VERSION + ":")
        assert gazetteer.MASK_MAP_VERSION in seen["version"]
        assert rewrite.SWEEP_VERSION in seen["version"]

    def test_the_freeze_reads_the_effective_payload_contract(self, monkeypatch):
        """`versions()` must report what the run built, not what the file names.

        The same defect the SCORING_MODEL accessor fixed: a freeze that reads a
        module constant cannot see an environment override, which is the one
        case it exists to catch. Caught here because an A/B result file stamped
        itself p2 while running p3.
        """
        from backend.util.pilot import score as pilot_score
        monkeypatch.setenv("PAYLOAD_VARIANT", "p3-context")
        assert pilot_score.versions()["PAYLOAD_VERSION"] == "p3-context"
        monkeypatch.delenv("PAYLOAD_VARIANT")
        assert pilot_score.versions()["PAYLOAD_VERSION"] == "p2"
