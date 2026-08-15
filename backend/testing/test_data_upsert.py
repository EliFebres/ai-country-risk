"""The write layer, and the seams where a writer and its reader disagree.

The defects this branch kept finding all had the same shape: both sides were
individually correct and the thing between them dropped something on the floor.
`evidence_texture` read `tier` and `source_system`, `to_item` set them, and the
manifest in between never copied them — so the meter returned
`abstract_share 0.000` for every country-year, which reads as "the corpus is
uniform" and means "nothing was measured".

So the tests here that matter most cross a seam rather than passing on one side
of it: a written artifact is read back by the thing that needs it.

No database except the opt-in Postgres block (`HISTORY_TEST_DATABASE_URL`).
"""

import datetime
import hashlib
import json
import os
from datetime import date

import pytest

from backend.util import provenance
from backend.data_upsert import schema, store
from backend.news_fetching import snapshot_select
from backend.util.pilot import reports
from backend.news_fetching import core

AS_OF = date(2026, 7, 27)


# ---------------------------------------------------------------------------
# Provenance — the record that makes a stored score reproducible
# ---------------------------------------------------------------------------

class TestTextSha256:
    def test_known_vector(self):
        # Pinned against hashlib itself: this hash ends up in the database, so a
        # change in how the text is encoded must fail here, not silently.
        assert provenance.text_sha256("abc") == hashlib.sha256(b"abc").hexdigest()

    def test_empty_gives_none(self):
        # "No text" and "the hash of the empty string" are different facts.
        assert provenance.text_sha256("") is None
        assert provenance.text_sha256(None) is None

    def test_unicode_is_utf8(self):
        text = "Grécia — inflação 25%"
        assert provenance.text_sha256(text) == \
            hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestArticleManifestEntry:
    def test_prefers_content_over_text(self):
        got = provenance.article_manifest_entry(
            {"id": "a1", "content": "full body", "text": "trafilatura"})
        assert got["content_sha256"] == provenance.text_sha256("full body")
        assert got["content_chars"] == len("full body")

    def test_prefers_link_over_publisher_link(self):
        # `resolve_and_enrich` overwrites `link` with the resolved publisher URL,
        # and that is the value risk_snapshot_article.url stores.
        got = provenance.article_manifest_entry({
            "id": "a1", "link": "https://resolved.example/x",
            "publisher_link": "https://stale.example/x",
        })
        assert got["url"] == "https://resolved.example/x"

    def test_carries_the_tier(self):
        # An NYT archive row is a headline and two sentences and a Guardian row
        # is a body; both count as one article everywhere else. Without this the
        # only record of a snapshot's evidence thinness is the ration log of the
        # run that built it, and `reports.evidence_texture` reported 0.000.
        got = provenance.article_manifest_entry({"id": "a1", "tier": "abstract-only"})
        assert got["tier"] == "abstract-only"

    def test_an_untiered_item_is_none_rather_than_absent(self):
        # The live daily path sets no tier. A key that is present and None is
        # readable as "not measured"; a missing key reads as a bug.
        assert provenance.article_manifest_entry({"id": "a1"})["tier"] is None

    def test_in_prompt_follows_prompt_entry(self):
        without = provenance.article_manifest_entry({"id": "a1"})
        with_entry = provenance.article_manifest_entry({"id": "a1"},
                                                       {"id": "a1", "title": "t"})
        assert without["in_prompt"] is False and without["prompt_text_sha256"] is None
        assert with_entry["in_prompt"] is True
        assert with_entry["prompt_text_sha256"] is not None

    def test_prompt_hash_is_key_order_independent(self):
        # Two runs building the same entry from differently-ordered dicts must
        # hash identically, or every re-run looks like the inputs changed.
        one = provenance.article_manifest_entry({"id": "a1"}, {"id": "a1", "title": "t"})
        two = provenance.article_manifest_entry({"id": "a1"}, {"title": "t", "id": "a1"})
        assert one["prompt_text_sha256"] == two["prompt_text_sha256"]

    def test_prompt_hash_changes_with_content(self):
        one = provenance.article_manifest_entry({"id": "a1"}, {"id": "a1", "digest": {"x": 1}})
        two = provenance.article_manifest_entry({"id": "a1"}, {"id": "a1", "digest": {"x": 2}})
        assert one["prompt_text_sha256"] != two["prompt_text_sha256"]

    def test_a_malformed_item_does_not_crash_the_manifest(self):
        got = provenance.build_article_manifest([{"id": "a1"}, None, "junk", 7], [])
        assert [e["id"] for e in got] == ["a1"]

    def test_fulltext_ids_marked(self):
        items = [{"id": "a1", "content": "one"}, {"id": "a2", "text": "two"}]
        got = provenance.build_article_manifest(items, [], fulltext_ids=["a2"])
        assert {e["id"]: e["in_fulltext"] for e in got} == {"a1": False, "a2": True}


