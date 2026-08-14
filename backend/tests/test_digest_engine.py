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

    def invoke(self, prompt):
        """The masked path's digest sweep. Echoes the fields back unchanged, so
        these tests stay about caching rather than about what a model rewrites."""
        self.sweeps = getattr(self, "sweeps", 0) + 1
        return {f: "" for f in ("what_happened", "actors", "numbers", "transmission")}


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
        # One instance per fixture, not per call: the masked path builds a chat
        # twice — once to digest, once to sweep the digest — and a fresh fake on
        # the second call would throw away the batch sizes the tests assert on.
        if h.structured is None:
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

    def test_a_swept_headline_survives_a_cache_hit(self, stage1):
        """The failure this guards: a cache hit serving a masked digest next to
        the article's original, named title.

        Seeded through the same write path the engine uses, so the key is
        whatever the engine really computes rather than a guess at it.
        """
        cache = _FakeContentCache()
        digest = dict(_digest(70.0))
        digest["masked_title"] = "the country election: a candidate leads"
        stage1.results = [digest]
        first = _item("a1", text="body one", link="http://x/1",
                      title="Jair Bolsonaro wins")
        digest_engine.digest_articles([first], country_display="a country", iso2="BR",
                                      as_of=AS_OF, masked=True, content_cache=cache)

        stage1.structured = None
        later = _item("a1", text="body one", link="http://x/1",
                      title="Jair Bolsonaro wins")
        digest_engine.digest_articles([later], country_display="a country", iso2="BR",
                                      as_of=AS_OF + timedelta(days=7),
                                      masked=True, content_cache=cache)
        assert stage1.structured is None, "re-digested instead of using the cache"
        assert "Bolsonaro" not in later["title"]

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


class TestTheMaskingVersionIsInTheKey:
    """A masking change must invalidate masked digests without anyone purging.

    The hash covers the digest's *input*; the sweep rewrites its *output*, and
    only for freshly generated digests. So without the version in here, changing
    the gazetteer or the sweep prompt leaves every cached digest in place and the
    pilot scores half its decade under one masking behaviour and half under
    another, with nothing on either row to say which.
    """

    def test_the_masked_prefix_carries_both_versions(self):
        from backend.utils.masking import gazetteer, rewrite

        assert digest_engine._content_sha("body", masked=True) == _sha(
            f"masked:{gazetteer.MASK_MAP_VERSION}:{rewrite.SWEEP_VERSION}\nbody")

    def test_a_named_digest_keeps_its_bare_hash(self):
        """The sweep is a masked-mode stage; nothing about named digests moved,
        so invalidating them would be throwing away paid-for work for nothing."""
        assert digest_engine._content_sha("body", masked=False) == _sha("body")

    def test_a_new_sweep_prompt_changes_the_masked_key(self, monkeypatch):
        from backend.utils.masking import rewrite

        before = digest_engine._content_sha("body", masked=True)
        monkeypatch.setattr(rewrite, "SWEEP_VERSION", "deadbeef")
        assert digest_engine._content_sha("body", masked=True) != before

    def test_a_new_mask_map_changes_the_masked_key(self, monkeypatch):
        from backend.utils.masking import gazetteer

        before = digest_engine._content_sha("body", masked=True)
        monkeypatch.setattr(gazetteer, "MASK_MAP_VERSION", "g99")
        assert digest_engine._content_sha("body", masked=True) != before

    def test_each_pass_versions_its_own_cache(self):
        """Derived, not maintained. This repo has already shipped a version bump
        that silently did not happen (`b146104`), and a hash cannot forget.

        One version per cache. These were briefly a single constant covering both
        prompts, on the right argument — no two masking behaviours may share a
        label — implemented bluntly: the body rewrite cannot change a digest, so
        folding it in discarded every cached digest whenever the body prompt
        moved. The invariant is satisfied by the *manifest* carrying both, not by
        one cache key carrying a version it has no use for.
        """
        import hashlib
        import json

        from backend.utils.masking import rewrite

        assert rewrite.SWEEP_VERSION == hashlib.sha256(
            (rewrite._DIGEST_SWEEP_PROMPT
             + "\x00".join(rewrite._DIGEST_SWEEP_FIELDS)).encode("utf-8")
        ).hexdigest()[:8]
        assert rewrite.REWRITE_VERSION == hashlib.sha256(
            (rewrite._REWRITE_PROMPT
             + json.dumps(rewrite._REWRITE_SCHEMA, sort_keys=True)).encode("utf-8")
        ).hexdigest()[:8]
        assert rewrite.SWEEP_VERSION != rewrite.REWRITE_VERSION

    def test_the_shared_rules_move_both_versions(self):
        """`_MASK_RULES` is in both prompts, so a scope fix invalidates both
        caches — which is correct, because it changes both behaviours."""
        import hashlib
        import json

        from backend.utils.masking import rewrite

        edited = rewrite._MASK_RULES + "\n8. And another thing."
        sweep = rewrite._DIGEST_SWEEP_PROMPT.replace(rewrite._MASK_RULES, edited)
        body = rewrite._REWRITE_PROMPT.replace(rewrite._MASK_RULES, edited)
        assert sweep != rewrite._DIGEST_SWEEP_PROMPT
        assert body != rewrite._REWRITE_PROMPT

        assert hashlib.sha256(
            (sweep + "\x00".join(rewrite._DIGEST_SWEEP_FIELDS)).encode("utf-8")
        ).hexdigest()[:8] != rewrite.SWEEP_VERSION
        assert hashlib.sha256(
            (body + json.dumps(rewrite._REWRITE_SCHEMA, sort_keys=True)).encode("utf-8")
        ).hexdigest()[:8] != rewrite.REWRITE_VERSION

    def test_both_passes_share_one_set_of_rules(self):
        """Two prompts that drift apart are two masking behaviours under one
        version. The scope fix for non-roster entities went into `_MASK_RULES`
        precisely so it could not land in one pass and miss the other."""
        from backend.utils.masking import rewrite

        assert rewrite._MASK_RULES in rewrite._DIGEST_SWEEP_PROMPT
        assert rewrite._MASK_RULES in rewrite._REWRITE_PROMPT


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


