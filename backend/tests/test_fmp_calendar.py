"""Characterization tests for ``backend.utils.data_fetching.fmp_calendar_fetch``.

``_normalize_event`` writes straight into CHECK-constrained Postgres columns,
so its filter/normalize rules are pinned before the refactor.
"""

from datetime import datetime, timezone

from backend.utils.data_fetching import fmp_calendar_fetch as fc


def raw_event(**overrides):
    base = {
        "date": "2026-08-01 12:30:00",
        "country": "US",
        "event": "CPI (YoY)",
        "impact": "High",
        "currency": "USD",
        "previous": "2.5",
        "estimate": "2.4",
        "actual": None,
    }
    base.update(overrides)
    return base


class TestToFloatOrNone:
    def test_none(self):
        assert fc._to_float_or_none(None) is None

    def test_empty_string(self):
        assert fc._to_float_or_none("") is None

    def test_numeric_string(self):
        assert fc._to_float_or_none("1.5") == 1.5

    def test_int(self):
        assert fc._to_float_or_none(3) == 3.0

    def test_garbage(self):
        assert fc._to_float_or_none("n/a") is None

    def test_percent_string_not_handled_here(self):
        # Unlike fmp_prices_fetch's version, this one does NOT strip '%'.
        assert fc._to_float_or_none("1.5%") is None


class TestParseFmpDatetime:
    def test_space_separated(self):
        got = fc._parse_fmp_datetime("2026-08-01 12:30:00")
        assert got == datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc)

    def test_t_separated(self):
        got = fc._parse_fmp_datetime("2026-08-01T12:30:00")
        assert got == datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc)

    def test_date_only(self):
        got = fc._parse_fmp_datetime("2026-08-01")
        assert got == datetime(2026, 8, 1, tzinfo=timezone.utc)

    def test_iso_with_z_fallback(self):
        got = fc._parse_fmp_datetime("2026-08-01T12:30:00.500Z")
        assert got == datetime(2026, 8, 1, 12, 30, 0, 500000, tzinfo=timezone.utc)

    def test_garbage(self):
        assert fc._parse_fmp_datetime("soon") is None

    def test_non_string(self):
        assert fc._parse_fmp_datetime(12345) is None

    def test_empty(self):
        assert fc._parse_fmp_datetime("  ") is None


class TestNormalizeEvent:
    def test_valid_high_impact_us_event(self):
        got = fc._normalize_event(raw_event())
        assert got == {
            "event_time": datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc),
            "country_code": "US",
            "country_name": "United States",
            "event": "CPI (YoY)",
            "importance": "h",
            "currency": "USD",
            "previous": 2.5,
            "estimate": 2.4,
            "actual": None,
        }

    def test_medium_impact_kept_as_m(self):
        assert fc._normalize_event(raw_event(impact="Medium"))["importance"] == "m"

    def test_low_impact_dropped(self):
        assert fc._normalize_event(raw_event(impact="Low")) is None

    def test_impact_case_normalized(self):
        assert fc._normalize_event(raw_event(impact="HIGH"))["importance"] == "h"

    def test_unlisted_country_dropped(self):
        assert fc._normalize_event(raw_event(country="AR")) is None

    def test_country_code_case_normalized(self):
        assert fc._normalize_event(raw_event(country="us"))["country_code"] == "US"

    def test_unparseable_date_dropped(self):
        assert fc._normalize_event(raw_event(date="whenever")) is None

    def test_empty_event_name_dropped(self):
        assert fc._normalize_event(raw_event(event="  ")) is None

    def test_empty_currency_becomes_none(self):
        assert fc._normalize_event(raw_event(currency=""))["currency"] is None

    def test_non_dict_dropped(self):
        assert fc._normalize_event("not a dict") is None
