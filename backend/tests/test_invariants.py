"""The properties that must hold no matter what else changes.

Every other test file in this suite checks that something works. These check
that something is *impossible*, because the failures they guard against do not
look like failures: a backfilled series that quietly read tomorrow's news
produces beautiful scores, an excellent backtest, and a machine that cannot do
the one thing it was built to do.

Six groups, and they are the reason this file is separate from the folder test
files — burying them inside `test_news_fetching.py` would hide what they are:

* **no future** — three ways hindsight gets into an article window, one class
  each, plus the macro vintage bound that does the same job for indicators;
* **mask integrity** — all forty-eight roster countries, not just the pilot;
* **resume and idempotency** — a resume that retries completed work, or worse
  skips failed work, is how a decade-long run goes quietly wrong;
* **the diagnostic arms** — a named row shares (country, as_of) with its masked
  twin and would overwrite the production series;
* **cost guards** — the caps, asserted as firing rather than as existing;
* **byte-for-byte rebuild** — the manifest's only consumer reads what the
  scorer wrote.

No network, no model, no database, except the opt-in Postgres block at the
bottom (`HISTORY_TEST_DATABASE_URL`), which is skipped by a bare `pytest` run.
"""

import datetime
import os

import pytest

from backend.utils import data_retrieval
from backend.utils.history import config, score, snapshot_select as sel, usage
from backend.data_upsert import store
from backend.data_fetching.vintage import lags, restamp
from backend.utils.masking import gazetteer as gz, rewrite
from backend.utils.news_fetching import core

AS_OF = datetime.date(2018, 6, 15)
COUNTRY = "PT"
MONDAY = datetime.date(2019, 1, 7)

# What the live gate actually scans. `assert_clean` and `mask_foreign` default
# to all forty-eight; nothing in production passes a shorter list.
ROSTER = list(gz.DEFAULT_ROSTER)


def row(**over):
    """One `historical_article` row as `store.read_window` returns it."""
    base = dict(
        url="https://www.theguardian.com/world/2018/jun/01/story",
        publisher_link=None,
        title="Portugal's parliament debates the budget as the deficit narrows",
        abstract="Lawmakers met on Friday.",
        body="Portugal's parliament debated the budget on Friday.",
        body_status="recovered",
        body_vintage="api-native",
        source_system="guardian",
        published_at=datetime.datetime(2018, 6, 1, 9, 30, tzinfo=datetime.timezone.utc),
        themes=["order"],
        tier="full",
    )
    base.update(over)
    return base


@pytest.fixture()
def store_rows(monkeypatch):
    """Feed `select` a fixed set of rows without a database."""
    def _install(rows):
        monkeypatch.setattr(sel.store, "read_window", lambda iso2, start, end: [
            r for r in rows if start <= r["published_at"] < end])
    return _install


# ---------------------------------------------------------------------------
# No future — articles
# ---------------------------------------------------------------------------

class TestTheWindow:
    def test_it_is_thirty_days_and_strict_at_the_top(self):
        start, end = sel.window(AS_OF)
        assert (end - start).days == config.SNAPSHOT_WINDOW_DAYS
        assert end == datetime.datetime(2018, 6, 15, tzinfo=datetime.timezone.utc)

    def test_an_article_published_on_the_anchor_is_not_read(self, store_rows):
        # Same-day news the live run's own cutoff would not reliably have had.
        store_rows([row(published_at=datetime.datetime(2018, 6, 15, 0, 0,
                                                       tzinfo=datetime.timezone.utc))])
        assert sel.select(COUNTRY, AS_OF) == []

    def test_an_article_published_after_the_anchor_is_not_read(self, store_rows):
        store_rows([row(published_at=datetime.datetime(2018, 7, 1,
                                                       tzinfo=datetime.timezone.utc))])
        assert sel.select(COUNTRY, AS_OF) == []

    def test_an_article_just_inside_the_window_is_read(self, store_rows):
        store_rows([row(published_at=datetime.datetime(2018, 6, 14, 23, 59,
                                                       tzinfo=datetime.timezone.utc))])
        assert len(sel.select(COUNTRY, AS_OF)) == 1

    def test_an_article_older_than_the_window_is_not_read(self, store_rows):
        store_rows([row(published_at=datetime.datetime(2018, 5, 1,
                                                       tzinfo=datetime.timezone.utc))])
        assert sel.select(COUNTRY, AS_OF) == []


