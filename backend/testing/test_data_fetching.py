"""The prices loop's daily refreshes.

Consumer-side tests: each one asserts that the *stored* date is read and acted
on. None of them assert that a write happened - the point of the change these
cover is that the database, not a process-local flag, decides whether the day's
work is already done, and the only way to show that is to vary what the database
says and watch the decision follow.

No network, no database: the readers and the FMP fetchers are stubbed.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.data_fetching import fmp_prices_fetch, prices_daemon
from backend.data_upsert import data_push

# 12:00 UTC is 08:00 ET, so the UTC and ET calendar days agree here and the
# fixtures stay readable.
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
TODAY_ET = prices_daemon._today_et(NOW)


@pytest.fixture
def daemon(monkeypatch):
    """A daemon whose every outbound call is recorded instead of made."""
    calls = {"reference_fetch": 0, "yield_fetch": 0, "upserts": 0}

    monkeypatch.setattr(
        fmp_prices_fetch, "fetch_reference_closes",
        lambda symbols, now_utc=None: calls.__setitem__("reference_fetch", calls["reference_fetch"] + 1) or {},
    )
    monkeypatch.setattr(
        fmp_prices_fetch, "fetch_treasury_yields",
        lambda assets, now_utc=None: calls.__setitem__("yield_fetch", calls["yield_fetch"] + 1) or {},
    )
    monkeypatch.setattr(
        data_push, "upsert_price_references",
        lambda refs, day: calls.__setitem__("upserts", calls["upserts"] + 1),
    )
    monkeypatch.setattr(
        data_push, "upsert_market_prices",
        lambda rows: calls.__setitem__("upserts", calls["upserts"] + 1),
    )

    d = prices_daemon.PricesDaemon()
    d.calls = calls
    return d


def _stored_reference_date(monkeypatch, value):
    monkeypatch.setattr(data_push, "read_reference_refreshed_on", lambda: value)


def _stored_yield_timestamp(monkeypatch, value):
    monkeypatch.setattr(data_push, "read_yields_updated_at", lambda: value)


# --- references ---------------------------------------------------------------

def test_references_skip_when_the_database_says_today(daemon, monkeypatch):
    _stored_reference_date(monkeypatch, TODAY_ET)
    daemon.maybe_refresh_references(NOW)
    assert daemon.calls["reference_fetch"] == 0


def test_references_refresh_when_the_stored_date_is_yesterday(daemon, monkeypatch):
    _stored_reference_date(monkeypatch, TODAY_ET - timedelta(days=1))
    daemon.maybe_refresh_references(NOW)
    assert daemon.calls["reference_fetch"] == 1


def test_references_refresh_when_nothing_is_stored(daemon, monkeypatch):
    _stored_reference_date(monkeypatch, None)
    daemon.maybe_refresh_references(NOW)
    assert daemon.calls["reference_fetch"] == 1


def test_a_restart_does_not_repeat_the_days_reference_work(monkeypatch):
    """The property the process-local flag could not provide.

    A fresh daemon - as after a restart - must reach the same conclusion, because
    the conclusion lives in the database rather than in the object.
    """
    _stored_reference_date(monkeypatch, TODAY_ET)
    monkeypatch.setattr(data_push, "read_price_references", lambda: {})
    fetched = []
    monkeypatch.setattr(fmp_prices_fetch, "fetch_reference_closes",
                        lambda symbols, now_utc=None: fetched.append(1) or {})

    restarted = prices_daemon.PricesDaemon()
    restarted.load_state()
    restarted.maybe_refresh_references(NOW)
    assert fetched == [], "a restart refetched work the database already had"


def test_a_partially_written_reference_table_does_not_hide_the_day(daemon, monkeypatch):
    """MAX(), not 'every row agrees'.

    The old rule read the day only when every stored row shared one date, so a
    single half-written row sent it back for a full refetch.
    """
    _stored_reference_date(monkeypatch, TODAY_ET)  # MAX() over a mixed table
    daemon.maybe_refresh_references(NOW)
    assert daemon.calls["reference_fetch"] == 0


def test_reference_refresh_retries_but_is_capped(daemon, monkeypatch):
    """A failed fetch leaves the stored date stale, so the next tick retries.

    That is correct and is the reason the cap exists: without it a down upstream
    would be hit on every poll interval, forever.
    """
    _stored_reference_date(monkeypatch, TODAY_ET - timedelta(days=1))
    for _ in range(prices_daemon.MAX_REFRESH_ATTEMPTS_PER_DAY):
        daemon.maybe_refresh_references(NOW)
    assert daemon.calls["reference_fetch"] == prices_daemon.MAX_REFRESH_ATTEMPTS_PER_DAY

    daemon.maybe_refresh_references(NOW)
    assert daemon.calls["reference_fetch"] == prices_daemon.MAX_REFRESH_ATTEMPTS_PER_DAY, "cap did not engage"

    daemon.maybe_refresh_references(NOW + timedelta(days=1))
    assert daemon.calls["reference_fetch"] == prices_daemon.MAX_REFRESH_ATTEMPTS_PER_DAY + 1, "cap did not reset"


def test_an_unreadable_database_does_not_trigger_a_refresh(daemon, monkeypatch):
    """Not knowing is not a reason to spend a request."""
    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(data_push, "read_reference_refreshed_on", boom)
    daemon.maybe_refresh_references(NOW)
    assert daemon.calls["reference_fetch"] == 0


# --- yields -------------------------------------------------------------------

def test_yields_skip_when_the_stored_row_is_from_today(daemon, monkeypatch):
    _stored_yield_timestamp(monkeypatch, NOW)
    daemon.maybe_refresh_yields(NOW)
    assert daemon.calls["yield_fetch"] == 0


def test_yields_refresh_when_the_stored_row_is_from_yesterday(daemon, monkeypatch):
    _stored_yield_timestamp(monkeypatch, NOW - timedelta(days=1))
    daemon.maybe_refresh_yields(NOW)
    assert daemon.calls["yield_fetch"] == 1


def test_yields_refresh_when_there_are_no_rows(daemon, monkeypatch):
    _stored_yield_timestamp(monkeypatch, None)
    daemon.maybe_refresh_yields(NOW)
    assert daemon.calls["yield_fetch"] == 1


def test_a_restart_no_longer_refetches_yields(monkeypatch):
    """The clearest regression this fixes.

    yields_day was never hydrated from anywhere, so every restart spent a
    treasury-rates call regardless of what was already stored.
    """
    _stored_yield_timestamp(monkeypatch, NOW)
    monkeypatch.setattr(data_push, "read_price_references", lambda: {})
    fetched = []
    monkeypatch.setattr(fmp_prices_fetch, "fetch_treasury_yields",
                        lambda assets, now_utc=None: fetched.append(1) or {})

    restarted = prices_daemon.PricesDaemon()
    restarted.load_state()
    restarted.maybe_refresh_yields(NOW)
    assert fetched == [], "a restart refetched yields the database already had"


# ---------------------------------------------------------------------------
# The curated loader: the two series no API serves.
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from backend.data_fetching import curated_loader  # noqa: E402

_HEADER = ("country_iso2,indicator_code,period,value,as_of,"
           "source_url,source_table,retrieved_at\n")


def _csv(tmp_path, body):
    p = tmp_path / "curated.csv"
    p.write_text(_HEADER + body, encoding="utf-8")
    return p


class TestCuratedLoader:
    def test_an_absent_file_is_silent(self, tmp_path):
        """A country with no curated rows still scores; the census says which
        indicators were missing."""
        assert curated_loader.load_curated(tmp_path / "nothing.csv") == {}

    def test_a_header_only_file_loads_nothing(self, tmp_path):
        assert curated_loader.load_curated(_csv(tmp_path, "")) == {}

    def test_a_good_row_loads_with_its_citation(self, tmp_path):
        path = _csv(tmp_path, "PT,RSF.PRESS.SCORE,2026,75.4,2026-05-03,"
                              "https://rsf.org/en/index,Index table,2026-09-22\n")
        got = curated_loader.load_curated(path)
        assert got["PT"]["RSF.PRESS.SCORE"]["value"] == 75.4
        assert got["PT"]["RSF.PRESS.SCORE"]["source_url"] == "https://rsf.org/en/index"
        assert got["PT"]["RSF.PRESS.SCORE"]["retrieved_at"] == "2026-09-22"

    def test_a_malformed_value_raises_rather_than_being_skipped(self, tmp_path):
        """A silently skipped row is how a file that loads 'successfully' ends
        up holding half the data someone thought they put in it."""
        path = _csv(tmp_path, "PT,RSF.PRESS.SCORE,2026,not-a-number,2026-05-03,"
                              "https://rsf.org,T,2026-09-22\n")
        with pytest.raises(curated_loader.CuratedFileError, match="not a number"):
            curated_loader.load_curated(path)

    def test_a_row_with_no_citation_raises(self, tmp_path):
        path = _csv(tmp_path, "PT,RSF.PRESS.SCORE,2026,75.4,2026-05-03,,,2026-09-22\n")
        with pytest.raises(curated_loader.CuratedFileError, match="no source_url"):
            curated_loader.load_curated(path)

    def test_an_unknown_indicator_code_raises(self, tmp_path):
        path = _csv(tmp_path, "PT,MADE.UP.CODE,2026,1.0,2026-05-03,"
                              "https://x,T,2026-09-22\n")
        with pytest.raises(curated_loader.CuratedFileError, match="not in INDICATOR_REGISTRY"):
            curated_loader.load_curated(path)

    def test_a_curated_row_cannot_shadow_a_fetched_indicator(self, tmp_path):
        """Otherwise a hand-typed number quietly overrides the World Bank."""
        path = _csv(tmp_path, "PT,FP.CPI.TOTL.ZG,2024,3.0,2025-06-01,"
                              "https://x,T,2026-09-22\n")
        with pytest.raises(curated_loader.CuratedFileError, match="not curated"):
            curated_loader.load_curated(path)

    def test_several_years_become_a_series_with_the_freshest_on_top(self, tmp_path):
        path = _csv(
            tmp_path,
            "PT,RSF.PRESS.SCORE,2024,80.0,2024-05-03,https://rsf.org,T,2026-09-22\n"
            "PT,RSF.PRESS.SCORE,2026,75.4,2026-05-03,https://rsf.org,T,2026-09-22\n"
            "PT,RSF.PRESS.SCORE,2025,78.0,2025-05-03,https://rsf.org,T,2026-09-22\n",
        )
        got = curated_loader.load_curated(path)["PT"]["RSF.PRESS.SCORE"]
        assert got["period"] == 2026
        assert got["value"] == 75.4
        assert got["series"] == {2024: 80.0, 2025: 78.0, 2026: 75.4}

    def test_a_country_with_no_rows_gets_an_empty_dict(self, tmp_path):
        path = _csv(tmp_path, "PT,RSF.PRESS.SCORE,2026,75.4,2026-05-03,"
                              "https://rsf.org,T,2026-09-22\n")
        assert curated_loader.load_for_country("DE", path) == {}