def _payload():
    """A realistic ``prepare_llm_payload_pretty`` shape, trimmed."""
    return {
        "country": "PT",
        "latest_year": 2025,
        "indicators": {
            "Inflation (CPI %)": {"latest": 2.3,
                                  "series": {"2023": 4.3, "2024": 2.7, "2025": 2.3}},
            "Political Corruption Index": {"latest": 0.1,
                                           "series": {"2023": 0.1, "2024": None}},
        },
        "_meta": {
            "units": {"Inflation (CPI %)": "%"},
            "source": "World Bank",
            "generated_at": "2026-07-26T09:14:00+00:00",
            "series_lookback": 10,
        },
    }


class TestMacroVintages:
    def test_reads_source_and_generated_at(self):
        got = provenance.macro_vintages(_payload())
        assert got["panel_source"] == "World Bank"
        assert got["panel_generated_at"] == "2026-07-26T09:14:00+00:00"
        assert got["latest_year"] == 2025

    def test_latest_year_per_indicator_ignores_nulls(self):
        got = provenance.macro_vintages(_payload())["latest_year_by_indicator"]
        assert got == {"Inflation (CPI %)": 2025, "Political Corruption Index": 2023}

    def test_missing_meta_degrades_but_keeps_scheme(self):
        got = provenance.macro_vintages({"indicators": {}})
        assert got["vintage_scheme"] == "as-published-latest"
        assert got["panel_source"] is None and got["panel_generated_at"] is None

    def test_the_evidence_payload_is_the_wrong_one_and_says_so_in_nulls(self):
        """Why the rebuild script has to build the panel payload itself.

        There are two payloads in the pipeline. `build_evidence_payload` makes
        the one the model reads as evidence; `prepare_llm_payload_pretty` makes
        the panel, and only the panel carries `_meta` and `indicators`. Handed
        the evidence payload, this function does not raise — it degrades every
        field to None, so a rebuilt manifest diffs cleanly against nothing and
        reports `DIFFERS` on a row that was fine. A silent None is the failure
        mode worth pinning.
        """
        got = provenance.macro_vintages({"structural": {"a": 1}, "series": {}})
        assert got["panel_source"] is None and got["panel_generated_at"] is None
        assert got["latest_year"] is None and got["latest_year_by_indicator"] == {}


class TestBuildInputManifest:
    def _build(self, monkeypatch, **over):
        monkeypatch.delenv("GIT_SHA", raising=False)
        kwargs = dict(
            items=[{"id": "a1", "content": "body", "link": "https://e.example/1"}],
            prompt_entries=[{"id": "a1", "title": "t"}],
            fulltext_ids=["a1"],
            payload=_payload(),
            model_id="gpt-4o-2024-08-06",
            prompt_version="v2.0",
            policy_version="p2.0",
            seed=42,
        )
        kwargs.update(over)
        return provenance.build_input_manifest(**kwargs)

    def test_stamps_and_articles(self, monkeypatch):
        got = self._build(monkeypatch)
        assert got["schema_version"] == 1
        assert got["model_id"] == "gpt-4o-2024-08-06"
        assert got["prompt_version"] == "v2.0" and got["policy_version"] == "p2.0"
        assert got["seed"] == 42
        assert len(got["articles"]) == 1 and got["articles"][0]["in_prompt"] is True
        assert got["macro_vintages"]["vintage_scheme"] == "as-published-latest"

    def test_git_sha_read_from_env(self, monkeypatch):
        monkeypatch.setenv("GIT_SHA", "deadbeef")
        got = provenance.build_input_manifest(
            items=[], prompt_entries=[], payload={}, model_id=None,
            prompt_version=None, policy_version=None, seed=None,
        )
        assert got["git_sha"] == "deadbeef"

    def test_json_serializable(self, monkeypatch):
        # It goes into a JSONB column; anything unserializable must fail here.
        json.dumps(self._build(monkeypatch), ensure_ascii=False)


