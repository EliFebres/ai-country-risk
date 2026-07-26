"""Characterization tests for ``backend.utils.data_fetching.country_data_fetch``.

``has_country_partition`` decides whether a country's World Bank panel gets
re-fetched. A false negative costs a slow refetch; a false positive leaves the
country with no data at all.
"""

from backend.utils.data_fetching.country_data_fetch import (
    PANEL_DIR,
    has_country_partition,
)


class TestHasCountryPartition:
    def test_missing_dir(self, tmp_path):
        assert has_country_partition(tmp_path, "DE") is False

    def test_empty_partition_dir(self, tmp_path):
        (tmp_path / "country_code=DE").mkdir()
        assert has_country_partition(tmp_path, "DE") is False

    def test_partition_with_parquet(self, tmp_path):
        part = tmp_path / "country_code=DE"
        part.mkdir()
        (part / "data_0.parquet").write_bytes(b"")
        assert has_country_partition(tmp_path, "DE") is True

    def test_non_parquet_files_ignored(self, tmp_path):
        part = tmp_path / "country_code=DE"
        part.mkdir()
        (part / "notes.txt").write_text("x")
        assert has_country_partition(tmp_path, "DE") is False


class TestPanelDir:
    def test_points_at_backend_data(self):
        # The writer's path must match the reader's (data_retrieval.DATA_DIR).
        from backend.utils.data_retrieval import DATA_DIR
        assert PANEL_DIR == DATA_DIR
        assert PANEL_DIR.name == "wb_panel_wide"
        assert PANEL_DIR.parent.parent.name == "backend"
