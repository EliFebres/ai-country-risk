"""Tests for the historical harvesters.

Each adapter has exactly one job: turn its own upstream's payload into the
canonical article item every other path already consumes. So each gets a raw
payload fixture — the shape the API actually returns, not a tidied version — and
the test asserts what comes out the other side.

The second job of this file is to keep the adapters honest about reuse.
``TestNoAdapterForksTheCore`` asserts by import that no adapter carries its own
ranking, selection or dedupe, and greps the sources for the copied names. An
adapter that reimplements ``select_with_theme_floor`` would produce a corpus
that looks right and is quietly retrieved by different rules than the live run.

No network. Every test here runs against a fixture.
"""

import datetime
import inspect
import re

import pytest

from backend.utils.history import config
from backend.utils.history.adapters import guardian
from backend.utils.news_fetching import core

# One result exactly as the Content API returns it, fields and all.
GUARDIAN_RESULT = {
    "id": "world/2018/mar/14/turkey-lira-central-bank",
    "type": "article",
    "sectionId": "world",
    "sectionName": "World news",
    "webPublicationDate": "2018-03-14T09:30:00Z",
    "webTitle": "Turkish central bank holds rates as the lira slides",
    "webUrl": "https://www.theguardian.com/world/2018/mar/14/turkey-lira-central-bank",
    "apiUrl": "https://content.guardianapis.com/world/2018/mar/14/turkey-lira-central-bank",
    "fields": {"bodyText": "The central bank left interest rates unchanged on Wednesday."},
    "isHosted": False,
    "pillarName": "News",
}


class TestGuardianPayload:
    def test_a_result_becomes_a_valid_item(self):
        item = guardian.to_item(GUARDIAN_RESULT, "order")
        assert core.validate_item(item)

    def test_the_fields_land_where_they_belong(self):
        item = guardian.to_item(GUARDIAN_RESULT, "order")
        assert item["title"].startswith("Turkish central bank")
        assert item["link"] == GUARDIAN_RESULT["webUrl"]
        assert item["published"] == "2018-03-14T09:30:00Z"
        assert item["source"] == "The Guardian"
        assert item["_theme"] == "order"
        assert item["text"].startswith("The central bank left")

    def test_publisher_link_defaults_to_the_only_url(self):
        # Which is what makes core.dedupe_key collapse a Guardian row and a
        # GDELT stub for the same story.
        assert core.dedupe_key(guardian.to_item(GUARDIAN_RESULT, "order")) \
            == GUARDIAN_RESULT["webUrl"]

    def test_the_body_is_capped_like_a_live_one(self):
        big = {**GUARDIAN_RESULT, "fields": {"bodyText": "x" * (core.MAX_BODY_CHARS + 5000)}}
        assert len(guardian.to_item(big, "order")["text"]) == core.MAX_BODY_CHARS

    def test_a_result_with_no_body_still_becomes_an_item(self):
        # Paywalled and withdrawn pieces come back without fields. They go in as
        # stubs for Wayback rather than being silently dropped.
        item = guardian.to_item({**GUARDIAN_RESULT, "fields": {}}, "order")
        assert item is not None and item["text"] == ""

    def test_a_result_with_no_url_is_dropped(self):
        assert guardian.to_item({**GUARDIAN_RESULT, "webUrl": ""}, "order") is None

    def test_a_result_with_no_date_is_dropped(self):
        assert guardian.to_item({**GUARDIAN_RESULT, "webPublicationDate": None}, "order") is None


class TestGuardianQuery:
    def test_the_implicit_and_is_spelled_out(self):
        q = guardian.guardian_query("friction", "Portugal")
        assert q.startswith('"Portugal" AND (')
        assert "taxation OR customs" in q

    def test_the_broad_query_needs_no_translation(self):
        assert guardian.guardian_query("broad", "Portugal") == '"Portugal"'

    def test_every_live_theme_translates(self):
        for theme in core.THEME_QUERIES:
            assert guardian.guardian_query(theme, "Brazil").startswith('"Brazil"')

    def test_the_terms_come_from_the_live_queries(self):
        # Not retyped. A term added to live retrieval has to reach historical
        # retrieval, or the two corpora stop being comparable.
        assert "brain drain" not in guardian.guardian_query("edge", "Brazil")
        assert "skilled workers leaving" in guardian.guardian_query("edge", "Brazil")


