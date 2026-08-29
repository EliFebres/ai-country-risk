"""The article path, live and historical, and the rule that keeps them one path.

Historical and live differ only in where article items come from. Everything
downstream — relevance, the per-theme floor, dedupe, theme classification — is
one implementation, and the tests that matter most here assert that by
*identity*: if someone re-inlines a shared function into an adapter, they fail.
An adapter that grew its own ranking would produce a corpus retrieved by
different rules than the live run, which is drift that looks fine in every count.

The other half is the leak. A 2018 page refetched today can carry a correction
added years later — future text wearing a past date, the subtlest failure in the
machine, invisible in any count. So the wayback tests pin the *policy*: a scan
that errors counts as a leak, and the dollar cap stops the drain.

No network, no model: every boundary is monkeypatched.
"""

import datetime
import json
import logging
import inspect
import re

import pytest

from backend.news_fetching import snapshot_select as sel, wayback
from backend.util import config
from backend.news_fetching.adapters import gdelt, guardian, newsapi_ai, nyt
from backend.news_fetching import article_enrichment as ae
from backend.news_fetching import article_ranking, core, source_filter

PUBLISHED = datetime.datetime(2018, 3, 14, 9, 30, tzinfo=datetime.timezone.utc)
URL = "https://www.reuters.com/article/x"


# ---------------------------------------------------------------------------
# One implementation, enforced by identity rather than by hope
# ---------------------------------------------------------------------------

class TestNoSecondCopy:
    def test_selection_is_the_shared_one(self):
        assert ae._select_with_theme_floor is core.select_with_theme_floor
        assert ae._by_relevance is core.by_relevance

    def test_headline_key_is_the_shared_one(self):
        assert ae._headline_key is core.headline_key

    def test_query_themes_are_the_shared_ones(self):
        assert ae._QUERY_THEMES is core.THEME_QUERIES

    def test_every_ledger_still_has_a_query(self):
        # The retrieval layer exists to feed the ledgers the prompt scores.
        # Dropping one would silently leave that ledger to the macro panel.
        assert set(core.THEME_QUERIES) == {
            "friction", "order", "security", "information", "edge", "broad"}

    def test_broad_runs_last(self):
        # First-seen-wins dedupe means the catch-all must not get to claim a
        # story a specific theme would have tagged.
        assert list(core.THEME_QUERIES)[-1] == core.BROAD_THEME

    def test_the_floor_fits_inside_the_budget(self):
        # 6 themes x the floor must leave room for the open fill, or the
        # relevance ranking stops mattering at all.
        assert len(core.THEME_QUERIES) * ae._PER_THEME_FLOOR < 20


class TestNoAdapterForksTheCore:
    """An adapter that grew its own ranking or selection would produce a corpus
    retrieved by different rules than the live run — exactly the drift the
    shared core exists to prevent, and exactly the kind that looks fine in every
    count."""

    MODULES = (guardian, gdelt, nyt, newsapi_ai)
    FORBIDDEN = ("_HIGH_KEYWORDS", "score_relevance", "select_with_theme_floor",
                 "_select_with_theme_floor", "headline_key", "_by_relevance")
    THEME_QUERYING = (guardian, gdelt)

    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
    def test_no_adapter_defines_a_shared_name(self, module):
        for name in self.FORBIDDEN:
            assert name not in vars(module), f"{module.__name__} defines {name}"

    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
    def test_no_adapter_reimplements_a_shared_function(self, module):
        source = inspect.getsource(module)
        for name in self.FORBIDDEN:
            assert not re.search(
                rf"^\s*(def|{re.escape(name)}\s*=)\s*{re.escape(name)}\b",
                source, re.M), f"{module.__name__} redefines {name}"

    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
    def test_no_adapter_carries_its_own_theme_queries(self, module):
        # NYT and newsapi.ai are exempt from the second half. The NYT archive
        # endpoint takes a year and a month and returns the whole paper;
        # newsapi.ai is a concept query priced per search, where six theme
        # queries would cost six times one. Both get their themes from
        # `store.article_row`'s classifier. Both are still forbidden a theme list.
        source = inspect.getsource(module)
        assert "THEME_QUERIES: dict" not in source
        if module in self.THEME_QUERYING:
            assert "core.THEME_QUERIES" in source

    def test_an_adapter_with_no_query_still_gets_themed(self):
        # The exemption above must not become a silently untagged corpus: rows
        # with no `_theme` are classified from their text at the store boundary,
        # which is what fills the same per-theme floor the live run uses.
        from backend.data_upsert import store
        assert "core.classify_themes" in inspect.getsource(store.article_row)

    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
    def test_no_adapter_extracts_bodies_or_opens_a_transaction(self, module):
        source = inspect.getsource(module)
        assert "trafilatura" not in source
        # Every write goes through store.upsert_articles, which is where the
        # body-wins rule lives.
        assert "_transaction" not in source


# ---------------------------------------------------------------------------
# The canonical article item
# ---------------------------------------------------------------------------

class TestTheCanonicalItem:
    def test_shape_is_always_complete(self):
        item = core.normalize_item(title="t", link="https://x.test/a")
        assert set(core._ITEM_KEYS) <= set(item)

    def test_publisher_link_defaults_to_link(self):
        # Every historical source hands back one URL. Defaulting here is what
        # makes dedupe_key behave identically for all of them.
        assert core.normalize_item(
            link="https://x.test/a")["publisher_link"] == "https://x.test/a"

    def test_two_sources_of_one_story_agree(self):
        # A Guardian row and a GDELT stub for the same article: the adapter sets
        # publisher_link to its only URL, so the keys collide and the store's
        # body-wins rule can do its job.
        guardian_item = core.normalize_item(link="https://g.test/story", text="body")
        gdelt_item = core.normalize_item(link="https://g.test/story")
        assert core.dedupe_key(guardian_item) == core.dedupe_key(gdelt_item)

    def test_whitespace_is_stripped_from_the_key(self):
        # The three call sites disagreed about this before the extraction.
        assert core.dedupe_key({"link": "  https://x.test/a\n"}) == "https://x.test/a"

    def test_nothing_to_key_on_is_empty_not_an_error(self):
        assert core.dedupe_key({}) == ""

    def test_a_missing_canonical_key_is_caught(self):
        item = core.normalize_item(link="https://x.test/a")
        del item["published"]
        with pytest.raises(ValueError, match="canonical keys"):
            core.validate_item(item)

    def test_no_url_is_caught(self):
        with pytest.raises(ValueError, match="neither publisher_link nor link"):
            core.validate_item(core.normalize_item(title="orphan"))

    def test_syndicated_copies_collapse(self):
        # Wire copies share a headline and differ only by URL, so the
        # publisher-URL key cannot see them. Each survivor costs a stage-1
        # digest call, and the model's topic_group merges them afterwards — so
        # the waste never shows up in the output.
        titles = [
            "Brazil government now expects 2026 inflation to be above central bank's target - Yahoo",
            "Brazil Government Now Expects 2026 Inflation to Be Above Central Bank's Target - Reuters",
        ]
        assert len({core.headline_key(t) for t in titles}) == 1

    def test_only_the_last_publisher_suffix_is_dropped(self):
        assert core.headline_key("Brazil - China trade deal signed - BBC") == \
            "brazil china trade deal signed"

    def test_missing_title_is_falsy_not_an_error(self):
        # An empty key must not make every untitled article a duplicate of the
        # first one — the caller skips the headline check when this is empty.
        assert core.headline_key(None) == "" and core.headline_key("") == ""


