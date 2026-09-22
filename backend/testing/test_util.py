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


# `_score_article_relevance` was deleted when the relevance gate replaced it.
# Its own characterization tests went with it — the fixture's recorded outputs
# are kept below only because `_rank_ids_by` used the score as a tiebreak, and
# that function's behaviour is still pinned.

_RECORDED_RELEVANCE = {
    c["id"]: c["output"] for c in FIXTURES["score_article_relevance"]
}


def _scored_by_id(date_mode: str) -> dict:
    """Rebuild the ranking input exactly as the baseline generator did.

    The scores are read from the fixture rather than recomputed, because the
    function that computed them is gone. The recorded numbers are what shipped,
    which is the only thing `_rank_ids_by`'s characterization needs.
    """
    out = {}
    for a in FIXTURES["articles"]:
        item = dict(a)
        item["relevance_score"] = _RECORDED_RELEVANCE[a["id"]]
        if date_mode == "naive":
            p = item.get("published")
            if isinstance(p, str) and "T" in p:
                item["published"] = p[:10]
        out[item["id"]] = item
    return out


@pytest.mark.parametrize("mode", ["aware", "naive"])
def test_rank_ids_by(mode):
    case = FIXTURES[f"rank_ids_by_all_{mode}"]
    got = etl._rank_ids_by(case["ids"], _scored_by_id(mode), dict(case["impact"]))
    assert got == case["output"]


def test_rank_ids_by_empty():
    assert etl._rank_ids_by([], {}, {}) == FIXTURES["rank_ids_by_empty"]["output"]


def test_rank_ids_by_mixed_tz_raises():
    """A pinned bug, recorded rather than fixed.

    Two articles with equal impact and differing tz-awareness reach the datetime
    comparison and blow up. Equal impacts are routine because the caller fills
    missing scores with ``imp_map.setdefault(aid, 0.0)``. Fixing this changes
    behaviour, so it belongs to a later session; this test exists so the fix is
    a deliberate, visible act rather than an accident.
    """
    case = FIXTURES["rank_ids_by_mixed_tz"]
    assert case["raises"] == "TypeError", "fixture no longer records the bug"
    with pytest.raises(TypeError) as excinfo:
        etl._rank_ids_by(case["ids"], _scored_by_id("aware"), dict(case["impact"]))
    assert str(excinfo.value) == case["message"]


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