class TestABodyMayNotBeYoungerThanTheAnchor:
    """The subtle one: the article's own date is innocent, its body is not."""

    def test_a_capture_after_the_anchor_loses_its_body(self):
        # Published 1 June, captured 15 August, read as of 15 June. Publishers
        # edit, append and re-headline; that body is two months of hindsight.
        assert sel.usable_body(row(body_vintage="wayback-20180815"), AS_OF) is None

    def test_a_capture_on_the_anchor_loses_its_body(self):
        assert sel.usable_body(row(body_vintage="wayback-20180615"), AS_OF) is None

    def test_a_capture_before_the_anchor_keeps_its_body(self):
        assert sel.usable_body(row(body_vintage="wayback-20180605"), AS_OF)

    def test_an_api_native_body_is_the_article_itself(self):
        # It arrived inside the search response, so its age is the article's.
        assert sel.usable_body(row(body_vintage="api-native"), AS_OF)

    def test_an_unreadable_vintage_is_not_a_licence_to_use_the_body(self):
        assert sel.usable_body(row(body_vintage="wayback-notadate"), AS_OF) is None
        assert sel.usable_body(row(body_vintage="something-else"), AS_OF) is None

    def test_losing_a_body_thins_the_article_but_does_not_drop_it(self, store_rows):
        # Thinner evidence, honestly thin — never a missing article.
        store_rows([row(body_vintage="wayback-20180815")])
        picked = sel.select(COUNTRY, AS_OF)
        assert len(picked) == 1
        assert picked[0]["text"] == ""
        assert picked[0]["title"]
        assert picked[0]["snippet"] == "Lawmakers met on Friday."


class TestALiveRefetchNeedsTheScan:
    def test_a_scanned_live_refetch_is_allowed(self):
        # Cleared of post-publication knowledge, and the article was published
        # before the anchor, so post-publication covers post-anchor.
        assert sel.usable_body(
            row(body_vintage="live-refetch", body_status="recovered"), AS_OF)

    def test_an_unscanned_live_refetch_is_refused(self):
        for status in ("pending", "failed", "degraded-title-only"):
            assert sel.usable_body(
                row(body_vintage="live-refetch", body_status=status), AS_OF) is None

    def test_a_flagged_body_never_reaches_here_anyway(self):
        # `wayback.recover_one` discards the text when the scan flags it, so the
        # row arrives with no body at all. Belt and braces.
        assert sel.usable_body(
            row(body="", body_vintage="live-refetch",
                body_status="degraded-title-only"), AS_OF) is None


class TestNoFutureSurvivesAssembly:
    """The end-to-end assertion: whatever the mix, nothing future-dated gets in."""

    def test_a_mixed_window_yields_only_knowable_evidence(self, store_rows):
        store_rows([
            row(url="https://ex.test/ok", body_vintage="api-native"),
            row(url="https://ex.test/future-article",
                published_at=datetime.datetime(2018, 6, 20, tzinfo=datetime.timezone.utc)),
            row(url="https://ex.test/future-capture", body_vintage="wayback-20180901"),
            row(url="https://ex.test/unscanned",
                body_vintage="live-refetch", body_status="failed"),
        ])
        picked = sel.select(COUNTRY, AS_OF)
        urls = {i["link"] for i in picked}

        assert "https://ex.test/future-article" not in urls
        for item in picked:
            assert item["published"] < AS_OF.isoformat()
            vintage = item.get("body_vintage")
            if vintage and vintage.startswith("wayback-"):
                assert sel.capture_date(vintage) < AS_OF
            if item["text"]:
                assert vintage in ("api-native", "live-refetch") or \
                    sel.capture_date(vintage) < AS_OF

    def test_the_only_bodies_present_are_ones_that_passed(self, store_rows):
        store_rows([row(url="https://ex.test/future-capture",
                        body_vintage="wayback-20180901")])
        picked = sel.select(COUNTRY, AS_OF)
        assert all(not i["text"] for i in picked)
        assert all(i.get("body_vintage") is None for i in picked)


