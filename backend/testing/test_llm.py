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


# ---------------------------------------------------------------------------
# The census: state, the alarm, and a coverage number nobody authors.
# ---------------------------------------------------------------------------

import datetime as _date  # noqa: E402

from backend.llm import payload_health  # noqa: E402


def _econ(resolved_per_ledger=(4, 4, 2, 3), stale=0):
    """An economics block with a given per-ledger resolution."""
    ledgers, dropped = {}, []
    expected = {"friction": 4, "order": 4, "information": 2, "edge": 3}
    by_source = {"World Bank WDI": {"expected": 13, "resolved": 0}}
    n = 0
    for ledger, want in zip(consts.LEDGERS, resolved_per_ledger):
        inds = []
        for i in range(want):
            n += 1
            inds.append({
                "code": f"C{n}", "period": 2024, "as_of": "2026-07-02",
                "stale_for_its_cadence": n <= stale,
            })
        ledgers[ledger] = {"question": "?", "indicators": inds}
        for i in range(expected[ledger] - want):
            dropped.append({"code": f"D{ledger}{i}", "ledger": ledger,
                            "source": "World Bank WDI", "label": "x", "reason": "no row"})
    by_source["World Bank WDI"]["resolved"] = n
    return {
        "ledgers": ledgers,
        "resolution": {
            "expected_by_ledger": expected,
            "resolved_by_ledger": dict(zip(consts.LEDGERS, resolved_per_ledger)),
            "empty_ledgers": [l for l, c in zip(consts.LEDGERS, resolved_per_ledger) if c == 0],
            "by_source": by_source,
            "dropped": dropped,
            "as_of_schemes": {"publication-lag-estimate": n},
        },
    }


def _selected(n, status="full"):
    out = []
    for i in range(n):
        a = _article(f"http://x/{i}", f"Story {i}")
        a["body_status"] = status
        out.append(a)
    return out


class TestEvidenceCoverage:
    """It was measured self-reporting 80 with twenty articles and 80 with six."""

    def test_more_articles_read_in_full_scores_higher_than_fewer(self):
        rich = payload_health.evidence_coverage(_econ(), _selected(20), budget=20)
        thin = payload_health.evidence_coverage(_econ(), _selected(6), budget=20)
        assert rich["evidence_coverage"] > thin["evidence_coverage"]

    def test_reading_headlines_scores_lower_than_reading_articles(self):
        full = payload_health.evidence_coverage(_econ(), _selected(20, "full"), budget=20)
        thin = payload_health.evidence_coverage(
            _econ(), _selected(20, "title-only"), budget=20
        )
        assert full["evidence_coverage"] > thin["evidence_coverage"]

    def test_one_empty_ledger_costs_a_quarter_of_the_indicator_component(self):
        """A payload can be rich overall and blind in one quarter of the
        framework; averaging over all indicators would hide that."""
        whole = payload_health.evidence_coverage(_econ((4, 4, 2, 3)), _selected(20), budget=20)
        blind = payload_health.evidence_coverage(_econ((4, 4, 2, 0)), _selected(20), budget=20)
        assert whole["evidence_coverage"] > blind["evidence_coverage"]
        assert blind["components"]["indicator_resolution"] == pytest.approx(0.75)

    def test_the_components_are_stored_beside_the_value(self):
        """So the number can be argued with rather than just believed."""
        got = payload_health.evidence_coverage(_econ(), _selected(10), budget=20)
        assert set(got["components"]) == set(payload_health.COVERAGE_WEIGHTS)
        assert got["weights"] == payload_health.COVERAGE_WEIGHTS

    def test_nothing_at_all_is_zero_not_a_default(self):
        got = payload_health.evidence_coverage(_econ((0, 0, 0, 0)), [], budget=20)
        assert got["evidence_coverage"] == 0