class TestClassifyThemes:
    """The fallback tagger. Live items are tagged by which query returned them;
    historical items arrive with no query provenance at all, so this is what
    decides which theme's floor slot they compete for."""

    @pytest.mark.parametrize("theme, title", [
        ("friction",    "New customs regulation raises the permit fee"),
        ("order",       "Parliament dissolved ahead of a snap election"),
        ("security",    "Military attack on the border escalates the conflict"),
        ("information", "Journalist jailed as censorship of the judiciary widens"),
        ("edge",        "Startup founders and skilled workers leaving for Berlin"),
    ])
    def test_each_specific_theme_is_recognised(self, theme, title):
        assert core.classify_themes(title, "")[0] == theme

    def test_never_returns_empty(self):
        # Callers take [0] unguarded, so an empty list would be an AttributeError
        # in the middle of a harvest.
        assert core.classify_themes(None, None) == [core.BROAD_THEME]
        assert core.classify_themes("Lisbon wins the cup final", "") == [core.BROAD_THEME]

    def test_only_the_head_of_a_long_body_votes(self):
        # Navigation, related-links and comment sections live at the end of a
        # scrape and would otherwise match every theme at once.
        buried = "x" * 5000 + " military conflict war attack"
        assert core.classify_themes("A quiet Tuesday", buried) == [core.BROAD_THEME]

    def test_terms_come_from_the_queries(self):
        # If a term is added to a query but not to the classifier, the two paths
        # tag the same words differently. Deriving one from the other is what
        # makes that impossible.
        assert "taxation" in core.THEME_TERMS["friction"]
        assert core.THEME_TERMS[core.BROAD_THEME] == ()

    def test_query_provenance_is_never_overwritten(self):
        item = core.normalize_item(link="https://x.test/a", title="election",
                                   theme="broad")
        assert core.ensure_theme(item)["_theme"] == "broad"

    def test_a_page_that_defeats_the_parser_does_not_raise(self):
        # A body that raises must never stop the surrounding batch — recovery
        # runs over thousands of archive captures, many of them junk.
        assert core.extract_body(b"\x00\x01not html") == ""
        assert core.extract_body(None) == ""


# ---------------------------------------------------------------------------
# The per-theme floor: which ~20 of ~60 articles the model pays to read
# ---------------------------------------------------------------------------

def art(theme, relevance, published="2026-07-20T00:00:00Z", title=""):
    return {"_theme": theme, "relevance_score": relevance,
            "published": published, "title": title,
            "publisher_link": f"https://x.test/{theme}/{relevance}/{title}"}


def select(items, max_articles=20, per_theme=ae._PER_THEME_FLOOR):
    return ae._select_with_theme_floor(items, max_articles, per_theme)


class TestTheThemeFloor:
    """Without a per-theme floor an election week returns twenty election
    stories, the friction and information ledgers get scored on the macro panel
    alone, and nothing anywhere reports that the tax story was retrieved and
    then dropped."""

    def test_a_loud_theme_cannot_take_every_slot(self):
        from collections import Counter
        # 30 election stories all scoring higher than anything else. Under a
        # plain relevance sort this is the whole budget.
        loud = [art("order", 0.95, title=f"election {i}") for i in range(30)]
        quiet = [art("friction", 0.4, title="new tax on imports"),
                 art("information", 0.4, title="regulator audits the statistics office")]
        counts = Counter(i["_theme"] for i in select(loud + quiet, max_articles=20))
        assert counts["friction"] == 1 and counts["information"] == 1
        assert counts["order"] == 18

    def test_the_floor_takes_each_themes_best(self):
        items = [art("friction", 0.9, title="best"), art("friction", 0.4, title="worst"),
                 art("edge", 0.5, title="edge")]
        assert {p["title"] for p in select(items, max_articles=2, per_theme=1)} == \
            {"best", "edge"}

    def test_an_absent_theme_forfeits_rather_than_shrinks(self):
        items = [art("order", 0.9 - i / 100, title=f"o{i}") for i in range(4)] + \
                [art("edge", 0.5, title="e0")]
        assert len(select(items, max_articles=5, per_theme=2)) == 5

    def test_never_exceeds_the_budget_and_never_invents(self):
        items = [art(t, 0.5, title=f"{t}{i}") for t in core.THEME_QUERIES for i in range(10)]
        assert len(select(items, max_articles=20)) == 20
        assert len(select([art("order", 0.9), art("edge", 0.5)], max_articles=20)) == 2
        assert select([], max_articles=20) == []

    def test_output_is_most_relevant_first(self):
        # The caller assigns ids a1..aN by position, so order is part of the
        # contract even though the floor picks out of order.
        items = [art("order", 0.3, title="low"), art("edge", 0.9, title="high"),
                 art("friction", 0.6, title="mid")]
        scores = [p["relevance_score"] for p in select(items, max_articles=3)]
        assert scores == sorted(scores, reverse=True)

    def test_an_untagged_item_still_reaches_the_open_fill(self):
        # Defensive: an item without a _theme must not vanish, only lose its
        # claim on a reserved slot.
        items = [art("order", 0.9), {"relevance_score": 0.8, "published": None}]
        assert len(select(items, max_articles=5, per_theme=1)) == 2

    @pytest.mark.parametrize("per_theme", [0, 1, 2, 5])
    def test_any_floor_returns_a_full_budget(self, per_theme):
        items = [art(t, 0.5, title=f"{t}{i}") for t in core.THEME_QUERIES for i in range(6)]
        assert len(select(items, max_articles=20, per_theme=per_theme)) == 20


