"""
Tests for backend/news_fetching.

Consumer-side where there is a consumer: the dedupe tests assert that the
surviving article carries what the discarded duplicates knew, and the page-date
tests assert that a date the page states is acted on rather than merely stored.

No network. `gnews_rss` is replaced at the module boundary, which is where the
rest of this suite draws the line.
"""

from backend.news_fetching import core, fetch_links


class TestThemeQueries:
    """The queries are the instrument — everything downstream can only rank what
    these bring back."""

    def test_every_theme_has_a_query_and_every_query_a_theme(self):
        assert set(core.THEME_QUERIES) == set(core.THEMES)

    def test_each_query_quotes_the_country_exactly_once(self):
        built = core.build_queries("Portugal")
        for theme, q in built.items():
            assert q.count('"Portugal"') == 1, theme

    def test_the_five_ledger_themes_are_all_present(self):
        """A theme that is not asked for is a ledger with no news behind it."""
        for ledger_theme in ("friction", "order", "information", "edge"):
            assert ledger_theme in core.THEME_QUERIES

    def test_security_is_asked_for_separately(self):
        """Security has no ledger of its own, but security reporting does not use
        the other themes' vocabulary, so a combined query loses it."""
        assert "conflict" in core.THEME_QUERIES["security"]


class TestQueryNames:
    def test_a_country_with_no_override_keeps_its_roster_name(self):
        assert core.query_name("PT", "Portugal") == "Portugal"

    def test_an_override_is_used_when_one_was_measured(self, monkeypatch):
        monkeypatch.setitem(core.QUERY_NAME_OVERRIDES, "HK", "Hong Kong")
        assert core.query_name("HK", "Hong Kong SAR, China") == "Hong Kong"

    def test_overrides_reach_the_query_text(self, monkeypatch):
        monkeypatch.setitem(core.QUERY_NAME_OVERRIDES, "HK", "Hong Kong")
        q = core.build_queries(core.query_name("HK", "Hong Kong SAR, China"))["broad"]
        assert '"Hong Kong"' in q and "SAR" not in q


class TestHeadlineKey:
    def test_the_outlet_suffix_google_appends_is_stripped(self):
        """Google News titles arrive as "Headline - Outlet"; the same story from
        the same outlet must not count twice because of it."""
        a = core.headline_key("Portugal raises rates - Reuters", "Reuters")
        b = core.headline_key("Portugal raises rates", "")
        assert a == b

    def test_punctuation_and_case_do_not_make_a_second_story(self):
        assert core.headline_key("Rates Rise, Again!") == core.headline_key("rates rise again")

    def test_different_stories_stay_different(self):
        assert core.headline_key("Rates rise") != core.headline_key("Rates fall")


class TestDedupe:
    def test_the_same_publisher_link_is_one_article(self):
        items = [
            {"publisher_link": "http://x/a", "title": "One", "themes": ["order"]},
            {"publisher_link": "http://x/a", "title": "One", "themes": ["broad"]},
        ]
        assert len(core.dedupe(items)) == 1

    def test_the_survivor_carries_every_theme_that_found_it(self):
        """A central bank under political pressure is genuinely friction *and*
        order. Dropping the second finding loses that, and the per-theme counts
        the model is shown would then be wrong."""
        items = [
            {"publisher_link": "http://x/a", "title": "One", "themes": ["friction"]},
            {"publisher_link": "http://x/a", "title": "One", "themes": ["order"]},
            {"publisher_link": "http://x/a", "title": "One", "themes": ["friction"]},
        ]
        out = core.dedupe(items)
        assert len(out) == 1
        assert sorted(out[0]["themes"]) == ["friction", "order"]

    def test_wire_copy_under_two_wrappers_is_one_article(self):
        """The failure the old wrapper-URL dedupe allowed: one story taking
        several of twenty slots because Google listed it more than once."""
        items = [
            {"publisher_link": "http://a/1", "title": "IMF warns on debt - AP",
             "source": "AP", "themes": ["order"]},
            {"publisher_link": "http://b/2", "title": "IMF warns on debt",
             "source": "", "themes": ["broad"]},
        ]
        out = core.dedupe(items)
        assert len(out) == 1
        assert sorted(out[0]["themes"]) == ["broad", "order"]

    def test_order_is_preserved_and_the_first_one_wins(self):
        items = [
            {"publisher_link": "http://x/1", "title": "First", "themes": ["order"]},
            {"publisher_link": "http://x/2", "title": "Second", "themes": ["edge"]},
            {"publisher_link": "http://x/1", "title": "First", "themes": ["broad"]},
        ]
        out = core.dedupe(items)
        assert [i["title"] for i in out] == ["First", "Second"]

    def test_the_input_is_not_mutated(self):
        items = [{"publisher_link": "http://x/1", "title": "A", "themes": ["order"]}]
        core.dedupe(items + [{"publisher_link": "http://x/1", "title": "A", "themes": ["edge"]}])
        assert items[0]["themes"] == ["order"]