class TestTheSnapshotPayloadParsesForTheUpsert:
    """``data_push`` imports psycopg2 at module level — skip where it is absent."""

    def _payload_with(self, **over):
        payload = _payload()
        payload["llm_output"] = {"score": 0.42, "bullet_summary": "ok"}
        payload.update(over)
        return payload

    def test_input_manifest_carried(self):
        pytest.importorskip("psycopg2")
        from backend.data_upsert import data_push

        manifest = {"schema_version": 1, "articles": []}
        got = data_push._parse_snapshot_payload(
            self._payload_with(input_manifest=manifest))
        assert got.input_manifest == manifest

    def test_absent_manifest_still_parses(self):
        # Constraint: a snapshot written before provenance existed must upsert.
        pytest.importorskip("psycopg2")
        from backend.data_upsert import data_push

        got = data_push._parse_snapshot_payload(self._payload_with())
        assert got.input_manifest is None and got.legal_gate is None
        assert got.llm_out["score"] == 0.42

    def test_malformed_manifest_becomes_null(self):
        pytest.importorskip("psycopg2")
        from backend.data_upsert import data_push

        got = data_push._parse_snapshot_payload(
            self._payload_with(input_manifest="not-a-dict"))
        assert got.input_manifest is None

    def test_legal_gate_carried_from_llm_output(self):
        pytest.importorskip("psycopg2")
        from backend.data_upsert import data_push

        gate = {"name": "Russia", "rule": "prohibition", "sources": ["eu-2022"]}
        payload = self._payload_with()
        payload["llm_output"]["legal_gate"] = gate
        assert data_push._parse_snapshot_payload(payload).legal_gate == gate


# ---------------------------------------------------------------------------
# The manifest carries what its readers actually read
# ---------------------------------------------------------------------------

@pytest.fixture
def _corpus():
    """Two Guardian bodies and one NYT abstract, through `to_item`.

    Built with the real `to_item` rather than hand-written dicts: the bug was
    that the manifest disagreed with what `to_item` produces, so a fixture that
    hand-rolls the item shape would have passed against the broken code.
    """
    rows = [
        {"url": "https://g/1", "title": "a", "body": "full body one",
         "body_vintage": "api-native", "source_system": "guardian",
         "tier": "full", "published_at": None},
        {"url": "https://g/2", "title": "b", "body": "full body two",
         "body_vintage": "api-native", "source_system": "guardian",
         "tier": "full", "published_at": None},
        {"url": "https://n/1", "title": "c", "body": "",
         "source_system": "nyt", "tier": "abstract-only", "published_at": None},
    ]
    return [snapshot_select.to_item(r, datetime.date(2019, 6, 3)) for r in rows]


def _ledger_row(iso2, as_of, items):
    """A ledger row shaped the way `score.score_one` writes one."""
    return {"country_iso2": iso2, "as_of": as_of,
            "manifest": {"articles": [provenance.article_manifest_entry(i)
                                      for i in items]}}


class TestEvidenceTexture:
    def test_the_manifest_carries_what_the_meter_reads(self, _corpus, monkeypatch):
        monkeypatch.setattr(
            store, "read_runs",
            lambda mode=None: [_ledger_row("PT", datetime.date(2019, 6, 3), _corpus)])
        got = reports.evidence_texture(["PT"])["PT 2019"]

        assert got["articles"] == 3
        assert got["guardian"] == 2, "the source mix came back empty"
        assert got["nyt"] == 1 and got["abstract"] == 1
        assert got["abstract_share"] == round(1 / 3, 3)
        assert got["articles_per_snapshot"] == 3.0

    def test_a_country_outside_the_roster_is_skipped_not_an_error(self, _corpus,
                                                                  monkeypatch):
        """BR left the roster and kept its harvest. Its rows are simply not in
        this report — the one thing that must not happen is a raise."""
        monkeypatch.setattr(
            store, "read_runs",
            lambda mode=None: [_ledger_row("BR", datetime.date(2019, 6, 3), _corpus)])
        assert reports.evidence_texture(["PT"]) == {}

    def test_an_empty_week_does_not_divide_by_zero(self, monkeypatch):
        monkeypatch.setattr(
            store, "read_runs",
            lambda mode=None: [{"country_iso2": "PT",
                                "as_of": datetime.date(2019, 6, 3),
                                "manifest": {"articles": 0}}])
        assert reports.evidence_texture(["PT"]) == {}


