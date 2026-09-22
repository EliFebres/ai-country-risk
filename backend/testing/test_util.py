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

# The one line that moves when the ETL body moves out of main.py.
from backend import main as etl

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


@pytest.mark.parametrize(
    "case", FIXTURES["score_article_relevance"], ids=lambda c: c["id"]
)
def test_score_article_relevance(case):
    got = etl._score_article_relevance(ARTICLES_BY_ID[case["id"]], COUNTRY)
    assert got == case["output"], f"{case['id']}: {got!r} != {case['output']!r}"


@pytest.mark.parametrize(
    "case", FIXTURES["score_article_relevance_other_country"], ids=lambda c: c["id"]
)
def test_score_article_relevance_country_not_mentioned(case):
    """A country that appears in none of the text collapses every score to the floor."""
    got = etl._score_article_relevance(ARTICLES_BY_ID[case["id"]], "Nowhereland")
    assert got == case["output"]


def _scored_by_id(date_mode: str) -> dict:
    """Rebuild the ranking input exactly as the baseline generator did."""
    out = {}
    for a in FIXTURES["articles"]:
        item = dict(a)
        item["relevance_score"] = etl._score_article_relevance(a, COUNTRY)
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
