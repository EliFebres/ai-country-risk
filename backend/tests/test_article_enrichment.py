"""Tests for the article selection in ``article_enrichment``.

Only the selection is covered, and deliberately so: the fetch itself is Google
News over the network, and pinning a live feed would test Google rather than us.
What is worth pinning is the arithmetic that decides which of ~60 retrieved
articles become the ~20 the model pays to read.

The failure this guards against is silent and expensive. Without a per-theme
floor an election week returns twenty election stories, the friction and
information ledgers get scored on the macro panel alone, and nothing anywhere
reports that the tax story was retrieved and then dropped.

No network, no database, no model — ``_select_with_theme_floor`` is pure.
"""

import pytest

from backend.utils.news_fetching import article_enrichment as ae


def art(theme, relevance, published="2026-07-20T00:00:00Z", title=""):
    """One scored article as the fetch loop would have tagged it."""
    return {"_theme": theme, "relevance_score": relevance,
            "published": published, "title": title,
            "publisher_link": f"https://x.test/{theme}/{relevance}/{title}"}


def select(items, max_articles=20, per_theme=ae._PER_THEME_FLOOR):
    return ae._select_with_theme_floor(items, max_articles, per_theme)


def themes_of(items):
    from collections import Counter
    return Counter(i["_theme"] for i in items)


class TestQueryThemes:
    def test_every_ledger_has_a_query(self):
        # The retrieval layer exists to feed the ledgers the prompt scores.
        # Dropping one here would silently leave that ledger to the macro panel.
        assert set(ae._QUERY_THEMES) == {
            "friction", "order", "security", "information", "edge", "broad"}

    def test_every_template_takes_the_country(self):
        for theme, template in ae._QUERY_THEMES.items():
            assert "{c}" in template, f"{theme} template ignores the country"
            assert template.format(c="Brazil").startswith('"Brazil"')

    def test_the_floor_fits_inside_the_budget(self):
        # 6 themes x the floor must leave room for the open fill, or the
        # relevance ranking stops mattering at all.
        assert len(ae._QUERY_THEMES) * ae._PER_THEME_FLOOR < 20


class TestHeadlineKey:
    """Syndicated wire copies share a headline and differ only by URL, so the
    publisher-URL key cannot see them. Each survivor costs a stage-1 digest
    call, and the model's topic_group merges them afterwards — so the waste
    never shows up in the output."""

    def test_syndicated_copies_collapse(self):
        # The four Brazil copies observed in a live run, verbatim shapes.
        titles = [
            "Brazil government now expects 2026 inflation to be above central bank's target - Yahoo",
            "Brazil Government Now Expects 2026 Inflation to Be Above Central Bank's Target - Reuters",
            "Brazil government now expects 2026 inflation to be above central bank's target - MSN",
        ]
        assert len({ae._headline_key(t) for t in titles}) == 1

    def test_publisher_suffix_is_dropped(self):
        assert ae._headline_key("Lula meets Xi - BBC") == ae._headline_key("Lula meets Xi - Reuters")

    def test_only_the_last_suffix_is_dropped(self):
        # A headline with its own " - " keeps everything up to the publisher.
        assert ae._headline_key("Brazil - China trade deal signed - BBC") == "brazil china trade deal signed"

    def test_different_stories_stay_different(self):
        assert ae._headline_key("Brazil raises rates - BBC") != ae._headline_key("Brazil cuts rates - BBC")

    def test_punctuation_and_case_are_ignored(self):
        assert ae._headline_key("Brazil's  Central-Bank: Acts!") == "brazil s central bank acts"

    def test_missing_title_is_falsy_not_an_error(self):
        # An empty key must not make every untitled article a duplicate of the
        # first one - the caller skips the headline check when this is empty.
        assert ae._headline_key(None) == "" and ae._headline_key("") == ""


class TestThemeFloor:
    def test_a_loud_theme_cannot_take_every_slot(self):
        # 30 election stories all scoring higher than anything else. Under a
        # plain relevance sort this is the whole budget.
        loud = [art("order", 0.95, title=f"election {i}") for i in range(30)]
        quiet = [art("friction", 0.4, title="new tax on imports"),
                 art("information", 0.4, title="regulator audits the statistics office")]
        counts = themes_of(select(loud + quiet, max_articles=20))
        assert counts["friction"] == 1 and counts["information"] == 1
        assert counts["order"] == 18

    def test_each_theme_gets_its_floor(self):
        items = [art(t, 0.5, title=f"{t}{i}") for t in ae._QUERY_THEMES for i in range(5)]
        counts = themes_of(select(items, max_articles=12, per_theme=2))
        assert all(counts[t] == 2 for t in ae._QUERY_THEMES)

    def test_the_floor_takes_each_themes_best(self):
        items = [art("friction", 0.9, title="best"), art("friction", 0.4, title="worst"),
                 art("edge", 0.5, title="edge")]
        picked = select(items, max_articles=2, per_theme=1)
        assert {p["title"] for p in picked} == {"best", "edge"}

    def test_remainder_fills_by_relevance(self):
        # Floor of 1 each takes the best two; the last slot is the best of what
        # is left, regardless of theme.
        items = [art("order", 0.9, title="o1"), art("order", 0.8, title="o2"),
                 art("edge", 0.5, title="e1"), art("edge", 0.2, title="e2")]
        assert [p["title"] for p in select(items, max_articles=3, per_theme=1)] == \
            ["o1", "o2", "e1"]

    def test_an_absent_theme_forfeits_rather_than_shrinks(self):
        # Only two themes have news; the budget still fills to 5.
        items = [art("order", 0.9 - i / 100, title=f"o{i}") for i in range(4)] + \
                [art("edge", 0.5, title="e0")]
        assert len(select(items, max_articles=5, per_theme=2)) == 5

    def test_never_exceeds_the_budget(self):
        items = [art(t, 0.5, title=f"{t}{i}") for t in ae._QUERY_THEMES for i in range(10)]
        assert len(select(items, max_articles=20)) == 20

    def test_returns_fewer_than_the_budget_when_that_is_all_there_is(self):
        assert len(select([art("order", 0.9), art("edge", 0.5)], max_articles=20)) == 2

    def test_output_is_most_relevant_first(self):
        # The caller assigns ids a1..aN by position, so order is part of the
        # contract even though the floor picks out of order.
        items = [art("order", 0.3, title="low"), art("edge", 0.9, title="high"),
                 art("friction", 0.6, title="mid")]
        scores = [p["relevance_score"] for p in select(items, max_articles=3)]
        assert scores == sorted(scores, reverse=True)

    def test_ties_break_toward_the_more_recent(self):
        items = [art("order", 0.5, published="2026-01-01T00:00:00Z", title="old"),
                 art("order", 0.5, published="2026-07-01T00:00:00Z", title="new")]
        assert [p["title"] for p in select(items, max_articles=2)] == ["new", "old"]

    def test_an_untagged_item_still_reaches_the_open_fill(self):
        # Defensive: an item without a _theme must not vanish, only lose its
        # claim on a reserved slot.
        items = [art("order", 0.9), {"relevance_score": 0.8, "published": None}]
        assert len(select(items, max_articles=5, per_theme=1)) == 2

    def test_empty_pool_is_empty_not_an_error(self):
        assert select([], max_articles=20) == []

    @pytest.mark.parametrize("per_theme", [0, 1, 2, 5])
    def test_any_floor_returns_a_full_budget(self, per_theme):
        items = [art(t, 0.5, title=f"{t}{i}") for t in ae._QUERY_THEMES for i in range(6)]
        assert len(select(items, max_articles=20, per_theme=per_theme)) == 20
