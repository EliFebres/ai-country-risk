"""
Tests for backend/llm.

Consumer-side: the gate's label is asserted to *exclude* an article from what
the scorer reads, not merely to have been produced. A classifier whose verdict
nothing acts on is the exact failure this gate exists to avoid.

No network and no model call. `ChatOpenAI` is replaced at the module boundary,
and a test that would construct one fails loudly rather than reaching OpenAI.
"""

import pytest

from backend.llm import relevance
from backend.util import usage


def _article(url, title, *, publisher="Reuters", published="2026-09-20T00:00:00Z",
             themes=("broad",)):
    return {
        "publisher_link": url,
        "link": url,
        "title": title,
        "source": publisher,
        "published": published,
        "summary": title,
        "themes": list(themes),
    }


def _labels(pairs, iso2="PT"):
    """Build a labels map: [(article, label, ledgers, is_event), ...]."""
    out = {}
    for article, label, ledgers, is_event in pairs:
        out[relevance.relevance_key(article, iso2)] = {
            "label": label,
            "reason": "because",
            "ledgers": list(ledgers),
            "is_structural_event": is_event,
        }
    return out


class TestPromptVersion:
    def test_the_version_is_the_prompt_s_own_hash(self):
        """Editing the prompt and forgetting to bump a version number is how a
        cache serves answers to a question nobody is asking any more."""
        from backend.util.hashing import content_hash

        assert relevance.RELEVANCE_PROMPT_VERSION == content_hash(relevance.RELEVANCE_PROMPT)

    def test_an_edited_prompt_produces_a_different_version(self):
        from backend.util.hashing import content_hash

        edited = relevance.RELEVANCE_PROMPT + "\nOne more rule."
        assert content_hash(edited) != relevance.RELEVANCE_PROMPT_VERSION

    def test_the_prompt_carries_the_cases_that_draw_the_line(self):
        p = relevance.RELEVANCE_PROMPT
        for case in ("school shooting", "corruption arrest", "factory closing",
                     "coup", "obituaries"):
            assert case in p


class TestRelevanceKey:
    def test_the_same_article_has_a_different_key_per_country(self):
        """A German election story that mentions Portugal once is structural for
        Germany and irrelevant for Portugal, so one row per text would be wrong."""
        a = _article("http://x/1", "German coalition collapses")
        assert relevance.relevance_key(a, "DE") != relevance.relevance_key(a, "PT")

    def test_the_same_article_and_country_is_stable(self):
        a = _article("http://x/1", "Rates rise")
        assert relevance.relevance_key(a, "PT") == relevance.relevance_key(dict(a), "PT")


class TestSelectionEligibility:
    def test_an_incident_never_reaches_the_scorer(self):
        """The consumer-side assertion: the label is acted on."""
        good = _article("http://x/1", "Prosecutions collapsing")
        bad = _article("http://x/2", "School shooting in Porto")
        labels = _labels([
            (good, "structural", ["order"], False),
            (bad, "incident", [], False),
        ])
        out = relevance.select([good, bad], labels, "PT")
        assert [a["title"] for a in out["selected"]] == ["Prosecutions collapsing"]

    def test_nothing_tops_up_from_ineligible_articles(self):
        """If six qualify, six are scored. This is the padding bug, refused."""
        good = [_article(f"http://g/{i}", f"Structural {i}") for i in range(6)]
        bad = [_article(f"http://b/{i}", f"Match report {i}") for i in range(40)]
        labels = _labels(
            [(a, "structural", ["friction"], False) for a in good]
            + [(a, "irrelevant", [], False) for a in bad]
        )
        out = relevance.select(good + bad, labels, "PT")
        assert out["counts"]["selected"] == 6
        assert out["counts"]["budget"] == 20

    def test_an_article_the_gate_never_answered_on_is_not_admitted(self):
        """A failed call must not read as a pass."""
        a = _article("http://x/1", "Unclassified")
        out = relevance.select([a], {}, "PT")
        assert out["selected"] == []
        assert out["rejected"][0]["label"] == "unclassified"

    def test_rejections_are_recorded_with_their_reason(self):
        """Without the record nobody can ask later whether the gate judged well."""
        bad = _article("http://x/2", "Benfica win 3-1")
        labels = _labels([(bad, "irrelevant", [], False)])
        out = relevance.select([bad], labels, "PT")
        r = out["rejected"][0]
        assert r["title"] == "Benfica win 3-1"
        assert r["label"] == "irrelevant"
        assert r["reason"] == "because"
        assert r["url"] == "http://x/2"


