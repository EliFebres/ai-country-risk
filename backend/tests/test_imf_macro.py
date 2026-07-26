"""Characterization tests for ``backend.utils.data_fetching.imf_macro_fetch``.

``_period_to_date`` turns SDMX period strings into the end-of-period dates
stored in ``recent_indicator``; its end-of-month arithmetic is pinned here
before being swapped for ``calendar.monthrange``.
"""

import datetime as dt

import pytest

from backend.utils.data_fetching import imf_macro_fetch as imf


class TestPeriodToDate:
    @pytest.mark.parametrize(
        "period,expected",
        [
            ("2026-M01", dt.date(2026, 1, 31)),
            ("2026-M03", dt.date(2026, 3, 31)),
            ("2026-M04", dt.date(2026, 4, 30)),
            ("2026-M12", dt.date(2026, 12, 31)),   # December special case
            ("2024-M02", dt.date(2024, 2, 29)),    # leap year
            ("2025-M02", dt.date(2025, 2, 28)),    # non-leap year
            ("2026-Q1", dt.date(2026, 3, 31)),
            ("2026-Q2", dt.date(2026, 6, 30)),
            ("2026-Q3", dt.date(2026, 9, 30)),
            ("2026-Q4", dt.date(2026, 12, 31)),    # Q4 special case
            ("2026", dt.date(2026, 12, 31)),       # annual
        ],
    )
    def test_valid_periods(self, period, expected):
        assert imf._period_to_date(period) == expected

    @pytest.mark.parametrize(
        "period",
        ["2026-M00", "2026-M13", "2026-Q0", "2026-Q5", "banana", "", None, "2026-W07"],
    )
    def test_invalid_periods_return_none(self, period):
        assert imf._period_to_date(period) is None

    def test_whitespace_stripped(self):
        assert imf._period_to_date("  2026-M03  ") == dt.date(2026, 3, 31)


class TestLocalname:
    def test_namespaced(self):
        assert imf._localname("{http://ns}Obs") == "Obs"

    def test_plain(self):
        assert imf._localname("Obs") == "Obs"
