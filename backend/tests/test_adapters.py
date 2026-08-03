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
from backend.utils.history.adapters import gdelt, guardian, nyt
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


# One record exactly as the DOC 2.0 artlist mode returns it.
GDELT_RECORD = {
    "url": "https://www.reuters.com/article/turkey-lira-idUSKCN1GQ0X1",
    "url_mobile": "",
    "title": "Turkish lira slides to a record low against the dollar",
    "seendate": "20180314T093000Z",
    "socialimage": "https://s.reutersmedia.net/x.jpg",
    "domain": "reuters.com",
    "language": "English",
    "sourcecountry": "Turkey",
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


class TestGdeltPayload:
    def test_a_record_becomes_a_valid_item(self):
        assert core.validate_item(gdelt.to_item(GDELT_RECORD, "order"))

    def test_the_compact_stamp_becomes_iso(self):
        assert gdelt.to_item(GDELT_RECORD, "order")["published"] == "2018-03-14T09:30:00Z"

    def test_a_record_carries_no_body(self):
        # The whole reason step 4 exists.
        assert gdelt.to_item(GDELT_RECORD, "order")["text"] == ""

    def test_the_domain_becomes_the_source(self):
        assert gdelt.to_item(GDELT_RECORD, "order")["source"] == "reuters.com"

    def test_a_record_with_no_url_is_dropped(self):
        assert gdelt.to_item({**GDELT_RECORD, "url": ""}, "order") is None

    def test_a_truncated_stamp_is_dropped(self):
        assert gdelt.to_item({**GDELT_RECORD, "seendate": "2018"}, "order") is None

    def test_the_english_filter_is_on_every_query(self):
        for theme in core.THEME_QUERIES:
            assert "sourcelang:english" in gdelt.gdelt_query(theme, "Brazil")

    def test_month_windows_tile_the_range(self):
        got = gdelt.month_windows(datetime.date(2016, 11, 15), datetime.date(2017, 2, 3))
        assert got[0] == (datetime.date(2016, 11, 15), datetime.date(2016, 11, 30))
        assert got[-1] == (datetime.date(2017, 1, 1), datetime.date(2017, 1, 31)) or \
            got[-1] == (datetime.date(2017, 2, 1), datetime.date(2017, 2, 3))
        for (_, a), (b, _) in zip(got, got[1:]):
            assert b == a + datetime.timedelta(days=1)


class TestGdeltFailureIsolation:
    """GDELT is one flaky free service answering thousands of queries.

    It rate-limits at one request every five seconds and says so in the body of
    the 429 it returns. A harvest that dies on a bad window throws away hours of
    polite waiting, so a failed window is checkpointed as failed — which also
    means the next run retries it, since only 'done' windows are skipped.
    """

    def test_one_bad_window_does_not_end_the_harvest(self, monkeypatch):
        seen, checkpoints = [], []

        def boom(iso2, name, start, end):
            seen.append((iso2, start))
            if len(seen) == 1:
                raise RuntimeError("429 Too Many Requests")
            return 3, 0

        monkeypatch.setattr(gdelt, "harvest_window", boom)
        monkeypatch.setattr(gdelt.store, "completed_windows", lambda *a: set())
        monkeypatch.setattr(gdelt.store, "write_checkpoint",
                            lambda *a, **kw: checkpoints.append(kw.get("status", "done")))

        written = gdelt.harvest(roster=["PT"], since="2017-01-01")

        assert len(seen) > 1, "the harvest stopped at the first failure"
        assert checkpoints[0] == "failed"
        assert written == 3 * (len(seen) - 1)

    def test_gdelt_waits_five_seconds_between_calls(self):
        # Quoted from GDELT's own 429 body. One per second is five times too
        # fast and every retry inside the backoff window 429s as well.
        assert config.GDELT_REQUEST_INTERVAL_SECONDS >= 5.0


class TestBothAdaptersAgree:
    """Two sources, one story: the keys must collide or the store cannot
    de-duplicate them and the body-wins rule never fires."""

    def test_one_url_gives_one_key(self):
        url = "https://www.reuters.com/article/x"
        g = guardian.to_item({**GUARDIAN_RESULT, "webUrl": url}, "order")
        d = gdelt.to_item({**GDELT_RECORD, "url": url}, "security")
        assert core.dedupe_key(g) == core.dedupe_key(d) == url

    def test_both_emit_the_same_shape(self):
        g = guardian.to_item(GUARDIAN_RESULT, "order")
        d = gdelt.to_item(GDELT_RECORD, "order")
        assert set(core._ITEM_KEYS) <= set(g) and set(core._ITEM_KEYS) <= set(d)


class TestNoAdapterForksTheCore:
    """The reuse rule, checked two ways.

    An adapter that grew its own ranking or selection would produce a corpus
    retrieved by different rules than the live run — which is exactly the drift
    the shared core exists to prevent, and exactly the kind that looks fine in
    every count."""

    MODULES = (guardian, gdelt, nyt)
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

    # Adapters that retrieve by theme. NYT is exempt from the second half of
    # the rule below because it has no query at all: the archive endpoint takes
    # a year and a month and returns the whole paper, so its themes come from
    # `store.article_row`'s classifier. It is still forbidden from carrying a
    # theme list of its own.
    THEME_QUERYING = (guardian, gdelt)

    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
    def test_no_adapter_carries_its_own_theme_queries(self, module):
        source = inspect.getsource(module)
        assert "THEME_QUERIES: dict" not in source
        if module in self.THEME_QUERYING:
            assert "core.THEME_QUERIES" in source

    def test_an_adapter_with_no_query_still_gets_themed(self):
        # The exemption above must not become a silently untagged corpus: rows
        # with no `_theme` are classified from their text at the store boundary,
        # which is what fills the same per-theme floor the live run uses.
        from backend.utils.history import store
        assert "core.classify_themes" in inspect.getsource(store.article_row)

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


class TestRetryRespectsAStatedRateLimit:
    """A backoff faster than the API's own floor cannot ever succeed.

    GDELT answers anything quicker than one request per five seconds with a
    429. Tenacity's default first retry is one second, so a single throttle
    became five guaranteed failures and the window was lost — with the harvest
    logging a rate-limit error at a service that was working perfectly.
    """

    def test_gdelt_asks_for_a_backoff_above_its_own_floor(self):
        # Read off the decorator rather than the timing: what matters is that
        # the first retry cannot be quicker than the interval GDELT states.
        source = inspect.getsource(gdelt)
        assert "initial=config.GDELT_REQUEST_INTERVAL_SECONDS + 1" in source

    def test_the_shared_default_is_unchanged_for_every_other_caller(self):
        # World Bank and FMP keep the one-second first retry they were tuned on.
        from backend.utils import http as http_mod
        params = inspect.signature(http_mod.retry_transient).parameters
        assert params["initial"].default == 1
        assert params["max_wait"].default == 30


# One document exactly as the Archive API returns it, fields and all.
NYT_DOC = {
    "web_url": "https://www.nytimes.com/2018/08/13/world/europe/turkey-lira-crisis.html",
    "snippet": "The currency's collapse deepened.",
    "lead_paragraph": "The currency's collapse deepened on Monday as investors fled.",
    "abstract": "The lira fell to a record low, deepening a crisis.",
    "headline": {"main": "Turkey's Currency Crisis Deepens", "kicker": None},
    "keywords": [{"name": "glocations", "value": "Turkey"},
                 {"name": "subject", "value": "Currency"},
                 {"name": "persons", "value": "Some Person"}],
    "pub_date": "2018-08-13T09:30:00+0000",
    "document_type": "article",
    "news_desk": "Foreign",
    "section_name": "World",
    "word_count": 1200,
    "source": "The New York Times",
}


class TestNytPayload:
    def test_a_document_becomes_a_valid_item(self):
        assert core.validate_item(nyt.to_item(NYT_DOC))

    def test_the_fields_land_where_they_belong(self):
        item = nyt.to_item(NYT_DOC)
        assert item["title"] == "Turkey's Currency Crisis Deepens"
        assert item["link"] == NYT_DOC["web_url"]
        assert item["source"] == "The New York Times"
        assert item["snippet"].startswith("The lira fell")

    def test_a_document_carries_no_body(self):
        # The NYT returns none and its pages are paywalled. The row says so via
        # tier='abstract-only' rather than pretending to be a missing body.
        assert nyt.to_item(NYT_DOC)["text"] == ""

    def test_the_lead_paragraph_backs_up_a_missing_abstract(self):
        item = nyt.to_item({**NYT_DOC, "abstract": ""})
        assert item["snippet"].startswith("The currency's collapse")

    def test_a_document_with_no_url_is_dropped(self):
        assert nyt.to_item({**NYT_DOC, "web_url": ""}) is None

    def test_a_document_with_no_headline_is_dropped(self):
        assert nyt.to_item({**NYT_DOC, "headline": {}}) is None

    def test_a_document_with_no_date_is_dropped(self):
        assert nyt.to_item({**NYT_DOC, "pub_date": None}) is None


class TestNytCountryFilter:
    """The archive cannot be queried per country, so the filter is the source."""

    def test_a_country_is_found_by_name(self):
        assert "Turkey" in nyt.searchable_text(NYT_DOC)

    def test_place_keywords_are_searched(self):
        # Catches a story whose headline names only a person or a city.
        doc = {**NYT_DOC, "headline": {"main": "A Quiet Week"}, "abstract": "",
               "lead_paragraph": ""}
        assert "Turkey" in nyt.searchable_text(doc)

    def test_person_keywords_are_not_searched(self):
        assert "Some Person" not in nyt.searchable_text(NYT_DOC)

    def test_the_filter_reuses_the_gazetteer(self):
        # Not a second list of country names: a country the gazetteer cannot
        # find is one it also cannot mask, and both should fail from one fix.
        from backend.utils.history.masking import gazetteer
        assert gazetteer.mentions(nyt.searchable_text(NYT_DOC), "TR")
        assert not gazetteer.mentions(nyt.searchable_text(NYT_DOC), "PT")


class TestNytMonths:
    def test_months_tile_the_range(self):
        got = nyt.months(datetime.date(2016, 11, 15), datetime.date(2017, 2, 3))
        assert got == [(2016, 11), (2016, 12), (2017, 1), (2017, 2)]

    def test_a_single_month_is_one_call(self):
        assert nyt.months(datetime.date(2018, 5, 2), datetime.date(2018, 5, 30)) == [(2018, 5)]

    def test_month_bounds_cover_the_month(self):
        assert nyt.month_bounds(2020, 2) == (datetime.date(2020, 2, 1),
                                             datetime.date(2020, 2, 29))
        assert nyt.month_bounds(2018, 12) == (datetime.date(2018, 12, 1),
                                              datetime.date(2018, 12, 31))

    def test_months_are_stable_across_runs(self):
        args = (datetime.date(2016, 8, 3), datetime.date(2018, 4, 1))
        assert nyt.months(*args) == nyt.months(*args)


class TestNytHarvestEconomics:
    def test_one_call_serves_every_country(self, monkeypatch):
        """Fetching per country would be five times the calls for the same bytes."""
        calls, rows = [], []
        monkeypatch.setattr(nyt, "_docs", lambda y, m: calls.append((y, m)) or [NYT_DOC])
        monkeypatch.setattr(nyt.store, "upsert_articles", lambda r: rows.extend(r) or len(r))

        written = nyt.harvest_month(2018, 8, ["TR", "PT", "BR"])

        assert len(calls) == 1
        assert written == {"TR": 1, "PT": 0, "BR": 0}

    def test_rows_land_on_the_degraded_tier(self, monkeypatch):
        rows = []
        monkeypatch.setattr(nyt, "_docs", lambda y, m: [NYT_DOC])
        monkeypatch.setattr(nyt.store, "upsert_articles", lambda r: rows.extend(r) or len(r))

        nyt.harvest_month(2018, 8, ["TR"])

        assert rows[0]["tier"] == "abstract-only"
        # Not 'pending': pending means a body is coming, and none is. A Wayback
        # fetch of a paywalled NYT page returns the paywall. Queueing these
        # would put ~200k URLs into the recovery drain for nothing.
        assert rows[0]["body_status"] == "degraded-title-only"
        assert rows[0]["body"] is None

    def test_a_month_is_skipped_only_for_countries_that_have_it(self, monkeypatch):
        asked = []
        monkeypatch.setattr(nyt.store, "completed_windows",
                            lambda src, iso2: {datetime.date(2016, 8, 1)} if iso2 == "PT" else set())
        monkeypatch.setattr(nyt, "harvest_month",
                            lambda y, m, wanted: asked.append((y, m, tuple(wanted))) or {})
        monkeypatch.setattr(nyt.store, "write_checkpoint", lambda *a, **kw: None)
        monkeypatch.setattr(nyt.time, "sleep", lambda s: None)

        nyt.harvest(roster=["PT", "TR"], since="2016-08-01")

        assert asked[0][2] == ("TR",), "PT already had August 2016"

    def test_one_bad_month_does_not_end_the_harvest(self, monkeypatch):
        seen, statuses = [], []

        def boom(year, month, wanted):
            seen.append((year, month))
            if len(seen) == 1:
                raise RuntimeError("500 from the archive")
            return {"PT": 2}

        monkeypatch.setattr(nyt.store, "completed_windows", lambda src, iso2: set())
        monkeypatch.setattr(nyt, "harvest_month", boom)
        monkeypatch.setattr(nyt.store, "write_checkpoint",
                            lambda *a, **kw: statuses.append(kw.get("status", "done")))
        monkeypatch.setattr(nyt.time, "sleep", lambda s: None)

        written = nyt.harvest(roster=["PT"], since="2016-08-01")

        assert len(seen) > 1 and statuses[0] == "failed"
        assert written == 2 * (len(seen) - 1)


class TestNytDeskFilter:
    """The archive hands over the whole paper, sport and recipes included."""

    def test_a_sports_document_is_not_risk_news(self):
        assert not nyt.carries_risk_news({**NYT_DOC, "news_desk": "Sports"})

    def test_a_foreign_desk_document_is(self):
        assert nyt.carries_risk_news({**NYT_DOC, "news_desk": "Foreign"})

    def test_a_document_with_no_desk_is_kept(self):
        # A quarter of the archive predates the field; absent is not excluded.
        assert nyt.carries_risk_news({**NYT_DOC, "news_desk": ""})
        assert nyt.carries_risk_news({k: v for k, v in NYT_DOC.items() if k != "news_desk"})

    def test_the_filter_runs_before_the_country_match(self, monkeypatch):
        rows = []
        monkeypatch.setattr(nyt, "_docs", lambda y, m: [
            {**NYT_DOC, "news_desk": "Sports"}, {**NYT_DOC, "news_desk": "Foreign"}])
        monkeypatch.setattr(nyt.store, "upsert_articles", lambda r: rows.extend(r) or len(r))
        assert nyt.harvest_month(2018, 8, ["TR"]) == {"TR": 1}


class TestNytVolumeCap:
    """The archive is mostly about the United States, and a month wants ~86."""

    def test_the_cap_keeps_the_most_relevant(self, monkeypatch):
        many = [{**NYT_DOC, "web_url": f"https://www.nytimes.com/{i}",
                 "headline": {"main": ("Turkey inflation crisis deepens" if i < 3
                                       else "A quiet day in Turkey")}}
                for i in range(10)]
        rows = []
        monkeypatch.setattr(nyt, "_docs", lambda y, m: many)
        monkeypatch.setattr(nyt.config, "NYT_MAX_PER_COUNTRY_MONTH", 3)
        monkeypatch.setattr(nyt.store, "upsert_articles", lambda r: rows.extend(r) or len(r))

        assert nyt.harvest_month(2018, 8, ["TR"]) == {"TR": 3}
        assert all("crisis" in r["title"] for r in rows), "the cap dropped the wrong tail"

    def test_a_dropped_tail_is_reported(self, monkeypatch, caplog):
        # A cap nobody reports reads afterwards as a complete harvest.
        monkeypatch.setattr(nyt, "_docs", lambda y, m: [
            {**NYT_DOC, "web_url": f"https://www.nytimes.com/{i}"} for i in range(5)])
        monkeypatch.setattr(nyt.config, "NYT_MAX_PER_COUNTRY_MONTH", 2)
        monkeypatch.setattr(nyt.store, "upsert_articles", lambda r: len(r))

        with caplog.at_level("INFO"):
            nyt.harvest_month(2018, 8, ["TR"])
        assert "kept 2 of 5" in caplog.text

    def test_under_the_cap_nothing_is_dropped(self, monkeypatch):
        monkeypatch.setattr(nyt, "_docs", lambda y, m: [NYT_DOC])
        monkeypatch.setattr(nyt.store, "upsert_articles", lambda r: len(r))
        assert nyt.harvest_month(2018, 8, ["TR"]) == {"TR": 1}
