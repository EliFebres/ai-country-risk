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