class TestThePipelineSeam:
    """`_process_country` is where a historical run enters the live pipeline.

    The contract is that supplying `as_of` and `items` changes the article
    source and the date and nothing else, and that supplying neither leaves the
    daily run exactly as it was.
    """

    def test_the_daily_run_still_fetches_and_enriches(self, monkeypatch):
        from backend.utils import pipeline
        called = []
        monkeypatch.setattr(pipeline.data_retrieval, "prepare_llm_payload_pretty",
                            lambda **kw: {"_meta": {"generated_at": "2026-08-02T00:00:00Z"}})
        monkeypatch.setattr(pipeline.article_enrichment, "fetch_relevant_news",
                            lambda *a, **kw: called.append("fetch") or [])
        monkeypatch.setattr(pipeline.article_enrichment, "resolve_and_enrich",
                            lambda items, iso2: called.append("enrich") or items)
        monkeypatch.setattr(pipeline, "_finish_country", lambda *a, **kw: None, raising=False)
        monkeypatch.setattr(pipeline.digest_engine, "digest_articles",
                            lambda items, **kw: (_ for _ in ()).throw(StopIteration))

        with pytest.raises(StopIteration):
            pipeline._process_country("Portugal", "PT", [])

        assert called == ["fetch", "enrich"]

    def test_a_historical_run_neither_fetches_nor_enriches(self, monkeypatch):
        from backend.utils import pipeline
        called = []
        monkeypatch.setattr(pipeline.data_retrieval, "prepare_llm_payload_pretty",
                            lambda **kw: {"_meta": {"generated_at": "2026-08-02T00:00:00Z"}})
        monkeypatch.setattr(pipeline.article_enrichment, "fetch_relevant_news",
                            lambda *a, **kw: called.append("fetch") or [])
        monkeypatch.setattr(pipeline.article_enrichment, "resolve_and_enrich",
                            lambda items, iso2: called.append("enrich") or items)
        monkeypatch.setattr(pipeline.digest_engine, "digest_articles",
                            lambda items, **kw: (_ for _ in ()).throw(StopIteration))

        with pytest.raises(StopIteration):
            pipeline._process_country("Portugal", "PT", [], as_of=AS_OF, items=[])

        assert called == [], "a historical run must not refetch its own articles"

    def test_the_pin_reaches_every_downstream_stage(self, monkeypatch):
        """One overwrite of `_meta.generated_at` has to move the whole run."""
        from backend.utils import pipeline
        from backend.data_upsert import data_push
        seen = {}
        monkeypatch.setattr(pipeline.data_retrieval, "prepare_llm_payload_pretty",
                            lambda **kw: {"_meta": {"generated_at": "2026-08-02T00:00:00Z"}})
        monkeypatch.setattr(pipeline.digest_engine, "digest_articles",
                            lambda items, **kw: seen.setdefault("digest", kw["as_of"]) and items)

        def stop(*a, **kw):
            seen["evidence"] = kw["as_of"]
            raise StopIteration

        monkeypatch.setattr(pipeline.data_retrieval, "build_evidence_payload", stop)

        with pytest.raises(StopIteration):
            pipeline._process_country("Portugal", "PT", [], as_of=AS_OF, items=[])

        assert seen["digest"] == AS_OF
        assert seen["evidence"] == AS_OF
        # And the upsert would key on the same date, from the same field.
        assert data_push.payload_as_of(
            {"_meta": {"generated_at": AS_OF.isoformat()}}) == AS_OF


# ---------------------------------------------------------------------------
# No future — macro vintages
# ---------------------------------------------------------------------------

class TestTheVintageRuleInThePayload:
    """`_resolve` is where a vintage either gets used or refused."""

    def observation(self, period_year, as_of, value):
        return data_retrieval._Observation(
            value=value, period=str(period_year), freq="A",
            period_end=datetime.date(period_year, 12, 31),
            as_of=as_of, source="IMF WEO")

    def test_the_newest_vintage_not_after_the_anchor_wins(self):
        merged = data_retrieval._resolve([
            self.observation(2017, datetime.date(2018, 4, 1), 2.7),
            self.observation(2017, datetime.date(2018, 10, 1), 2.8),
            self.observation(2017, datetime.date(2026, 4, 1), 3.5),
        ], as_of=datetime.date(2018, 6, 4))
        assert [o.value for o in merged] == [2.7], "June 2018 knew April's estimate only"

    def test_a_later_vintage_is_used_once_it_exists(self):
        merged = data_retrieval._resolve([
            self.observation(2017, datetime.date(2018, 4, 1), 2.7),
            self.observation(2017, datetime.date(2018, 10, 1), 2.8),
        ], as_of=datetime.date(2018, 12, 1))
        assert [o.value for o in merged] == [2.8]

    def test_a_period_covering_the_future_is_refused(self):
        # In June 2018 nobody knows 2018's annual figure.
        merged = data_retrieval._resolve([
            self.observation(2017, datetime.date(2018, 4, 1), 2.7),
            self.observation(2018, datetime.date(2018, 4, 1), 2.3),
        ], as_of=datetime.date(2018, 6, 4))
        assert [o.period for o in merged] == ["2017"]

    def test_a_real_vintage_outranks_the_panels_year_end_stamp(self):
        """January to March is a quarter of the anchors, and it was reading today.

        The panel stamps every annual figure with 31 December of its own year,
        because it has no record of when the World Bank published it. Between
        that year end and the next WEO edition, the placeholder had the newer
        date and won — so a February 2018 snapshot read 2026's revision of 2017
        rather than the October 2017 estimate that was the newest thing anyone
        could actually have had.
        """
        panel_stamp = data_retrieval._Observation(
            value=1.37, period="2017", freq="A",
            period_end=datetime.date(2017, 12, 31),
            as_of=datetime.date(2017, 12, 31), source="World Bank panel", dated=False)
        real_edition = data_retrieval._Observation(
            value=1.581, period="2017", freq="A",
            period_end=datetime.date(2017, 12, 31),
            as_of=datetime.date(2017, 10, 1), source="IMF WEO 2017-10", dated=True)
        merged = data_retrieval._resolve([panel_stamp, real_edition],
                                         as_of=datetime.date(2018, 2, 20))
        assert [o.value for o in merged] == [1.581], "the placeholder outranked the edition"

    def test_the_daily_run_is_unaffected(self):
        """No as_of means no filtering — the live path must not change."""
        observations = [
            self.observation(2026, datetime.date(2026, 4, 1), 1.1),
            self.observation(2017, datetime.date(2026, 4, 1), 3.5),
        ]
        assert len(data_retrieval._resolve(observations)) == 2

    def test_build_evidence_payload_defaults_to_no_vintage_filter(self):
        import inspect
        sig = inspect.signature(data_retrieval.build_evidence_payload)
        assert sig.parameters["vintage_as_of"].default is None


