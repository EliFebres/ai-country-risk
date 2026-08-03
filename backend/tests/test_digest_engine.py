"""Characterization tests for ``backend.utils.ai.digest_engine`` — the
deterministic parts of stage 1: full-text selection and the cache decision
logic. The LLM and the database are faked; nothing here touches the network.
"""

import hashlib

import pytest

from backend.utils.ai import digest_engine
from backend.utils.data_upsert import data_push

from datetime import date, timedelta

AS_OF = date(2026, 7, 26)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest(severity: float) -> dict:
    return {
        "what_happened": "x",
        "actors": "y",
        "numbers": "not stated",
        "transmission": "not stated",
        "directly_about_country": True,
        "stage1_severity": severity,
    }


def _item(aid: str, severity=None, relevance=0.5, published="2026-07-20T00:00:00Z", **kw) -> dict:
    it = {"id": aid, "stage1_severity": severity, "relevance_score": relevance, "published": published}
    it.update(kw)
    return it


class TestSelectFulltextIds:
    def test_severity_desc(self):
        items = [_item("a1", 10.0), _item("a2", 90.0), _item("a3", 50.0)]
        assert digest_engine.select_fulltext_ids(items) == ["a2", "a3", "a1"]

    def test_tie_broken_by_relevance_then_published_then_id(self):
        items = [
            _item("a2", 50.0, relevance=0.5, published="2026-07-01T00:00:00Z"),
            _item("a1", 50.0, relevance=0.5, published="2026-07-01T00:00:00Z"),
            _item("a3", 50.0, relevance=0.5, published="2026-07-10T00:00:00Z"),
            _item("a4", 50.0, relevance=0.9, published="2026-06-01T00:00:00Z"),
        ]
        # relevance beats date; date beats id; equal everything → id asc.
        assert digest_engine.select_fulltext_ids(items, k=4) == ["a4", "a3", "a1", "a2"]

    def test_none_severity_ranks_below_all_scored(self):
        items = [_item("a1", None, relevance=1.0), _item("a2", 1.0, relevance=0.0)]
        assert digest_engine.select_fulltext_ids(items, k=2) == ["a2", "a1"]

    def test_fill_from_unscored_by_relevance(self):
        items = [
            _item("a1", 80.0),
            _item("a2", None, relevance=0.2),
            _item("a3", None, relevance=0.9),
        ]
        assert digest_engine.select_fulltext_ids(items) == ["a1", "a3", "a2"]

    def test_k_larger_than_items(self):
        items = [_item("a1", 10.0)]
        assert digest_engine.select_fulltext_ids(items, k=3) == ["a1"]

    def test_items_without_id_skipped(self):
        items = [_item("a1", 10.0), {"stage1_severity": 99.0}]
        assert digest_engine.select_fulltext_ids(items) == ["a1"]

    def test_undated_loses_tie(self):
        items = [_item("a1", 50.0, published=None), _item("a2", 50.0)]
        assert digest_engine.select_fulltext_ids(items, k=2) == ["a2", "a1"]

    def test_guards(self):
        with pytest.raises(TypeError):
            digest_engine.select_fulltext_ids("nope")
        with pytest.raises(ValueError):
            digest_engine.select_fulltext_ids([], k=-1)


class _FakeStructured:
    """Stands in for the structured-output runnable; records batch sizes."""

    def __init__(self, results):
        self.results = list(results)
        self.batch_sizes = []

    def batch(self, inputs, config=None, return_exceptions=False):
        self.batch_sizes.append(len(inputs))
        assert return_exceptions is True
        return self.results[: len(inputs)]


class _FakeChat:
    def __init__(self, structured):
        self._structured = structured

    def with_structured_output(self, schema=None, strict=None):
        return self._structured


@pytest.fixture
def stage1(monkeypatch):
    """Wire fakes for the cache, the upsert, and the LLM; return the handles.

    ``stage1.cache`` is what read_article_digests returns; ``stage1.results``
    feeds the fake batch; ``stage1.upserted`` collects persisted rows.
    """
    class Handles:
        cache = {}
        results = []
        upserted = []
        structured = None

    h = Handles()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(data_push, "read_article_digests", lambda iso2, as_of: dict(h.cache))
    monkeypatch.setattr(data_push, "upsert_article_digests", lambda rows: h.upserted.extend(rows))

    def _build(api_key):
        h.structured = _FakeStructured(h.results)
        return _FakeChat(h.structured)

    monkeypatch.setattr(digest_engine.ai_client, "build_digest_chat", _build)
    return h


