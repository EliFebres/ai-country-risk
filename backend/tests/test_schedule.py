"""Characterization tests for main.py's due checks.

These two predicates are the whole scheduler: they decide, from the ``job_run``
timestamp alone, whether a job runs on this tick. A job that has never run must
always be due (that is how a fresh database bootstraps itself), and ``_weekly``
must fire on the ISO-week rollover rather than 7×24h after the last run.
"""

from datetime import datetime, timedelta, timezone

from backend.main import _every, _weekly


def _utc(y, m, d, h=0):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


class TestWeekly:
    def test_never_run_is_due(self):
        assert _weekly(None, _utc(2026, 7, 28)) is True

    def test_same_iso_week_is_not_due(self):
        # Mon 2026-07-27 and Fri 2026-07-31 share ISO week 31.
        assert _weekly(_utc(2026, 7, 27), _utc(2026, 7, 31)) is False

    def test_sunday_to_monday_crosses(self):
        # Two days apart, but a new ISO week — this is the Monday run.
        assert _weekly(_utc(2026, 7, 26, 23), _utc(2026, 7, 27, 1)) is True

    def test_same_week_number_different_year(self):
        assert _weekly(_utc(2025, 7, 28), _utc(2026, 7, 27)) is True


class TestEvery:
    def test_never_run_is_due(self):
        assert _every(30)(None, _utc(2026, 7, 28)) is True

    def test_short_of_the_interval(self):
        now = _utc(2026, 7, 28)
        assert _every(30)(now - timedelta(days=29, hours=23), now) is False

    def test_exactly_the_interval(self):
        now = _utc(2026, 7, 28)
        assert _every(30)(now - timedelta(days=30), now) is True

    def test_long_overdue(self):
        now = _utc(2026, 7, 28)
        assert _every(30)(now - timedelta(days=400), now) is True