class TestSurvivesTheVintageBound:
    """An observation may never claim to be public before its period ended.

    Every `indicator_series` row used to carry the bulk-fetch date as its
    `as_of`, so the bound discarded the whole table for any historical anchor —
    a 2019 snapshot with no CPI, no exchange rate and no policy rate, and
    nothing anywhere saying so.
    """

    FETCHED = datetime.date(2026, 7, 28)

    def stored(self, **kwargs):
        return {"country_iso2": "PT", "indicator_code": "CPI.YOY", "freq": "M",
                "period": "2018-03", "value": 1.2, "as_of": self.FETCHED,
                "source": "IMF CPI", "vintage_scheme": "as-published-latest",
                **kwargs}

    def test_a_fetch_dated_row_is_discarded_by_a_2019_anchor(self):
        obs = data_retrieval._Observation(
            value=1.2, period="2018-03", freq="M",
            period_end=lags.period_end("2018-03", "M"), as_of=self.FETCHED, source="IMF")
        assert data_retrieval._resolve([obs], as_of=datetime.date(2019, 6, 1)) == []

    def test_a_re_dated_row_survives_it(self):
        changed, _ = restamp.plan([self.stored()])
        obs = data_retrieval._Observation(
            value=1.2, period="2018-03", freq="M",
            period_end=lags.period_end("2018-03", "M"),
            as_of=changed[0]["as_of"], source="IMF")
        assert data_retrieval._resolve([obs], as_of=datetime.date(2019, 6, 1)) == [obs]
        # …and is still refused by an anchor before it was published.
        assert data_retrieval._resolve([obs], as_of=datetime.date(2018, 4, 1)) == []

    def test_the_world_bank_panel_still_survives_the_same_bound(self):
        """The store that was already right must not be broken by fixing the other.

        `_panel_observations` stamps each annual value with its own year end,
        which is why the panel was the only thing a historical payload could see
        before this migration. Pinned here because nothing else would notice if
        it changed.
        """
        import pandas as pd
        panel = pd.DataFrame({"year": [2017, 2018, 2024], "gdp": [1.0, 2.0, 3.0]})
        observations = data_retrieval._panel_observations(panel, "gdp")
        assert [o.as_of for o in observations] == [
            datetime.date(2017, 12, 31), datetime.date(2018, 12, 31),
            datetime.date(2024, 12, 31)]
        kept = data_retrieval._resolve(observations, as_of=datetime.date(2019, 6, 1))
        assert [o.period for o in kept] == ["2017", "2018"]

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
# The diagnostic arms must not touch the production series
# ---------------------------------------------------------------------------

@pytest.fixture
def ledger(monkeypatch):
    """An in-memory stand-in for `history_run_ledger`."""
    rows = []
    monkeypatch.setattr(score.store, "write_run",
                        lambda as_of, iso2, mode, **kw: rows.append(
                            {"as_of": as_of, "country_iso2": iso2, "mode": mode, **kw}))
    monkeypatch.setattr(score.store, "total_spend_usd", lambda: 0.0)
    monkeypatch.setattr(score.store, "completed_runs", lambda mode, iso2=None: set())
    return rows


@pytest.fixture
def scored(monkeypatch):
    """Capture what `_process_country` was asked to do, without doing it."""
    calls = []

    def fake(country_name, iso2, pool, **kw):
        calls.append({"iso2": iso2, **kw})
        return {"score": 0.5}, {"schema_version": 1}

    monkeypatch.setattr(score.pipeline, "_process_country", fake)
    monkeypatch.setattr(score.snapshot_select, "select",
                        lambda iso2, as_of: [{"id": "a1", "title": "x"}])
    return calls