class TestDivergenceIsSigned:
    """|masked - named| answered "how far apart" and threw away "which way",
    which is the finding: masking scoring a country riskier than its name did
    means the name carried reassurance, safer means it carried alarm. Opposite
    defects, opposite fixes, one number."""

    @staticmethod
    def _arms(monkeypatch, masked, named, bare=None):
        """Three arms over the same dates, as the readers hand them over."""
        monkeypatch.setattr(reports, "_masked_scores", lambda roster: masked)
        monkeypatch.setattr(
            reports, "_arm_scores",
            lambda mode: named if mode == "named" else (bare or {}))

    def test_the_sign_survives(self, monkeypatch):
        day = datetime.date(2019, 6, 3)
        self._arms(monkeypatch, {("PT", day): 0.40}, {("PT", day): 0.55})
        row = reports.divergence(["PT"])["PT"]
        assert row["overall"] == -0.15, "masked scored it safer; the sign says so"
        assert row["abs_overall"] == 0.15

    def test_opposite_weeks_cannot_cancel_into_a_clean_zero(self, monkeypatch):
        """The reason both are reported. A country diverging hard in both
        directions has a signed mean near zero, and reading that alone as
        "masking is clean" is the failure an absolute mean was guarding."""
        a, b = datetime.date(2019, 6, 3), datetime.date(2019, 6, 10)
        self._arms(monkeypatch,
                   {("PT", a): 0.60, ("PT", b): 0.20},
                   {("PT", a): 0.40, ("PT", b): 0.40})
        row = reports.divergence(["PT"])["PT"]
        assert row["overall"] == 0.0 and row["abs_overall"] == 0.2

    def test_structural_recovery_reads_the_magnitudes(self, monkeypatch):
        """Off the signed means, a bare arm diverging the other way would score
        as a large recovery — the block would look like it was working hardest
        exactly where it had stopped working."""
        day = datetime.date(2019, 6, 3)
        self._arms(monkeypatch, {("PT", day): 0.45}, {("PT", day): 0.50},
                   bare={("PT", day): 0.70})
        row = reports.divergence(["PT"])["PT"]
        assert row["overall"] == -0.05 and row["without_structural"] == 0.2
        # 0.20 - 0.05 on the magnitudes. Signed it would have been 0.25.
        assert row["structural_recovery"] == 0.15

    def test_the_ranking_carries_both(self, monkeypatch):
        day = datetime.date(2019, 6, 3)
        self._arms(monkeypatch, {("PT", day): 0.40}, {("PT", day): 0.55})
        top = reports.structural_candidates(["PT"])[0]
        assert top["divergence"] == 0.15 and top["signed_divergence"] == -0.15