class _RunawayThenRecover:
    """A stage-1 runnable that loops on the first call and succeeds on a retry.

    The observed failure exactly: OpenAI refuses to parse because the model hit
    its output ceiling, and LangChain surfaces that as a generic parse error
    whose only distinguishing feature is the sentence it carries.
    """

    LENGTH_ERROR = ValueError(
        "Could not parse response content as the length limit was reached - "
        "CompletionUsage(completion_tokens=16384, prompt_tokens=4785)")

    def __init__(self, recover=True):
        self.calls, self.prompt_lengths, self.recover = [], [], recover

    def batch(self, inputs, config=None, return_exceptions=False):
        self.calls.append(len(inputs))
        self.prompt_lengths.append([len(m[0].content) for m in inputs])
        if len(self.calls) == 1:
            return [self.LENGTH_ERROR] * len(inputs)
        return [_digest(50.0) if self.recover else self.LENGTH_ERROR
                for _ in inputs]

    def invoke(self, prompt):
        return {f: "" for f in ("what_happened", "actors", "numbers", "transmission")}


class TestTheRunawayRetry:
    """Seven digests in one twenty-bundle probe run died at exactly
    `completion_tokens=16384`, on prompts of three to five thousand. Not an
    outage — a generation that never terminated. The article still reaches the
    scorer, as a truncated body instead of a digest, and says nothing.
    """

    def _wire(self, monkeypatch, runnable):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setattr(data_push, "read_article_digests", lambda iso2, as_of: {})
        monkeypatch.setattr(data_push, "upsert_article_digests", lambda rows: None)
        monkeypatch.setattr(digest_engine.ai_client, "build_digest_chat",
                            lambda key: _FakeChat(runnable))

    def test_a_runaway_is_retried_on_shorter_text(self, monkeypatch):
        runnable = _RunawayThenRecover()
        self._wire(monkeypatch, runnable)
        it = _item("a1", text="x" * 20000, link="http://x/1")
        digest_engine.digest_articles([it], country_display="a country",
                                      iso2="PT", as_of=AS_OF)
        assert runnable.calls == [1, 1], "did not retry the runaway"
        # A plain retry reproduces the loop: temperature is 0 and the seed is
        # fixed, so the retry has to send a materially different prompt.
        assert runnable.prompt_lengths[1][0] < runnable.prompt_lengths[0][0]
        assert isinstance(it["digest"], dict), "the article stayed degraded"

    def test_a_retry_that_also_fails_leaves_the_article_degraded(self, monkeypatch):
        runnable = _RunawayThenRecover(recover=False)
        self._wire(monkeypatch, runnable)
        it = _item("a1", text="x" * 20000, link="http://x/1")
        digest_engine.digest_articles([it], country_display="a country",
                                      iso2="PT", as_of=AS_OF)
        assert runnable.calls == [1, 1], "retried more than once"
        assert it["digest"] is None

    def test_an_outage_is_not_retried_on_truncated_input(self, monkeypatch):
        """The narrowness is the point. A network failure must fail, not come
        back as a digest of the first 6,000 characters."""
        class _Down(_RunawayThenRecover):
            def batch(self, inputs, config=None, return_exceptions=False):
                self.calls.append(len(inputs))
                return [ConnectionError("connection reset")] * len(inputs)

        runnable = _Down()
        self._wire(monkeypatch, runnable)
        it = _item("a1", text="x" * 20000, link="http://x/1")
        digest_engine.digest_articles([it], country_display="a country",
                                      iso2="PT", as_of=AS_OF)
        assert runnable.calls == [1], "retried a plain outage"
        assert it["digest"] is None

    def test_only_the_runaways_are_retried(self, monkeypatch):
        class _OneEach(_RunawayThenRecover):
            def batch(self, inputs, config=None, return_exceptions=False):
                self.calls.append(len(inputs))
                if len(self.calls) == 1:
                    return [_digest(10.0), self.LENGTH_ERROR]
                return [_digest(50.0)] * len(inputs)

        runnable = _OneEach()
        self._wire(monkeypatch, runnable)
        items = [_item("a1", text="a" * 9000, link="http://x/1"),
                 _item("a2", text="b" * 9000, link="http://x/2")]
        digest_engine.digest_articles(items, country_display="a country",
                                      iso2="PT", as_of=AS_OF)
        assert runnable.calls == [2, 1], "re-sent the digest that already worked"


