"""Characterization tests for the two-stage prompt builders in
``backend.utils.ai.langchain_llm``: the digest serialization the scorer reads,
the FULL_TEXT block, and the prompt templates' placeholder wiring.
"""

import json

import pytest

from backend.utils.ai import constants as ai_constants
from backend.utils.ai import langchain_llm as llm


def _digest(severity: float) -> dict:
    return {
        "what_happened": "x",
        "actors": "y",
        "numbers": "not stated",
        "transmission": "not stated",
        "directly_about_country": True,
        "stage1_severity": severity,
    }


class TestDigestsToJson:
    def test_ids_passed_through_verbatim(self):
        items = [{"id": "a7", "title": "T", "digest": _digest(1.0), "stage1_severity": 1.0}]
        got = json.loads(llm._digests_to_json(items))
        assert got[0]["id"] == "a7"

    def test_digest_shape(self):
        items = [{"id": "a1", "source": "S", "published": "2026-07-20T00:00:00Z",
                  "title": "T", "digest": _digest(42.0), "stage1_severity": 42.0,
                  "summary": "should not appear"}]
        got = json.loads(llm._digests_to_json(items))[0]
        assert set(got) == {"id", "source", "published_at", "title", "digest", "stage1_severity"}
        assert got["published_at"] == "2026-07-20"
        assert got["digest"] == _digest(42.0)

    def test_degraded_item_gets_legacy_summary_shape(self):
        items = [{"id": "a1", "title": "T", "digest": None, "summary": "sum"}]
        got = json.loads(llm._digests_to_json(items))[0]
        assert set(got) == {"id", "source", "published_at", "title", "summary"}
        assert got["summary"] == "sum"

    def test_all_items_included_no_ten_cap(self):
        items = [{"id": f"a{i}", "title": "T", "digest": _digest(1.0), "stage1_severity": 1.0}
                 for i in range(1, 21)]
        assert len(json.loads(llm._digests_to_json(items))) == 20


class TestFulltextBlock:
    def test_header_and_order(self):
        items = [{"id": "a1", "title": "One", "text": "body1"},
                 {"id": "a2", "title": "Two", "text": "body2"}]
        block = llm._fulltext_block(items, ["a2", "a1"])
        assert block.index("--- id: a2 · Two ---") < block.index("--- id: a1 · One ---")
        assert "body1" in block and "body2" in block

    def test_cap_applied(self):
        items = [{"id": "a1", "title": "T", "text": "x" * (llm._MAX_FULLTEXT_CHARS + 500)}]
        block = llm._fulltext_block(items, ["a1"])
        assert len(block) < llm._MAX_FULLTEXT_CHARS + 100  # header + capped body

    def test_unknown_id_skipped_and_empty_gives_none(self):
        items = [{"id": "a1", "title": "T", "text": "body"}]
        assert "body" in llm._fulltext_block(items, ["a1", "a99"])
        assert llm._fulltext_block(items, []) == "(none)"
        assert llm._fulltext_block(items, ["a99"]) == "(none)"


class TestPromptTemplates:
    # .format() is the seam where brace-escaping bugs appear; pin that both
    # templates format cleanly with their exact placeholder sets.

    def test_ai_prompt_placeholders(self):
        # v3's placeholder set. The subject changed when v1/v2 were deleted;
        # the check did not — .format() is still where brace-escaping bugs in a
        # prompt full of JSON examples show up.
        prompt = ai_constants.AI_PROMPT_V3.format(
            country="Portugal",
            as_of_date="2026-07-27",
            evidence_json="{}",
            articles_json="[]",
            full_text_block="(none)",
        )
        assert "EVIDENCE_JSON" in prompt
        assert "ARTICLES_JSON" in prompt
        assert "FULL_TEXT" in prompt
        assert "Portugal" in prompt
        assert "2026-07-27" in prompt
        # No unsubstituted placeholder survived.
        assert "{country}" not in prompt and "{evidence_json}" not in prompt

    def test_digest_prompt_placeholders(self):
        prompt = ai_constants.DIGEST_PROMPT.format(
            country="Portugal", article_text="Some article text."
        )
        assert "Some article text." in prompt
        assert "Portugal" in prompt
        assert "{article_text}" not in prompt

    def test_digest_schema_is_strict(self):
        s = ai_constants.DIGEST_SCHEMA
        assert s["additionalProperties"] is False
        assert set(s["required"]) == set(s["properties"])
        sev = s["properties"]["stage1_severity"]
        assert sev["minimum"] == 0 and sev["maximum"] == 100
