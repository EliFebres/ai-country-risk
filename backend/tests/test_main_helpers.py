"""Characterization tests for the pure helpers in ``backend.main``.

The relevance scorer and date-sort fallback decide which articles reach the
LLM and the database; their quirks (epoch fallback, 0.1 no-mention floor) are
pinned here on purpose.
"""

from datetime import datetime, timezone

import pytest

from backend import main


class TestScoreArticleRelevance:
    def test_no_country_mention_floors_at_point_one(self):
        art = {"title": "Central bank raises rates", "summary": "inflation policy"}
        assert main._score_article_relevance(art, "Germany") == 0.1

    def test_country_mention_alone_gives_base(self):
        art = {"title": "A quiet day in Germany", "summary": ""}
        assert main._score_article_relevance(art, "Germany") == pytest.approx(0.3)

    def test_high_keywords_capped_at_half_point(self):
        # 5 high keywords in the summary -> min(5*0.15, 0.5) = 0.5, no title bonus.
        art = {
            "title": "Germany update",
            "summary": "government parliament election sanctions military",
        }
        assert main._score_article_relevance(art, "Germany") == pytest.approx(0.3 + 0.5)

    def test_title_high_keyword_adds_bonus(self):
        # One high keyword, in the title: 0.3 + 0.15 + 0.15 title bonus.
        art = {"title": "Germany election looms", "summary": ""}
        assert main._score_article_relevance(art, "Germany") == pytest.approx(0.6)

    def test_medium_keywords_capped(self):
        # 4 medium keywords -> min(4*0.08, 0.2) = 0.2.
        art = {"title": "Germany", "summary": "economy finance debt growth"}
        assert main._score_article_relevance(art, "Germany") == pytest.approx(0.5)

    def test_noise_penalty(self):
        # "festival" and "concert" are noise: 0.3 - 2*0.2 = -0.1 -> clamped to 0.
        art = {"title": "Germany music festival concert", "summary": ""}
        assert main._score_article_relevance(art, "Germany") == 0.0

    def test_score_clamped_to_one(self):
        art = {
            "title": "Germany war sanctions election government",
            "summary": "military conflict coup security budget fiscal trade policy "
                       "economy finance currency debt growth minister reform",
        }
        assert main._score_article_relevance(art, "Germany") == 1.0

    def test_snippet_used_when_summary_missing(self):
        art = {"title": "x", "snippet": "Germany inflation policy"}
        assert main._score_article_relevance(art, "Germany") > 0.1

    def test_case_insensitive(self):
        art = {"title": "GERMANY ELECTION", "summary": ""}
        assert main._score_article_relevance(art, "germany") == pytest.approx(0.6)


class TestParseDateForSort:
    EPOCH = datetime(1970, 1, 1)

    def test_none_returns_epoch(self):
        assert main._parse_date_for_sort(None) == self.EPOCH

    def test_empty_returns_epoch(self):
        assert main._parse_date_for_sort("") == self.EPOCH

    def test_garbage_returns_epoch(self):
        assert main._parse_date_for_sort("not a date") == self.EPOCH

    def test_iso_with_z(self):
        got = main._parse_date_for_sort("2026-05-01T12:30:00Z")
        assert got == datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)

    def test_iso_naive(self):
        assert main._parse_date_for_sort("2026-05-01T12:30:00") == datetime(2026, 5, 1, 12, 30)

    def test_date_only(self):
        assert main._parse_date_for_sort("2026-05-01") == datetime(2026, 5, 1)

    def test_date_prefix_of_longer_junk(self):
        # Falls through ISO parse, then strptime on the first 10 chars.
        assert main._parse_date_for_sort("2026-05-01 extra junk") == datetime(2026, 5, 1)


class TestToUtcIso:
    def test_aware_utc(self):
        dt = datetime(2026, 5, 1, 12, 30, 45, tzinfo=timezone.utc)
        assert main._to_utc_iso(dt) == "2026-05-01T12:30Z"

    def test_naive_assumed_utc(self):
        assert main._to_utc_iso(datetime(2026, 5, 1, 12, 30)) == "2026-05-01T12:30Z"


class TestHasCountryPartition:
    def test_missing_dir(self, tmp_path):
        assert main._has_country_partition(tmp_path, "DE") is False

    def test_empty_partition_dir(self, tmp_path):
        (tmp_path / "country_code=DE").mkdir()
        assert main._has_country_partition(tmp_path, "DE") is False

    def test_partition_with_parquet(self, tmp_path):
        part = tmp_path / "country_code=DE"
        part.mkdir()
        (part / "data_0.parquet").write_bytes(b"")
        assert main._has_country_partition(tmp_path, "DE") is True

    def test_non_parquet_files_ignored(self, tmp_path):
        part = tmp_path / "country_code=DE"
        part.mkdir()
        (part / "notes.txt").write_text("x")
        assert main._has_country_partition(tmp_path, "DE") is False


class TestRankIdsBy:
    def test_orders_by_impact_then_recency_then_relevance(self):
        items = {
            "a1": {"published": "2026-01-01", "relevance_score": 0.9},
            "a2": {"published": "2026-06-01", "relevance_score": 0.1},
            "a3": {"published": "2026-01-01", "relevance_score": 0.5},
        }
        impacts = {"a1": 0.2, "a2": 0.8, "a3": 0.2}
        # a2 wins on impact; a1 vs a3 tie on impact and date -> relevance breaks it.
        assert main._rank_ids_by(list(items), items, impacts) == ["a2", "a1", "a3"]

    def test_missing_impact_defaults_to_zero(self):
        items = {"a1": {}, "a2": {}}
        assert main._rank_ids_by(["a1", "a2"], items, {"a2": 0.5})[0] == "a2"