class TestGuardianWindows:
    def test_year_windows_are_calendar_years(self):
        got = guardian.year_windows(datetime.date(2016, 8, 3), datetime.date(2018, 4, 1))
        assert got[0] == (datetime.date(2016, 8, 3), datetime.date(2016, 12, 31))
        assert got[1] == (datetime.date(2017, 1, 1), datetime.date(2017, 12, 31))
        assert got[-1] == (datetime.date(2018, 1, 1), datetime.date(2018, 4, 1))

    def test_windows_are_stable_across_runs(self):
        # The checkpoints key on window_start, so a resumed harvest that asked
        # for different windows would skip the wrong things.
        args = (datetime.date(2016, 8, 3), datetime.date(2018, 4, 1))
        assert guardian.year_windows(*args) == guardian.year_windows(*args)

    def test_a_year_splits_into_quarters(self):
        got = guardian.subdivide(datetime.date(2016, 1, 1), datetime.date(2016, 12, 31))
        assert len(got) == 4 and got[0][1] == datetime.date(2016, 3, 31)

    def test_a_quarter_splits_into_months(self):
        got = guardian.subdivide(datetime.date(2016, 1, 1), datetime.date(2016, 3, 31))
        assert len(got) == 3 and got[1] == (datetime.date(2016, 2, 1), datetime.date(2016, 2, 29))

    def test_a_month_cannot_split_further(self):
        assert guardian.subdivide(datetime.date(2016, 1, 1), datetime.date(2016, 1, 31)) == []

    def test_subdivision_covers_the_parent_exactly(self):
        # A gap here is a hole in the corpus that nothing would report.
        start, end = datetime.date(2017, 1, 1), datetime.date(2017, 12, 31)
        children = guardian.subdivide(start, end)
        assert children[0][0] == start and children[-1][1] == end
        for (_, a), (b, _) in zip(children, children[1:]):
            assert b == a + datetime.timedelta(days=1)


class TestNoAdapterForksTheCore:
    """The reuse rule, checked two ways.

    An adapter that grew its own ranking or selection would produce a corpus
    retrieved by different rules than the live run — which is exactly the drift
    the shared core exists to prevent, and exactly the kind that looks fine in
    every count."""

    MODULES = (guardian,)
    FORBIDDEN = ("_HIGH_KEYWORDS", "score_relevance", "select_with_theme_floor",
                 "_select_with_theme_floor", "headline_key", "_by_relevance")

    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
    def test_no_adapter_defines_a_shared_name(self, module):
        for name in self.FORBIDDEN:
            assert name not in vars(module), f"{module.__name__} defines {name}"

    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
    def test_no_adapter_reimplements_a_shared_function(self, module):
        source = inspect.getsource(module)
        for name in self.FORBIDDEN:
            assert not re.search(rf"^\s*(def|{re.escape(name)}\s*=)\s*{re.escape(name)}\b",
                                 source, re.M), f"{module.__name__} redefines {name}"

    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
    def test_no_adapter_carries_its_own_theme_queries(self, module):
        source = inspect.getsource(module)
        assert "THEME_QUERIES: dict" not in source
        assert "core.THEME_QUERIES" in source

    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
    def test_no_adapter_extracts_bodies_itself(self, module):
        assert "trafilatura" not in inspect.getsource(module)

    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
    def test_no_adapter_opens_its_own_transaction(self, module):
        # Every write goes through store.upsert_articles, which is where the
        # body-wins rule lives.
        assert "_transaction" not in inspect.getsource(module)


class TestPilotConfig:
    def test_every_pilot_country_is_in_the_live_roster(self):
        # A typo here would produce an empty query and a country-shaped hole.
        for iso2 in config.PILOT_ROSTER:
            assert config.country_name(iso2)

    def test_the_mandated_country_is_present(self):
        assert "US" in config.PILOT_ROSTER

    def test_an_unknown_code_fails_loudly(self):
        with pytest.raises(KeyError):
            config.country_name("ZZ")

    def test_gdelt_starts_no_earlier_than_its_own_floor(self):
        assert config.GDELT_START >= "2017-01-01"
