"""Characterization of the ETL's pure functions.

These are the functions that take inputs and return outputs with no network, no
database and no model call: date normalisation, article relevance scoring, and
the Top-3 ranking. ``characterization.json`` holds outputs recorded from
``backend/main.py`` *before* the v2.0 folder refactor. Replaying the same inputs
must produce byte-identical results afterwards. That is the refactor's
acceptance test.

If one of these fails, the refactor changed behaviour. Fix the code, not the
fixture - the fixture is the record of what shipped.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

# The ETL body moved out of main.py; the fixtures were recorded before it did.
from backend.util import pipeline as etl

FIXTURES = json.loads(
    (Path(__file__).with_name("characterization.json")).read_text(encoding="utf-8")
)
COUNTRY = FIXTURES["country"]
ARTICLES_BY_ID = {a["id"]: a for a in FIXTURES["articles"]}


def _dt_repr(d: datetime) -> dict:
    """Serialise a datetime the way the baseline recorded it.

    The tzinfo field is not decoration: ``_parse_date_for_sort`` returns
    tz-aware for ISO-with-offset input and tz-naive otherwise, and that split is
    load-bearing (see test_rank_ids_by_mixed_tz_raises).
    """
    return {"iso": d.isoformat(), "tzinfo": (str(d.tzinfo) if d.tzinfo else None)}


def _rebuild_datetime(rec: dict) -> datetime:
    return datetime.fromisoformat(rec["iso"])


@pytest.mark.parametrize("case", FIXTURES["to_utc_iso"], ids=lambda c: c["input"]["iso"])
def test_to_utc_iso(case):
    assert etl._to_utc_iso(_rebuild_datetime(case["input"])) == case["output"]


@pytest.mark.parametrize(
    "case", FIXTURES["parse_date_for_sort"], ids=lambda c: repr(c["input"])
)
def test_parse_date_for_sort(case):
    """Both the value and the tz-awareness must match."""
    assert _dt_repr(etl._parse_date_for_sort(case["input"])) == case["output"]


# `_score_article_relevance` and `_rank_ids_by` were both deleted: the relevance
# gate replaced the first, and the Top-3 is now the first three of the gate's own
# selection order rather than a re-rank of the model's impact scores. Their
# characterization tests went with them, and with `_rank_ids_by` went the pinned
# mixed-timezone crash it raised on equal impacts — deleted rather than fixed,
# which was the outcome `docs/deferred.md` anticipated.
#
# `_parse_date_for_sort` and `_to_utc_iso` survive and are still pinned above.

# --- the supervisor's ETL due-check -------------------------------------------
#
# Consumer-side tests: they assert that the stored snapshot date is *read* and
# acted on, not that anything was written. The decision is the whole point of
# the guard, so the decision is what is pinned.

from datetime import date, timedelta  # noqa: E402


def _supervisor(monkeypatch, stored):
    """A Supervisor whose database returns `stored` (a date, None, or an Exception)."""
    from backend.data_upsert import data_push
    from backend.util import supervisor as sup

    def fake_read():
        if isinstance(stored, Exception):
            raise stored
        return stored

    monkeypatch.setattr(data_push, "read_latest_snapshot_date", fake_read)
    return sup.Supervisor(), sup


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def test_etl_is_due_when_the_database_is_empty(monkeypatch):
    """A fresh deploy against an empty database runs immediately."""
    s, _ = _supervisor(monkeypatch, None)
    assert s.etl_is_due(NOW) is True


@pytest.mark.parametrize(
    "age_days, expected",
    [
        (0, False),   # ran today
        (1, False),
        (7, False),   # exactly at the threshold is not yet stale
        (8, True),    # a missed week
        (40, True),   # the machine was off for a month
    ],
)
def test_etl_due_follows_the_age_of_the_stored_ratings(monkeypatch, age_days, expected):
    s, _ = _supervisor(monkeypatch, NOW.date() - timedelta(days=age_days))
    assert s.etl_is_due(NOW) is expected


def test_a_restart_changes_nothing(monkeypatch):
    """There is no in-memory schedule to lose, so a new process decides the same."""
    stored = NOW.date() - timedelta(days=2)
    first, _ = _supervisor(monkeypatch, stored)
    second, _ = _supervisor(monkeypatch, stored)
    assert first.etl_is_due(NOW) == second.etl_is_due(NOW) is False


def test_a_database_error_skips_the_tick_rather_than_running_the_etl(monkeypatch):
    """Not knowing the age is not a reason to spend money."""
    s, _ = _supervisor(monkeypatch, RuntimeError("connection refused"))
    assert s.etl_is_due(NOW) is False


def test_attempts_are_capped_per_day(monkeypatch):
    """A persistently failing ETL retries, but does not hammer the paid APIs."""
    s, sup = _supervisor(monkeypatch, None)  # always due
    for _ in range(sup.MAX_ETL_ATTEMPTS_PER_DAY):
        assert s.etl_is_due(NOW) is True
        s._record_attempt(NOW.date())
    assert s.etl_is_due(NOW) is False, "cap did not engage"

    tomorrow = NOW + timedelta(days=1)
    assert s.etl_is_due(tomorrow) is True, "cap did not reset the next day"


def test_the_decision_is_logged_either_way(monkeypatch, caplog):
    """A supervisor that silently decides to do nothing looks identical to a stuck one."""
    import logging

    s, _ = _supervisor(monkeypatch, NOW.date() - timedelta(days=2))
    with caplog.at_level(logging.INFO, logger="supervisor"):
        s.etl_is_due(NOW)
    assert "skip" in caplog.text and "threshold" in caplog.text

    caplog.clear()
    s2, _ = _supervisor(monkeypatch, NOW.date() - timedelta(days=30))
    with caplog.at_level(logging.INFO, logger="supervisor"):
        s2.etl_is_due(NOW)
    assert "RUN" in caplog.text and "30d old" in caplog.text


# ---------------------------------------------------------------------------
# Content hashing — the one place a cache key is computed.
# ---------------------------------------------------------------------------


class TestContentHash:
    """Two call sites that normalise differently give one article two hashes, the
    cache misses forever, and the bill looks like a cache that is working."""

    def test_the_same_text_hashes_the_same_way_twice(self):
        from backend.util.hashing import content_hash

        assert content_hash("Portugal raises rates") == content_hash("Portugal raises rates")

    def test_reflowed_whitespace_is_the_same_article(self):
        """A re-scrape that rewraps a paragraph must not pay for a second digest."""
        from backend.util.hashing import content_hash

        assert content_hash("a  b\n\nc\t d ") == content_hash("a b c d")

    def test_a_non_breaking_space_is_the_same_article(self):
        from backend.util.hashing import content_hash

        assert content_hash("EUR 15bn") == content_hash("EUR 15bn")

    def test_case_is_not_collapsed(self):
        """A digest of shouting is not a digest of the same words spoken."""
        from backend.util.hashing import content_hash

        assert content_hash("US SANCTIONS LIFTED") != content_hash("us sanctions lifted")

    def test_added_words_are_a_different_article(self):
        from backend.util.hashing import content_hash

        assert content_hash("a b c") != content_hash("a b c d")

    def test_none_is_refused_rather_than_hashed(self):
        """Hashing the string 'None' would be a cache key that silently collides."""
        from backend.util.hashing import content_hash

        with pytest.raises(TypeError):
            content_hash(None)

    def test_the_normalised_text_is_inspectable(self):
        """A cache key you cannot see the input of is unauditable."""
        from backend.util.hashing import content_hash, normalize

        assert normalize("  a   b  ") == "a b"
        assert content_hash("  a   b  ") == content_hash(normalize("  a   b  "))

    def test_the_digest_is_a_sha256_hex_string(self):
        from backend.util.hashing import content_hash

        h = content_hash("x")
        assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# `as_of` — when a number became knowable.
# ---------------------------------------------------------------------------

import datetime as _dt  # noqa: E402

from backend.util import constants as _constants, trends as _trends, vintage as _vintage  # noqa: E402


class TestPeriodEnd:
    def test_a_year_ends_on_the_last_day_of_it(self):
        assert _vintage.period_end(2024) == _dt.date(2024, 12, 31)
        assert _vintage.period_end("2024") == _dt.date(2024, 12, 31)

    def test_a_quarter_ends_at_its_quarter(self):
        assert _vintage.period_end("2024-05-15", "Q") == _dt.date(2024, 6, 30)

    def test_a_month_ends_at_its_month_including_february(self):
        assert _vintage.period_end("2024-02-03", "M") == _dt.date(2024, 2, 29)

    def test_an_unreadable_period_raises_rather_than_guessing(self):
        with pytest.raises(ValueError):
            _vintage.period_end("sometime last year")


class TestAsOf:
    def test_a_source_that_publishes_a_date_is_believed(self):
        as_of, scheme = _vintage.as_of_for(
            "OECD PISA", 2022, published="2023-12-05"
        )
        assert as_of == _dt.date(2023, 12, 5)
        assert scheme == _vintage.SOURCE_PUBLISHED

    def test_a_source_that_publishes_no_date_gets_its_declared_lag(self):
        as_of, scheme = _vintage.as_of_for("World Bank WDI", 2024)
        assert scheme == _vintage.LAG_ESTIMATE
        assert as_of > _dt.date(2024, 12, 31)

    def test_the_scheme_travels_with_the_date(self):
        """A measured date and a derived one must be distinguishable downstream."""
        _, a = _vintage.as_of_for("World Bank WDI", 2024)
        _, b = _vintage.as_of_for("World Bank WDI", 2024, published="2026-06-01")
        assert a != b

    def test_an_undeclared_source_raises_rather_than_assuming_no_lag(self):
        """Defaulting to zero lag is the fetch-clock bug wearing a different hat."""
        with pytest.raises(KeyError, match="no publication lag declared"):
            _vintage.as_of_for("Some Ministry Nobody Dated", 2024)

    def test_every_registry_source_can_be_dated(self):
        """A registry entry whose source has no lag is an indicator that cannot
        reach the payload at all."""
        for code, spec in _constants.INDICATOR_REGISTRY.items():
            as_of, scheme = _vintage.as_of_for(spec["source"], 2024, freq=spec["freq"])
            assert scheme == _vintage.LAG_ESTIMATE, code
            assert isinstance(as_of, _dt.date)


class TestAsOfInvariant:
    """The test that would have caught the fetch-clock bug on day one."""

    def test_as_of_never_precedes_the_period_it_describes(self):
        assert not _vintage.is_plausible(
            _dt.date(2024, 6, 1), "World Bank WDI", 2024
        )

    def test_as_of_never_exceeds_period_end_plus_the_declared_lag(self):
        """A 2019 figure stamped with today's date — which is exactly what the
        fetch clock produced — is not a plausible publication date."""
        assert not _vintage.is_plausible(
            _dt.date(2026, 9, 22), "World Bank WDI", 2019
        )

    def test_a_real_publication_date_passes(self):
        as_of, _ = _vintage.as_of_for("World Bank WDI", 2024)
        assert _vintage.is_plausible(as_of, "World Bank WDI", 2024)

    def test_every_registry_source_produces_a_plausible_date(self):
        for code, spec in _constants.INDICATOR_REGISTRY.items():
            for year in (2019, 2022, 2024):
                as_of, _ = _vintage.as_of_for(spec["source"], year, freq=spec["freq"])
                assert _vintage.is_plausible(as_of, spec["source"], year, spec["freq"]), \
                    f"{code} @ {year}"


# ---------------------------------------------------------------------------
# Trajectory — computed here so the model does not do arithmetic in its head.
# ---------------------------------------------------------------------------


class TestDirection:
    def test_a_clear_rise_is_rising(self):
        assert _trends.direction_of([2.0, 3.0, 5.0, 7.0, 9.0]) == "rising"

    def test_a_clear_fall_is_falling(self):
        assert _trends.direction_of([20.0, 15.0, 10.0, 8.0, 7.0]) == "falling"

    def test_movement_inside_the_band_is_flat(self):
        assert _trends.direction_of([5.0, 5.1, 4.95, 5.05, 5.02]) == "flat"

    def test_two_points_are_a_line_not_a_trend(self):
        assert _trends.direction_of([1.0, 9.0]) == "unknown"

    def test_gaps_do_not_stop_a_direction_being_read(self):
        assert _trends.direction_of([2.0, None, 5.0, None, 9.0]) == "rising"

    def test_nothing_observed_is_unknown_not_flat(self):
        """`flat` is a claim about the country; `unknown` is a claim about us."""
        assert _trends.direction_of([None, None, None, None, None]) == "unknown"


class TestAnnualHistory:
    def test_a_missing_year_is_present_and_empty_rather_than_absent(self):
        """A filled gap is a number the model treats as measured, and it never
        was; a shortened window hides that the gap existed."""
        got = _trends.describe({2022: 1.0, 2024: 3.0}, latest_year=2024)
        years = [h["year"] for h in got["history"]]
        assert years == [2020, 2021, 2022, 2023, 2024]
        assert got["history"][3]["value"] == "unknown"
        assert got["missing"] == 3

    def test_nothing_is_interpolated(self):
        got = _trends.describe({2020: 0.0, 2024: 100.0}, latest_year=2024)
        values = [h["value"] for h in got["history"]]
        assert values == [0.0, "unknown", "unknown", "unknown", 100.0]

    def test_acceleration_needs_enough_points_to_claim(self):
        assert _trends.describe({2023: 1.0, 2024: 2.0}, latest_year=2024)["accelerating"] is None

    def test_a_steepening_rise_is_accelerating(self):
        got = _trends.describe(
            {2020: 1.0, 2021: 2.0, 2022: 3.0, 2023: 8.0, 2024: 20.0}, latest_year=2024
        )
        assert got["direction"] == "rising"
        assert got["accelerating"] is True
