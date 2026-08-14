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
import inspect
import re

import pytest

from backend.utils.history import config, snapshot_select as sel, wayback
from backend.utils.history.adapters import gdelt, guardian, nyt
from backend.utils.news_fetching import article_enrichment as ae
from backend.utils.news_fetching import article_ranking, core, source_filter

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

    MODULES = (guardian, gdelt, nyt)
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
        # NYT is exempt from the second half: the archive endpoint takes a year
        # and a month and returns the whole paper, so its themes come from
        # `store.article_row`'s classifier. It is still forbidden a theme list.
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
        from backend.utils.ai import client as ai_client
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
