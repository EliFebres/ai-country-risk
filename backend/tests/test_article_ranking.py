"""Characterization tests for ``backend.utils.news_fetching.article_ranking``.

Two rounds of ranking are pinned here. ``score_relevance`` decides which
articles are worth sending to the LLM at all (its 0.1 no-mention floor and
keyword caps are deliberate). ``ensure_top_three`` is the only guard behind
the ``rank BETWEEN 1 AND 3`` DB constraint: it must return exactly 3 ids
whenever 3+ articles exist, across the topic-representative, backfill, and
no-topic-map branches.
"""

import pytest

from backend.utils.news_fetching import article_ranking


def items(n: int) -> dict:
    """n articles a1..aN with distinct dates so recency tie-breaks are stable."""
    return {
        f"a{i}": {"id": f"a{i}", "published": f"2026-01-{i:02d}", "relevance_score": 0.5}
        for i in range(1, n + 1)
    }


class TestTopicRepresentatives:
    def test_three_topics_one_rep_each(self):
        by_id = items(6)
        imp = {"a1": 0.9, "a2": 0.8, "a3": 0.7, "a4": 0.6, "a5": 0.5, "a6": 0.4}
        topics = {"a1": "t1", "a2": "t1", "a3": "t2", "a4": "t2", "a5": "t3", "a6": "t3"}
        got = article_ranking.ensure_top_three(by_id, imp, topics, "XX")
        # Best of each topic by impact: a1 (t1), a3 (t2), a5 (t3), ordered by impact.
        assert got == ["a1", "a3", "a5"]

    def test_more_than_three_topics_takes_top_three_by_impact(self):
        by_id = items(4)
        imp = {"a1": 0.1, "a2": 0.9, "a3": 0.5, "a4": 0.7}
        topics = {"a1": "t1", "a2": "t2", "a3": "t3", "a4": "t4"}
        got = article_ranking.ensure_top_three(by_id, imp, topics, "XX")
        assert got == ["a2", "a4", "a3"]

    def test_topic_ids_missing_from_items_are_ignored(self):
        by_id = items(3)
        imp = {"a1": 0.9, "a2": 0.8, "a3": 0.7}
        topics = {"a1": "t1", "a2": "t2", "a3": "t3", "ghost": "t4"}
        got = article_ranking.ensure_top_three(by_id, imp, topics, "XX")
        assert got == ["a1", "a2", "a3"]


class TestBackfill:
    def test_two_topics_backfilled_to_three(self):
        by_id = items(5)
        imp = {"a1": 0.9, "a2": 0.8, "a3": 0.7, "a4": 0.6, "a5": 0.5}
        topics = {"a1": "t1", "a2": "t1", "a3": "t2"}
        got = article_ranking.ensure_top_three(by_id, imp, topics, "XX")
        # Reps: a1 (t1), a3 (t2); backfill best remaining by impact: a2.
        assert got == ["a1", "a3", "a2"]

    def test_one_topic_backfilled(self):
        by_id = items(4)
        imp = {"a1": 0.2, "a2": 0.9, "a3": 0.5, "a4": 0.7}
        topics = {"a2": "t1"}
        got = article_ranking.ensure_top_three(by_id, imp, topics, "XX")
        # Rep: a2; backfill by impact: a4, a3.
        assert got == ["a2", "a4", "a3"]

    def test_fewer_than_three_articles_returns_what_exists(self):
        by_id = items(2)
        imp = {"a1": 0.9, "a2": 0.8}
        topics = {"a1": "t1", "a2": "t2"}
        assert article_ranking.ensure_top_three(by_id, imp, topics, "XX") == ["a1", "a2"]


class TestNoTopicMap:
    def test_empty_topic_map_ranks_by_impact(self):
        by_id = items(4)
        imp = {"a1": 0.2, "a2": 0.9, "a3": 0.5, "a4": 0.7}
        assert article_ranking.ensure_top_three(by_id, imp, {}, "XX") == ["a2", "a4", "a3"]

    def test_none_topic_map_ranks_by_impact(self):
        by_id = items(4)
        imp = {"a1": 0.2, "a2": 0.9, "a3": 0.5, "a4": 0.7}
        assert article_ranking.ensure_top_three(by_id, imp, None, "XX") == ["a2", "a4", "a3"]

    def test_recency_breaks_impact_ties(self):
        by_id = items(3)  # a3 has the latest date
        imp = {"a1": 0.5, "a2": 0.5, "a3": 0.5}
        assert article_ranking.ensure_top_three(by_id, imp, {}, "XX") == ["a3", "a2", "a1"]


class TestEdges:
    def test_empty_items_returns_empty(self):
        assert article_ranking.ensure_top_three({}, {"a1": 0.9}, {"a1": "t1"}, "XX") == []

    def test_imp_map_backfilled_in_place(self):
        by_id = items(3)
        imp = {"a1": 0.9}
        article_ranking.ensure_top_three(by_id, imp, {}, "XX")
        # Side effect relied on downstream: missing ids get 0.0 impact.
        assert imp == {"a1": 0.9, "a2": 0.0, "a3": 0.0}