class TestSelectionOrder:
    def test_a_structural_event_is_read_first(self):
        """A coup or a default takes the full-text slots before anything else."""
        trend = _article("http://x/1", "Inflation easing over four quarters",
                         published="2026-09-21T00:00:00Z")
        coup = _article("http://x/2", "Government falls",
                        published="2026-09-01T00:00:00Z")
        labels = _labels([
            (trend, "structural", ["friction"], False),
            (coup, "structural", ["order"], True),
        ])
        out = relevance.select([trend, coup], labels, "PT")
        assert out["selected"][0]["title"] == "Government falls"

    def test_the_budget_spreads_across_ledgers_rather_than_one_story(self):
        """Twenty newest on a busy country can be eight versions of one event."""
        friction = [
            _article(f"http://f/{i}", f"Tax story {i}", published=f"2026-09-{20-i:02d}T00:00:00Z")
            for i in range(10)
        ]
        edge = [
            _article(f"http://e/{i}", f"Education story {i}", published="2026-08-01T00:00:00Z")
            for i in range(3)
        ]
        labels = _labels(
            [(a, "structural", ["friction"], False) for a in friction]
            + [(a, "structural", ["edge"], False) for a in edge]
        )
        out = relevance.select(friction + edge, labels, "PT", budget=6)
        assert out["per_ledger"]["edge"] == 3, out["per_ledger"]
        assert out["per_ledger"]["friction"] == 3

    def test_an_empty_ledger_is_skipped_and_counted_as_zero(self):
        """Round-robin is not a floor: it never admits anything ineligible."""
        a = _article("http://x/1", "Only friction here")
        labels = _labels([(a, "structural", ["friction"], False)])
        out = relevance.select([a], labels, "PT")
        assert out["per_ledger"] == {"friction": 1, "order": 0, "information": 0, "edge": 0}

    def test_a_structural_article_with_no_ledger_is_still_selectable(self):
        """Eligible is eligible; it follows the ledgered ones rather than vanishing."""
        a = _article("http://x/1", "Structural but unledgered")
        labels = _labels([(a, "structural", [], False)])
        out = relevance.select([a], labels, "PT")
        assert len(out["selected"]) == 1

    def test_one_outlet_cannot_take_the_whole_budget(self):
        many = [_article(f"http://r/{i}", f"Reuters piece {i}", publisher="Reuters")
                for i in range(5)]
        one = [_article("http://b/1", "Bloomberg piece", publisher="Bloomberg")]
        labels = _labels([(a, "structural", ["order"], False) for a in many + one])
        out = relevance.select(many + one, labels, "PT", budget=2)
        pubs = {a["source"] for a in out["selected"]}
        assert pubs == {"Reuters", "Bloomberg"}

    def test_the_order_is_deterministic_for_a_given_eligible_set(self):
        arts = [_article(f"http://x/{i}", f"Story {i}", published="2026-09-20T00:00:00Z")
                for i in range(8)]
        labels = _labels([(a, "structural", ["order"], False) for a in arts])
        first = [a["title"] for a in relevance.select(arts, labels, "PT", budget=4)["selected"]]
        again = [a["title"] for a in relevance.select(arts, labels, "PT", budget=4)["selected"]]
        assert first == again

    def test_more_eligible_articles_never_produce_fewer_selected(self):
        """The inversion the old selection bug produced, asserted against."""
        pool = []
        labels = {}
        previous = 0
        for i in range(25):
            a = _article(f"http://x/{i}", f"Story {i}",
                         published=f"2026-09-{(i % 28) + 1:02d}T00:00:00Z")
            pool.append(a)
            labels.update(_labels([(a, "structural", [relevance.LEDGERS[i % 4]], False)]))
            n = relevance.select(pool, labels, "PT")["counts"]["selected"]
            assert n >= previous, f"adding article {i} reduced selection {previous} -> {n}"
            previous = n
        assert previous == 20

    def test_the_full_text_slots_are_the_first_three_in_that_order(self):
        arts = [_article(f"http://x/{i}", f"Story {i}",
                         published=f"2026-09-{20 - i:02d}T00:00:00Z") for i in range(6)]
        labels = _labels([(a, "structural", ["order"], False) for a in arts])
        out = relevance.select(arts, labels, "PT")
        assert out["selected"][:relevance.FULL_TEXT_K] == out["selected"][:3]
        assert len(out["selected"]) == 6