class TestTheDigestOutputIsCapped:
    def test_the_digest_chat_caps_its_output(self):
        """Uncapped, a loop costs $0.0098; capped it costs $0.0006. Over a
        2,188-snapshot pilot that is ~$10 of pure waste against a $130 guard."""
        from backend.utils.ai import client as ai_client

        assert ai_client._DIGEST_MAX_TOKENS <= 2048

    def test_the_scoring_chat_is_not_capped(self):
        """The scorer's schema is large and its output is the product. A cap
        there would truncate a score, which is a different kind of bug."""
        import inspect

        from backend.utils.ai import client as ai_client

        assert "max_tokens" not in inspect.getsource(ai_client.build_chat)


class TestATruncatedRetryIsStamped:
    """A recovered digest is a digest of a truncated article, not of the article.

    The retry sends the first 6,000 characters. Recording what comes back as a
    clean digest would be a recovery that silently changed what the evidence is —
    the same class as every other bug this branch has turned up.
    """

    def _wire(self, monkeypatch, runnable):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setattr(data_push, "read_article_digests", lambda iso2, as_of: {})
        monkeypatch.setattr(data_push, "upsert_article_digests", lambda rows: None)
        monkeypatch.setattr(digest_engine.ai_client, "build_digest_chat",
                            lambda key: _FakeChat(runnable))

    def test_a_recovered_digest_carries_its_provenance(self, monkeypatch):
        runnable = _RunawayThenRecover()
        self._wire(monkeypatch, runnable)
        it = _item("a1", text="x" * 20000, link="http://x/1")
        digest_engine.digest_articles([it], country_display="a country",
                                      iso2="PT", as_of=AS_OF)
        assert it["digest"][digest_engine.DIGEST_SOURCE_KEY] == "truncated-retry"

    def test_an_ordinary_digest_carries_no_marker(self, monkeypatch):
        """Absent rather than false, so a normal digest's bytes — and therefore
        its prompt hash — are exactly what they were before the retry existed."""
        runnable = _RunawayThenRecover()
        runnable.calls.append(0)          # skip straight to the success branch
        self._wire(monkeypatch, runnable)
        it = _item("a1", text="short", link="http://x/1")
        digest_engine.digest_articles([it], country_display="a country",
                                      iso2="PT", as_of=AS_OF)
        assert digest_engine.DIGEST_SOURCE_KEY not in it["digest"]

    def test_stage1_health_counts_it_apart_from_degraded(self):
        from backend.utils import provenance

        health = provenance.stage1_health([
            {"id": "a1", "digest": _digest(50.0)},
            {"id": "a2", "digest": {**_digest(50.0),
                                    "digest_source": "truncated-retry"}},
            {"id": "a3", "digest": None},
        ])
        assert health["articles"] == 3
        assert health["degraded"] == 1 and health["degraded_ids"] == ["a3"]
        # Digested, and not cleanly — three states, not two.
        assert health["digested"] == 2
        assert health["truncated"] == 1 and health["truncated_ids"] == ["a2"]

    def test_the_marker_survives_into_the_cache_row(self, monkeypatch):
        """It is set inside the digest, so every later snapshot reusing that
        cached digest is marked too."""
        rows = []
        runnable = _RunawayThenRecover()
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setattr(data_push, "read_article_digests", lambda iso2, as_of: {})
        monkeypatch.setattr(data_push, "upsert_article_digests", rows.extend)
        monkeypatch.setattr(digest_engine.ai_client, "build_digest_chat",
                            lambda key: _FakeChat(runnable))
        digest_engine.digest_articles(
            [_item("a1", text="x" * 20000, link="http://x/1")],
            country_display="a country", iso2="PT", as_of=AS_OF)
        assert rows[0]["digest"][digest_engine.DIGEST_SOURCE_KEY] == "truncated-retry"