class TestPagePublishedAt:
    """The feed dates a republished piece to the day it was re-listed, so a
    30-day window built on the feed date quietly admits years-old material."""

    def test_open_graph_published_time_is_read(self):
        html = '<meta property="article:published_time" content="2026-09-01T10:00:00Z">'
        assert fetch_links._page_published_at(html) == "2026-09-01T10:00:00Z"

    def test_the_attribute_order_publishers_actually_use_both_ways(self):
        html = '<meta content="2026-09-01T10:00:00Z" property="article:published_time">'
        assert fetch_links._page_published_at(html) == "2026-09-01T10:00:00Z"

    def test_json_ld_is_read_when_there_is_no_meta_tag(self):
        html = '<script type="application/ld+json">{"datePublished":"2019-01-07T08:00:00Z"}</script>'
        assert fetch_links._page_published_at(html) == "2019-01-07T08:00:00Z"

    def test_a_naive_date_is_treated_as_utc_rather_than_dropped(self):
        html = '<meta property="article:published_time" content="2026-09-01T10:00:00">'
        assert fetch_links._page_published_at(html) == "2026-09-01T10:00:00Z"

    def test_a_page_dated_in_the_future_is_a_template_not_a_date(self):
        html = '<meta property="article:published_time" content="2099-01-01T00:00:00Z">'
        assert fetch_links._page_published_at(html) is None

    def test_a_page_with_no_date_says_so(self):
        assert fetch_links._page_published_at("<html><body>no date here</body></html>") is None
        assert fetch_links._page_published_at("") is None

    def test_an_unparseable_date_does_not_raise(self):
        html = '<meta property="article:published_time" content="last Tuesday">'
        assert fetch_links._page_published_at(html) is None


class TestFetchCandidates:
    """The pool everything else draws from."""

    def test_one_theme_failing_does_not_cost_the_other_five(self, monkeypatch):
        def fake(query, **kw):
            if "conflict" in query:
                raise RuntimeError("feed down")
            return [{"publisher_link": f"http://x/{hash(query) % 97}", "title": "T",
                     "source": "Reuters"}]

        monkeypatch.setattr(core.fetch_links, "gnews_rss", fake)
        out = core.fetch_candidates("Portugal", "PT")
        assert out["report"]["per_theme"]["security"] == 0
        assert "security" in out["report"]["errors"]
        assert out["report"]["after_dedupe"] >= 1

    def test_stale_republications_are_counted_and_excluded(self, monkeypatch):
        """Counted, not silently dropped — a retrieval problem that is invisible
        looks exactly like a country with thin coverage."""
        def fake(query, **kw):
            return [
                {"publisher_link": "http://x/fresh", "title": "Fresh", "source": "R"},
                {"publisher_link": "http://x/old", "title": "Old", "source": "R",
                 "stale_republication": True},
            ]

        monkeypatch.setattr(core.fetch_links, "gnews_rss", fake)
        out = core.fetch_candidates("Portugal", "PT")
        assert out["report"]["stale_republications"] == len(core.THEMES)
        assert [i["title"] for i in out["items"]] == ["Fresh"]

    def test_the_report_names_the_top_publishers(self, monkeypatch):
        seq = iter(range(1000))

        def fake(query, **kw):
            n = next(seq)
            return [
                {"publisher_link": f"http://a/{n}", "title": f"Story {n}a", "source": "Reuters"},
                {"publisher_link": f"http://b/{n}", "title": f"Story {n}b", "source": "Reuters"},
                {"publisher_link": f"http://c/{n}", "title": f"Story {n}c", "source": "Bloomberg"},
            ]

        monkeypatch.setattr(core.fetch_links, "gnews_rss", fake)
        report = core.fetch_candidates("Portugal", "PT")["report"]
        assert report["top_publishers"][0] == ("Reuters", 2 * len(core.THEMES))
        assert dict(report["top_publishers"])["Bloomberg"] == len(core.THEMES)

    def test_the_report_says_when_a_query_name_was_overridden(self, monkeypatch):
        monkeypatch.setitem(core.QUERY_NAME_OVERRIDES, "HK", "Hong Kong")
        monkeypatch.setattr(core.fetch_links, "gnews_rss", lambda q, **kw: [])
        report = core.fetch_candidates("Hong Kong SAR, China", "HK")["report"]
        assert report["query_name"] == "Hong Kong"
        assert report["query_name_overridden"] is True