class TestTheDiagnosticArmsStayOutOfTheSeries:
    """A named row shares (country, as_of) with its masked twin. Written to
    `risk_snapshot` it would overwrite the production series, and a series that
    silently changes regime half way through its own history is worse than no
    series."""

    @pytest.mark.parametrize("mode", ["named", "masked_nostructural"])
    def test_a_diagnostic_arm_does_not_upsert(self, ledger, scored, mode):
        score.score_one("PT", MONDAY, mode)
        assert scored[0]["upsert"] is False

    def test_the_masked_arm_does_upsert(self, ledger, scored):
        score.score_one("PT", MONDAY, "masked")
        assert scored[0]["upsert"] is True

    @pytest.mark.parametrize("mode", ["named", "masked_nostructural"])
    def test_a_diagnostic_arms_output_lands_in_the_ledger(self, ledger, scored, mode):
        """It has nowhere else to live, so the ledger row is the result."""
        score.score_one("PT", MONDAY, mode)
        assert ledger[0]["result"] == {"score": 0.5}

    def test_the_masked_arms_output_does_not(self, ledger, scored):
        """It is in `risk_snapshot`, where the front end reads it. Duplicating
        it into the ledger would create a second copy to disagree with."""
        score.score_one("PT", MONDAY, "masked")
        assert ledger[0]["result"] is None

    def test_every_diagnostic_mode_is_a_known_mode(self):
        assert set(config.DIAGNOSTIC_MODES) < set(config.SCORING_MODES)


# ---------------------------------------------------------------------------
# Resume — completed work is skipped, failed work is not
# ---------------------------------------------------------------------------

