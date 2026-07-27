"""Characterization tests for ``backend.utils.dates``.

``utc_minute_iso`` is the ETL's ``generated_at`` contract — ``data_push``
parses its output back into a snapshot's ``as_of``, so the exact format is
load-bearing. ``parse_date_for_sort``'s epoch fallback decides where
undated articles land in the ranking.
"""

from datetime import datetime, timezone

from backend.utils.dates import date_prefix, parse_date_for_sort, utc_minute_iso


class TestUtcMinuteIso:
    def test_aware_utc(self):
        dt = datetime(2026, 5, 1, 12, 30, 45, tzinfo=timezone.utc)
        assert utc_minute_iso(dt) == "2026-05-01T12:30Z"

    def test_naive_assumed_utc(self):
        assert utc_minute_iso(datetime(2026, 5, 1, 12, 30)) == "2026-05-01T12:30Z"


class TestParseDateForSort:
    EPOCH = datetime(1970, 1, 1)

    def test_none_returns_epoch(self):
        assert parse_date_for_sort(None) == self.EPOCH

    def test_empty_returns_epoch(self):
        assert parse_date_for_sort("") == self.EPOCH

    def test_garbage_returns_epoch(self):
        assert parse_date_for_sort("not a date") == self.EPOCH

    def test_iso_with_z(self):
        got = parse_date_for_sort("2026-05-01T12:30:00Z")
        assert got == datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)

    def test_iso_naive(self):
        assert parse_date_for_sort("2026-05-01T12:30:00") == datetime(2026, 5, 1, 12, 30)

    def test_date_only(self):
        assert parse_date_for_sort("2026-05-01") == datetime(2026, 5, 1)

    def test_date_prefix_of_longer_junk(self):
        # Falls through ISO parse, then strptime on the first 10 chars.
        assert parse_date_for_sort("2026-05-01 extra junk") == datetime(2026, 5, 1)


class TestDatePrefix:
    def test_datetime(self):
        assert date_prefix(datetime(2026, 5, 1, 12, 30)) == "2026-05-01"

    def test_iso_string(self):
        assert date_prefix("2026-05-01T12:30:00Z") == "2026-05-01"

    def test_other_types_give_empty(self):
        assert date_prefix(None) == ""
        assert date_prefix(12345) == ""
