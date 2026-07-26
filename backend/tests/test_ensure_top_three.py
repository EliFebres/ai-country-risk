"""Characterization tests for ``backend.main.ensure_top_three``.

This is the only guard behind the ``rank BETWEEN 1 AND 3`` DB constraint: it
must return exactly 3 ids whenever 3+ articles exist, across the
topic-representative, backfill, and no-topic-map branches. Written when the
function was lifted from a closure inside the per-country loop.
"""

from backend import main


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
        got = main.ensure_top_three(by_id, imp, topics, "XX")
        # Best of each topic by impact: a1 (t1), a3 (t2), a5 (t3), ordered by impact.
        assert got == ["a1", "a3", "a5"]

    def test_more_than_three_topics_takes_top_three_by_impact(self):
        by_id = items(4)
        imp = {"a1": 0.1, "a2": 0.9, "a3": 0.5, "a4": 0.7}
        topics = {"a1": "t1", "a2": "t2", "a3": "t3", "a4": "t4"}
        got = main.ensure_top_three(by_id, imp, topics, "XX")
        assert got == ["a2", "a4", "a3"]

    def test_topic_ids_missing_from_items_are_ignored(self):
        by_id = items(3)
        imp = {"a1": 0.9, "a2": 0.8, "a3": 0.7}
        topics = {"a1": "t1", "a2": "t2", "a3": "t3", "ghost": "t4"}
        got = main.ensure_top_three(by_id, imp, topics, "XX")
        assert got == ["a1", "a2", "a3"]


class TestBackfill:
    def test_two_topics_backfilled_to_three(self):
        by_id = items(5)
        imp = {"a1": 0.9, "a2": 0.8, "a3": 0.7, "a4": 0.6, "a5": 0.5}
        topics = {"a1": "t1", "a2": "t1", "a3": "t2"}
        got = main.ensure_top_three(by_id, imp, topics, "XX")
        # Reps: a1 (t1), a3 (t2); backfill best remaining by impact: a2.
        assert got == ["a1", "a3", "a2"]

    def test_one_topic_backfilled(self):
        by_id = items(4)
        imp = {"a1": 0.2, "a2": 0.9, "a3": 0.5, "a4": 0.7}
        topics = {"a2": "t1"}
        got = main.ensure_top_three(by_id, imp, topics, "XX")
        # Rep: a2; backfill by impact: a4, a3.
        assert got == ["a2", "a4", "a3"]

    def test_fewer_than_three_articles_returns_what_exists(self):
        by_id = items(2)
        imp = {"a1": 0.9, "a2": 0.8}
        topics = {"a1": "t1", "a2": "t2"}
        assert main.ensure_top_three(by_id, imp, topics, "XX") == ["a1", "a2"]


class TestNoTopicMap:
    def test_empty_topic_map_ranks_by_impact(self):
        by_id = items(4)
        imp = {"a1": 0.2, "a2": 0.9, "a3": 0.5, "a4": 0.7}
        assert main.ensure_top_three(by_id, imp, {}, "XX") == ["a2", "a4", "a3"]

    def test_none_topic_map_ranks_by_impact(self):
        by_id = items(4)
        imp = {"a1": 0.2, "a2": 0.9, "a3": 0.5, "a4": 0.7}
        assert main.ensure_top_three(by_id, imp, None, "XX") == ["a2", "a4", "a3"]

    def test_recency_breaks_impact_ties(self):
        by_id = items(3)  # a3 has the latest date
        imp = {"a1": 0.5, "a2": 0.5, "a3": 0.5}
        assert main.ensure_top_three(by_id, imp, {}, "XX") == ["a3", "a2", "a1"]


class TestEdges:
    def test_empty_items_returns_empty(self):
        assert main.ensure_top_three({}, {"a1": 0.9}, {"a1": "t1"}, "XX") == []

    def test_imp_map_backfilled_in_place(self):
        by_id = items(3)
        imp = {"a1": 0.9}
        main.ensure_top_three(by_id, imp, {}, "XX")
        # Side effect relied on downstream: missing ids get 0.0 impact.
        assert imp == {"a1": 0.9, "a2": 0.0, "a3": 0.0}