class TestPayloadFingerprint:
    def test_the_same_evidence_fingerprints_the_same(self):
        e, s = _econ(), _selected(5)
        assert payload_health.payload_fingerprint(e, s) == \
            payload_health.payload_fingerprint(e, s)

    def test_a_different_article_set_is_a_different_fingerprint(self):
        e = _econ()
        assert payload_health.payload_fingerprint(e, _selected(5)) != \
            payload_health.payload_fingerprint(e, _selected(6))

    def test_a_changed_indicator_vintage_is_a_different_fingerprint(self):
        """A score that moved because the data was revised is not a score that
        moved because the country did."""
        a, b = _econ(), _econ()
        b["ledgers"]["friction"]["indicators"][0]["as_of"] = "2026-08-01"
        s = _selected(3)
        assert payload_health.payload_fingerprint(a, s) != \
            payload_health.payload_fingerprint(b, s)


def _census(iso2="PT", resolved=(4, 4, 2, 3), selected=None):
    return payload_health.build_census(
        iso2,
        _date.date(2026, 9, 22),
        economics=_econ(resolved),
        pool_report={"fetched": 52, "after_dedupe": 45, "stale_republications": 2,
                     "per_theme": {"broad": 10}, "duplicate_slots": 1,
                     "query_name": "Portugal"},
        gate={"selected": selected if selected is not None else _selected(12),
              "counts": {"eligible": 14, "selected": 12, "budget": 20,
                         "rejected_by_label": {"irrelevant": 20, "incident": 11}},
              "per_theme": {"broad": 6}, "per_ledger": {"friction": 5}},
        digests={"generated": 9, "cached": 3, "truncated_retry": 1, "failed": 0},
        versions={"git_sha": "abc", "seed": 42},
        budget=20,
    )


class TestCensus:
    def test_it_records_expected_against_resolved_and_names_the_dropped(self):
        c = _census()
        assert c["indicators"]["expected_by_ledger"]["friction"] == 4
        assert c["indicators"]["dropped"] == []
        thin = _census(resolved=(1, 4, 2, 3))
        assert [d["reason"] for d in thin["indicators"]["dropped"]] == ["no row"] * 3

    def test_the_rejections_themselves_are_kept_not_just_their_counts(self):
        """"Was the gate right?" has to be answerable a week later without
        recomputing a cache key."""
        c = payload_health.build_census(
            "PT", _date.date(2026, 9, 22),
            economics=_econ(),
            pool_report={"fetched": 3, "after_dedupe": 3, "per_theme": {}},
            gate={"selected": [], "counts": {"eligible": 0, "selected": 0, "budget": 20,
                                             "rejected_by_label": {"irrelevant": 1}},
                  "per_theme": {}, "per_ledger": {},
                  "rejected": [{"url": "http://x/1", "title": "Benfica win 3-1",
                                "publisher": "A Bola", "label": "irrelevant",
                                "reason": "a match report"}]},
            digests={}, versions={}, budget=20,
        )
        kept = c["articles"]["rejected"][0]
        assert kept["title"] == "Benfica win 3-1"
        assert kept["label"] == "irrelevant"
        assert kept["reason"] == "a match report"

    def test_it_records_the_whole_article_funnel(self):
        art = _census()["articles"]
        assert art["fetched"] == 52
        assert art["after_dedupe"] == 45
        assert art["passed_gate"] == 14
        assert art["selected"] == 12
        assert art["rejected_by_label"] == {"irrelevant": 20, "incident": 11}

    def test_it_separates_digests_generated_from_digests_cached(self):
        """A working cache and an empty one look identical from the outside
        unless both are counted."""
        art = _census()["articles"]
        assert art["digests_generated"] == 9
        assert art["digests_cached"] == 3
        assert art["digests_truncated_retry"] == 1

    def test_it_carries_the_versions_of_everything_that_could_move_a_score(self):
        assert _census()["versions"]["git_sha"] == "abc"
        assert _census()["versions"]["schema_violations"] == 0

    def test_an_empty_ledger_is_named_in_the_census(self):
        c = _census(resolved=(4, 4, 0, 3))
        assert c["indicators"]["empty_ledgers"] == ["information"]
        assert "LEDGER WITH NO INDICATORS" in payload_health.format_census(c)


