"""The no-future line, as executable code.

Every other test in this repo checks that something works. These check that
something is *impossible*, because the failure they guard against does not look
like a failure. A backfilled series that quietly read tomorrow's news produces
beautiful scores, an excellent backtest, and a machine that cannot do the one
thing it was built to do.

Three ways hindsight gets in, and one test class each:

* an article published after the anchor,
* an article published before the anchor whose *body* was captured after it —
  the subtle one, because the article's own date looks perfectly innocent,
* a page refetched today that the leakage scan never cleared.

Pure: every test drives `to_item`/`usable_body`/`select` against fixture rows,
with the store monkeypatched. No network, no database, no model.
"""

import datetime

import pytest

from backend.utils.history import config, snapshot_select as sel

AS_OF = datetime.date(2018, 6, 15)
COUNTRY = "PT"


def row(**over):
    """One `historical_article` row as `store.read_window` returns it."""
    base = dict(
        url="https://www.theguardian.com/world/2018/jun/01/story",
        publisher_link=None,
        title="Portugal's parliament debates the budget as the deficit narrows",
        abstract="Lawmakers met on Friday.",
        body="Portugal's parliament debated the budget on Friday.",
        body_status="recovered",
        body_vintage="api-native",
        source_system="guardian",
        published_at=datetime.datetime(2018, 6, 1, 9, 30, tzinfo=datetime.timezone.utc),
        themes=["order"],
        tier="full",
    )
    base.update(over)
    return base


@pytest.fixture()
def store_rows(monkeypatch):
    """Feed `select` a fixed set of rows without a database."""
    def _install(rows):
        monkeypatch.setattr(sel.store, "read_window", lambda iso2, start, end: [
            r for r in rows if start <= r["published_at"] < end])
    return _install


class TestTheWindow:
    def test_it_is_thirty_days_and_strict_at_the_top(self):
        start, end = sel.window(AS_OF)
        assert (end - start).days == config.SNAPSHOT_WINDOW_DAYS
        assert end == datetime.datetime(2018, 6, 15, tzinfo=datetime.timezone.utc)

    def test_an_article_published_on_the_anchor_is_not_read(self, store_rows):
        # Same-day news the live run's own cutoff would not reliably have had.
        store_rows([row(published_at=datetime.datetime(2018, 6, 15, 0, 0,
                                                       tzinfo=datetime.timezone.utc))])
        assert sel.select(COUNTRY, AS_OF) == []

    def test_an_article_published_after_the_anchor_is_not_read(self, store_rows):
        store_rows([row(published_at=datetime.datetime(2018, 7, 1,
                                                       tzinfo=datetime.timezone.utc))])
        assert sel.select(COUNTRY, AS_OF) == []

    def test_an_article_just_inside_the_window_is_read(self, store_rows):
        store_rows([row(published_at=datetime.datetime(2018, 6, 14, 23, 59,
                                                       tzinfo=datetime.timezone.utc))])
        assert len(sel.select(COUNTRY, AS_OF)) == 1

    def test_an_article_older_than_the_window_is_not_read(self, store_rows):
        store_rows([row(published_at=datetime.datetime(2018, 5, 1,
                                                       tzinfo=datetime.timezone.utc))])
        assert sel.select(COUNTRY, AS_OF) == []


