"""Tests for the source-agnostic half of the news path.

The point of ``news_fetching.core`` is that the live run and every historical
harvester share one implementation of everything downstream of "we have a list
of article items". So the tests that matter here are the ones that would fail if
a second copy appeared, or if the two paths started disagreeing about what an
article item is.

The theme-floor arithmetic is deliberately NOT re-tested — ``TestNoSecondCopy``
asserts that ``article_enrichment._select_with_theme_floor`` *is* the core
function, so the twenty existing cases in ``test_article_enrichment`` already
cover it and cannot silently start covering a fork.

No network, no database, no model.
"""

import pytest

from backend.utils.news_fetching import article_enrichment as ae
from backend.utils.news_fetching import core


class TestNoSecondCopy:
    """The reuse rule, enforced by identity rather than by hope.

    If someone re-inlines one of these into the live module, these fail — which
    is the only cheap way to catch the drift the whole extraction exists to
    prevent."""

    def test_selection_is_the_shared_one(self):
        assert ae._select_with_theme_floor is core.select_with_theme_floor
        assert ae._by_relevance is core.by_relevance

    def test_headline_key_is_the_shared_one(self):
        assert ae._headline_key is core.headline_key

    def test_query_themes_are_the_shared_ones(self):
        assert ae._QUERY_THEMES is core.THEME_QUERIES

    def test_every_ledger_still_has_a_query(self):
        assert set(core.THEME_QUERIES) == {
            "friction", "order", "security", "information", "edge", "broad"}

    def test_broad_runs_last(self):
        # First-seen-wins dedupe means the catch-all must not get to claim a
        # story a specific theme would have tagged.
        assert list(core.THEME_QUERIES)[-1] == core.BROAD_THEME


class TestDedupeKey:
    def test_publisher_link_wins(self):
        item = {"publisher_link": "https://real.test/a", "link": "https://news.google.com/x"}
        assert core.dedupe_key(item) == "https://real.test/a"

    def test_falls_back_to_link(self):
        assert core.dedupe_key({"link": "https://x.test/a"}) == "https://x.test/a"

    def test_whitespace_is_stripped(self):
        # The three call sites disagreed about this before the extraction.
        assert core.dedupe_key({"link": "  https://x.test/a\n"}) == "https://x.test/a"

    def test_nothing_to_key_on_is_empty_not_an_error(self):
        assert core.dedupe_key({}) == ""
        assert core.dedupe_key({"publisher_link": None, "link": None}) == ""

    def test_two_sources_of_one_story_agree(self):
        # A Guardian row and a GDELT stub for the same article: the adapter sets
        # publisher_link to its only URL, so the keys collide and the store's
        # body-wins rule can do its job.
        guardian = core.normalize_item(link="https://g.test/story", text="body")
        gdelt = core.normalize_item(link="https://g.test/story")
        assert core.dedupe_key(guardian) == core.dedupe_key(gdelt)


class TestHeadlineKey:
    def test_syndicated_copies_collapse(self):
        titles = [
            "Brazil government now expects 2026 inflation to be above central bank's target - Yahoo",
            "Brazil Government Now Expects 2026 Inflation to Be Above Central Bank's Target - Reuters",
        ]
        assert len({core.headline_key(t) for t in titles}) == 1

    def test_missing_title_is_falsy_not_an_error(self):
        assert core.headline_key(None) == "" and core.headline_key("") == ""


