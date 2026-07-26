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


class TestExtractIso2AndAsof:
    def test_country_key_with_iso2_value(self):
        # This is the shape prepare_llm_payload_pretty actually emits.
        iso2, as_of = llm._extract_iso2_and_asof({"country": "DE"})
        assert iso2 == "DE"
        assert as_of == date.today()

    def test_no_usable_key_gives_none(self):
        iso2, _ = llm._extract_iso2_and_asof({"country": "Germany"})
        assert iso2 is None  # 2-char check fails on a full name

    def test_lowercase_uppercased(self):
        iso2, _ = llm._extract_iso2_and_asof({"country": "de"})
        assert iso2 == "DE"
