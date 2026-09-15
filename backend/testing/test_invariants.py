"""The properties that must hold no matter what else changes.

Every other test file in this suite checks that something works. These check
that something is *impossible*, or that a run records what it actually did:

* **the payload census** — what reached the model, against what the registry
  promised, and the macro stamps it rests on;
* **mask integrity** — all forty-eight roster countries;
* **cost guards** — the caps, asserted as firing rather than as existing;
* **the digest cache key** — a masked digest is never served for a named one.

No network, no model, no database.
"""

import datetime

import pytest

from backend.llm import digest_engine, payload
from backend.data_fetching import lags
from backend.llm import gazetteer as gz, rewrite

AS_OF = datetime.date(2018, 6, 15)
COUNTRY = "PT"
MONDAY = datetime.date(2019, 1, 7)

# What the live gate actually scans. `assert_clean` and `mask_foreign` default
# to all forty-eight; nothing in production passes a shorter list.
ROSTER = list(gz.DEFAULT_ROSTER)


class TestTheDailyRunFetchesAndEnriches:
    def test_the_daily_run_still_fetches_and_enriches(self, monkeypatch):
        from backend.util import pipeline
        called = []
        monkeypatch.setattr(pipeline.llm_payload, "prepare_llm_payload_pretty",
                            lambda **kw: {"_meta": {"generated_at": "2026-08-02T00:00:00Z"}})
        monkeypatch.setattr(pipeline.article_enrichment, "fetch_relevant_news",
                            lambda *a, **kw: called.append("fetch") or [])
        monkeypatch.setattr(pipeline.article_enrichment, "resolve_and_enrich",
                            lambda items, iso2: called.append("enrich") or items)
        monkeypatch.setattr(pipeline.digest_engine, "digest_articles",
                            lambda items, **kw: (_ for _ in ()).throw(StopIteration))

        with pytest.raises(StopIteration):
            pipeline._process_country("Portugal", "PT", [])

        assert called == ["fetch", "enrich"]


class TestTheResolveRule:
    """`_resolve` merges one indicator's copies, freshest winning."""

    def observation(self, period_year, as_of, value):
        return payload._Observation(
            value=value, period=str(period_year), freq="A",
            period_end=datetime.date(period_year, 12, 31),
            as_of=as_of, source="IMF WEO")

    def test_a_real_vintage_outranks_the_panels_year_end_stamp(self):
        """The panel stamps every annual figure with 31 December of its own year,
        because it has no record of when the World Bank published it. That
        placeholder must not outrank an edition that carries a real date."""
        panel_stamp = payload._Observation(
            value=1.37, period="2017", freq="A",
            period_end=datetime.date(2017, 12, 31),
            as_of=datetime.date(2017, 12, 31), source="World Bank panel", dated=False)
        real_edition = payload._Observation(
            value=1.581, period="2017", freq="A",
            period_end=datetime.date(2017, 12, 31),
            as_of=datetime.date(2017, 10, 1), source="IMF WEO 2017-10", dated=True)
        merged = payload._resolve([panel_stamp, real_edition])
        assert [o.value for o in merged] == [1.581], "the placeholder outranked the edition"

    def test_nothing_is_filtered_by_date(self):
        observations = [
            self.observation(2026, datetime.date(2026, 4, 1), 1.1),
            self.observation(2017, datetime.date(2026, 4, 1), 3.5),
        ]
        assert len(payload._resolve(observations)) == 2

