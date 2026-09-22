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

# ---------------------------------------------------------------------------
# Stage one: the digest engine.
# ---------------------------------------------------------------------------

from backend.llm import digest_engine  # noqa: E402


class _Raw:
    def __init__(self, finish_reason="stop", usage=None):
        self.response_metadata = {"finish_reason": finish_reason}
        self.usage_metadata = usage or {"input_tokens": 100, "output_tokens": 20}


class _FakeStructured:
    """Replays a scripted list of (finish_reason, parsed) responses."""

    def __init__(self, script):
        self.script = list(script)
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append(messages[0].content)
        finish, parsed = self.script.pop(0)
        if isinstance(parsed, Exception):
            raise parsed
        return {"raw": _Raw(finish), "parsed": parsed}


def _digest(what="Rates rose."):
    return {"what_happened": what, "institutions": ["Banco de Portugal"],
            "direction": "deteriorating", "numbers": ["inflation 4.2%"]}


class TestDigestPrompt:
    def test_the_version_is_the_prompt_s_own_hash(self):
        from backend.util.hashing import content_hash

        assert digest_engine.DIGEST_PROMPT_VERSION == content_hash(digest_engine.DIGEST_PROMPT)

    def test_the_prompt_forbids_filling_gaps_from_outside_knowledge(self):
        """Anything invented here reaches the scorer indistinguishable from
        reporting."""
        p = digest_engine.DIGEST_PROMPT
        assert "not stated" in p
        assert "extraction engine, not an analyst" in p

    def test_the_output_ceiling_is_a_few_times_the_schema_not_the_default(self):
        """A digest model left at its default ceiling can burn thousands of
        tokens producing a two-hundred-token object."""
        assert digest_engine.MAX_OUTPUT_TOKENS <= 1000


class TestBodyStatus:
    def test_an_article_with_no_body_is_title_only(self):
        assert digest_engine.body_status_for({"text": ""}, full_text=False)[0] == "title-only"

    def test_an_article_that_was_only_digested_says_so(self):
        got = digest_engine.body_status_for({"text": "body"}, full_text=False)
        assert got[0] == "digest-only"

    def test_a_short_body_read_in_full_is_full(self):
        status, clipped, original = digest_engine.body_status_for(
            {"text": "short body"}, full_text=True
        )
        assert (status, clipped) == ("full", False)
        assert original == len("short body")

    def test_a_long_body_read_in_full_is_clipped_and_says_how_long_it_was(self):
        body = "x" * (digest_engine.BODY_CAP_CHARS + 500)
        status, clipped, original = digest_engine.body_status_for(
            {"text": body}, full_text=True
        )
        assert (status, clipped) == ("clipped", True)
        assert original == digest_engine.BODY_CAP_CHARS + 500


class TestDigestRetry:
    def test_a_clean_digest_takes_one_call(self):
        fake = _FakeStructured([("stop", _digest())])
        got = digest_engine.digest_text("body", structured=fake)
        assert got["what_happened"] == "Rates rose."
        assert "truncated_retry" not in got
        assert len(fake.prompts) == 1

    def test_hitting_the_ceiling_retries_once_on_a_shorter_slice(self):
        """The failure this bounds: an ordinary input running to the output
        ceiling and producing nothing."""
        fake = _FakeStructured([("length", None), ("stop", _digest())])
        body = "y" * 20_000
        got = digest_engine.digest_text(body, structured=fake)
        assert got["truncated_retry"] is True
        assert len(fake.prompts) == 2
        assert len(fake.prompts[1]) < len(fake.prompts[0])

    def test_the_retry_reads_the_first_six_thousand_characters(self):
        fake = _FakeStructured([("length", None), ("stop", _digest())])
        body = "z" * 20_000
        digest_engine.digest_text(body, structured=fake)
        assert fake.prompts[1].count("z") == digest_engine.RETRY_CHARS

    def test_two_failures_give_up_rather_than_inventing_a_digest(self):
        fake = _FakeStructured([("length", None), ("length", None)])
        assert digest_engine.digest_text("body", structured=fake) is None

    def test_a_raising_call_is_retried_then_given_up_on(self):
        fake = _FakeStructured([("stop", RuntimeError("boom")), ("stop", _digest())])
        assert digest_engine.digest_text("body", structured=fake)["what_happened"]

    def test_usage_is_metered_on_every_attempt(self):
        m = usage.Meter()
        fake = _FakeStructured([("length", None), ("stop", _digest())])
        digest_engine.digest_text("body", structured=fake, meter=m)
        assert m.calls == 2


class TestDigestArticles:
    def test_a_cached_digest_is_served_without_a_call(self, monkeypatch):
        art = _article("http://x/1", "T")
        art["text"] = "the body"
        from backend.util.hashing import content_hash

        monkeypatch.setattr(
            digest_engine.store,
            "read_artifacts",
            lambda hashes, **kw: {content_hash("the body"): _digest()},
        )

        def boom(*a, **kw):
            raise AssertionError("constructed a client for a cached digest")

        monkeypatch.setattr(digest_engine, "ChatOpenAI", boom)

        out = digest_engine.digest_articles([art])
        assert out["counts"]["cached"] == 1
        assert out["counts"]["generated"] == 0
        assert out["digests"]["http://x/1"]["cached"] is True

    def test_an_article_with_no_body_is_counted_not_digested(self, monkeypatch):
        def boom(*a, **kw):
            raise AssertionError("looked up a digest for an article with no body")

        monkeypatch.setattr(digest_engine.store, "read_artifacts", boom)
        out = digest_engine.digest_articles([_article("http://x/1", "T")])
        assert out["counts"]["no_body"] == 1
        assert out["digests"] == {}