class TestClassifyCaching:
    def test_a_fully_cached_country_makes_no_model_call(self, monkeypatch):
        """A same-day re-run should cost almost nothing. If it still calls, the
        cache is decoration."""
        arts = [_article(f"http://x/{i}", f"Story {i}") for i in range(3)]
        keys = [relevance.relevance_key(a, "PT") for a in arts]

        monkeypatch.setattr(
            relevance.store,
            "read_artifacts",
            lambda hashes, **kw: {
                k: {"label": "structural", "reason": "r", "ledgers": ["order"],
                    "is_structural_event": False}
                for k in keys
            },
        )

        def boom(*a, **kw):
            raise AssertionError("constructed a model client for a fully cached country")

        monkeypatch.setattr(relevance, "ChatOpenAI", boom)

        out = relevance.classify(arts, "Portugal", "PT")
        assert len(out) == 3
        assert all(v["cached"] for v in out.values())

    def test_a_cache_outage_does_not_stop_the_run(self, monkeypatch):
        def down(*a, **kw):
            raise RuntimeError("cache unavailable")

        monkeypatch.setattr(relevance.store, "read_artifacts", down)
        monkeypatch.setattr(relevance.os, "getenv", lambda *a: None)  # no key -> no calls
        out = relevance.classify([_article("http://x/1", "T")], "Portugal", "PT")
        assert out == {}

    def test_nothing_to_classify_asks_nothing(self, monkeypatch):
        def boom(*a, **kw):
            raise AssertionError("looked something up for an empty pool")

        monkeypatch.setattr(relevance.store, "read_artifacts", boom)
        assert relevance.classify([], "Portugal", "PT") == {}


class TestUsageMeter:
    def test_an_unknown_model_is_not_free(self):
        """"The run was free" is the one wrong answer a cost meter must never give."""
        with pytest.raises(KeyError, match="no price"):
            usage.cost_usd("some-model-nobody-priced", 1000, 1000)

    def test_a_projection_over_the_cap_raises_rather_than_warns(self):
        with pytest.raises(usage.BudgetExhausted):
            usage.project("big step", model="gpt-4o-2024-08-06", calls=1_000_000,
                          input_tokens_per_call=1000, output_tokens_per_call=1000,
                          cap_usd=20.0)

    def test_a_response_with_no_usage_is_counted_as_unmetered_not_as_free(self):
        m = usage.Meter()
        assert m.add_response("gpt-4o-mini-2024-07-18", {"parsed": {}}) is False
        assert m.unmetered_calls == 1
        assert m.spend_usd == 0.0

    def test_usage_is_read_from_the_raw_message(self, monkeypatch):
        class Raw:
            usage_metadata = {"input_tokens": 1000, "output_tokens": 100}

        m = usage.Meter()
        assert m.add_response("gpt-4o-mini-2024-07-18", {"raw": Raw(), "parsed": {}}) is True
        assert m.input_tokens == 1000 and m.output_tokens == 100
        assert m.spend_usd == pytest.approx(0.00021)

    def test_the_cap_is_actually_checkable(self):
        m = usage.Meter()
        m.add("gpt-4o-2024-08-06", 10_000_000, 0)
        with pytest.raises(usage.BudgetExhausted):
            m.check(20.0)


class TestDuplicateStoryReport:
    def test_the_same_story_from_two_outlets_is_reported(self):
        sel = [
            _article("http://a/1", "Central bank raises rates sharply", publisher="Reuters"),
            _article("http://b/1", "Central bank raises rates sharply", publisher="AP"),
            _article("http://c/1", "Education spending falls again", publisher="FT"),
        ]
        rep = relevance.duplicate_story_report(sel)
        assert rep["duplicated_slots"] == 1
        assert len(rep["clusters"]) == 1

    def test_distinct_stories_are_not_clustered(self):
        sel = [
            _article("http://a/1", "Central bank raises rates"),
            _article("http://b/1", "Education spending falls"),
        ]
        assert relevance.duplicate_story_report(sel)["duplicated_slots"] == 0