class TestEnsureTopThree:
    """The only guard behind the `rank BETWEEN 1 AND 3` DB constraint: it must
    return exactly 3 ids whenever 3+ articles exist."""

    def items(self, n):
        return {f"a{i}": {"id": f"a{i}", "published": f"2026-01-{i:02d}",
                          "relevance_score": 0.5} for i in range(1, n + 1)}

    def test_one_representative_per_topic(self):
        imp = {"a1": 0.9, "a2": 0.8, "a3": 0.7, "a4": 0.6, "a5": 0.5, "a6": 0.4}
        topics = {"a1": "t1", "a2": "t1", "a3": "t2", "a4": "t2", "a5": "t3", "a6": "t3"}
        got = article_ranking.ensure_top_three(self.items(6), imp, topics, "XX")
        assert got == ["a1", "a3", "a5"]

    def test_three_come_back_whenever_three_exist(self):
        # Across all three branches: representatives, backfill, no topic map.
        assert len(article_ranking.ensure_top_three(
            self.items(5), {"a1": 0.9}, {"a1": "t1"}, "XX")) == 3
        assert len(article_ranking.ensure_top_three(self.items(5), {}, {}, "XX")) == 3

    def test_fewer_than_three_articles_yields_what_there_is(self):
        assert len(article_ranking.ensure_top_three(self.items(2), {}, {}, "XX")) == 2

    def test_build_top_articles_shapes_rows_with_ranks(self):
        items_by_id = {"a1": {"id": "a1", "link": "http://x", "title": "T",
                              "source": "S", "published": "2026-01-01",
                              "image": "http://i"}}
        rows = article_ranking.build_top_articles(["a1"], items_by_id, {"a1": 0.7})
        assert rows == [{
            "rank": 1, "id": "a1", "url": "http://x", "title": "T", "source": "S",
            "published_at": "2026-01-01", "impact": 0.7, "summary": "",
            "image": "http://i",
        }]

    def test_a_missing_item_still_yields_a_ranked_row(self):
        rows = article_ranking.build_top_articles(["ghost"], {}, {})
        assert rows[0]["rank"] == 1 and rows[0]["url"] == "" and rows[0]["id"] is None


class TestTheDenylist:
    """A denylist that silently stops matching is invisible in production.
    These run against the real `blocked_sources.txt` — the same data production
    uses — and assert on its first entry."""

    def test_a_blocked_domain_is_blocked_through_www_and_subdomains(self):
        for url in ("https://whalesbook.com/article",
                    "https://www.whalesbook.com/article",
                    "https://news.whalesbook.com/a"):
            assert source_filter.is_blocked_url(url) is True

    def test_a_suffix_lookalike_is_not_blocked(self):
        # notwhalesbook.com must NOT match whalesbook.com.
        assert source_filter.is_blocked_url("https://notwhalesbook.com/a") is False

    def test_an_unrelated_domain_is_allowed(self):
        assert source_filter.is_blocked_url("https://reuters.com/article") is False
        assert source_filter.is_blocked_url(None) is False


# ---------------------------------------------------------------------------
# Body recovery — the policy, not the model
# ---------------------------------------------------------------------------

@pytest.fixture()
def marked(monkeypatch):
    """Capture what recover_one writes instead of writing it."""
    calls = []
    monkeypatch.setattr(wayback.store, "mark_body",
                        lambda url, **kw: calls.append({"url": url, **kw}))
    return calls


def wayback_row():
    return {"url": URL, "published_at": PUBLISHED, "country_iso2": "TR",
            "source_system": "gdelt", "title": "Lira slides"}


class TestLeakageScanPolicy:
    def test_a_scan_that_errors_counts_as_a_leak(self, monkeypatch):
        # Fails closed. A body nobody could verify must not be scored as if it
        # had been: being short one article costs a week a little evidence, one
        # leaked body costs the series its honesty.
        class Boom:
            def with_structured_output(self, **_):
                raise RuntimeError("no key")
        monkeypatch.setattr(wayback.ai_client, "build_digest_chat", lambda k: Boom())
        assert wayback.references_future("text", PUBLISHED, "sk-test") is True

    def test_a_malformed_answer_counts_as_a_leak(self, monkeypatch):
        class Chain:
            def with_structured_output(self, **_):
                return self

            def invoke(self, _):
                return "not a dict"
        monkeypatch.setattr(wayback.ai_client, "build_digest_chat", lambda k: Chain())
        assert wayback.references_future("text", PUBLISHED, "sk-test") is True

    def test_a_clean_answer_passes_and_carries_the_publication_date(self, monkeypatch):
        seen = {}

        class Chain:
            def with_structured_output(self, **_):
                return self

            def invoke(self, prompt):
                seen["prompt"] = prompt
                return {"references_future": False, "evidence": ""}
        monkeypatch.setattr(wayback.ai_client, "build_digest_chat", lambda k: Chain())
        assert wayback.references_future("body text", PUBLISHED, "sk-test") is False
        assert "2018-03-14" in seen["prompt"] and "body text" in seen["prompt"]

    def test_it_uses_the_cheap_model(self):
        # The scan runs over thousands of bodies. The scoring model would be an
        # order of magnitude more expensive for a boolean.
        from backend.llm import client as ai_client
        assert wayback.ai_client.build_digest_chat is ai_client.build_digest_chat


class TestTheScanBudget:
    def test_the_cap_stops_the_drain_rather_than_being_exceeded(self, monkeypatch, marked):
        monkeypatch.setattr(wayback, "find_capture", lambda *a: None)
        monkeypatch.setattr(wayback, "fetch_live", lambda *a: "x" * 24000)
        monkeypatch.setattr(wayback, "references_future", lambda *a: False)
        with pytest.raises(wayback.BudgetExhausted):
            wayback.recover_one(wayback_row(), "sk-test",
                                [config.LEAKAGE_SCAN_BUDGET_USD])
        assert marked == [], "nothing may be written once the budget is gone"

    def test_spend_accumulates_across_articles(self, monkeypatch, marked):
        monkeypatch.setattr(wayback, "find_capture", lambda *a: None)
        monkeypatch.setattr(wayback, "fetch_live", lambda *a: "x" * 24000)
        monkeypatch.setattr(wayback, "references_future", lambda *a: False)
        spent = [0.0]
        wayback.recover_one(wayback_row(), "sk-test", spent)
        first = spent[0]
        wayback.recover_one(wayback_row(), "sk-test", spent)
        assert spent[0] == pytest.approx(2 * first) and first > 0

    def test_a_capture_costs_nothing(self, monkeypatch, marked):
        monkeypatch.setattr(wayback, "find_capture", lambda *a: "20180314181500")
        monkeypatch.setattr(wayback, "fetch_capture", lambda *a: "Archived text.")
        spent = [0.0]
        wayback.recover_one(wayback_row(), "sk-test", spent)
        assert spent[0] == 0.0

    def test_the_projection_scales_with_volume(self):
        one = wayback.scan_cost_usd(["x" * 24000])
        assert one > 0
        assert wayback.scan_cost_usd(["x" * 24000] * 10) == pytest.approx(10 * one)


# ---------------------------------------------------------------------------
# Snapshot assembly reuses the live selection rather than forking it
# ---------------------------------------------------------------------------