class TestABodyMayNotBeYoungerThanTheAnchor:
    """The subtle one: the article's own date is innocent, its body is not."""

    def test_a_capture_after_the_anchor_loses_its_body(self):
        # Published 1 June, captured 15 August, read as of 15 June. Publishers
        # edit, append and re-headline; that body is two months of hindsight.
        assert sel.usable_body(row(body_vintage="wayback-20180815"), AS_OF) is None

    def test_a_capture_on_the_anchor_loses_its_body(self):
        assert sel.usable_body(row(body_vintage="wayback-20180615"), AS_OF) is None

    def test_a_capture_before_the_anchor_keeps_its_body(self):
        assert sel.usable_body(row(body_vintage="wayback-20180605"), AS_OF)

    def test_an_api_native_body_is_the_article_itself(self):
        # It arrived inside the search response, so its age is the article's.
        assert sel.usable_body(row(body_vintage="api-native"), AS_OF)

    def test_an_unreadable_vintage_is_not_a_licence_to_use_the_body(self):
        assert sel.usable_body(row(body_vintage="wayback-notadate"), AS_OF) is None
        assert sel.usable_body(row(body_vintage="something-else"), AS_OF) is None

    def test_losing_a_body_thins_the_article_but_does_not_drop_it(self, store_rows):
        # Thinner evidence, honestly thin — never a missing article.
        store_rows([row(body_vintage="wayback-20180815")])
        picked = sel.select(COUNTRY, AS_OF)
        assert len(picked) == 1
        assert picked[0]["text"] == ""
        assert picked[0]["title"]
        assert picked[0]["snippet"] == "Lawmakers met on Friday."


class TestALiveRefetchNeedsTheScan:
    def test_a_scanned_live_refetch_is_allowed(self):
        # Cleared of post-publication knowledge, and the article was published
        # before the anchor, so post-publication covers post-anchor.
        assert sel.usable_body(
            row(body_vintage="live-refetch", body_status="recovered"), AS_OF)

    def test_an_unscanned_live_refetch_is_refused(self):
        for status in ("pending", "failed", "degraded-title-only"):
            assert sel.usable_body(
                row(body_vintage="live-refetch", body_status=status), AS_OF) is None

    def test_a_flagged_body_never_reaches_here_anyway(self):
        # `wayback.recover_one` discards the text when the scan flags it, so the
        # row arrives with no body at all. Belt and braces.
        assert sel.usable_body(
            row(body="", body_vintage="live-refetch",
                body_status="degraded-title-only"), AS_OF) is None


class TestNoFutureSurvivesAssembly:
    """The end-to-end assertion: whatever the mix, nothing future-dated gets in."""

    def test_a_mixed_window_yields_only_knowable_evidence(self, store_rows):
        store_rows([
            row(url="https://ex.test/ok", body_vintage="api-native"),
            row(url="https://ex.test/future-article",
                published_at=datetime.datetime(2018, 6, 20, tzinfo=datetime.timezone.utc)),
            row(url="https://ex.test/future-capture", body_vintage="wayback-20180901"),
            row(url="https://ex.test/unscanned",
                body_vintage="live-refetch", body_status="failed"),
        ])
        picked = sel.select(COUNTRY, AS_OF)
        urls = {i["link"] for i in picked}

        assert "https://ex.test/future-article" not in urls
        for item in picked:
            assert item["published"] < AS_OF.isoformat()
            vintage = item.get("body_vintage")
            if vintage and vintage.startswith("wayback-"):
                assert sel.capture_date(vintage) < AS_OF
            if item["text"]:
                assert vintage in ("api-native", "live-refetch") or \
                    sel.capture_date(vintage) < AS_OF

    def test_the_only_bodies_present_are_ones_that_passed(self, store_rows):
        store_rows([row(url="https://ex.test/future-capture",
                        body_vintage="wayback-20180901")])
        picked = sel.select(COUNTRY, AS_OF)
        assert all(not i["text"] for i in picked)
        assert all(i.get("body_vintage") is None for i in picked)