# ---------------------------------------------------------------------------
# The economics block — indicators by ledger, with trajectory and vintage.
# ---------------------------------------------------------------------------

from backend.llm import payload as payload_mod  # noqa: E402
from backend.util import constants as consts  # noqa: E402


def _panel(**cols):
    out = {}
    for col, series in cols.items():
        latest_year = max(series)
        out[col] = {
            "latest": series[latest_year],
            "latest_year": latest_year,
            "series": series,
        }
    return out


class TestEconomicsBlock:
    def test_indicators_arrive_grouped_by_ledger(self):
        """The score is four ledger judgements; a registry-ordered list asks the
        model to do the sorting itself on every call."""
        block = payload_mod.build_economics_block(
            _panel(INFLATION={2023: 4.0, 2024: 7.2})
        )
        assert set(block["ledgers"]) == set(consts.LEDGERS)
        friction = block["ledgers"]["friction"]["indicators"]
        assert [i["code"] for i in friction] == ["FP.CPI.TOTL.ZG"]

    def test_each_ledger_carries_the_question_it_is_asking(self):
        block = payload_mod.build_economics_block({})
        assert "how well does it convert" in block["ledgers"]["friction"]["question"]

    def test_the_direction_reaches_the_payload_in_words(self):
        block = payload_mod.build_economics_block(
            _panel(INFLATION={2020: 2.0, 2021: 3.0, 2022: 5.0, 2023: 7.0, 2024: 9.0})
        )
        entry = block["ledgers"]["friction"]["indicators"][0]
        assert entry["direction"] == "rising"
        assert entry["history"][0]["year"] == 2020

    def test_a_value_carries_when_it_became_knowable_not_when_we_fetched_it(self):
        block = payload_mod.build_economics_block(_panel(INFLATION={2024: 7.2}))
        entry = block["ledgers"]["friction"]["indicators"][0]
        assert entry["as_of"] > "2024-12-31"
        assert entry["as_of_scheme"] == "publication-lag-estimate"

    def test_staleness_is_measured_against_the_series_own_cadence(self):
        import datetime as d

        block = payload_mod.build_economics_block(
            _panel(INFLATION={2019: 1.0}), today=d.date(2026, 9, 22)
        )
        entry = block["ledgers"]["friction"]["indicators"][0]
        assert entry["stale_for_its_cadence"] is True

    def test_an_indicator_with_no_observation_is_omitted_and_named(self):
        """Never a zero and never a padded null — a zero reads as reassurance."""
        block = payload_mod.build_economics_block(_panel(INFLATION={2024: 7.2}))
        dropped = {d["code"] for d in block["resolution"]["dropped"]}
        assert "SL.UEM.TOTL.ZS" in dropped
        codes_in_payload = {
            i["code"]
            for led in block["ledgers"].values()
            for i in led["indicators"]
        }
        assert "SL.UEM.TOTL.ZS" not in codes_in_payload

    def test_every_dropped_indicator_carries_a_reason(self):
        block = payload_mod.build_economics_block({})
        assert block["resolution"]["dropped"]
        for d in block["resolution"]["dropped"]:
            assert d["reason"]
            assert d["ledger"] in consts.LEDGERS
            assert d["source"]

    def test_an_empty_ledger_is_impossible_to_miss(self):
        """Stated in the payload as well as in the census: the model is told the
        evidence is absent rather than inferring it from an empty list."""
        block = payload_mod.build_economics_block(_panel(INFLATION={2024: 7.2}))
        order = block["ledgers"]["order"]
        assert order["indicators"] == []
        assert "do not read the absence as a good result" in order["note"]
        assert "order" in block["resolution"]["empty_ledgers"]

    def test_the_resolution_report_counts_expected_against_resolved(self):
        block = payload_mod.build_economics_block(_panel(INFLATION={2024: 7.2}))
        res = block["resolution"]
        assert res["expected_by_ledger"]["friction"] == 4
        assert res["resolved_by_ledger"]["friction"] == 1
        assert res["by_source"]["World Bank WDI"]["expected"] >= 1

    def test_curated_values_reach_the_payload(self):
        """RSF and PISA are the reason information and edge are not one-indicator
        ledgers; verified by consumption, not by row count."""
        block = payload_mod.build_economics_block(
            _panel(STAT_PERFORMANCE={2023: 80.0}),
            curated={
                "RSF.PRESS.SCORE": {"value": 76.0, "period": 2026,
                                    "as_of": "2026-05-03", "series": {2026: 76.0}},
                "OECD.PISA.MEAN": {"value": 492.0, "period": 2022,
                                   "as_of": "2023-12-05", "series": {2022: 492.0}},
            },
        )
        info = {i["code"] for i in block["ledgers"]["information"]["indicators"]}
        edge = {i["code"] for i in block["ledgers"]["edge"]["indicators"]}
        assert info == {"IQ.SPI.OVRL", "RSF.PRESS.SCORE"}
        assert "OECD.PISA.MEAN" in edge
        assert block["resolution"]["as_of_schemes"]["source-published"] == 2

    def test_the_census_can_tell_measured_dates_from_derived_ones(self):
        block = payload_mod.build_economics_block(
            _panel(INFLATION={2024: 7.2}),
            curated={"RSF.PRESS.SCORE": {"value": 76.0, "period": 2026,
                                         "as_of": "2026-05-03", "series": {}}},
        )
        schemes = block["resolution"]["as_of_schemes"]
        assert schemes["publication-lag-estimate"] == 1
        assert schemes["source-published"] == 1