class TestSelectionMatchesTheLiveRun:
    def test_it_reuses_the_live_relevance_and_floor(self):
        # Two copies of "the 20 articles" would be a silent disagreement about
        # what the historical series is comparable to.
        source = inspect.getsource(sel)
        assert "article_ranking.score_relevance" in source
        assert "core.select_with_theme_floor" in source
        assert "article_enrichment._PER_THEME_FLOOR" in source

    def test_the_ration_keeps_the_most_relevant_abstracts(self):
        """The NYT archive returns no bodies and is overwhelmingly about the US.
        Left uncapped, a US snapshot fills with two-sentence abstracts while a
        Portugal one keeps full Guardian bodies — and every cross-country
        comparison the pilot exists to make becomes partly a comparison of
        evidence texture."""
        items = [{"tier": "abstract-only", "relevance_score": s, "published": None}
                 for s in (1, 9, 5, 7, 3)]
        kept = sel.ration_abstracts(items, max_articles=5)   # cap = 2
        assert [i["relevance_score"] for i in kept] == [9, 7]

    def test_the_ration_leaves_full_bodied_articles_alone(self):
        items = ([{"tier": "full", "relevance_score": 0.1, "published": None}] * 30
                 + [{"tier": "abstract-only", "relevance_score": 9, "published": None}])
        assert len(sel.ration_abstracts(items, max_articles=20)) == 31

    def test_the_snippet_is_the_lede_and_only_the_lede(self):
        """Choosing the snippet to beat the body-mention ceiling is how a
        snapshot fills up with articles that merely mention the country."""
        body = "Nothing about the country here. " * 20 + "Portugal appears late."
        got = sel.relevance_snippet({"abstract": None}, body, "Portugal")
        assert got == body[:sel._SNIPPET_CHARS]
        assert "Portugal" not in got, (
            "excerpting from the first country mention lifts every incidental "
            "article to the body-mention ceiling")

    def test_an_abstract_is_preferred_when_there_is_one(self):
        assert sel.relevance_snippet(
            {"abstract": "Lawmakers met on Friday."}, "body text",
            "Portugal") == "Lawmakers met on Friday."


# ---------------------------------------------------------------------------
# The quota wall has to arrive as a quota, not as a mystery
# ---------------------------------------------------------------------------

class TestTheQuotaWallIsNamed:
    """"Come back tomorrow" and "this country-year is broken" take different
    branches, so a 429 must not be reported as a generic request error.

    On 2026-08-15 it was. `429` is retryable, so `retry_transient` spent five
    attempts on it and re-raised the `HTTPError`; that sailed past the
    `remaining <= 0` check — which only ever sees a *successful* response — into
    the driver's catch-all, which wrote `note='request error'` and moved on to
    the next country-year. One wall became 46 identical failed checkpoints across
    four countries in fifteen minutes, and the harvest read as broken rather than
    as rate-limited.
    """

    @staticmethod
    def _raise_429(monkeypatch, calls=None):
        """Make `_get` fail the way the API does once the day's budget is gone."""
        import requests

        response = requests.Response()
        response.status_code = 429

        def boom(_params):
            if calls is not None:
                calls.append(_params)
            raise requests.HTTPError("429 Too Many Requests", response=response)

        monkeypatch.setattr(guardian, "_get", boom)
        monkeypatch.setenv("GUARDIAN_API_KEY", "k")

    def test_a_429_that_outlives_the_retries_is_a_quota_not_a_request_error(
            self, monkeypatch):
        self._raise_429(monkeypatch)
        with pytest.raises(guardian.QuotaExhausted, match="429"):
            guardian._page("q", datetime.date(2019, 1, 1),
                           datetime.date(2019, 12, 31), 1)

    def test_the_harvest_stops_cleanly_instead_of_burning_the_roster(
            self, monkeypatch):
        """The whole point of the branch: `QuotaExhausted` returns, so the
        remaining country-years stay unattempted and resumable rather than being
        checkpointed `failed` one wasted call at a time."""
        calls = []
        self._raise_429(monkeypatch, calls)
        checkpoints = []
        monkeypatch.setattr(guardian.store, "completed_windows",
                            lambda *_a, **_k: set())
        monkeypatch.setattr(guardian.store, "write_checkpoint",
                            lambda *a, **k: checkpoints.append(k.get("note", "")))

        written = guardian.harvest(roster=["PT", "TR", "KR"], since="2017-01-01")

        assert written == 0
        # The wall was actually reached — without this the rest passes vacuously
        # on an empty work list.
        assert len(calls) == 1
        # And it was reached *once*: the run returned rather than spending one
        # wasted call per remaining country-year, which is the 46-row burst.
        assert "request error" not in checkpoints

    def test_a_genuine_http_error_is_still_an_error(self, monkeypatch):
        """Only 429 means quota. A 404 must not be laundered into a wait."""
        import requests

        response = requests.Response()
        response.status_code = 404

        def boom(_params):
            raise requests.HTTPError("404 Not Found", response=response)

        monkeypatch.setattr(guardian, "_get", boom)
        monkeypatch.setenv("GUARDIAN_API_KEY", "k")
        with pytest.raises(requests.HTTPError):
            guardian._page("q", datetime.date(2019, 1, 1),
                           datetime.date(2019, 12, 31), 1)


# ---------------------------------------------------------------------------
# A 429 is two events wearing one status code
# ---------------------------------------------------------------------------

class TestTheWallAndTheThrottleTakeDifferentBranches:
    """The daily wall must cost one attempt; a burst throttle must still retry.

    `_get` used to treat every 429 the same — retryable — so the wall spent five
    attempts and slept between them before anything recognised it, and those
    attempts are billed against the budget that had just run out. Telling the
    two apart needs the response's own `X-RateLimit-Remaining-Day`, which is why
    headers are now folded in on refusals and not only on success.
    """

    @staticmethod
    def _respond(monkeypatch, status, headers, attempts):
        """Make the wire return one canned response, counting attempts."""
        import requests

        def fake_get(_url, **kwargs):
            attempts.append(kwargs.get("params"))
            resp = requests.Response()
            resp.status_code = status
            resp.headers.update(headers)
            return resp

        monkeypatch.setattr(guardian.requests, "get", fake_get)
        monkeypatch.setenv("GUARDIAN_API_KEY", "k")
        # A real backoff would make the throttle case take ~15s of wall clock.
        monkeypatch.setattr(guardian.time, "sleep", lambda *_a: None)

    def test_a_429_reporting_no_budget_left_is_the_wall_and_costs_one_attempt(
            self, monkeypatch):
        attempts = []
        self._respond(monkeypatch, 429,
                      {"X-RateLimit-Remaining-Day": "0",
                       "X-RateLimit-Limit-Day": "500"}, attempts)

        with pytest.raises(guardian.QuotaExhausted):
            guardian._get({"q": "x"})

        assert len(attempts) == 1, "the wall must not burn the retry budget"

    def test_a_429_with_budget_left_is_a_throttle_and_is_retried(self, monkeypatch):
        import requests

        attempts = []
        self._respond(monkeypatch, 429,
                      {"X-RateLimit-Remaining-Day": "412",
                       "X-RateLimit-Limit-Day": "500"}, attempts)

        # Not QuotaExhausted: budget remains, so this is a burst limit and
        # ending the day's harvest over it would cost hours to save seconds.
        with pytest.raises(requests.HTTPError):
            guardian._get({"q": "x"})

        assert len(attempts) == 5, "a throttle should exhaust the retry budget"

    def test_a_429_with_no_header_at_all_takes_the_throttle_branch(self, monkeypatch):
        """The case that must not be guessed.

        A missing header is not evidence of a wall. Reading it as one would let
        a single unlabelled throttle — or an API that stopped sending headers —
        end a day's harvesting on no evidence.
        """
        import requests

        attempts = []
        self._respond(monkeypatch, 429, {}, attempts)

        with pytest.raises(requests.HTTPError):
            guardian._get({"q": "x"})

        assert len(attempts) == 5

    def test_the_wall_reports_the_limit_the_response_stated(self, monkeypatch):
        attempts = []
        self._respond(monkeypatch, 429,
                      {"X-RateLimit-Remaining-Day": "0",
                       "X-RateLimit-Limit-Day": "328"}, attempts)

        with pytest.raises(guardian.QuotaExhausted) as caught:
            guardian._get({"q": "x"})

        assert caught.value.daily_limit == 328

    def test_retry_after_is_read_off_the_refusal(self, monkeypatch):
        """`Retry-After` arrives *with* the 429 and nowhere else.

        Folding headers in only on success meant the one value that says when
        the quota resets was never seen, and the harvest's "resets in ..." line
        reported "an unreported time" on every wall.
        """
        monkeypatch.setattr(guardian, "_QUOTA",
                            {"limit": None, "remaining": None,
                             "reset_seconds": None, "observed_calls": 0})
        attempts = []
        self._respond(monkeypatch, 429,
                      {"X-RateLimit-Remaining-Day": "0", "Retry-After": "29340"},
                      attempts)

        with pytest.raises(guardian.QuotaExhausted):
            guardian._get({"q": "x"})

        assert guardian._QUOTA["reset_seconds"] == 29340