class TestSelectionMatchesTheLiveRun:
    def test_it_reuses_the_live_relevance_and_floor(self):
        # Two copies of "the 20 articles" would be a silent disagreement about
        # what the historical series is comparable to.
        import inspect
        source = inspect.getsource(sel)
        assert "article_ranking.score_relevance" in source
        assert "core.select_with_theme_floor" in source
        assert "article_enrichment._PER_THEME_FLOOR" in source

    def test_the_budget_is_respected(self, store_rows):
        store_rows([row(url=f"https://ex.test/{i}") for i in range(40)])
        assert len(sel.select(COUNTRY, AS_OF, max_articles=20)) == 20

    def test_the_retrieving_theme_leads(self, store_rows):
        store_rows([row(themes=["friction", "broad"])])
        assert sel.select(COUNTRY, AS_OF)[0]["_theme"] == "friction"

    def test_a_thin_week_is_allowed_to_be_thin(self, store_rows):
        # Inventing articles to fill a quota is the failure this machine exists
        # to avoid.
        store_rows([])
        assert sel.select(COUNTRY, AS_OF) == []

    def test_assembly_is_deterministic(self, store_rows):
        rows = [row(url=f"https://ex.test/{i}") for i in range(10)]
        store_rows(rows)
        assert [i["link"] for i in sel.select(COUNTRY, AS_OF)] == \
               [i["link"] for i in sel.select(COUNTRY, AS_OF)]

    def test_a_body_stands_in_for_a_missing_abstract(self, store_rows):
        # `score_relevance` reads title + snippet; a whole body would inflate
        # its keyword counts against the curve live articles are scored on.
        store_rows([row(abstract=None, body="x" * 5000)])
        assert len(sel.select(COUNTRY, AS_OF)[0]["snippet"]) == sel._SNIPPET_CHARS


class TestTheAbstractTierIsRationed:
    """The NYT archive returns no bodies, and it is overwhelmingly about the US.

    Left uncapped, a US snapshot fills with two-sentence abstracts while a
    Portugal one keeps full Guardian bodies — and every cross-country comparison
    the pilot exists to make becomes partly a comparison of evidence texture.
    """

    def abstracts(self, n, **over):
        return [row(url=f"https://www.nytimes.com/a{i}", source_system="nyt",
                    tier="abstract-only", body=None, body_status="failed",
                    abstract="Portugal's deficit narrowed, officials said.",
                    published_at=datetime.datetime(2018, 6, 1, 9, 30,
                                                   tzinfo=datetime.timezone.utc),
                    **over)
                for i in range(n)]

    def test_a_flood_of_abstracts_cannot_take_the_whole_snapshot(self, store_rows):
        store_rows(self.abstracts(40) + [row(url=f"https://g/{i}") for i in range(5)])
        picked = sel.select(COUNTRY, AS_OF, max_articles=20)
        thin = [i for i in picked if i.get("tier") == "abstract-only"]
        assert len(thin) <= int(20 * config.ABSTRACT_TIER_SHARE)

    def test_abstracts_still_fill_the_gaps_they_are_there_for(self, store_rows):
        """A cap is not a ban: under the cap, nothing is dropped."""
        store_rows(self.abstracts(3) + [row(url=f"https://g/{i}") for i in range(5)])
        picked = sel.select(COUNTRY, AS_OF, max_articles=20)
        assert len([i for i in picked if i.get("tier") == "abstract-only"]) == 3

    def test_the_ration_keeps_the_most_relevant_abstracts(self):
        items = [{"tier": "abstract-only", "relevance_score": s, "published": None}
                 for s in (1, 9, 5, 7, 3)]
        kept = sel.ration_abstracts(items, max_articles=5)   # cap = 2
        assert sorted(i["relevance_score"] for i in kept) == [7, 9]

    def test_the_ration_leaves_full_bodied_articles_alone(self):
        items = ([{"tier": "full", "relevance_score": 0.1, "published": None}] * 30
                 + [{"tier": "abstract-only", "relevance_score": 9, "published": None}])
        assert len(sel.ration_abstracts(items, max_articles=20)) == 31

    def test_the_survivors_keep_their_order(self):
        """`select` must stay byte-reproducible; a re-sort here would break it."""
        items = [{"tier": "abstract-only", "relevance_score": s, "published": None}
                 for s in (1, 9, 5, 7, 3)]
        kept = sel.ration_abstracts(items, max_articles=5)
        assert [i["relevance_score"] for i in kept] == [9, 7]


