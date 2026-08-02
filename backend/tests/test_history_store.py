"""Tests for the article substrate.

Split in two, deliberately.

The mapping from a canonical article item to a ``historical_article`` row is
pure, and it is where the mistakes that matter live — a wrong hash, a lost
theme, a silently-dropped article with no date. Those tests always run.

The rules that live in SQL — a body beating a stub, a harvester unable to lower
a status, an idempotent checkpoint — cannot be tested without a Postgres, and
pointing them at ``DATABASE_URL`` would mean a bare ``pytest`` run creating
tables in the production database. So they run only against a database named
explicitly by ``HISTORY_TEST_DATABASE_URL`` and skip otherwise. A default test
run touches no network and no database.
"""

import datetime
import os

import pytest

from backend.utils.history import store
from backend.utils.news_fetching import core

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


def row(**over):
    return store.article_row(item(**over.pop("item", {})), **{
        "country_iso2": "PT", "source_system": "guardian",
        "body_status": "recovered", "body_vintage": "api-native", **over})


class TestArticleRow:
    def test_the_item_round_trips(self):
        r = row()
        assert r["url"] == URL
        assert r["title"].startswith("Central bank raises")
        assert r["body"] == "The central bank raised interest rates on Wednesday."
        assert r["abstract"] == "The bank moved after a third month above target."
        assert r["country_iso2"] == "PT" and r["source_system"] == "guardian"
        assert r["tier"] == "full"

    def test_published_at_becomes_a_real_timestamp(self):
        assert row()["published_at"] == datetime.datetime(
            2018, 3, 14, 9, 30, tzinfo=datetime.timezone.utc)

    def test_the_hash_is_stable(self):
        assert row()["content_sha256"] == row()["content_sha256"]

    def test_the_hash_follows_the_body(self):
        assert row()["content_sha256"] != row(item={"text": "different text"})["content_sha256"]

    def test_the_hash_is_of_the_unmasked_body(self):
        # The whole point of storing raw text: the digest cache keys on this
        # hash, so it must be computable from the body sitting in the column.
        from backend.utils import provenance
        r = row()
        assert r["content_sha256"] == provenance.text_sha256(r["body"])

    def test_no_body_means_no_hash(self):
        # "no text" and "the hash of the empty string" are different facts.
        r = row(item={"text": ""}, body_status="pending", body_vintage=None)
        assert r["body"] is None and r["content_sha256"] is None

    def test_the_retrieving_theme_leads(self):
        assert row()["themes"][0] == "order"

    def test_the_classifier_tops_up_the_themes(self):
        # Snapshot assembly fills the same six-theme floor the live run uses, so
        # a row tagged only by the query that found it would under-serve it.
        r = row(item={"theme": "broad",
                      "title": "New customs permit rules follow the election",
                      "text": ""})
        assert r["themes"][0] == "broad"
        assert {"friction", "order"} <= set(r["themes"])

    def test_themes_never_repeat(self):
        r = row(item={"theme": "order", "title": "Parliament calls an election"})
        assert len(r["themes"]) == len(set(r["themes"]))

    def test_an_undated_article_is_refused(self):
        # published_at is NOT NULL because an article with no date cannot be
        # placed in any snapshot window. Failing loudly beats a silent drop.
        with pytest.raises(ValueError, match="no parseable publication date"):
            row(item={"published": None})

    def test_an_unparseable_date_is_refused(self):
        with pytest.raises(ValueError, match="no parseable publication date"):
            row(item={"published": "last Tuesday"})

    def test_an_item_with_no_url_is_refused(self):
        with pytest.raises(ValueError, match="no URL"):
            row(item={"link": ""})

    def test_an_unknown_status_is_refused(self):
        with pytest.raises(ValueError, match="body_status must be one of"):
            row(body_status="probably-fine")

    def test_a_gdelt_stub_is_a_valid_row(self):
        r = store.article_row(
            core.normalize_item(title="Lira slides again",
                                link="https://ex.test/a", published=PUBLISHED),
            country_iso2="TR", source_system="gdelt", body_status="pending")
        assert r["body"] is None and r["body_vintage"] is None
        assert r["body_status"] == "pending" and r["content_sha256"] is None


class TestMarkBodyValidation:
    def test_an_unknown_status_is_refused_before_any_write(self):
        with pytest.raises(ValueError, match="body_status must be one of"):
            store.mark_body("https://ex.test/a", body=None, body_status="lost")

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

@pytest.fixture()
def db(monkeypatch):
    """Point the store at the test database and start from an empty table."""
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    with store._transaction() as cur:
        cur.execute(store._HISTORICAL_ARTICLE_DDL)
        cur.execute(store._HARVEST_CHECKPOINT_DDL)
        cur.execute("DELETE FROM historical_article")
        cur.execute("DELETE FROM harvest_checkpoint")
    return store


def one(url=URL):
    with store._transaction() as cur:
        cur.execute("SELECT body, body_status, body_vintage, source_system, "
                    "content_sha256 FROM historical_article WHERE url = %s", (url,))
        return cur.fetchone()


