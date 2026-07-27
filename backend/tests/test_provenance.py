"""Characterization tests for ``backend.utils.provenance``.

Provenance is the record that makes a stored score reproducible, so its own
behavior has to be pinned: the hashes must be stable across runs (a drifting
hash would read as "the inputs changed" on every re-run), a missing field must
degrade to None rather than raise, and ``vintage_scheme`` must survive a payload
with no ``_meta`` — Phase E filters on that string.

Pure functions only: no network, no database, no clock.
"""

import hashlib
import json

import pytest

from backend.utils import provenance


class TestTextSha256:
    def test_known_vector(self):
        # Pinned against hashlib itself: this hash ends up in the database, so a
        # change in how the text is encoded must fail here, not silently.
        assert provenance.text_sha256("abc") == hashlib.sha256(b"abc").hexdigest()

    def test_none_gives_none(self):
        assert provenance.text_sha256(None) is None

    def test_empty_gives_none(self):
        # "No text" and "the hash of the empty string" are different facts.
        assert provenance.text_sha256("") is None

    def test_unicode_is_utf8(self):
        text = "Grécia — inflação 25%"
        assert provenance.text_sha256(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()

    def test_stable_across_calls(self):
        assert provenance.text_sha256("x") == provenance.text_sha256("x")


class TestArticleManifestEntry:
    def test_prefers_content_over_text(self):
        got = provenance.article_manifest_entry({"id": "a1", "content": "full body", "text": "trafilatura"})
        assert got["content_sha256"] == provenance.text_sha256("full body")
        assert got["content_chars"] == len("full body")

    def test_falls_back_to_text(self):
        got = provenance.article_manifest_entry({"id": "a1", "text": "trafilatura"})
        assert got["content_sha256"] == provenance.text_sha256("trafilatura")

    def test_prefers_link_over_publisher_link(self):
        # `resolve_and_enrich` overwrites `link` with the resolved publisher URL,
        # and that is the value risk_snapshot_article.url stores.
        got = provenance.article_manifest_entry({
            "id": "a1", "link": "https://resolved.example/x",
            "publisher_link": "https://stale.example/x",
        })
        assert got["url"] == "https://resolved.example/x"

    def test_falls_back_to_publisher_link(self):
        got = provenance.article_manifest_entry({"id": "a1", "publisher_link": "https://p.example/x"})
        assert got["url"] == "https://p.example/x"

    def test_missing_body_gives_no_hash_and_zero_chars(self):
        got = provenance.article_manifest_entry({"id": "a1", "link": "https://e.example/x"})
        assert got["content_sha256"] is None
        assert got["content_chars"] == 0

    def test_published_read_from_published_key(self):
        # The item key is `published`; `published_at` only exists on output rows.
        got = provenance.article_manifest_entry({"id": "a1", "published": "2026-05-01T09:00:00Z"})
        assert got["published_at"] == "2026-05-01T09:00:00Z"

    def test_in_prompt_follows_prompt_entry(self):
        without = provenance.article_manifest_entry({"id": "a1"})
        with_entry = provenance.article_manifest_entry({"id": "a1"}, {"id": "a1", "title": "t"})
        assert without["in_prompt"] is False
        assert without["prompt_text_sha256"] is None
        assert with_entry["in_prompt"] is True
        assert with_entry["prompt_text_sha256"] is not None

    def test_prompt_hash_is_key_order_independent(self):
        # Two runs building the same entry from differently-ordered dicts must
        # hash identically, or every re-run looks like the inputs changed.
        one = provenance.article_manifest_entry({"id": "a1"}, {"id": "a1", "title": "t"})
        two = provenance.article_manifest_entry({"id": "a1"}, {"title": "t", "id": "a1"})
        assert one["prompt_text_sha256"] == two["prompt_text_sha256"]

    def test_prompt_hash_changes_with_content(self):
        one = provenance.article_manifest_entry({"id": "a1"}, {"id": "a1", "digest": {"x": 1}})
        two = provenance.article_manifest_entry({"id": "a1"}, {"id": "a1", "digest": {"x": 2}})
        assert one["prompt_text_sha256"] != two["prompt_text_sha256"]

    def test_in_fulltext_flag(self):
        assert provenance.article_manifest_entry({"id": "a1"}, in_fulltext=True)["in_fulltext"] is True
        assert provenance.article_manifest_entry({"id": "a1"})["in_fulltext"] is False

    def test_empty_item_does_not_raise(self):
        got = provenance.article_manifest_entry({})
        assert got["id"] is None and got["url"] is None and got["content_chars"] == 0

    def test_non_string_body_is_ignored(self):
        got = provenance.article_manifest_entry({"id": "a1", "content": 12345})
        assert got["content_sha256"] is None and got["content_chars"] == 0


class TestBuildArticleManifest:
    def _items(self):
        return [
            {"id": "a1", "content": "one", "link": "https://e.example/1"},
            {"id": "a2", "text": "two", "link": "https://e.example/2"},
            {"id": "a3", "link": "https://e.example/3"},
        ]

    def test_one_entry_per_item_ids_in_order(self):
        got = provenance.build_article_manifest(self._items(), [{"id": "a1"}])
        assert [e["id"] for e in got] == ["a1", "a2", "a3"]

    def test_in_prompt_reflects_the_prompt_entries(self):
        got = provenance.build_article_manifest(self._items(), [{"id": "a1"}, {"id": "a3"}])
        assert {e["id"]: e["in_prompt"] for e in got} == {"a1": True, "a2": False, "a3": True}

    def test_fulltext_ids_marked(self):
        got = provenance.build_article_manifest(self._items(), [], fulltext_ids=["a2"])
        assert {e["id"]: e["in_fulltext"] for e in got} == {"a1": False, "a2": True, "a3": False}

    def test_malformed_item_does_not_crash(self):
        got = provenance.build_article_manifest([{"id": "a1"}, None, "junk", 7], [])
        assert [e["id"] for e in got] == ["a1"]

    def test_no_articles(self):
        assert provenance.build_article_manifest([], []) == []
        assert provenance.build_article_manifest(None, None) == []


def _payload():
    """A realistic ``prepare_llm_payload_pretty`` shape, trimmed."""
    return {
        "country": "PT",
        "latest_year": 2025,
        "indicators": {
            "Inflation (CPI %)": {"latest": 2.3, "series": {"2023": 4.3, "2024": 2.7, "2025": 2.3}},
            "Political Corruption Index": {"latest": 0.1, "series": {"2023": 0.1, "2024": None}},
        },
        "_meta": {
            "units": {"Inflation (CPI %)": "%"},
            "source": "World Bank",
            "generated_at": "2026-07-26T09:14:00+00:00",
            "series_lookback": 10,
        },
    }


class TestMacroVintages:
    def test_reads_source_and_generated_at(self):
        got = provenance.macro_vintages(_payload())
        assert got["panel_source"] == "World Bank"
        assert got["panel_generated_at"] == "2026-07-26T09:14:00+00:00"
        assert got["latest_year"] == 2025

    def test_vintage_scheme_is_the_queryable_literal(self):
        # Phase B replaces this with "first-release"; Phase E filters on it.
        assert provenance.macro_vintages(_payload())["vintage_scheme"] == "as-published-latest"

    def test_latest_year_per_indicator_ignores_nulls(self):
        got = provenance.macro_vintages(_payload())["latest_year_by_indicator"]
        assert got == {"Inflation (CPI %)": 2025, "Political Corruption Index": 2023}

    def test_missing_meta_degrades_but_keeps_scheme(self):
        got = provenance.macro_vintages({"indicators": {}})
        assert got["vintage_scheme"] == "as-published-latest"
        assert got["panel_source"] is None and got["panel_generated_at"] is None

    def test_empty_payload_does_not_raise(self):
        assert provenance.macro_vintages({})["vintage_scheme"] == "as-published-latest"
        assert provenance.macro_vintages(None)["latest_year_by_indicator"] == {}


class TestBuildInputManifest:
    def _build(self, monkeypatch, **over):
        monkeypatch.delenv("GIT_SHA", raising=False)
        kwargs = dict(
            items=[{"id": "a1", "content": "body", "link": "https://e.example/1"}],
            prompt_entries=[{"id": "a1", "title": "t"}],
            fulltext_ids=["a1"],
            payload=_payload(),
            model_id="gpt-4o-2024-08-06",
            prompt_version="v2.0",
            policy_version="p2.0",
            seed=42,
        )
        kwargs.update(over)
        return provenance.build_input_manifest(**kwargs)

    def test_schema_version_present(self, monkeypatch):
        assert self._build(monkeypatch)["schema_version"] == 1

    def test_stamps_and_articles(self, monkeypatch):
        got = self._build(monkeypatch)
        assert got["model_id"] == "gpt-4o-2024-08-06"
        assert got["prompt_version"] == "v2.0"
        assert got["policy_version"] == "p2.0"
        assert got["seed"] == 42
        assert len(got["articles"]) == 1
        assert got["articles"][0]["in_prompt"] is True
        assert got["macro_vintages"]["vintage_scheme"] == "as-published-latest"

    def test_git_sha_none_when_unset(self, monkeypatch):
        assert self._build(monkeypatch)["git_sha"] is None

    def test_git_sha_read_from_env(self, monkeypatch):
        monkeypatch.setenv("GIT_SHA", "deadbeef")
        got = provenance.build_input_manifest(
            items=[], prompt_entries=[], payload={}, model_id=None,
            prompt_version=None, policy_version=None, seed=None,
        )
        assert got["git_sha"] == "deadbeef"

    def test_json_serializable(self, monkeypatch):
        # It goes into a JSONB column; anything unserializable must fail here.
        json.dumps(self._build(monkeypatch), ensure_ascii=False)


class TestSnapshotPayloadProvenance:
    """``data_push`` imports psycopg2 at module level — skip where it is absent."""

    def _payload_with(self, **over):
        payload = _payload()
        payload["llm_output"] = {"score": 0.42, "bullet_summary": "ok"}
        payload.update(over)
        return payload

    def test_input_manifest_carried(self):
        pytest.importorskip("psycopg2")
        from backend.utils.data_upsert import data_push

        manifest = {"schema_version": 1, "articles": []}
        got = data_push._parse_snapshot_payload(self._payload_with(input_manifest=manifest))
        assert got.input_manifest == manifest

    def test_absent_manifest_still_parses(self):
        # Constraint: a snapshot written before provenance existed must upsert.
        pytest.importorskip("psycopg2")
        from backend.utils.data_upsert import data_push

        got = data_push._parse_snapshot_payload(self._payload_with())
        assert got.input_manifest is None
        assert got.legal_gate is None
        assert got.llm_out["score"] == 0.42

    def test_malformed_manifest_becomes_null(self):
        pytest.importorskip("psycopg2")
        from backend.utils.data_upsert import data_push

        got = data_push._parse_snapshot_payload(self._payload_with(input_manifest="not-a-dict"))
        assert got.input_manifest is None

    def test_legal_gate_carried_from_llm_output(self):
        pytest.importorskip("psycopg2")
        from backend.utils.data_upsert import data_push

        gate = {"name": "Russia", "rule": "prohibition", "sources": ["eu-2022"]}
        payload = self._payload_with()
        payload["llm_output"]["legal_gate"] = gate
        assert data_push._parse_snapshot_payload(payload).legal_gate == gate