class TestThePayloadCensus:
    """What a payload held, recorded against what the registry promised."""

    def test_the_world_bank_annuals_are_stamped_with_their_year_end(self):
        """`country_data_fetch.panel_rows` has no publication date, so each
        value carries its own year end."""
        import pandas as pd

        from backend.data_fetching import country_data_fetch

        panel = pd.DataFrame({"POL_CORRUPTION": [1.0, 2.0, 3.0]},
                             index=[2017, 2018, 2024])
        rows = country_data_fetch.panel_rows(panel, "PT")
        assert [r["as_of"] for r in rows] == [
            datetime.date(2017, 12, 31), datetime.date(2018, 12, 31),
            datetime.date(2024, 12, 31)]
        assert {r["freq"] for r in rows} == {"A"}

    def test_the_wb_series_fetcher_dates_rows_by_publication(self):
        """`wb_series_fetch` used to stamp `date.today()` on past periods."""
        rows = lags.restamp([
            {"country_iso2": "PT", "indicator_code": "GOV_WGI_GE.EST",
             "freq": "A", "period": period, "value": 1.0,
             "as_of": datetime.date(2026, 8, 28), "source": "World Bank WGI"}
            for period in ("2016", "2017", "2018", "2024")
        ])

        # Not the fetch date any more, and never before the period it describes.
        assert all(r["as_of"] != datetime.date(2026, 8, 28) for r in rows)
        assert all(lags.within_bounds(r["as_of"], r["period"], r["freq"])
                   for r in rows)

    def test_a_ledger_resolving_nothing_is_named_not_counted(self):
        """A table of counts prints a zero among other zeros. The census names
        an empty ledger."""
        series = {"GOV_WGI_GE.EST": [
            {"value": 1.0, "period": "2017", "freq": "A",
             "as_of": datetime.date(2018, 12, 31)}]}
        evidence = payload.build_evidence_payload(
            "PT", as_of=datetime.date(2019, 6, 1), series=series)
        health = payload.payload_health(
            evidence, series, datetime.date(2019, 6, 1))

        assert "information" in health["indicators"]["empty_ledgers"]
        assert "edge" in health["indicators"]["empty_ledgers"]
        assert health["indicators"]["by_ledger"]["friction"]["resolved"] == 1

    def test_an_unmapped_series_is_told_apart_from_a_missing_row(self):
        """`no row` is a source never fetched; `unmapped` is rows that exist and
        still did not reach the payload. They need different fixes."""
        anchor = datetime.date(2019, 6, 1)
        stored = {"GOV_WGI_GE.EST": [
            {"value": 1.0, "period": "2017", "freq": "A",
             "as_of": datetime.date(2018, 12, 31)}]}
        evidence = payload.build_evidence_payload("PT", as_of=anchor, series={})
        health = payload.payload_health(evidence, stored, anchor)
        assert health["indicators"]["dropped"]["GOV_WGI_GE.EST"] == "unmapped"

        health = payload.payload_health(evidence, {}, anchor)
        assert health["indicators"]["dropped"]["GOV_WGI_GE.EST"] == "no row"

    def test_the_health_says_how_much_of_the_payload_cleared_the_bar(self):
        """Padding and evidence are indistinguishable once they are in a payload.

        `apply_threshold` fills the budget from below the relevance bar when too
        few articles clear it, and nothing downstream can see which is which --
        not the prompt, not the model, not `evidence_coverage`. PT 2019 was
        topped up at 52 of 52 anchors with a median of six articles over the bar
        out of twenty, and every stored manifest recorded "20 articles".
        """
        anchor_date = datetime.date(2019, 6, 1)
        evidence = payload.build_evidence_payload(
            "PT", as_of=anchor_date, series={})
        items = [{"_theme": "order", "relevance_score": 0.9, "tier": "full"},
                 {"_theme": "order", "relevance_score": 0.4, "tier": "full"},
                 {"_theme": "broad", "relevance_score": 0.1, "tier": "full"},
                 {"_theme": "broad", "relevance_score": 0.1, "tier": "full"}]
        health = payload.payload_health(evidence, {}, anchor_date, items)
        assert health["articles"]["articles"] == 4
        assert health["articles"]["cleared_threshold"] == 2
        assert health["articles"]["relevance_threshold"] == 0.3
        assert health["articles"]["floor_enforced"] is False

    def test_the_manifest_records_the_census_and_the_corpus(self):
        """A run must write down what its payload held and what it read.

        The consumer-side rule this project adopted after the sixth
        write-a-thing-nobody-reads instance, and had not applied to the census
        itself. `evidence_sha256` proves two payloads differed; it cannot say
        which was thinner, which is exactly how ten missing indicators survived
        a pilot, a bake-off and two A/B arms.
        """
        from backend.util import provenance

        stamps = dict(model_id="gpt-4o", prompt_version="v4.0",
                      policy_version="p2.0", seed=42)
        manifest = provenance.build_input_manifest(
            items=[], payload={}, payload_health={"indicators": {"resolved": 23}},
            **stamps)
        assert manifest["payload_health"]["indicators"]["resolved"] == 23
        assert manifest["article_set_sha256"]

        # Absent rather than null when it could not be computed: a null the
        # reader has to interpret is the same noise an absent indicator is.
        assert "payload_health" not in provenance.build_input_manifest(
            items=[], payload={}, **stamps)

    def test_the_corpus_hash_moves_only_when_the_corpus_does(self):
        """What makes a three-arm comparison checkable rather than assumed.

        Two arms that selected different articles are not measuring the payload,
        and until this there was no way to tell short of reconstructing the
        selection by hand.
        """
        from backend.util import provenance

        def manifest_for(urls):
            return provenance.build_input_manifest(
                items=[{"id": u, "link": u, "title": u} for u in urls], payload={},
                model_id="gpt-4o", prompt_version="v4.0",
                policy_version="p2.0", seed=42)

        assert (manifest_for(["a", "b"])["article_set_sha256"]
                == manifest_for(["a", "b"])["article_set_sha256"])
        assert (manifest_for(["a", "b"])["article_set_sha256"]
                != manifest_for(["a", "c"])["article_set_sha256"])

    def test_the_pipeline_takes_the_census_of_what_it_sent(self):
        """Not of a payload rebuilt afterwards.

        `payload_census.census` builds its own payload without the structural
        block, so a census taken from it would record a
        number about a payload nobody sent -- the precise failure being fixed.
        Asserted on the source, the way the panel-backfill scoping guard is,
        because the alternative is standing up the whole scoring path.
        """
        import inspect

        from backend.util import pipeline
        src = inspect.getsource(pipeline._process_country)
        assert "payload_health=" in src, "the run does not record a census"
        assert "llm_payload.payload_health(evidence, series" in src, (
            "the census must be taken against the payload that was sent")

    def test_the_panel_backfill_asks_about_its_own_codes(self):
        """A step that runs, logs OK and does no work is this project's
        signature defect, and the bootstrap reproduced it exactly.

        The World Bank backfill skips a country that already has annual rows.
        The WEO editions write 160k annual rows before it runs, so asking "any
        annual row at all" reported all 48 countries as done and the panel step
        finished in 13 seconds having fetched nothing. The question has to name
        the codes the panel itself owns.
        """
        import inspect

        from backend.data_fetching import country_data_fetch

        source = inspect.getsource(country_data_fetch.backfill_missing_panels)
        assert "source=PANEL_SOURCE" in source, (
            "the incremental check must be scoped by source. Scoping it by "
            "indicator code is not enough — CPI.YOY is both a panel column and "
            "a WEO subject, so a code filter answers the same way and skips "
            "every country all over again.")

    def test_the_panel_stamps_its_own_source(self):
        """A World Bank annual attributed to the IMF is wrong on the row, and it
        is also the only thing separating it from the WEO edition that shares
        its indicator code."""
        import pandas as pd

        from backend.data_fetching import country_data_fetch

        panel = pd.DataFrame({"INFLATION": [1.0]}, index=[2019])
        row, = country_data_fetch.panel_rows(panel, "PT")
        assert row["indicator_code"] == "CPI.YOY"
        assert row["source"] == country_data_fetch.PANEL_SOURCE
        assert "IMF" not in row["source"]

    def test_the_current_years_annual_is_not_stamped_in_the_future(self):
        """A year-end stamp on the current year claims a publication date months
        from now, which reads as negative staleness in the live payload and is
        plainly false — the value is already in the table."""
        import pandas as pd

        from backend.data_fetching import country_data_fetch

        today = datetime.date.today()
        panel = pd.DataFrame({"POL_CORRUPTION": [1.0]}, index=[today.year])
        row, = country_data_fetch.panel_rows(panel, "PT")
        assert row["as_of"] <= today

    def test_a_lag_that_is_too_short_is_the_dangerous_direction(self):
        # Erring long is the design: a short lag hands a snapshot a number
        # nobody had, and nothing downstream would show it.
        assert lags.lag_days("SOME.NEW.CODE", "?") == 365
        assert lags.lag_days("BIS.FX.USD", "M") == 0


