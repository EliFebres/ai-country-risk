"""
Tests for backend/data_upsert.

Consumer-side tests: each one asserts that what was written is read back and
acted on. None of them assert that a write happened — a write nobody reads is
the failure these tests exist to catch.

The suite touches no database. `data_push`'s module-level functions are replaced
at the attribute boundary, which is where the rest of this suite draws the line.
"""

import pytest

from backend.data_upsert import data_push
from backend.util import constants, pipeline


class TestSeedRoster:
    """`country` is the parent every foreign key points at, so the run proves it arrived."""

    def test_every_roster_country_is_read_back(self, monkeypatch):
        """The happy path: what the roster promised is what the database returns."""
        seen = {}

        def fake_upsert(roster=None):
            rows = roster if roster is not None else constants.COUNTRY_ROSTER
            seen.update({c["iso2"]: c for c in rows})
            return len(rows)

        monkeypatch.setattr(data_push, "upsert_countries", fake_upsert)
        monkeypatch.setattr(data_push, "read_countries", lambda: dict(seen))

        assert pipeline.seed_roster() == len(constants.COUNTRY_ROSTER)

    def test_a_country_that_did_not_arrive_stops_the_run(self, monkeypatch):
        """A silent partial write is the bug; it has to be loud at the seed, not
        a hundred lines later as a foreign-key error on one country."""
        monkeypatch.setattr(data_push, "upsert_countries", lambda roster=None: 48)
        monkeypatch.setattr(
            data_push,
            "read_countries",
            lambda: {
                c["iso2"]: {"name": c["name"]}
                for c in constants.COUNTRY_ROSTER
                if c["iso2"] != "TW"
            },
        )

        with pytest.raises(RuntimeError, match="TW"):
            pipeline.seed_roster()

    def test_a_write_that_did_nothing_at_all_stops_the_run(self, monkeypatch):
        """`upsert_countries` returning cleanly is not evidence of anything."""
        monkeypatch.setattr(data_push, "upsert_countries", lambda roster=None: 0)
        monkeypatch.setattr(data_push, "read_countries", dict)

        with pytest.raises(RuntimeError, match="country seed did not reach"):
            pipeline.seed_roster()


class TestRoster:
    """The roster is the single source of truth, so its shape is pinned here."""

    def test_is_the_msci_universe_plus_russia(self):
        tiers = {}
        for c in constants.COUNTRY_ROSTER:
            tiers[c["tier"]] = tiers.get(c["tier"], 0) + 1
        assert tiers == {"DM": 23, "EM": 24, "Special": 1}
        assert len(constants.COUNTRY_ROSTER) == 48

    def test_codes_are_unique_and_well_formed(self):
        iso2 = [c["iso2"] for c in constants.COUNTRY_ROSTER]
        iso3 = [c["iso3"] for c in constants.COUNTRY_ROSTER]
        assert len(set(iso2)) == len(iso2)
        assert len(set(iso3)) == len(iso3)
        assert all(len(c) == 2 and c.isupper() for c in iso2)
        assert all(len(c) == 3 and c.isupper() for c in iso3)

    def test_every_entry_carries_a_map_position(self):
        """The front-end reads positions from `country`, seeded from here, so a
        missing coordinate is a country that renders nowhere."""
        for c in constants.COUNTRY_ROSTER:
            assert -90 <= c["lat"] <= 90, c["name"]
            assert -180 <= c["lng"] <= 180, c["name"]

    def test_derived_lookups_cover_the_whole_roster(self):
        for m in (constants.ISO3_BY_ISO2, constants.COUNTRY_NAME_BY_ISO2, constants.TIER_BY_ISO2):
            assert len(m) == len(constants.COUNTRY_ROSTER)


# ---------------------------------------------------------------------------
# The evidence store: `article` bodies and the `llm_artifact` cache.
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Records what was executed, and answers `verify`'s existence probe."""

    def __init__(self, present=True):
        self.executed = []
        self._present = present

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return ("public.article",) if self._present else (None,)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.committed = False
        self.rolled_back = False
        self.autocommit = True

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