class TestLintFindingsAreReadBackNotJustWritten:
    """The half of observe-only that was missing.

    Enforcement was deleted on the argument that a contradiction would be
    written down next to the score and looked at. `upsert_lint_findings` had
    been writing `risk_lint` on every run since; nothing anywhere read it. A
    tripwire with no reader is not a safety net, and the argument for deleting
    enforcement was only sound with the reader in place.
    """

    def findings(self):
        return [
            {"country_iso2": "RU", "as_of": date(2026, 7, 28),
             "rule": "war_flag_vs_low_score", "detail": {"score_12m": 44},
             "created_at": None},
            {"country_iso2": "TR", "as_of": date(2026, 7, 28),
             "rule": "war_flag_vs_low_score", "detail": {"score_12m": 51},
             "created_at": None},
            {"country_iso2": "TR", "as_of": date(2026, 7, 28),
             "rule": "suppressed_calm_vs_low_uncertainty", "detail": {},
             "created_at": None},
        ]

    def test_the_daily_run_logs_what_lint_found(self, monkeypatch, caplog):
        from backend.util import pipeline

        monkeypatch.setattr(pipeline.data_push, "read_lint_findings",
                            lambda **_kw: self.findings())
        monkeypatch.setattr(pipeline.data_push, "read_stage1_degradation",
                            lambda **_kw: [])
        with caplog.at_level("WARNING"):
            pipeline.log_run_summary(as_of=date(2026, 7, 28))
        assert "3 finding(s)" in caplog.text
        assert "war_flag_vs_low_score" in caplog.text
        # Named, so the reader knows where to look rather than only how many.
        assert "RU" in caplog.text and "TR" in caplog.text

    def test_a_summary_failure_never_touches_the_run(self, monkeypatch):
        """It runs after every write the run was going to make."""
        from backend.util import pipeline

        def boom(**_kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(pipeline.data_push, "read_lint_findings", boom)
        monkeypatch.setattr(pipeline.data_push, "read_stage1_degradation", boom)
        pipeline.log_run_summary(as_of=date(2026, 7, 28))   # must not raise

    def test_the_report_groups_findings_by_rule(self, monkeypatch):
        monkeypatch.setattr(reports.data_push, "read_lint_findings",
                            lambda **_kw: self.findings())
        got = reports.lint_findings(["RU", "TR"])
        assert got["total"] == 3
        assert got["by_rule"]["war_flag_vs_low_score"]["countries"] == ["RU", "TR"]
        # A rule firing across the roster is a different problem from one
        # country tripping one rule, so the count has to survive the grouping.
        assert got["by_rule"]["war_flag_vs_low_score"]["n"] == 2

    def test_the_report_ignores_countries_outside_the_roster(self, monkeypatch):
        monkeypatch.setattr(reports.data_push, "read_lint_findings",
                            lambda **_kw: self.findings())
        assert reports.lint_findings(["RU"])["total"] == 1


class TestStage1DegradationIsSurfaced:
    """`28a8889` recorded stage-1 degradation so that "the scorer read digests"
    and "the scorer read truncated bodies" would stop being indistinguishable.
    Nothing read the block, so they stayed exactly as indistinguishable."""

    def rows(self):
        return [
            {"country_iso2": "PT", "as_of": date(2026, 7, 28), "articles": 20,
             "digested": 17, "degraded": 3, "degraded_ids": ["a4", "a9", "a15"]},
            {"country_iso2": "PT", "as_of": date(2026, 7, 21), "articles": 20,
             "digested": 19, "degraded": 1, "degraded_ids": ["a2"]},
        ]

    def test_the_daily_run_warns_about_degraded_snapshots(self, monkeypatch, caplog):
        from backend.util import pipeline

        monkeypatch.setattr(pipeline.data_push, "read_lint_findings", lambda **_kw: [])
        monkeypatch.setattr(pipeline.data_push, "read_stage1_degradation",
                            lambda **_kw: self.rows())
        with caplog.at_level("WARNING"):
            pipeline.log_run_summary(as_of=date(2026, 7, 28))
        assert "truncated bodies" in caplog.text and "PT" in caplog.text

    def test_the_report_aggregates_the_degraded_share(self, monkeypatch):
        monkeypatch.setattr(reports.data_push, "read_stage1_degradation",
                            lambda **_kw: self.rows())
        got = reports.stage1_degradation(["PT"])
        assert got["affected_snapshots"] == 2
        assert got["per_country"]["PT"]["degraded"] == 4
        assert got["per_country"]["PT"]["degraded_share"] == 0.1


# ---------------------------------------------------------------------------
# The article row — where a wrong hash or a lost theme actually happens
# ---------------------------------------------------------------------------

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


class TestArticleRow:
    def test_the_item_round_trips(self):
        r = article_row()
        assert r["url"] == URL
        assert r["body"] == "The central bank raised interest rates on Wednesday."
        assert r["abstract"] == "The bank moved after a third month above target."
        assert r["country_iso2"] == "PT" and r["source_system"] == "guardian"
        assert r["tier"] == "full"

    def test_published_at_becomes_a_real_timestamp(self):
        assert article_row()["published_at"] == datetime.datetime(
            2018, 3, 14, 9, 30, tzinfo=datetime.timezone.utc)

    def test_the_hash_is_of_the_unmasked_body(self):
        # The whole point of storing raw text: the digest cache keys on this
        # hash, so it must be computable from the body sitting in the column.
        r = article_row()
        assert r["content_sha256"] == provenance.text_sha256(r["body"])

    def test_the_hash_follows_the_body(self):
        assert article_row()["content_sha256"] != \
            article_row(item={"text": "different text"})["content_sha256"]

    def test_no_body_means_no_hash(self):
        # "no text" and "the hash of the empty string" are different facts.
        r = article_row(item={"text": ""}, body_status="pending", body_vintage=None)
        assert r["body"] is None and r["content_sha256"] is None

    def test_the_classifier_tops_up_the_themes(self):
        # Snapshot assembly fills the same six-theme floor the live run uses, so
        # a row tagged only by the query that found it would under-serve it.
        r = article_row(item={"theme": "broad",
                              "title": "New customs permit rules follow the election",
                              "text": ""})
        assert r["themes"][0] == "broad"
        assert {"friction", "order"} <= set(r["themes"])
        assert len(r["themes"]) == len(set(r["themes"]))

    def test_an_undated_article_is_refused(self):
        # published_at is NOT NULL because an article with no date cannot be
        # placed in any snapshot window. Failing loudly beats a silent drop.
        with pytest.raises(ValueError, match="no parseable publication date"):
            article_row(item={"published": None})

    def test_an_item_with_no_url_is_refused(self):
        with pytest.raises(ValueError, match="no URL"):
            article_row(item={"link": ""})

    def test_an_unknown_status_is_refused(self):
        with pytest.raises(ValueError, match="body_status must be one of"):
            article_row(body_status="probably-fine")

    def test_the_status_ladder_is_ordered_worst_to_best(self):
        # The upsert's rank comparison reads this order out of a Postgres array
        # literal; if the two ever disagree a harvester could downgrade a
        # recovered body back to pending and buy a second billable scan.
        assert store.BODY_STATUSES == (
            "pending", "failed", "degraded-title-only", "recovered")
        assert "'" + "','".join(store.BODY_STATUSES) + "'" in store._STATUS_RANK


# ---------------------------------------------------------------------------
# Against a real Postgres — opt in with HISTORY_TEST_DATABASE_URL
# ---------------------------------------------------------------------------

TEST_DB = os.getenv("HISTORY_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB, reason="set HISTORY_TEST_DATABASE_URL to run")


@pytest.fixture()
def db(monkeypatch):
    """Point the store at the test database and start from an empty table."""
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    with store._transaction() as cur:
        schema.create_all(cur)
        for table in ("article", "run_ledger", "llm_artifact", "snapshot_diagnostic"):
            cur.execute(f"DELETE FROM {table}")
    return store


def one(url=URL):
    with store._transaction() as cur:
        cur.execute("SELECT body, body_status, body_vintage, source_system, "
                    "content_sha256 FROM article WHERE url = %s", (url,))
        return cur.fetchone()


@needs_db
class TestBodyStatusTransitions:
    def test_recovery_marks_a_pending_article(self, db):
        db.upsert_articles([store.article_row(
            item(text=""), country_iso2="PT", source_system="gdelt",
            body_status="pending")])
        db.mark_body(URL, body="Recovered text.",
                     body_status="recovered", body_vintage="wayback-20180315",
                     wayback_url="https://web.archive.org/web/20180315id_/x")
        body, status, vintage, _, sha = one()
        assert (body, status, vintage) == ("Recovered text.", "recovered",
                                           "wayback-20180315")
        assert sha is not None

    def test_a_flagged_live_refetch_is_demoted_and_the_body_discarded(self, db):
        db.upsert_articles([article_row(body_vintage="live-refetch")])
        db.mark_body(URL, body=None,
                     body_status="degraded-title-only", body_vintage="live-refetch")
        body, status, _, _, sha = one()
        assert body is None and status == "degraded-title-only" and sha is None

    def test_a_failure_is_recorded_and_leaves_the_queue(self, db):
        db.upsert_articles([store.article_row(
            item(text=""), country_iso2="PT", source_system="gdelt",
            body_status="pending")])
        db.mark_body(URL, body=None, body_status="failed")
        assert db.read_pending() == []
