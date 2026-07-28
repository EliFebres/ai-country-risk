"""Characterization tests for the deterministic helpers in
``backend.utils.ai.langchain_llm`` — everything around the LLM call that can
be tested without OpenAI: the sanctions gate and its date/iso2 plumbing.
"""

from datetime import date

from backend.utils.ai import langchain_llm as llm


class TestParseIsoDate:
    def test_valid(self):
        assert llm._parse_iso_date("2026-05-01") == date(2026, 5, 1)

    def test_datetime_prefix(self):
        assert llm._parse_iso_date("2026-05-01T12:00:00Z") == date(2026, 5, 1)

    def test_none_gives_date_min(self):
        assert llm._parse_iso_date(None) == date.min

    def test_garbage_gives_date_min(self):
        assert llm._parse_iso_date("not-a-date") == date.min


class TestLegalGateDecision:
    # Runs against the real legal_restrictions.yaml — the same data production
    # uses. RU/IR/KP/CU are full 1.0-gate entries.

    def test_russia_after_effective_date_fires(self):
        got = llm._legal_gate_decision("RU", date(2023, 1, 1))
        assert got is not None
        assert got["name"] == "Russia"
        assert "rule" in got and "sources" in got

    def test_russia_before_effective_date_does_not_fire(self):
        # effective_from is 2022-06-06.
        assert llm._legal_gate_decision("RU", date(2022, 1, 1)) is None

    def test_russia_on_effective_date_fires(self):
        assert llm._legal_gate_decision("RU", date(2022, 6, 6)) is not None

    def test_lowercase_iso2_fires(self):
        assert llm._legal_gate_decision("ru", date(2023, 1, 1)) is not None

    def test_unsanctioned_country_none(self):
        assert llm._legal_gate_decision("DE", date(2026, 1, 1)) is None

    def test_none_iso2_none(self):
        assert llm._legal_gate_decision(None, date(2026, 1, 1)) is None

    def test_iran_fires(self):
        got = llm._legal_gate_decision("IR", date(2026, 1, 1))
        assert got is not None and got["name"] == "Iran"


class TestExtractIso2:
    """Both payload shapes resolve to the same code.

    The evidence payload nests the country under `_meta`; the older panel
    payload has it at the top level. The sanctions lookup keys off whichever is
    present, and a miss means the badge silently never applies — so this is
    worth pinning for both shapes.
    """

    def test_top_level_country_key(self):
        # The shape prepare_llm_payload_pretty emits.
        assert llm._extract_iso2({"country": "DE"}) == "DE"

    def test_meta_country_key(self):
        # The shape build_evidence_payload emits.
        assert llm._extract_iso2({"_meta": {"country": "DE"}}) == "DE"

    def test_no_usable_key_gives_none(self):
        assert llm._extract_iso2({"country": "Germany"}) is None  # 2-char check
        assert llm._extract_iso2({}) is None
        assert llm._extract_iso2({"_meta": {}}) is None

    def test_lowercase_uppercased(self):
        assert llm._extract_iso2({"country": "de"}) == "DE"
        assert llm._extract_iso2({"_meta": {"country": "de"}}) == "DE"


class TestPromptArticleIds:
    """The one representation of "which articles reached the prompt".

    The provenance manifest hashes the same entries this derives its ids from,
    so if these two ever disagree a stored manifest would claim the model read
    something it never saw.
    """

    def _items(self, count, with_digest):
        return [
            {"id": f"a{i}", "title": f"t{i}", "summary": "s",
             **({"digest": {"facts": []}} if with_digest else {})}
            for i in range(1, count + 1)
        ]

    def test_digest_path_sends_every_article(self):
        items = self._items(15, with_digest=True)
        assert llm.prompt_article_ids(items) == {f"a{i}" for i in range(1, 16)}

    def test_legacy_fallback_caps_at_max_prompt_articles(self):
        items = self._items(15, with_digest=False)
        got = llm.prompt_article_ids(items)
        assert got == {f"a{i}" for i in range(1, llm._MAX_PROMPT_ARTICLES + 1)}
        assert len(got) == llm._MAX_PROMPT_ARTICLES

    def test_one_digest_is_enough_to_take_the_digest_path(self):
        # A single successful stage-1 digest means every article is sent, the
        # ones without a digest in their legacy shape.
        items = self._items(12, with_digest=False)
        items[3]["digest"] = {"facts": []}
        assert len(llm.prompt_article_ids(items)) == 12

    def test_no_articles(self):
        assert llm.prompt_article_ids([]) == set()

    def test_matches_the_prompt_entries_it_derives_from(self):
        items = self._items(4, with_digest=True)
        entries = llm.prompt_entries(items)
        assert llm.prompt_article_ids(items) == {e["id"] for e in entries}