# ---------------------------------------------------------------------------
# Mask integrity — all forty-eight, not just the pilot
# ---------------------------------------------------------------------------

class TestTheMapItself:
    def test_every_roster_country_has_a_gazetteer(self):
        # Not just the pilot four: the daily run masks all forty-eight, and a
        # country with no entry would be scored named without anyone noticing.
        assert set(gz.COUNTRIES) == set(gz.DEFAULT_ROSTER)
        assert len(gz.COUNTRIES) == 48

    def test_every_category_has_a_role(self):
        for iso2, entry in gz.COUNTRIES.items():
            assert set(entry) <= set(gz.ROLES), iso2

    def test_every_country_has_a_name_and_a_currency(self):
        # The thin tier's floor. Anything below this is not masking.
        for iso2, entry in gz.COUNTRIES.items():
            assert entry.get("names") and entry.get("currency"), iso2

    def test_the_thin_tier_carries_a_demonym_or_a_multiword_name(self):
        # "Japanese" and "New Zealand" identify a country as surely as its name.
        for iso2 in gz.THIN:
            entry = gz.COUNTRIES[iso2]
            assert entry.get("demonyms") or " " in entry["names"][0], iso2

    def test_the_version_is_stamped(self):
        # The digest cache keys on masked content hashes, so a silently
        # improved gazetteer would serve digests of differently-masked text.
        assert gz.MASK_MAP_VERSION