class TestSchemaProvisioning:
    def test_create_all_provisions_both_tables(self):
        from backend.data_upsert import schema

        cur = _FakeCursor()
        assert schema.create_all(cur) == ["article", "llm_artifact"]
        sql = " ".join(s for s, _ in cur.executed)
        assert "CREATE TABLE IF NOT EXISTS article" in sql
        assert "CREATE TABLE IF NOT EXISTS llm_artifact" in sql

    def test_every_statement_is_safe_to_run_again(self):
        """`create_all` runs on every startup, so a second run must be a no-op."""
        from backend.data_upsert import schema

        cur = _FakeCursor()
        schema.create_all(cur)
        for sql, _ in cur.executed:
            assert "IF NOT EXISTS" in sql, sql

    def test_the_artifact_key_carries_version_and_mode(self):
        """Two answers to the same text under different prompts are different rows."""
        from backend.data_upsert import schema

        assert (
            "PRIMARY KEY (content_sha256, kind, version, mode)"
            in " ".join(schema.LLM_ARTIFACT.split())
        )

    def test_provisioning_that_created_nothing_is_an_error(self, monkeypatch):
        """A CREATE TABLE IF NOT EXISTS that returns without raising is not
        evidence that the table is there. This is the consumer-side check."""
        from backend.data_upsert import store

        cur = _FakeCursor(present=False)
        monkeypatch.setattr(store, "_connect", lambda: _FakeConn(cur))

        with pytest.raises(RuntimeError, match="did not create"):
            store.ensure_schema()

    def test_provisioning_that_worked_reports_every_table(self, monkeypatch):
        from backend.data_upsert import store

        cur = _FakeCursor(present=True)
        monkeypatch.setattr(store, "_connect", lambda: _FakeConn(cur))

        assert store.ensure_schema() == {"article": True, "llm_artifact": True}


class TestArtifactCache:
    def test_a_lookup_is_keyed_on_kind_version_and_mode(self, monkeypatch):
        """The whole point of deriving `version` from the prompt text is that an
        edited prompt stops matching. That only holds if the read asks for it."""
        from backend.data_upsert import store

        cur = _FakeCursor()
        cur.fetchall = lambda: []
        monkeypatch.setattr(store, "_connect", lambda: _FakeConn(cur))

        store.read_artifacts(["h1", "h2"], kind="digest", version="v-abc", mode="named")

        sql, params = cur.executed[-1]
        assert "FROM llm_artifact" in sql
        assert params == ("digest", "v-abc", "named", ["h1", "h2"])

    def test_a_miss_is_an_absent_key_not_a_null(self, monkeypatch):
        """"We have no answer" and "the model answered null" are different facts."""
        from backend.data_upsert import store

        cur = _FakeCursor()
        cur.fetchall = lambda: [("h1", {"label": "structural"})]
        monkeypatch.setattr(store, "_connect", lambda: _FakeConn(cur))

        got = store.read_artifacts(["h1", "h2"], kind="relevance", version="v1")
        assert got == {"h1": {"label": "structural"}}
        assert "h2" not in got

    def test_nothing_to_look_up_asks_the_database_nothing(self, monkeypatch):
        from backend.data_upsert import store

        def boom():
            raise AssertionError("opened a connection for an empty lookup")

        monkeypatch.setattr(store, "_connect", boom)
        assert store.read_artifacts([], kind="digest", version="v1") == {}

    def test_a_written_row_carries_the_version_that_produced_it(self, monkeypatch):
        from backend.data_upsert import store

        cur = _FakeCursor()
        captured = {}
        monkeypatch.setattr(store, "_connect", lambda: _FakeConn(cur))
        monkeypatch.setattr(
            store.extras,
            "execute_values",
            lambda c, sql, rows: captured.update(sql=" ".join(sql.split()), rows=rows),
        )

        store.write_artifacts(
            [("h1", {"label": "incident"})],
            kind="relevance",
            version="v-abc",
            model="gpt-4o-mini-2024-07-18",
        )

        h, kind, version, mode, model, _payload = captured["rows"][0]
        assert (h, kind, version, mode) == ("h1", "relevance", "v-abc", "named")
        assert model == "gpt-4o-mini-2024-07-18"
        assert "ON CONFLICT (content_sha256, kind, version, mode) DO NOTHING" in captured["sql"]


class TestArticleStore:
    def test_a_row_without_a_country_is_not_stored(self, monkeypatch):
        """`country_iso2` is what every downstream read filters on."""
        from backend.data_upsert import store

        def boom():
            raise AssertionError("opened a connection for nothing to write")

        monkeypatch.setattr(store, "_connect", boom)
        assert store.upsert_articles([{"url": "http://x", "body_status": "full"}]) == 0
        assert store.upsert_articles([{"country_iso2": "PT"}]) == 0

    def test_a_later_stub_cannot_erase_a_body_already_paid_for(self, monkeypatch):
        from backend.data_upsert import store

        cur = _FakeCursor()
        captured = {}
        monkeypatch.setattr(store, "_connect", lambda: _FakeConn(cur))
        monkeypatch.setattr(
            store.extras,
            "execute_values",
            lambda c, sql, rows: captured.update(sql=" ".join(sql.split())),
        )

        store.upsert_articles(
            [{"url": "http://x", "country_iso2": "PT", "body_status": "title-only"}]
        )
        assert "body = COALESCE(EXCLUDED.body, article.body)" in captured["sql"]
        assert "WHEN EXCLUDED.body IS NOT NULL THEN EXCLUDED.body_status" in captured["sql"]