class TestRelevanceSnippetStaysHonest:
    """The snippet is what `score_relevance` answers "is this about the country?"
    from. Choosing it to beat the body-mention ceiling is how a snapshot fills
    up with articles that merely mention the country in passing."""

    def test_an_abstract_is_preferred_when_there_is_one(self):
        assert sel.relevance_snippet(row(), "body text", "Portugal") == "Lawmakers met on Friday."

    def test_otherwise_it_is_the_lede_and_only_the_lede(self):
        body = "Nothing about the country here. " * 20 + "Portugal appears late."
        got = sel.relevance_snippet(row(abstract=None), body, "Portugal")
        assert got == body[:sel._SNIPPET_CHARS]
        assert "Portugal" not in got, (
            "excerpting from the first country mention lifts every incidental "
            "article to the body-mention ceiling")

    def test_an_article_with_no_body_and_no_abstract_has_no_snippet(self):
        assert sel.relevance_snippet(row(abstract=None), None, "Portugal") == ""


class TestThePipelineSeam:
    """`_process_country` is where a historical run enters the live pipeline.

    The contract is that supplying `as_of` and `items` changes the article
    source and the date and nothing else, and that supplying neither leaves the
    daily run exactly as it was.
    """

    def test_the_daily_run_still_fetches_and_enriches(self, monkeypatch):
        from backend.utils import pipeline
        called = []
        monkeypatch.setattr(pipeline.data_retrieval, "prepare_llm_payload_pretty",
                            lambda **kw: {"_meta": {"generated_at": "2026-08-02T00:00:00Z"}})
        monkeypatch.setattr(pipeline.article_enrichment, "fetch_relevant_news",
                            lambda *a, **kw: called.append("fetch") or [])
        monkeypatch.setattr(pipeline.article_enrichment, "resolve_and_enrich",
                            lambda items, iso2: called.append("enrich") or items)
        monkeypatch.setattr(pipeline, "_finish_country", lambda *a, **kw: None, raising=False)
        monkeypatch.setattr(pipeline.digest_engine, "digest_articles",
                            lambda items, **kw: (_ for _ in ()).throw(StopIteration))

        with pytest.raises(StopIteration):
            pipeline._process_country("Portugal", "PT", [])

        assert called == ["fetch", "enrich"]

    def test_a_historical_run_neither_fetches_nor_enriches(self, monkeypatch):
        from backend.utils import pipeline
        called = []
        monkeypatch.setattr(pipeline.data_retrieval, "prepare_llm_payload_pretty",
                            lambda **kw: {"_meta": {"generated_at": "2026-08-02T00:00:00Z"}})
        monkeypatch.setattr(pipeline.article_enrichment, "fetch_relevant_news",
                            lambda *a, **kw: called.append("fetch") or [])
        monkeypatch.setattr(pipeline.article_enrichment, "resolve_and_enrich",
                            lambda items, iso2: called.append("enrich") or items)
        monkeypatch.setattr(pipeline.digest_engine, "digest_articles",
                            lambda items, **kw: (_ for _ in ()).throw(StopIteration))

        with pytest.raises(StopIteration):
            pipeline._process_country("Portugal", "PT", [], as_of=AS_OF, items=[])

        assert called == [], "a historical run must not refetch its own articles"

    def test_the_pin_reaches_every_downstream_stage(self, monkeypatch):
        """One overwrite of `_meta.generated_at` has to move the whole run."""
        from backend.utils import pipeline
        from backend.utils.data_upsert import data_push
        seen = {}
        monkeypatch.setattr(pipeline.data_retrieval, "prepare_llm_payload_pretty",
                            lambda **kw: {"_meta": {"generated_at": "2026-08-02T00:00:00Z"}})
        monkeypatch.setattr(pipeline.digest_engine, "digest_articles",
                            lambda items, **kw: seen.setdefault("digest", kw["as_of"]) and items)

        def stop(*a, **kw):
            seen["evidence"] = kw["as_of"]
            raise StopIteration

        monkeypatch.setattr(pipeline.data_retrieval, "build_evidence_payload", stop)

        with pytest.raises(StopIteration):
            pipeline._process_country("Portugal", "PT", [], as_of=AS_OF, items=[])

        assert seen["digest"] == AS_OF
        assert seen["evidence"] == AS_OF
        # And the upsert would key on the same date, from the same field.
        assert data_push.payload_as_of(
            {"_meta": {"generated_at": AS_OF.isoformat()}}) == AS_OF
