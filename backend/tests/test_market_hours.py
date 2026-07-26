"""Characterization tests for ``backend.utils.market_hours``.

These pin the exact market-gating behavior (including the hand-rolled DST
arithmetic) before any refactor touches it. The daemon's API spend and the
freshness of the Prices pane both depend on ``is_open`` being right.
"""

from datetime import date, datetime, timezone

import pytest

from backend.utils import market_hours


def utc(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


class TestNthWeekday:
    def test_second_sunday_of_march_2026(self):
        assert market_hours._nth_weekday(2026, 3, 6, 2) == date(2026, 3, 8)

    def test_first_sunday_of_november_2026(self):
        assert market_hours._nth_weekday(2026, 11, 6, 1) == date(2026, 11, 1)

    def test_second_sunday_of_march_2025(self):
        assert market_hours._nth_weekday(2025, 3, 6, 2) == date(2025, 3, 9)

    def test_first_sunday_of_november_2025(self):
        assert market_hours._nth_weekday(2025, 11, 6, 1) == date(2025, 11, 2)


class TestIsUsDst:
    @pytest.mark.parametrize(
        "d,expected",
        [
            (date(2026, 3, 7), False),    # day before spring-forward
            (date(2026, 3, 8), True),     # spring-forward day (date-based rule)
            (date(2026, 7, 4), True),     # midsummer
            (date(2026, 10, 31), True),   # day before fall-back
            (date(2026, 11, 1), False),   # fall-back day (date-based rule)
            (date(2026, 12, 25), False),  # winter
            (date(2025, 3, 9), True),
            (date(2025, 11, 2), False),
        ],
    )
    def test_dst_boundaries(self, d, expected):
        assert market_hours._is_us_dst(d) is expected


class TestEasternNow:
    def test_edt_offset_in_summer(self):
        # 2026-07-01 16:00 UTC -> 12:00 EDT
        et = market_hours.eastern_now(utc(2026, 7, 1, 16, 0))
        assert et == datetime(2026, 7, 1, 12, 0)
        assert et.tzinfo is None

    def test_est_offset_in_winter(self):
        # 2026-01-15 16:00 UTC -> 11:00 EST
        assert market_hours.eastern_now(utc(2026, 1, 15, 16, 0)) == datetime(2026, 1, 15, 11, 0)

    def test_naive_input_treated_as_utc(self):
        naive = datetime(2026, 7, 1, 16, 0)
        assert market_hours.eastern_now(naive) == datetime(2026, 7, 1, 12, 0)

    def test_offset_chosen_by_utc_date_on_spring_transition(self):
        # Quirk pinned deliberately: the DST decision uses the *UTC* calendar
        # date. At 2026-03-08 03:00 UTC the real US/Eastern wall clock is still
        # EST (22:00 Mar 7), but the UTC date (Mar 8) is already a DST date, so
        # the code applies -4 and returns 23:00. Any replacement must either
        # reproduce this or prove the difference is unobservable via is_open.
        assert market_hours.eastern_now(utc(2026, 3, 8, 3, 0)) == datetime(2026, 3, 7, 23, 0)

    def test_offset_chosen_by_utc_date_on_fall_transition(self):
        # Same quirk in November: 2026-11-01 04:00 UTC is really 00:00 EDT
        # (Nov 1), but the UTC-date rule applies -5 and returns 23:00 Oct 31.
        assert market_hours.eastern_now(utc(2026, 11, 1, 4, 0)) == datetime(2026, 10, 31, 23, 0)


class TestIsOpenCryptoAndBonds:
    @pytest.mark.parametrize("cls", ["crypto", "bonds"])
    @pytest.mark.parametrize(
        "now", [utc(2026, 7, 4, 3, 0), utc(2026, 1, 1, 0, 0), utc(2026, 7, 1, 12, 0)]
    )
    def test_always_open(self, cls, now):
        assert market_hours.is_open(cls, now) is True

    def test_unknown_class_not_gated(self):
        assert market_hours.is_open("forex", utc(2026, 7, 4, 3, 0)) is True


class TestIsOpenStocks:
    # 2026-07-01 is a Wednesday. EDT = UTC-4, so 09:30 ET = 13:30 UTC.
    def test_open_mid_session(self):
        assert market_hours.is_open("stocks", utc(2026, 7, 1, 14, 0)) is True

    def test_open_at_bell(self):
        assert market_hours.is_open("stocks", utc(2026, 7, 1, 13, 30)) is True

    def test_closed_one_minute_before_bell(self):
        assert market_hours.is_open("stocks", utc(2026, 7, 1, 13, 29)) is False

    def test_closed_at_close(self):
        # 16:00 ET exactly -> closed (half-open interval)
        assert market_hours.is_open("stocks", utc(2026, 7, 1, 20, 0)) is False

    def test_open_one_minute_before_close(self):
        assert market_hours.is_open("stocks", utc(2026, 7, 1, 19, 59)) is True

    def test_closed_saturday(self):
        assert market_hours.is_open("stocks", utc(2026, 7, 4, 15, 0)) is False

    def test_closed_sunday(self):
        assert market_hours.is_open("stocks", utc(2026, 7, 5, 15, 0)) is False

    def test_winter_session_uses_est(self):
        # 2026-01-14 is a Wednesday; EST = UTC-5, so 09:30 ET = 14:30 UTC.
        assert market_hours.is_open("stocks", utc(2026, 1, 14, 14, 30)) is True
        assert market_hours.is_open("stocks", utc(2026, 1, 14, 14, 29)) is False


class TestIsOpenCommodities:
    # Globex break is 17:00-18:00 ET. In July (EDT), that is 21:00-22:00 UTC.
    def test_open_weekday_morning(self):
        assert market_hours.is_open("commodities", utc(2026, 7, 1, 14, 0)) is True

    def test_closed_during_daily_break(self):
        assert market_hours.is_open("commodities", utc(2026, 7, 1, 21, 30)) is False

    def test_reopens_after_break(self):
        assert market_hours.is_open("commodities", utc(2026, 7, 1, 22, 0)) is True

    def test_break_start_boundary_closed(self):
        assert market_hours.is_open("commodities", utc(2026, 7, 1, 21, 0)) is False

    def test_friday_closes_at_break_start(self):
        # 2026-07-03 is a Friday: open before 17:00 ET, closed after.
        assert market_hours.is_open("commodities", utc(2026, 7, 3, 20, 59)) is True
        assert market_hours.is_open("commodities", utc(2026, 7, 3, 21, 0)) is False

    def test_saturday_closed_all_day(self):
        assert market_hours.is_open("commodities", utc(2026, 7, 4, 15, 0)) is False

    def test_sunday_opens_at_evening_restart(self):
        # 2026-07-05 is a Sunday: opens 18:00 ET = 22:00 UTC.
        assert market_hours.is_open("commodities", utc(2026, 7, 5, 21, 59)) is False
        assert market_hours.is_open("commodities", utc(2026, 7, 5, 22, 0)) is True