class TestTheLedgerSaysWhyTheHarvestStopped:
    """A ledger row nobody can read back is the point of writing one.

    The wall wrote no row at all: the window stayed unattempted, which resumes
    correctly but leaves a multi-week harvest unable to say whether it stopped
    on a budget or on a fault.
    """

    def test_the_wall_writes_a_row_noting_the_quota(self, monkeypatch):
        rows = []
        monkeypatch.setattr(guardian.store, "completed_windows",
                            lambda *_a, **_k: set())
        monkeypatch.setattr(guardian.store, "write_checkpoint",
                            lambda *a, **k: rows.append(k))
        monkeypatch.setenv("GUARDIAN_API_KEY", "k")

        def wall(*_a, **_k):
            raise guardian.QuotaExhausted("spent", 500)

        monkeypatch.setattr(guardian, "harvest_window", wall)
        guardian.harvest(roster=["PT", "TR"], since="2017-01-01")

        assert len(rows) == 1, "one row for the window that hit the wall, no more"
        assert rows[0]["note"] == "quota exhausted"

    def test_that_row_does_not_count_as_harvested(self, monkeypatch):
        """The row must never make the window look done.

        `completed_windows` skips `status='done'`, so stamping the wall as done
        would silently drop a country-year from the corpus and nothing
        downstream would ever ask for it again.
        """
        rows = []
        monkeypatch.setattr(guardian.store, "completed_windows",
                            lambda *_a, **_k: set())
        monkeypatch.setattr(guardian.store, "write_checkpoint",
                            lambda *a, **k: rows.append(k))
        monkeypatch.setenv("GUARDIAN_API_KEY", "k")
        monkeypatch.setattr(guardian, "harvest_window",
                            lambda *_a, **_k: (_ for _ in ()).throw(
                                guardian.QuotaExhausted("spent", 500)))

        guardian.harvest(roster=["PT"], since="2017-01-01")

        assert rows[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# Harvest order, and knowing when the harvest is done
# ---------------------------------------------------------------------------

class TestTheHarvestOrderUnblocksTheBlockedWork:
    """Tier 1 is the pilot five, because everything measured sits on them.

    Order matters here in a way it does not for most loops: the harvest is
    quota-bound over weeks, so whatever is late in the list is late by days. A
    bake-off re-run or a Gate-2 re-measure is blocked until the pilot five are
    banked; the other 43 block nothing.
    """

    def test_the_pilot_five_come_first(self):
        assert config.HARVEST_ROSTER[:5] == list(config.HARVEST_TIER_1)

    def test_brazil_is_harvested_even_though_it_is_not_scored(self):
        """The coupling that lost BR's corpus once already.

        BR is deliberately absent from `PILOT_ROSTER` — harvested, not scored —
        so defaulting the harvest to the scoring roster drops it.
        """
        assert "BR" in config.HARVEST_ROSTER
        assert "BR" not in config.PILOT_ROSTER

    def test_it_is_the_whole_roster_exactly_once(self):
        from backend.util import constants

        assert sorted(config.HARVEST_ROSTER) == sorted(
            e["iso2"] for e in constants.COUNTRY_ROSTER)
        assert len(config.HARVEST_ROSTER) == len(set(config.HARVEST_ROSTER)) == 48

    def test_the_masking_roster_is_not_the_harvest_roster(self):
        """`DEFAULT_ROSTER` must stay all 48 whatever is being harvested.

        Masking correctness does not care whose turn it is, and wiring the two
        together would make the mask map a function of harvest progress.
        """
        from backend.llm import gazetteer

        assert set(gazetteer.DEFAULT_ROSTER) == set(config.HARVEST_ROSTER)
        assert gazetteer.DEFAULT_ROSTER != tuple(config.HARVEST_ROSTER), (
            "same members, different order — the masking roster is its own thing")

    def test_the_harvesters_default_to_it(self, monkeypatch):
        """The consumer side: what `harvest()` actually walks with no roster."""
        seen = []
        monkeypatch.setenv("GUARDIAN_API_KEY", "k")
        monkeypatch.setattr(guardian.store, "completed_windows",
                            lambda _s, iso2: seen.append(iso2) or set())
        monkeypatch.setattr(guardian, "harvest_window",
                            lambda *_a, **_k: (_ for _ in ()).throw(
                                guardian.QuotaExhausted("spent", 500)))
        monkeypatch.setattr(guardian.store, "write_checkpoint", lambda *a, **k: None)

        guardian.harvest(since="2016-01-01")

        # One lookup per country now, but assert on first-appearance order so
        # the test survives the loop shape changing again.
        order = list(dict.fromkeys(seen))
        assert order[:5] == list(config.HARVEST_TIER_1)
        assert len(order) == 48
        assert len(seen) == 48, "one completed_windows query per country, not per window"


class TestAConvergedHarvestSaysSo:
    """A finite job running unattended has to be able to report that it is done.

    Otherwise a converged harvest and a stuck one produce identical silence in
    the log, and the only choices are killing it early or leaving it for months.
    """

    def test_guardian_reports_completion_and_does_no_work(self, monkeypatch, caplog):
        monkeypatch.setenv("GUARDIAN_API_KEY", "k")
        # Every window already done.
        monkeypatch.setattr(
            guardian.store, "completed_windows",
            lambda _s, _i: {w[0] for w in guardian.year_windows(
                datetime.date(2016, 1, 1), datetime.date.today())})

        def must_not_run(*_a, **_k):
            raise AssertionError("a converged harvest must not call the API")

        monkeypatch.setattr(guardian, "harvest_window", must_not_run)

        with caplog.at_level(logging.INFO):
            assert guardian.harvest(roster=["PT"], since="2016-01-01") == 0
        assert "nothing to harvest" in caplog.text
        assert "roster complete through" in caplog.text

    def test_nyt_reports_completion_and_does_no_work(self, monkeypatch, caplog):
        monkeypatch.setenv("NYT_API_KEY", "k")
        monkeypatch.setattr(
            nyt.store, "completed_windows",
            lambda _s, _i: {nyt.month_bounds(y, m)[0] for y, m in nyt.months(
                datetime.date(2016, 1, 1), datetime.date.today())})

        def must_not_run(*_a, **_k):
            raise AssertionError("a converged harvest must not call the API")

        monkeypatch.setattr(nyt, "harvest_month", must_not_run)

        with caplog.at_level(logging.INFO):
            assert nyt.harvest(roster=["PT"], since="2016-01-01") == 0
        assert "nothing to harvest" in caplog.text

    def test_an_unfinished_harvest_reports_what_is_left(self, monkeypatch, caplog):
        """The other half: the number that says how far from done it is."""
        monkeypatch.setenv("GUARDIAN_API_KEY", "k")
        monkeypatch.setattr(guardian.store, "completed_windows", lambda *_a: set())
        monkeypatch.setattr(guardian.store, "write_checkpoint", lambda *a, **k: None)
        calls = {"n": 0}

        def one_then_wall(*_a, **_k):
            calls["n"] += 1
            if calls["n"] > 1:
                raise guardian.QuotaExhausted("spent", 500)
            return 3

        monkeypatch.setattr(guardian, "harvest_window", one_then_wall)
        with caplog.at_level(logging.INFO):
            guardian.harvest(roster=["PT"], since="2016-01-01")

        assert "country-years left" in caplog.text


# ---------------------------------------------------------------------------
# newsapi.ai — the first metered source
# ---------------------------------------------------------------------------

def _response(status=200, headers=None, payload=None):
    """A hand-built response, the way the Guardian wall tests build theirs."""
    import requests
    resp = requests.Response()
    resp.status_code = status
    resp.headers.update(headers or {})
    resp._content = json.dumps(payload if payload is not None else {}).encode()
    return resp


def _article(url="https://p.test/a", body="x" * 5000, published="2019-06-01T09:00:00",
             title="A thing happened", source_title="Publico",
             source_country="Portugal"):
    """One Event Registry article in the shape their API returns."""
    return {
        "url": url, "title": title, "body": body, "dateTimePub": published,
        "source": {"uri": "publico.pt", "title": source_title,
                   "location": {"country": {"label": {"eng": source_country}}}},
    }


class TestTheArchiveStaysReachable:
    """The failure this guards is silent and total: `forceMaxDataTimeWindow`
    clamps a search to the last 31 days, so every archive window would come back
    empty and correctly billed. Every account of this API describes an
    `allowUseOfArchive` flag you switch *on*; the SDK's real default is on, and
    the flag exists only to turn it off."""

    def test_the_clamp_is_never_sent(self, monkeypatch):
        monkeypatch.setenv("NEWSAPI_AI_API_KEY", "k")
        payload = newsapi_ai._payload("Portugal", datetime.date(2019, 1, 1),
                                      datetime.date(2019, 12, 31), 1)
        assert "forceMaxDataTimeWindow" not in payload

    def test_the_clamp_is_nowhere_in_the_code(self):
        # Belt and braces: a future edit adding it to a different code path
        # would pass the test above and still lose the archive. Matched as a
        # quoted string rather than as a word, because the module names it
        # twice in prose in order to forbid it — and a test that cannot tell
        # the documentation from the payload would have to be deleted the
        # first time someone explained the rule.
        source = inspect.getsource(newsapi_ai)
        assert '"forceMaxDataTimeWindow"' not in source
        assert "'forceMaxDataTimeWindow'" not in source

    def test_the_body_is_asked_for_whole(self, monkeypatch):
        # articleBodyLen is a truncation length, not a flag: any value but -1
        # silently caps every article, and the cap would read as short bodies.
        monkeypatch.setenv("NEWSAPI_AI_API_KEY", "k")
        payload = newsapi_ai._payload("Portugal", datetime.date(2019, 1, 1),
                                      datetime.date(2019, 12, 31), 1)
        assert payload["articleBodyLen"] == -1


class TestABodyIsNotJustNonEmptyText:
    """`adapters.guardian` calls anything truthy a body, so a one-character
    string is stored there as `recovered`. Aggregating 150,000 publishers makes
    that untenable: a 400-character syndication stub read as full evidence
    poisons every digest built on it, and the count still says `recovered`."""

    def test_a_full_body_is_recovered_and_api_native(self):
        item = core.normalize_item(link="https://p.test/a", text="x" * 5000)
        status, vintage, out = newsapi_ai.classify_body(item)
        assert (status, vintage) == ("recovered", "api-native")
        assert out["text"] == "x" * 5000

    def test_a_stub_is_demoted_to_an_abstract_and_queued(self):
        item = core.normalize_item(link="https://p.test/a", text="x" * 400)
        status, vintage, out = newsapi_ai.classify_body(item)
        assert (status, vintage) == ("pending", None)
        # Demoted, not discarded: the store writes `snippet` to `abstract`.
        assert out["text"] == ""
        assert out["snippet"] == "x" * 400

    def test_no_text_at_all_is_queued_not_invented(self):
        item = core.normalize_item(link="https://p.test/a", text="")
        status, vintage, out = newsapi_ai.classify_body(item)
        assert (status, vintage) == ("pending", None)
        assert not out["text"]

    def test_the_floor_is_the_boundary_it_claims_to_be(self):
        floor = config.NEWSAPI_MIN_BODY_CHARS
        at = core.normalize_item(link="https://p.test/a", text="x" * floor)
        under = core.normalize_item(link="https://p.test/b", text="x" * (floor - 1))
        assert newsapi_ai.classify_body(at)[0] == "recovered"
        assert newsapi_ai.classify_body(under)[0] == "pending"

    def test_only_states_the_queue_can_return_are_written(self):
        # The one-way-door rule: a status outside BODY_STATUSES would pass here
        # and fail at the CHECK constraint, mid-harvest.
        from backend.data_upsert import schema
        for text in ("", "x" * 400, "x" * 5000):
            status, _v, _i = newsapi_ai.classify_body(
                core.normalize_item(link="https://p.test/a", text=text))
            assert status in schema.BODY_STATUSES


class TestTheSpendIsMeasuredNotAssumed:
    """The number that decides a $90/mo purchase must come off the wire. The
    published price list gives '5 tokens/searched year' and says nothing about a
    sub-year window, so arithmetic here would be an assertion wearing a
    measurement's clothes."""

    def setup_method(self):
        newsapi_ai._SPEND.update(tokens=0, calls=0, measured_calls=0,
                                 limit=None, remaining=None)

    def test_the_billing_header_is_what_gets_counted(self):
        resp = _response(headers={"req-tokens": "7", "x-ratelimit-remaining": "4993"})
        assert newsapi_ai._read_spend_headers(resp, fallback=5) == 7
        assert newsapi_ai.spend()["tokens"] == 7
        assert newsapi_ai.spend()["remaining"] == 4993
        assert newsapi_ai.spend()["tokens_are_measured"] is True

    def test_a_response_with_no_header_falls_back_and_says_so(self):
        resp = _response(headers={})
        assert newsapi_ai._read_spend_headers(resp, fallback=5) == 5
        assert newsapi_ai.spend()["tokens"] == 5
        # The whole point of the flag: 5 here is the price list, not the bill.
        assert newsapi_ai.spend()["tokens_are_measured"] is False

    def test_a_refusal_is_still_read(self):
        # The remaining count arrives precisely when the API refuses, which is
        # the one moment anybody wants to read it. Guardian learned this by
        # folding headers only on success and blinding itself to its own wall.
        resp = _response(status=401, headers={"req-tokens": "5",
                                              "x-ratelimit-remaining": "0"})
        newsapi_ai._read_spend_headers(resp, fallback=5)
        assert newsapi_ai.spend()["remaining"] == 0
        assert newsapi_ai.spend()["tokens"] == 5

    def test_the_asserted_price_charges_a_whole_year_for_a_month(self):
        # Conservative on purpose: the budget check has to be safe when the
        # header is missing, and over-charging stops early where under-charging
        # overshoots.
        one_month = newsapi_ai.asserted_tokens(datetime.date(2019, 3, 1),
                                               datetime.date(2019, 3, 31))
        assert one_month == config.NEWSAPI_TOKENS_PER_ARCHIVE_YEAR
        three_years = newsapi_ai.asserted_tokens(datetime.date(2015, 1, 1),
                                                 datetime.date(2017, 12, 31))
        # Their own worked example: 2015 to 2017 is 15 tokens.
        assert three_years == 15


class TestTheBudgetIsACapNotASuggestion:
    """Checked before the call, against what the call is expected to cost. A
    budget checked afterwards lets the run overshoot by exactly the call that
    broke it, which on a source billing 5 tokens a page is the difference
    between a cap and a hope."""

    def test_the_call_that_would_breach_is_never_made(self, monkeypatch):
        monkeypatch.setenv("NEWSAPI_AI_API_KEY", "k")

        def must_not_run(*_a, **_k):
            raise AssertionError("the budget was checked after the spend")

        monkeypatch.setattr(newsapi_ai, "_post", must_not_run)
        spent = [config.NEWSAPI_TOKEN_BUDGET - 1]
        with pytest.raises(newsapi_ai.TokenBudgetExhausted, match="budget"):
            newsapi_ai._page("Portugal", datetime.date(2019, 1, 1),
                             datetime.date(2019, 12, 31), 1, spent)

    def test_a_run_with_room_left_proceeds_and_charges_what_it_was_billed(
            self, monkeypatch):
        # Patched at the wire rather than at `_post`, deliberately: the spend
        # is folded in by `_read_spend_headers` *inside* `_post`, so a fake
        # that replaces `_post` reports a run that cost nothing. The first
        # version of this test did exactly that and passed while measuring
        # neither the call nor the charge.
        monkeypatch.setenv("NEWSAPI_AI_API_KEY", "k")
        monkeypatch.setattr(newsapi_ai.requests, "post", lambda *_a, **_k: _response(
            headers={"req-tokens": "5"},
            payload={"articles": {"results": [], "totalResults": 0, "pages": 1}}))
        spent = [0]
        newsapi_ai._page("Portugal", datetime.date(2019, 1, 1),
                         datetime.date(2019, 12, 31), 1, spent)
        assert spent[0] == 5


class TestA401IsAWallNotAnAuthError:
    """Event Registry answers 401 when the account's tokens run out, which is
    the same status a bad key gets. Retrying either is wrong, and every attempt
    is a billable search spent against the allowance that just ran out. The
    Guardian learned the general form on 2026-08-15, when one wall became 46
    identical `request error` checkpoints in fifteen minutes."""

    def test_the_refusal_is_not_retried(self, monkeypatch):
        monkeypatch.setenv("NEWSAPI_AI_API_KEY", "k")
        monkeypatch.setattr(newsapi_ai.time, "sleep", lambda *_a: None)
        attempts = []

        def refuse(_url, **kwargs):
            attempts.append(kwargs)
            return _response(status=401, headers={"req-tokens": "5"},
                             payload={"error": "token limit reached"})

        monkeypatch.setattr(newsapi_ai.requests, "post", refuse)
        with pytest.raises(newsapi_ai.QuotaExhausted, match="401"):
            newsapi_ai._post({"action": "getArticles"}, 5)
        assert len(attempts) == 1, "a refusal must not be retried five times"

    def test_the_harvest_checkpoints_the_wall_and_returns(self, monkeypatch, caplog):
        monkeypatch.setenv("NEWSAPI_AI_API_KEY", "k")
        monkeypatch.setattr(newsapi_ai.store, "completed_windows", lambda *_a: set())
        written = []
        monkeypatch.setattr(newsapi_ai.store, "write_checkpoint",
                            lambda *a, **k: written.append(k))

        def wall(*_a, **_k):
            raise newsapi_ai.QuotaExhausted("spent")

        monkeypatch.setattr(newsapi_ai, "harvest_window", wall)

        with caplog.at_level(logging.WARNING):
            # Returns rather than raising: a spent allowance is a scheduling
            # fact, and the run has to leave a resumable ledger behind.
            assert newsapi_ai.harvest(roster=["PT"], since="2019-01-01",
                                      until="2019-12-31") == 0
        assert written and written[0]["note"] == "quota exhausted"
        assert written[0]["status"] == "failed"
        assert "Re-run to resume" in caplog.text

    def test_a_transient_error_is_still_retried(self, monkeypatch):
        # The other half. A 503 is not a wall, and treating it as one would
        # abandon a window the service was merely too busy to serve.
        monkeypatch.setenv("NEWSAPI_AI_API_KEY", "k")
        monkeypatch.setattr(newsapi_ai.time, "sleep", lambda *_a: None)
        attempts = []

        def busy(_url, **kwargs):
            attempts.append(kwargs)
            return _response(status=503)

        monkeypatch.setattr(newsapi_ai.requests, "post", busy)
        with pytest.raises(Exception):
            newsapi_ai._post({"action": "getArticles"}, 5)
        assert len(attempts) == 5


class TestPublishedAtIsLoadBearing:
    """It feeds the no-future invariant. A naive string reaches Postgres to be
    read in the session's timezone, which is a backfill that silently re-dates
    itself by the deploy region's offset — an error that reads as data."""

    def test_a_missing_date_drops_the_article_rather_than_defaulting(self):
        assert newsapi_ai.to_item({"url": "https://p.test/a", "title": "t"}) is None

    def test_a_missing_url_drops_the_article(self):
        assert newsapi_ai.to_item({"dateTimePub": "2019-06-01T09:00:00Z"}) is None

    def test_a_naive_stamp_is_marked_utc_not_left_bare(self):
        item = newsapi_ai.to_item(_article(published="2019-06-01T09:00:00"))
        assert item["published"].endswith("Z")

    def test_an_offset_that_is_already_there_is_left_alone(self):
        item = newsapi_ai.to_item(_article(published="2019-06-01T09:00:00+01:00"))
        assert item["published"] == "2019-06-01T09:00:00+01:00"

    def test_the_publishers_stamp_wins_over_the_index_stamp(self):
        # `dateTime` is when the index saw the article; `dateTimePub` is when it
        # was published. Only the second is what a snapshot window means.
        item = newsapi_ai.to_item({"url": "https://p.test/a",
                                   "dateTime": "2019-07-02T00:00:00Z",
                                   "dateTimePub": "2019-06-01T09:00:00Z"})
        assert item["published"] == "2019-06-01T09:00:00Z"

    def test_the_store_refuses_what_the_adapter_let_through(self):
        # The backstop: `article_row` raises rather than writing a NULL date.
        from backend.data_upsert import store as _store
        bad = core.normalize_item(link="https://p.test/a", published="not a date")
        with pytest.raises(ValueError, match="publication date"):
            _store.article_row(bad, country_iso2="PT",
                               source_system="newsapi_ai", body_status="pending")


class TestTheWindowsAreThePriceTag:
    """Archive searches bill per searched year, so window width is a cost as
    well as a shape. Calendar boundaries rather than rolling spans, for the
    reason `guardian.year_windows` gives: a resumed harvest must ask the same
    windows as the run before it or its checkpoints mean nothing."""

    def test_a_year_is_one_window(self):
        got = newsapi_ai.windows(datetime.date(2019, 1, 1),
                                 datetime.date(2019, 12, 31), "year")
        assert got == [(datetime.date(2019, 1, 1), datetime.date(2019, 12, 31))]

    def test_a_year_is_twelve_monthly_windows(self):
        got = newsapi_ai.windows(datetime.date(2019, 1, 1),
                                 datetime.date(2019, 12, 31), "month")
        assert len(got) == 12
        assert got[0] == (datetime.date(2019, 1, 1), datetime.date(2019, 1, 31))
        assert got[-1] == (datetime.date(2019, 12, 1), datetime.date(2019, 12, 31))

    def test_windows_are_clamped_to_the_request(self):
        got = newsapi_ai.windows(datetime.date(2019, 3, 15),
                                 datetime.date(2019, 5, 10), "month")
        assert got[0][0] == datetime.date(2019, 3, 15)
        assert got[-1][1] == datetime.date(2019, 5, 10)

    def test_february_does_not_lose_a_day(self):
        got = newsapi_ai.windows(datetime.date(2020, 2, 1),
                                 datetime.date(2020, 2, 29), "month")
        assert got == [(datetime.date(2020, 2, 1), datetime.date(2020, 2, 29))]

    def test_an_unknown_granularity_is_refused(self):
        with pytest.raises(ValueError, match="granularity"):
            newsapi_ai.windows(datetime.date(2019, 1, 1),
                               datetime.date(2019, 12, 31), "fortnight")

    def test_the_concept_uri_is_derived_not_looked_up(self):
        # A lookup would be a billed call per country to learn a string that
        # never changes.
        assert newsapi_ai.concept_uri("Portugal").endswith("/Portugal")
        assert newsapi_ai.concept_uri("South Korea").endswith("/South_Korea")


class TestABadKeyIsNotAQuotaStop:
    """Both arrive as 401 and they want opposite handling. Measured against the
    live endpoint on 2026-08-28: an unrecognised key returns a bare-text 401
    with no JSON and no billing headers, reading "The apiKey that was provided
    is not recognized as a valid key for a user."

    Conflated, a bad key would checkpoint every window `quota exhausted`, return
    0 like any ordinary wall, and print "re-run to resume" about a run that can
    never succeed."""

    @staticmethod
    def _refuse(monkeypatch, text):
        import requests
        resp = requests.Response()
        resp.status_code = 401
        resp._content = text.encode()
        monkeypatch.setenv("NEWSAPI_AI_API_KEY", "k")
        monkeypatch.setattr(newsapi_ai.time, "sleep", lambda *_a: None)
        monkeypatch.setattr(newsapi_ai.requests, "post", lambda *_a, **_k: resp)

    def test_an_unrecognised_key_raises_bad_key(self, monkeypatch):
        self._refuse(monkeypatch, "The apiKey that was provided is not "
                                  "recognized as a valid key for a user.")
        with pytest.raises(newsapi_ai.BadKey, match="dashboard"):
            newsapi_ai._post({"action": "getArticles"}, 5)

    def test_a_spent_allowance_is_still_a_quota_stop(self, monkeypatch):
        self._refuse(monkeypatch, "Your account has reached the limit of tokens")
        with pytest.raises(newsapi_ai.QuotaExhausted, match="allowance spent"):
            newsapi_ai._post({"action": "getArticles"}, 5)

    def test_a_bad_key_is_not_a_quota_stop(self, monkeypatch):
        # The distinction that matters: BadKey must not be catchable as the
        # thing the harvest treats as resumable.
        self._refuse(monkeypatch, "not recognized as a valid key")
        assert not issubclass(newsapi_ai.BadKey, newsapi_ai.QuotaExhausted)

    def test_the_harvest_stops_dead_rather_than_checkpointing_every_window(
            self, monkeypatch):
        monkeypatch.setenv("NEWSAPI_AI_API_KEY", "k")
        monkeypatch.setattr(newsapi_ai.store, "completed_windows", lambda *_a: set())
        written = []
        monkeypatch.setattr(newsapi_ai.store, "write_checkpoint",
                            lambda *a, **k: written.append(k))

        def bad(*_a, **_k):
            raise newsapi_ai.BadKey("nope")

        monkeypatch.setattr(newsapi_ai, "harvest_window", bad)
        with pytest.raises(newsapi_ai.BadKey):
            newsapi_ai.harvest(roster=["PT", "TR"], since="2019-01-01",
                               until="2019-12-31")
        assert written == [], "a misconfiguration must not write checkpoints"