@needs_db
class TestRoundTrip:
    def test_an_article_survives_the_write(self, db):
        assert db.upsert_articles([row()]) == 1
        body, status, vintage, source, sha = one()
        assert body.startswith("The central bank") and status == "recovered"
        assert vintage == "api-native" and source == "guardian"
        assert sha == row()["content_sha256"]

    def test_writing_twice_writes_one_row(self, db):
        db.upsert_articles([row()])
        db.upsert_articles([row()])
        assert len(db.read_pending()) == 0
        with store._transaction() as cur:
            cur.execute("SELECT COUNT(*) FROM historical_article")
            assert cur.fetchone()[0] == 1


@needs_db
class TestBodyBeatsStub:
    """The one rule that has to hold no matter which harvester ran first."""

    def _stub(self):
        return store.article_row(
            item(text=""), country_iso2="PT", source_system="gdelt",
            body_status="pending")

    def test_a_stub_arriving_second_cannot_blank_the_body(self, db):
        db.upsert_articles([row()])
        db.upsert_articles([self._stub()])
        body, status, _, source, _ = one()
        assert body.startswith("The central bank")
        assert status == "recovered" and source == "guardian"

    def test_a_body_arriving_second_fills_the_stub(self, db):
        db.upsert_articles([self._stub()])
        db.upsert_articles([row()])
        body, status, vintage, source, _ = one()
        assert body.startswith("The central bank")
        assert status == "recovered" and vintage == "api-native" and source == "guardian"

    def test_order_does_not_matter_within_one_batch(self, db):
        # Postgres refuses an ON CONFLICT that touches one row twice in a single
        # command, so the rule has to hold inside the batch too — and it must
        # hold whichever way round the two copies happen to be listed.
        assert db.upsert_articles([row(), self._stub()]) == 1
        assert one()[0].startswith("The central bank")

    def test_order_does_not_matter_within_one_batch_reversed(self, db):
        assert db.upsert_articles([self._stub(), row()]) == 1
        assert one()[0].startswith("The central bank")


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
        assert (body, status, vintage) == ("Recovered text.", "recovered", "wayback-20180315")
        assert sha is not None

    def test_a_flagged_live_refetch_is_demoted_and_the_body_discarded(self, db):
        db.upsert_articles([row(body_vintage="live-refetch")])
        db.mark_body(URL, body=None,
                     body_status="degraded-title-only", body_vintage="live-refetch")
        body, status, _, _, sha = one()
        assert body is None and status == "degraded-title-only" and sha is None

    def test_a_later_harvest_cannot_undo_a_demotion(self, db):
        # A GDELT re-run must not push a demoted article back to 'pending' and
        # buy it a second billable leakage scan.
        db.upsert_articles([row()])
        db.mark_body(URL, body=None, body_status="degraded-title-only")
        db.upsert_articles([store.article_row(
            item(text=""), country_iso2="PT", source_system="gdelt",
            body_status="pending")])
        assert one()[1] == "degraded-title-only"

    def test_a_failure_is_recorded_and_leaves_the_queue(self, db):
        db.upsert_articles([store.article_row(
            item(text=""), country_iso2="PT", source_system="gdelt",
            body_status="pending")])
        db.mark_body(URL, body=None, body_status="failed")
        assert db.read_pending() == []


@needs_db
class TestCheckpoints:
    def test_upsert_is_idempotent(self, db):
        for n in (3, 7):
            db.write_checkpoint("guardian", "PT", datetime.date(2018, 1, 1),
                                datetime.date(2018, 12, 31), items_written=n)
        with store._transaction() as cur:
            cur.execute("SELECT items_written FROM harvest_checkpoint")
            assert cur.fetchall() == [(7,)]

    def test_completed_windows_is_what_a_rerun_skips(self, db):
        db.write_checkpoint("guardian", "PT", datetime.date(2018, 1, 1),
                            datetime.date(2018, 12, 31))
        db.write_checkpoint("guardian", "PT", datetime.date(2019, 1, 1),
                            datetime.date(2019, 12, 31), status="quota-exhausted")
        # Only 'done' counts — an interrupted window must be retried, not skipped.
        assert db.completed_windows("guardian", "PT") == {datetime.date(2018, 1, 1)}

    def test_windows_are_per_source_and_country(self, db):
        db.write_checkpoint("guardian", "PT", datetime.date(2018, 1, 1),
                            datetime.date(2018, 12, 31))
        assert db.completed_windows("gdelt", "PT") == set()
        assert db.completed_windows("guardian", "BR") == set()


@needs_db
class TestExistingUrls:
    def test_it_finds_the_overlap(self, db):
        db.upsert_articles([row()])
        urls = [row()["url"], "https://ex.test/never-seen"]
        assert db.existing_urls(urls) == {row()["url"]}

    def test_an_empty_ask_does_not_query(self, db):
        assert db.existing_urls([]) == set()