class TestResolutionAlarm:
    """State is what is; the alarm is what changed."""

    def test_the_first_run_reports_state_and_raises_no_alarm(self):
        assert payload_health.resolution_alarm("PT", _census(), []) == []

    def test_a_country_that_always_resolved_zero_does_not_shout(self):
        """Taiwan resolving zero is expected. An alarm that fires every week
        stops being read."""
        tw = _census("TW", resolved=(0, 0, 2, 3))
        history = [{"friction": 0, "order": 0, "information": 2, "edge": 3}] * 4
        assert payload_health.resolution_alarm("TW", tw, history) == []

    def test_a_country_that_dropped_from_its_own_baseline_shouts(self):
        """Portugal going from twenty indicators to twelve is a source break."""
        now = _census("PT", resolved=(1, 4, 2, 3))
        history = [{"friction": 4, "order": 4, "information": 2, "edge": 3}] * 4
        alarms = payload_health.resolution_alarm("PT", now, history)
        assert len(alarms) == 1
        assert "friction" in alarms[0] and "source break" in alarms[0]

    def test_a_small_wobble_is_not_an_alarm(self):
        now = _census("PT", resolved=(3, 4, 2, 3))
        history = [{"friction": 4, "order": 4, "information": 2, "edge": 3}] * 4
        assert payload_health.resolution_alarm("PT", now, history) == []

    def test_the_baseline_is_a_median_so_one_bad_week_does_not_move_it(self):
        now = _census("PT", resolved=(1, 4, 2, 3))
        history = [
            {"friction": 0, "order": 4, "information": 2, "edge": 3},
            {"friction": 4, "order": 4, "information": 2, "edge": 3},
            {"friction": 4, "order": 4, "information": 2, "edge": 3},
        ]
        assert payload_health.resolution_alarm("PT", now, history)


# ---------------------------------------------------------------------------
# The validator: grammars enforce structure, not bounds.
# ---------------------------------------------------------------------------

from backend.llm import validate as validator  # noqa: E402
from backend.llm import constants as ai_consts  # noqa: E402


def _answer(**over):
    base = {
        "score_12m": 54,
        "score_3m": 57,
        "friction": 40, "order": 62, "information": 30, "edge": 71,
        "condition_flags": {f: False for f in ai_consts.CONDITION_FLAGS},
        "bullet_summary": "Fiscal position deteriorating; courts contested.",
        "subscore_evidence": {f: "because" for f in ai_consts.LEDGER_FIELDS},
        "article_scores": [{"id": "a1", "door": "cost", "bearing": 40, "note": "n"}],
    }
    base.update(over)
    return base