class TestClassifyThemes:
    """The fallback tagger — the one genuinely new behavior in the extraction.

    Live items are tagged by which query returned them. Historical items arrive
    with no query provenance at all, so this is what decides which theme's floor
    slot they compete for."""

    @pytest.mark.parametrize("theme, title", [
        ("friction",    "New customs regulation raises the permit fee"),
        ("order",       "Parliament dissolved ahead of a snap election"),
        ("security",    "Military attack on the border escalates the conflict"),
        ("information", "Journalist jailed as censorship of the judiciary widens"),
        ("edge",        "Startup founders and skilled workers leaving for Berlin"),
    ])
    def test_each_specific_theme_is_recognised(self, theme, title):
        assert core.classify_themes(title, "")[0] == theme

    def test_no_signal_falls_back_to_broad(self):
        assert core.classify_themes("Lisbon wins the cup final", "") == [core.BROAD_THEME]

    def test_never_returns_empty(self):
        # Callers take [0] unguarded, so an empty list would be an AttributeError
        # in the middle of a harvest.
        assert core.classify_themes(None, None) == [core.BROAD_THEME]

    def test_strongest_match_leads(self):
        title = "Election protest over a new tax"
        # 'election' + 'protest' beat 'tax' on count.
        assert core.classify_themes(title, "")[:2] == ["order", "friction"]

    def test_the_body_is_read_too(self):
        assert core.classify_themes("A quiet Tuesday", "The central bank raised interest rates.")[0] == "order"

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


class TestNormalizeItem:
    def test_shape_is_always_complete(self):
        item = core.normalize_item(title="t", link="https://x.test/a")
        assert set(core._ITEM_KEYS) <= set(item)

    def test_publisher_link_defaults_to_link(self):
        # Every historical source hands back one URL. Defaulting here is what
        # makes dedupe_key behave identically for all of them.
        assert core.normalize_item(link="https://x.test/a")["publisher_link"] == "https://x.test/a"

    def test_explicit_publisher_link_is_kept(self):
        item = core.normalize_item(link="https://news.google.com/x", publisher_link="https://real.test/a")
        assert item["publisher_link"] == "https://real.test/a"

    def test_extra_fields_pass_through(self):
        assert core.normalize_item(link="https://x.test/a", abstract="abs")["abstract"] == "abs"

    def test_theme_is_none_until_someone_tags_it(self):
        assert core.normalize_item(link="https://x.test/a")["_theme"] is None


class TestValidateItem:
    def test_a_normalized_item_passes(self):
        assert core.validate_item(core.normalize_item(link="https://x.test/a"))

    def test_a_missing_canonical_key_is_caught(self):
        item = core.normalize_item(link="https://x.test/a")
        del item["published"]
        with pytest.raises(ValueError, match="canonical keys"):
            core.validate_item(item)

    def test_no_url_is_caught(self):
        with pytest.raises(ValueError, match="neither publisher_link nor link"):
            core.validate_item(core.normalize_item(title="orphan"))

    def test_a_non_dict_is_a_type_error(self):
        with pytest.raises(TypeError):
            core.validate_item("not an item")


class TestEnsureTheme:
    def test_query_provenance_is_never_overwritten(self):
        item = core.normalize_item(link="https://x.test/a", title="election", theme="broad")
        assert core.ensure_theme(item)["_theme"] == "broad"

    def test_a_missing_theme_is_classified(self):
        item = core.normalize_item(link="https://x.test/a", title="Parliament dissolved", text="")
        assert core.ensure_theme(item)["_theme"] == "order"

    def test_it_mutates_in_place(self):
        item = core.normalize_item(link="https://x.test/a", title="new tax on imports")
        assert core.ensure_theme(item) is item


class TestExtractBody:
    def test_a_real_page_yields_its_text(self):
        html = ("<html><body><article><p>" + "The central bank raised rates today. " * 12
                + "</p></article></body></html>")
        assert "central bank raised rates" in core.extract_body(html)

    def test_nothing_extractable_is_empty_not_an_error(self):
        assert core.extract_body("<html><body></body></html>") == ""

    def test_empty_input_is_empty(self):
        assert core.extract_body(None) == "" and core.extract_body("") == ""

    def test_a_page_that_defeats_the_parser_does_not_raise(self):
        # A body that raises must never stop the surrounding batch — recovery
        # runs over thousands of archive captures, many of them junk.
        assert core.extract_body(b"\x00\x01not html") == ""