class TestScoreRelevance:
    def test_no_country_mention_floors_at_point_one(self):
        art = {"title": "Central bank raises rates", "summary": "inflation policy"}
        assert article_ranking.score_relevance(art, "Germany") == 0.1

    def test_country_mention_alone_gives_base(self):
        art = {"title": "A quiet day in Germany", "summary": ""}
        assert article_ranking.score_relevance(art, "Germany") == pytest.approx(0.3)

    def test_high_keywords_capped_at_half_point(self):
        # 5 high keywords in the summary -> min(5*0.15, 0.5) = 0.5, no title bonus.
        art = {
            "title": "Germany update",
            "summary": "government parliament election sanctions military",
        }
        assert article_ranking.score_relevance(art, "Germany") == pytest.approx(0.3 + 0.5)

    def test_title_high_keyword_adds_bonus(self):
        # One high keyword, in the title: 0.3 + 0.15 + 0.15 title bonus.
        art = {"title": "Germany election looms", "summary": ""}
        assert article_ranking.score_relevance(art, "Germany") == pytest.approx(0.6)

    def test_medium_keywords_capped(self):
        # 4 medium keywords -> min(4*0.08, 0.2) = 0.2.
        art = {"title": "Germany", "summary": "economy finance debt growth"}
        assert article_ranking.score_relevance(art, "Germany") == pytest.approx(0.5)

    def test_noise_penalty(self):
        # "festival" and "concert" are noise: 0.3 - 2*0.2 = -0.1 -> clamped to 0.
        art = {"title": "Germany music festival concert", "summary": ""}
        assert article_ranking.score_relevance(art, "Germany") == 0.0

    def test_score_clamped_to_one(self):
        art = {
            "title": "Germany war sanctions election government",
            "summary": "military conflict coup security budget fiscal trade policy "
                       "economy finance currency debt growth minister reform",
        }
        assert article_ranking.score_relevance(art, "Germany") == 1.0

    def test_snippet_used_when_summary_missing(self):
        art = {"title": "x", "snippet": "Germany inflation policy"}
        assert article_ranking.score_relevance(art, "Germany") > 0.1

    def test_case_insensitive(self):
        art = {"title": "GERMANY ELECTION", "summary": ""}
        assert article_ranking.score_relevance(art, "germany") == pytest.approx(0.6)


class TestRankIdsBy:
    def test_orders_by_impact_then_recency_then_relevance(self):
        items_by_id = {
            "a1": {"published": "2026-01-01", "relevance_score": 0.9},
            "a2": {"published": "2026-06-01", "relevance_score": 0.1},
            "a3": {"published": "2026-01-01", "relevance_score": 0.5},
        }
        impacts = {"a1": 0.2, "a2": 0.8, "a3": 0.2}
        # a2 wins on impact; a1 vs a3 tie on impact and date -> relevance breaks it.
        assert article_ranking.rank_ids_by(list(items_by_id), items_by_id, impacts) == ["a2", "a1", "a3"]

    def test_missing_impact_defaults_to_zero(self):
        items_by_id = {"a1": {}, "a2": {}}
        assert article_ranking.rank_ids_by(["a1", "a2"], items_by_id, {"a2": 0.5})[0] == "a2"


class TestImpactTopicMaps:
    def test_extracts_both_maps(self):
        out = {"news_article_scores": [
            {"id": "a1", "impact": 0.8, "topic_group": "war"},
            {"id": "a2", "impact": 0.2, "topic_group": "macro"},
        ]}
        imp, topics = article_ranking.impact_topic_maps(out)
        assert imp == {"a1": 0.8, "a2": 0.2}
        assert topics == {"a1": "war", "a2": "macro"}

    def test_bad_impact_becomes_zero(self):
        out = {"news_article_scores": [{"id": "a1", "impact": "junk", "topic_group": "x"}]}
        imp, _ = article_ranking.impact_topic_maps(out)
        assert imp == {"a1": 0.0}

    def test_missing_topic_group_defaults_unknown(self):
        out = {"news_article_scores": [{"id": "a1", "impact": 0.5}]}
        _, topics = article_ranking.impact_topic_maps(out)
        assert topics == {"a1": "unknown"}

    def test_entries_without_id_skipped(self):
        out = {"news_article_scores": [{"impact": 0.5}, "not a dict"]}
        assert article_ranking.impact_topic_maps(out) == ({}, {})

    def test_empty_output(self):
        assert article_ranking.impact_topic_maps({}) == ({}, {})


class TestBuildTopArticles:
    def test_shapes_rows_with_ranks(self):
        items_by_id = {"a1": {"id": "a1", "link": "http://x", "title": "T",
                              "source": "S", "published": "2026-01-01", "image": "http://i"}}
        rows = article_ranking.build_top_articles(["a1"], items_by_id, {"a1": 0.7})
        assert rows == [{
            "rank": 1, "id": "a1", "url": "http://x", "title": "T", "source": "S",
            "published_at": "2026-01-01", "impact": 0.7, "summary": "", "image": "http://i",
        }]

    def test_missing_item_yields_blank_row(self):
        rows = article_ranking.build_top_articles(["ghost"], {}, {})
        assert rows[0]["rank"] == 1 and rows[0]["url"] == "" and rows[0]["id"] is None