class TestValidator:
    def test_a_good_answer_passes_clean(self):
        got = validator.validate(_answer(), article_ids=["a1"])
        assert got.ok
        assert got["answer"]["score_12m"] == 54

    def test_a_score_outside_its_bounds_is_clamped_and_the_raw_value_kept(self):
        """A number that was silently corrected is a number nobody can audit:
        "the score was 100" reads identically whether the model said 100 or 140."""
        got = validator.validate(_answer(score_12m=140), article_ids=["a1"])
        assert got["answer"]["score_12m"] == 100
        v = [x for x in got.violations if x["field"] == "score_12m"][0]
        assert v["raw"] == 140
        assert v["clamped_to"] == 100

    def test_a_null_ledger_survives_rather_than_becoming_a_number(self):
        """A ledger with nothing to read is a statement about the evidence."""
        got = validator.validate(_answer(information=None), article_ids=["a1"])
        assert got["answer"]["information"] is None
        assert got.ok

    def test_a_missing_composite_is_a_violation_not_a_null(self):
        got = validator.validate(_answer(score_12m=None), article_ids=["a1"])
        assert any(v["field"] == "score_12m" for v in got.violations)

    def test_an_answer_about_an_article_that_was_never_sent_is_caught(self):
        got = validator.validate(
            _answer(article_scores=[{"id": "a99", "door": None, "bearing": 5, "note": ""}]),
            article_ids=["a1"],
        )
        assert any(v["problem"] == "unknown article" for v in got.violations)

    def test_an_article_the_model_never_scored_is_caught(self):
        """Silent, and the reason articles eleven to twenty used to enter Top-3
        selection with an impact of zero."""
        got = validator.validate(_answer(), article_ids=["a1", "a2", "a3"])
        unscored = [v for v in got.violations if v["problem"] == "article not scored"]
        assert {v["raw"] for v in unscored} == {"a2", "a3"}

    def test_a_duplicate_article_score_is_caught(self):
        rows = [{"id": "a1", "door": None, "bearing": 5, "note": ""}] * 2
        got = validator.validate(_answer(article_scores=rows), article_ids=["a1"])
        assert any(v["problem"] == "duplicate article" for v in got.violations)
        assert len(got["answer"]["article_scores"]) == 1

    def test_an_unknown_condition_flag_is_reported(self):
        flags = {f: False for f in ai_consts.CONDITION_FLAGS}
        flags["invented_flag"] = True
        got = validator.validate(_answer(condition_flags=flags), article_ids=["a1"])
        assert any(v["problem"] == "unknown flag" for v in got.violations)

    def test_a_missing_flag_is_reported_and_defaulted_visibly(self):
        flags = {f: False for f in ai_consts.CONDITION_FLAGS}
        flags.pop("capital_controls")
        got = validator.validate(_answer(condition_flags=flags), article_ids=["a1"])
        assert any(v["field"] == "condition_flags.capital_controls" for v in got.violations)
        assert got["answer"]["condition_flags"]["capital_controls"] is False

    def test_a_missing_subscore_reason_is_a_violation(self):
        ev = {f: "because" for f in ai_consts.LEDGER_FIELDS}
        ev["edge"] = ""
        got = validator.validate(_answer(subscore_evidence=ev), article_ids=["a1"])
        assert any(v["field"] == "subscore_evidence.edge" for v in got.violations)

    def test_a_non_object_answer_does_not_raise(self):
        got = validator.validate("nope")
        assert got["answer"] is None
        assert got.violations


class TestScoringContract:
    def test_the_schema_uses_type_unions_for_nullable_ledgers(self):
        """The grammar is part of the instrument, not packaging around it."""
        props = ai_consts.RISK_SCHEMA["schema"]["properties"]
        for field in ai_consts.LEDGER_FIELDS:
            assert props[field]["type"] == ["integer", "null"], field

    def test_the_composites_are_not_nullable(self):
        props = ai_consts.RISK_SCHEMA["schema"]["properties"]
        assert props["score_12m"]["type"] == "integer"

    def test_evidence_coverage_is_not_something_the_model_returns(self):
        """It was measured self-reporting 80 with twenty articles and 80 with six."""
        assert "evidence_coverage" not in ai_consts.RISK_SCHEMA["schema"]["properties"]
        assert "Do not return an evidence-coverage figure" in ai_consts.RISK_PROMPT

    def test_the_prompt_carries_the_framework(self):
        p = ai_consts.RISK_PROMPT
        for phrase in ("THE THREE LEDGERS", "THREE-DOOR EVENT TEST",
                       "CALIBRATION ANCHORS", "Never round to a multiple of 5",
                       "A QUIET WEEK IS NOT A GOOD WEEK"):
            assert phrase in p, phrase

    def test_the_prompt_says_nothing_downstream_alters_the_score(self):
        assert "Nothing downstream alters the score" in ai_consts.RISK_PROMPT

    def test_the_legal_gate_is_a_badge_and_not_an_override(self):
        """Russia's rating was a constant, the same number whether the week held
        a mobilisation or nothing at all."""
        from backend.llm import langchain_llm

        assert not hasattr(langchain_llm, "_legal_gate_decision")
        badge = langchain_llm.legal_badge("RU")
        assert badge and badge["rule"]
        src = (__import__("pathlib").Path(langchain_llm.__file__)).read_text(encoding="utf-8")
        assert "1.0 if gate else" not in src