class TestDigestArticlesCache:
    def test_matching_sha_reuses_without_api_call(self, stage1):
        it = _item("a1", text="body one", link="http://x/1")
        stage1.cache = {"http://x/1": {"content_sha256": _sha("body one"),
                                       "digest": _digest(70.0), "stage1_severity": 70.0}}
        digest_engine.digest_articles([it], country_display="Portugal", iso2="PT", as_of=AS_OF)
        assert it["digest"] == _digest(70.0)
        assert it["stage1_severity"] == 70.0
        assert stage1.structured is None  # no LLM built at all
        assert stage1.upserted == []

    def test_changed_sha_redigests_and_persists(self, stage1):
        it = _item("a1", text="new body", link="http://x/1")
        stage1.cache = {"http://x/1": {"content_sha256": _sha("old body"),
                                       "digest": _digest(10.0), "stage1_severity": 10.0}}
        stage1.results = [_digest(88.0)]
        digest_engine.digest_articles([it], country_display="Portugal", iso2="PT", as_of=AS_OF)
        assert it["digest"] == _digest(88.0)
        assert it["stage1_severity"] == 88.0
        assert stage1.structured.batch_sizes == [1]
        assert len(stage1.upserted) == 1
        row = stage1.upserted[0]
        assert row["url"] == "http://x/1"
        assert row["content_sha256"] == _sha("new body")
        assert row["country_iso2"] == "PT" and row["as_of"] == AS_OF

    def test_cache_read_failure_degrades_to_full_digest(self, stage1, monkeypatch):
        def _boom(iso2, as_of):
            raise RuntimeError("db down")
        monkeypatch.setattr(data_push, "read_article_digests", _boom)
        it = _item("a1", text="body", link="http://x/1")
        stage1.results = [_digest(5.0)]
        digest_engine.digest_articles([it], country_display="Portugal", iso2="PT", as_of=AS_OF)
        assert it["digest"] == _digest(5.0)

    def test_per_item_failure_leaves_none_and_rest_proceed(self, stage1):
        items = [_item("a1", text="one", link="http://x/1"),
                 _item("a2", text="two", link="http://x/2")]
        stage1.results = [RuntimeError("timeout"), _digest(33.0)]
        digest_engine.digest_articles(items, country_display="Portugal", iso2="PT", as_of=AS_OF)
        assert items[0]["digest"] is None and items[0]["stage1_severity"] is None
        assert items[1]["digest"] == _digest(33.0)
        assert [r["url"] for r in stage1.upserted] == ["http://x/2"]

    def test_results_map_by_index_around_cache_hits(self, stage1):
        items = [_item("a1", text="one", link="http://x/1"),
                 _item("a2", text="two", link="http://x/2"),
                 _item("a3", text="three", link="http://x/3")]
        stage1.cache = {"http://x/2": {"content_sha256": _sha("two"),
                                       "digest": _digest(50.0), "stage1_severity": 50.0}}
        stage1.results = [_digest(11.0), _digest(13.0)]  # for a1, a3 in order
        digest_engine.digest_articles(items, country_display="Portugal", iso2="PT", as_of=AS_OF)
        assert items[0]["stage1_severity"] == 11.0
        assert items[1]["stage1_severity"] == 50.0  # cached
        assert items[2]["stage1_severity"] == 13.0
        assert stage1.structured.batch_sizes == [2]

    def test_no_api_key_leaves_all_undigested(self, stage1, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        it = _item("a1", text="body", link="http://x/1")
        digest_engine.digest_articles([it], country_display="Portugal", iso2="PT", as_of=AS_OF)
        assert it["digest"] is None and it["stage1_severity"] is None
        assert stage1.structured is None

    def test_guards(self):
        with pytest.raises(TypeError):
            digest_engine.digest_articles("nope", country_display="X", iso2="PT", as_of=AS_OF)
        with pytest.raises(ValueError):
            digest_engine.digest_articles([], country_display=" ", iso2="PT", as_of=AS_OF)
        with pytest.raises(ValueError):
            digest_engine.digest_articles([], country_display="X", iso2="PRT", as_of=AS_OF)
        with pytest.raises(TypeError):
            digest_engine.digest_articles([], country_display="X", iso2="PT", as_of="2026-07-26")


class _FakeContentCache:
    """A content-keyed cache, in a dict. Matches `history.store`'s two functions."""

    def __init__(self):
        self.rows = {}          # (sha, model, mode) -> row
        self.reads = []

    def read_digest_cache(self, hashes, digest_model, mode):
        self.reads.append((sorted(hashes), digest_model, mode))
        return {sha: self.rows[(sha, digest_model, mode)]
                for sha in hashes if (sha, digest_model, mode) in self.rows}

    def write_digest_cache(self, rows, digest_model, mode):
        for r in rows:
            self.rows[(r["content_sha256"], digest_model, mode)] = {
                "digest": r["digest"], "stage1_severity": r.get("stage1_severity")}
        return len(rows)


class TestTheContentKeyedCache:
    """The overlap between consecutive anchors, which is most of the pilot's bill.

    Weekly anchors and a 30-day window put one article in about four snapshots.
    Keyed on `as_of` every one of those is a miss.
    """

    def test_a_later_anchor_reuses_the_earlier_anchors_digest(self, stage1):
        cache = _FakeContentCache()
        first = _item("a1", text="body one", link="http://x/1")
        stage1.results = [_digest(70.0)]
        digest_engine.digest_articles([first], country_display="Portugal", iso2="PT",
                                      as_of=AS_OF, content_cache=cache)
        assert stage1.structured.batch_sizes == [1]

        # Next week's snapshot: same article, different as_of, so the per-day
        # cache misses. Without the content cache this is a second API call.
        stage1.structured = None
        later = _item("a1", text="body one", link="http://x/1")
        digest_engine.digest_articles([later], country_display="Portugal", iso2="PT",
                                      as_of=AS_OF + timedelta(days=7),
                                      content_cache=cache)
        assert later["digest"] == _digest(70.0)
        assert later["stage1_severity"] == 70.0
        assert stage1.structured is None, "re-digested an article it already had"

    def test_masked_and_named_never_share_a_digest(self, stage1):
        """Two genuinely different texts. Serving one for the other leaks a name."""
        cache = _FakeContentCache()
        stage1.results = [_digest(70.0)]
        digest_engine.digest_articles([_item("a1", text="body one", link="http://x/1")],
                                      country_display="Portugal", iso2="PT",
                                      as_of=AS_OF, masked=False, content_cache=cache)
        stage1.structured = None
        stage1.results = [_digest(40.0)]
        it = _item("a1", text="body one", link="http://x/1")
        digest_engine.digest_articles([it], country_display="a country", iso2="PT",
                                      as_of=AS_OF, masked=True, content_cache=cache)
        assert stage1.structured.batch_sizes == [1], "served a named digest to a masked run"
        assert it["digest"] == _digest(40.0)

    def test_the_two_masked_arms_share_digests(self, stage1):
        """What keeps the third arm nearly free.

        `masked` and `masked_nostructural` differ only in the structural block,
        which the digest never sees. If these two re-digest the same content the
        pilot's digest line doubles for no benefit.
        """
        cache = _FakeContentCache()
        stage1.results = [_digest(70.0)]
        digest_engine.digest_articles([_item("a1", text="body one", link="http://x/1")],
                                      country_display="a country", iso2="PT",
                                      as_of=AS_OF, masked=True, content_cache=cache)
        stage1.structured = None
        it = _item("a1", text="body one", link="http://x/1")
        digest_engine.digest_articles([it], country_display="a country", iso2="PT",
                                      as_of=AS_OF, masked=True, content_cache=cache)
        assert stage1.structured is None
        assert it["digest"] == _digest(70.0)
        assert {mode for _, _, mode in cache.reads} == {"masked"}

    def test_the_daily_run_passes_none_and_is_untouched(self, stage1):
        it = _item("a1", text="body one", link="http://x/1")
        stage1.results = [_digest(70.0)]
        digest_engine.digest_articles([it], country_display="Portugal", iso2="PT",
                                      as_of=AS_OF)
        assert it["digest"] == _digest(70.0)

    def test_a_broken_content_cache_degrades_to_digesting(self, stage1):
        class _Boom:
            def read_digest_cache(self, *a, **k): raise RuntimeError("down")
            def write_digest_cache(self, *a, **k): raise RuntimeError("down")

        it = _item("a1", text="body one", link="http://x/1")
        stage1.results = [_digest(70.0)]
        digest_engine.digest_articles([it], country_display="Portugal", iso2="PT",
                                      as_of=AS_OF, content_cache=_Boom())
        assert it["digest"] == _digest(70.0)


class TestArticleInputText:
    def test_longer_of_content_and_text(self):
        assert digest_engine.article_input_text({"content": "long content", "text": "txt"}) == "long content"
        assert digest_engine.article_input_text({"content": "c", "text": "longer text"}) == "longer text"

    def test_fallback_chain(self):
        assert digest_engine.article_input_text({"summary": "sum"}) == "sum"
        assert digest_engine.article_input_text({"snippet": "snip"}) == "snip"
        assert digest_engine.article_input_text({}) == ""

    def test_non_dict_raises(self):
        with pytest.raises(TypeError):
            digest_engine.article_input_text(None)