class TestNoRosterTermSurvivesForAnyCountry:
    """The gate every masking bug kept getting past.

    Whatever the scored country, no roster term may remain anywhere in the
    masked text — and a country that only masks its *own* forms leaks every
    other country in the bundle.
    """

    def test_every_roster_country_masks_itself_clean(self):
        text = ("Portugal's parliament met in Lisbon as Turkey raised rates, "
                "the Bank of Korea responded and Brazilian lawmakers debated.")
        for iso2 in ROSTER:
            masked = rewrite.mask_text(text, iso2, ROSTER)
            assert gz.scan(masked, ROSTER) == [], f"{iso2}: {masked!r}"

    def test_every_roster_currency_symbol_survives_no_country(self):
        text = "figures of €2.1bn, R$4,200, ₺18.5 and ₩1.2tn were reported"
        for iso2 in ROSTER:
            masked = rewrite.mask_text(text, iso2, ROSTER)
            assert gz.scan(masked, ROSTER) == [], f"{iso2}: {masked!r}"


# ---------------------------------------------------------------------------
# Cost guards — asserted as firing, not as existing
# ---------------------------------------------------------------------------

class TestTheTokenCapsAreSetWhereTheyWereBreached:
    def test_the_digest_chat_caps_its_output(self):
        """Uncapped, a loop costs $0.0098; capped it costs $0.0006. Over a
        2,188-snapshot pilot that is ~$10 of pure waste against a $130 guard."""
        from backend.llm import client as ai_client
        assert ai_client._DIGEST_MAX_TOKENS <= 2048

    def test_the_rewrite_gets_more_than_a_digest(self):
        """The digest cap is right for a digest and fatal for the mask rewrite:
        the median harvested body is ~5,300 characters and cannot come back
        inside 1,024 tokens, so it died at the ceiling and the article degraded
        to title-only — 71% of stored bodies, with nothing but a WARNING."""
        from backend.llm import client as ai_client
        assert ai_client.rewrite_max_tokens("x" * 5300) > ai_client._DIGEST_MAX_TOKENS
        assert ai_client.rewrite_max_tokens("x" * 500) == ai_client._DIGEST_MAX_TOKENS
        assert ai_client.rewrite_max_tokens("x" * 10_000_000) < 16384


class TestTheDigestCacheKeyCarriesTheMask:
    """The masked digest key is `masked:{mask_map_version}:{sweep_version}`.

    A guard that compared only the sweep let a `g3` row through against a `g5`
    tree: a gazetteer bump alone must move every masked key, and a named digest
    must never be served to a masked run -- it would put a president's name in
    the prompt with every gate reporting clean.
    """

    TEXT = "Central bank raises rates as inflation climbs"

    def test_a_mask_map_bump_alone_moves_the_masked_key(self, monkeypatch):
        before = digest_engine._content_sha(self.TEXT, True)
        monkeypatch.setattr(gz, "MASK_MAP_VERSION", "g99")
        assert digest_engine._content_sha(self.TEXT, True) != before

    def test_a_named_digest_is_not_served_to_a_masked_run(self):
        assert (digest_engine._content_sha(self.TEXT, False)
                != digest_engine._content_sha(self.TEXT, True))


