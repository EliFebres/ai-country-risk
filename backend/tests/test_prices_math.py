"""Characterization tests for the price math in ``backend.prices_daemon`` and
``backend.utils.data_fetching.fmp_prices_fetch``.

The 1D/1Q/YTD numbers on the Prices pane come straight from these helpers;
note yields carry POINT changes while everything else carries percent.
"""

from datetime import date

import pytest

from backend import prices_daemon
from backend.utils.data_fetching import fmp_prices_fetch as fp


class TestPct:
    def test_basic_gain(self):
        assert prices_daemon._pct(110.0, 100.0) == 10.0

    def test_basic_loss(self):
        assert prices_daemon._pct(90.0, 100.0) == -10.0

    def test_rounding_to_two_places(self):
        assert prices_daemon._pct(100.567, 100.0) == 0.57

    def test_none_price(self):
        assert prices_daemon._pct(None, 100.0) is None

    def test_none_reference(self):
        assert prices_daemon._pct(100.0, None) is None

    def test_zero_reference(self):
        assert prices_daemon._pct(100.0, 0) is None


class TestQuarterStart:
    @pytest.mark.parametrize(
        "d,expected",
        [
            (date(2026, 1, 15), date(2026, 1, 1)),
            (date(2026, 3, 31), date(2026, 1, 1)),
            (date(2026, 4, 1), date(2026, 4, 1)),
            (date(2026, 6, 30), date(2026, 4, 1)),
            (date(2026, 7, 4), date(2026, 7, 1)),
            (date(2026, 12, 31), date(2026, 10, 1)),
        ],
    )
    def test_quarter_boundaries(self, d, expected):
        assert fp._quarter_start(d) == expected


class TestFirstCloseOnOrAfter:
    SERIES = [
        {"date": date(2026, 1, 2), "close": 100.0},
        {"date": date(2026, 1, 5), "close": 101.0},
        {"date": date(2026, 4, 1), "close": 105.0},
    ]

    def test_exact_match(self):
        got = fp._first_close_on_or_after(self.SERIES, date(2026, 1, 5))
        assert got["close"] == 101.0

    def test_next_after_gap(self):
        # Jan 3 has no row; the first row on/after is Jan 5.
        got = fp._first_close_on_or_after(self.SERIES, date(2026, 1, 3))
        assert got["close"] == 101.0

    def test_before_series_start(self):
        got = fp._first_close_on_or_after(self.SERIES, date(2025, 12, 1))
        assert got["close"] == 100.0

    def test_after_series_end_gives_none(self):
        assert fp._first_close_on_or_after(self.SERIES, date(2026, 5, 1)) is None

    def test_empty_series(self):
        assert fp._first_close_on_or_after([], date(2026, 1, 1)) is None


class TestToFloatOrNonePrices:
    def test_percent_string_stripped(self):
        # Unlike fmp_calendar_fetch's version, this one strips '%'.
        assert fp._to_float_or_none("+1.2%") == 1.2

    def test_plain_string(self):
        assert fp._to_float_or_none("3.14") == 3.14

    def test_none(self):
        assert fp._to_float_or_none(None) is None

    def test_empty(self):
        assert fp._to_float_or_none("") is None

    def test_garbage(self):
        assert fp._to_float_or_none("n/a") is None