class TestResume:
    def test_a_completed_anchor_is_skipped(self, ledger, scored, monkeypatch):
        monkeypatch.setattr(score.store, "completed_runs",
                            lambda mode, iso2=None: {MONDAY})
        totals = score.run(roster=["PT"], start=MONDAY, end=MONDAY, mode="masked")
        assert totals == {"scored": 0, "skipped": 1, "failed": 0, "spend_usd": 0.0}
        assert not scored

    def test_an_unknown_mode_is_refused_before_anything_runs(self):
        with pytest.raises(ValueError):
            score.run(mode="masked-ish")

    def test_a_failed_snapshot_costs_its_own_week(self, ledger, monkeypatch, scored):
        monkeypatch.setattr(score.pipeline, "_process_country",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        result = score.score_one("PT", MONDAY, "masked")
        assert result["status"] == "failed"
        assert ledger[0]["status"] == "failed" and "boom" in ledger[0]["manifest"]["error"]

    def test_a_thin_week_is_complete_and_empty_not_failed(self, ledger, monkeypatch):
        """An empty window is a real answer about that week. Recorded failed, a
        resume would retry it forever and the report would call the corpus
        broken."""
        monkeypatch.setattr(score.snapshot_select, "select", lambda iso2, as_of: [])
        result = score.score_one("PT", MONDAY, "masked")
        assert result["status"] == "complete" and result["llm_score"] is None
        assert ledger[0]["manifest"] == {"articles": 0}

    def test_a_leaked_snapshot_is_left_retryable_for_the_resume(
            self, ledger, monkeypatch, scored):
        """`completed_runs` counts only 'complete', so a failed row is retried
        rather than silently skipped — which is the difference between a resume
        that heals the gap and one that bakes it in."""
        monkeypatch.setattr(score.pipeline, "_process_country",
                            lambda *a, **k: (_ for _ in ()).throw(
                                rewrite.MaskLeak("names a roster term")))
        score.score_one("PT", MONDAY, "masked")

        stored = ledger[-1]
        assert stored["status"] == "failed"
        assert "roster term" in stored["manifest"]["error"]
        assert stored["status"] != "complete"

    def test_a_mask_leak_costs_its_snapshot_and_not_the_run(
            self, ledger, monkeypatch, scored):
        """`MaskLeak` is deliberately fatal to a snapshot. It must not be fatal
        to the pilot.

        Refusing to send a mislabelled snapshot is right. Refusing at anchor
        1,500 of 2,188 and taking the other 688 with it is not — and the euro
        symbol that survived the foreign pass would have raised this on any
        bundle quoting a foreign currency figure, which is not a rare bundle.
        """
        calls = []

        def leak_once(country_name, iso2, pool, **kw):
            calls.append(kw["as_of"])
            if len(calls) == 1:
                raise rewrite.MaskLeak("payload still names 1 roster term(s): €")
            return {"score": 0.5}, {"schema_version": 1}

        monkeypatch.setattr(score.pipeline, "_process_country", leak_once)
        days = score.anchors(MONDAY, MONDAY + datetime.timedelta(weeks=3))
        totals = score.run(roster=["PT"], start=days[0], end=days[-1], mode="masked")

        # The run continued: every remaining anchor was attempted.
        assert len(calls) == len(days)
        assert totals["failed"] == 1 and totals["scored"] == len(days) - 1

    def test_the_diagnostic_sample_is_stable_across_runs(self, monkeypatch):
        """A sample that redraws every run cannot be compared with itself, and
        the whole point of the arm is comparison."""
        days = [datetime.date(2018, 1, 1) + datetime.timedelta(weeks=i) for i in range(400)]
        series = [(d, 0.50 + (0.30 if i in (10, 40, 300, 320) else 0.0))
                  for i, d in enumerate(days)]
        monkeypatch.setattr(score, "_masked_series",
                            lambda iso2, since=None, until=None: series)
        assert score.diagnostic_dates("PT") == score.diagnostic_dates("PT")


# ---------------------------------------------------------------------------
# Cost guards — asserted as firing, not as existing
# ---------------------------------------------------------------------------

class TestTheBudgetMeterCatchesCallsItWasNeverToldAbout:
    """The whole point: no call site knows the meter exists."""

    @staticmethod
    def _message(input_tokens=1000, output_tokens=200,
                 model="gpt-4o-mini-2024-07-18"):
        from langchain_core.messages import AIMessage
        return AIMessage(
            content="ok",
            usage_metadata={"input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "total_tokens": input_tokens + output_tokens},
            response_metadata={"model_name": model},
        )

    @staticmethod
    def _llm(responses):
        from langchain_core.language_models import FakeMessagesListChatModel
        return FakeMessagesListChatModel(responses=responses)

    def test_calls_inside_the_block_are_metered(self):
        llm = self._llm([self._message(), self._message()])
        with usage.meter(budget_usd=10.0) as m:
            llm.invoke("a")
            llm.invoke("b")
        assert m.calls == 2
        assert m.input_tokens == 2000 and m.output_tokens == 400

    def test_calls_outside_the_block_are_not(self):
        """The live daily run never enters the block, and must never be metered."""
        llm = self._llm([self._message(), self._message()])
        with usage.meter(budget_usd=10.0) as m:
            llm.invoke("inside")
        llm.invoke("outside")
        assert m.calls == 1

    def test_batched_calls_are_metered_individually(self):
        """`digest_engine` digests via `.batch()`; each article must be counted."""
        llm = self._llm([self._message()] * 3)
        with usage.meter(budget_usd=10.0) as m:
            llm.batch(["a", "b", "c"])
        assert m.calls == 3 and m.input_tokens == 3000

    def test_an_unknown_model_is_priced_at_the_expensive_rate(self):
        # Failing safe: a model nobody added to the table must make the governor
        # stop early, never spend freely.
        assert usage.price("gpt-5-does-not-exist", 1_000_000, 0) == \
            usage.price("gpt-4o-2024-08-06", 1_000_000, 0)

    def test_check_raises_over_budget(self):
        with usage.meter(budget_usd=10.0) as m:
            m.spend_usd = 10.01
            with pytest.raises(usage.BudgetExhausted):
                m.check()

    def test_prior_spend_counts_against_the_cap(self):
        """A resumed pilot must not restart its budget at zero."""
        with usage.meter(budget_usd=10.0, already_spent_usd=9.5) as m:
            m.spend_usd = 1.0
            assert m.total_usd == pytest.approx(10.5)
            with pytest.raises(usage.BudgetExhausted):
                m.check()

    def test_a_call_never_raises_from_inside_the_callback(self):
        """Raising mid-batch would abandon paid-for work with no record of it.

        The meter records; the runner stops at a snapshot boundary.
        """
        llm = self._llm([self._message(9_000_000, 9_000_000)])
        with usage.meter(budget_usd=0.01) as m:
            llm.invoke("expensive")          # must not raise
        assert m.total_usd > 0.01
        with pytest.raises(usage.BudgetExhausted):
            m.check()

    def test_a_call_with_no_usage_is_still_counted_as_a_call(self, caplog):
        """Undercounting spend is the one failure the governor must announce."""
        from langchain_core.messages import AIMessage
        llm = self._llm([AIMessage(content="no usage block")])
        with usage.meter(budget_usd=10.0) as m:
            llm.invoke("x")
        assert m.calls == 1 and m.spend_usd == 0.0
        assert "undercounted" in caplog.text


class TestTheGovernorStopsTheRun:
    def test_budget_exhaustion_stops_the_run_rather_than_being_skipped(
            self, ledger, monkeypatch, scored):
        monkeypatch.setattr(score.pipeline, "_process_country",
                            lambda *a, **k: (_ for _ in ()).throw(usage.BudgetExhausted("x")))
        with pytest.raises(usage.BudgetExhausted):
            score.score_one("PT", MONDAY, "masked")
        assert ledger[0]["status"] == "failed"

    def test_the_governor_fires_on_a_snapshot_that_overruns(
            self, ledger, monkeypatch, scored):
        """`_confirm_spend` checks a projection before the run; this checks the
        meter after each snapshot. Without it a run costing three times its
        projection spends past `PILOT_BUDGET_USD` with nothing to stop it —
        `Meter` deliberately never raises from its own callback."""
        monkeypatch.setattr(score.store, "total_spend_usd",
                            lambda: config.PILOT_BUDGET_USD + 1.0)
        with pytest.raises(usage.BudgetExhausted):
            score.score_one("PT", MONDAY, "masked")

    def test_the_overrunning_snapshot_is_still_banked_before_the_stop(
            self, ledger, monkeypatch, scored):
        """The snapshot is paid for either way. A budget stop that also loses
        the work it just bought is the worst of both, and a resume would then
        buy it a second time."""
        monkeypatch.setattr(score.store, "total_spend_usd",
                            lambda: config.PILOT_BUDGET_USD + 1.0)
        with pytest.raises(usage.BudgetExhausted):
            score.score_one("PT", MONDAY, "masked")
        assert ledger[-1]["status"] == "complete"

    def test_the_run_stops_rather_than_propagating(self, ledger, monkeypatch, scored):
        """`run` owns the stop: one country's budget stop ends the run cleanly
        with its totals, rather than a traceback out of a multi-hour pilot."""
        monkeypatch.setattr(score.store, "total_spend_usd",
                            lambda: config.PILOT_BUDGET_USD + 1.0)
        totals = score.run(roster=["PT"], start=MONDAY, end=MONDAY, mode="masked")
        assert totals["failed"] == 1 and totals["scored"] == 0

    def test_a_projection_refuses_before_there_is_anything_to_measure(self, monkeypatch):
        """It used to return `n * 0.036`, a constant measured before the
        selector fix moved the median snapshot from 6.5 articles to twenty — so
        it was low by a third, and came back indistinguishable from a measured
        number into the line that asks somebody to approve a spend."""
        monkeypatch.setattr(score.store, "read_runs", lambda mode=None: [])
        with pytest.raises(score.NoObservedCost):
            score.projection(52)

    def test_a_projection_follows_the_observed_cost(self, monkeypatch):
        monkeypatch.setattr(score.store, "read_runs",
                            lambda mode=None: [{"status": "complete", "spend_usd": 0.10}])
        assert score.projection(100) == pytest.approx(10.0)


class TestTheTokenCapsAreSetWhereTheyWereBreached:
    def test_the_digest_chat_caps_its_output(self):
        """Uncapped, a loop costs $0.0098; capped it costs $0.0006. Over a
        2,188-snapshot pilot that is ~$10 of pure waste against a $130 guard."""
        from backend.utils.ai import client as ai_client
        assert ai_client._DIGEST_MAX_TOKENS <= 2048

    def test_the_rewrite_gets_more_than_a_digest(self):
        """The digest cap is right for a digest and fatal for the mask rewrite:
        the median harvested body is ~5,300 characters and cannot come back
        inside 1,024 tokens, so it died at the ceiling and the article degraded
        to title-only — 71% of stored bodies, with nothing but a WARNING."""
        from backend.utils.ai import client as ai_client
        assert ai_client.rewrite_max_tokens("x" * 5300) > ai_client._DIGEST_MAX_TOKENS
        assert ai_client.rewrite_max_tokens("x" * 500) == ai_client._DIGEST_MAX_TOKENS
        assert ai_client.rewrite_max_tokens("x" * 10_000_000) < 16384

    def test_the_leakage_scan_cap_is_the_briefed_three_dollars(self):
        assert config.LEAKAGE_SCAN_BUDGET_USD == 3.0


# ---------------------------------------------------------------------------
# Byte-for-byte rebuild
# ---------------------------------------------------------------------------

class TestTheRebuildScriptReadsTheSamePayloadTheScorerWrote:
    """`rebuild_snapshot` is `input_manifest`'s only consumer, and it was
    comparing a manifest built from the panel payload against one built from the
    evidence payload. Every rebuild reported `macro_vintages DIFFERS`, on every
    row, for a reason that had nothing to do with the row."""

    def test_it_builds_the_panel_payload(self):
        import inspect

        from backend.scripts import rebuild_snapshot

        source = inspect.getsource(rebuild_snapshot.rebuild)
        assert "prepare_llm_payload_pretty" in source
        assert "payload=panel" in source, "the manifest must get the panel"


# ---------------------------------------------------------------------------
# Idempotency, against a real Postgres — opt in with HISTORY_TEST_DATABASE_URL
# ---------------------------------------------------------------------------

TEST_DB = os.getenv("HISTORY_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB, reason="set HISTORY_TEST_DATABASE_URL to run")

PUBLISHED = "2018-03-14T09:30:00Z"
URL = "https://www.theguardian.com/world/2018/mar/14/story"


def item(**over):
    """One harvested article as an adapter would have emitted it."""
    base = dict(
        title="Central bank raises interest rates as inflation climbs",
        link=URL,
        published=PUBLISHED,
        source="The Guardian",
        snippet="The bank moved after a third month above target.",
        text="The central bank raised interest rates on Wednesday.",
        theme="order",
    )
    base.update(over)
    return core.normalize_item(**base)


def article_row(**over):
    return store.article_row(item(**over.pop("item", {})), **{
        "country_iso2": "PT", "source_system": "guardian",
        "body_status": "recovered", "body_vintage": "api-native", **over})


@pytest.fixture()
def db(monkeypatch):
    """Point the store at the test database and start from an empty table."""
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    with store._transaction() as cur:
        cur.execute(store._HISTORICAL_ARTICLE_DDL)
        cur.execute(store._HARVEST_CHECKPOINT_DDL)
        cur.execute(store._RUN_LEDGER_DDL)
        cur.execute(store._DIGEST_CACHE_DDL)
        cur.execute("DELETE FROM historical_article")
        cur.execute("DELETE FROM harvest_checkpoint")
        cur.execute("DELETE FROM history_run_ledger")
        cur.execute("DELETE FROM history_digest_cache")
    return store


def one(url=URL):
    with store._transaction() as cur:
        cur.execute("SELECT body, body_status, body_vintage, source_system, "
                    "content_sha256 FROM historical_article WHERE url = %s", (url,))
        return cur.fetchone()


@needs_db
class TestTheWriteIsIdempotent:
    def test_writing_twice_writes_one_row(self, db):
        db.upsert_articles([article_row()])
        db.upsert_articles([article_row()])
        with store._transaction() as cur:
            cur.execute("SELECT COUNT(*) FROM historical_article")
            assert cur.fetchone()[0] == 1

    def test_a_checkpoint_upsert_is_idempotent(self, db):
        for n in (3, 7):
            db.write_checkpoint("guardian", "PT", datetime.date(2018, 1, 1),
                                datetime.date(2018, 12, 31), items_written=n)
        with store._transaction() as cur:
            cur.execute("SELECT COUNT(*) FROM harvest_checkpoint")
            assert cur.fetchone()[0] == 1


@needs_db
class TestBodyBeatsStub:
    """The one rule that has to hold no matter which harvester ran first."""

    def _stub(self):
        return store.article_row(
            item(text=""), country_iso2="PT", source_system="gdelt",
            body_status="pending")

    def test_a_stub_arriving_second_cannot_blank_the_body(self, db):
        db.upsert_articles([article_row()])
        db.upsert_articles([self._stub()])
        body, status, _, source, _ = one()
        assert body.startswith("The central bank")
        assert status == "recovered" and source == "guardian"

    def test_a_body_arriving_second_fills_the_stub(self, db):
        db.upsert_articles([self._stub()])
        db.upsert_articles([article_row()])
        body, status, vintage, source, _ = one()
        assert body.startswith("The central bank")
        assert status == "recovered" and vintage == "api-native"

    def test_order_does_not_matter_within_one_batch(self, db):
        # Postgres refuses an ON CONFLICT that touches one row twice in a single
        # command, so the rule has to hold inside the batch too — either way round.
        assert db.upsert_articles([article_row(), self._stub()]) == 1
        assert one()[0].startswith("The central bank")

    def test_order_does_not_matter_within_one_batch_reversed(self, db):
        assert db.upsert_articles([self._stub(), article_row()]) == 1
        assert one()[0].startswith("The central bank")

    def test_a_later_harvest_cannot_undo_a_demotion(self, db):
        # A GDELT re-run must not push a demoted article back to 'pending' and
        # buy it a second billable leakage scan.
        db.upsert_articles([article_row()])
        db.mark_body(URL, body=None, body_status="degraded-title-only")
        db.upsert_articles([self._stub()])
        assert one()[1] == "degraded-title-only"
