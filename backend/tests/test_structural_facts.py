"""The facts that replace what masking took away, and the mask they must survive.

Masking strips the country's name and every prior that came with it — including
legitimate ones. A model that cannot see it is looking at the United States
cannot know the debt is issued in a currency the borrower prints, or that a
euro-area member's is not. Those are structure, not reputation, so they are
supplied as stated evidence instead.

Which creates a trap the rest of the project does not have: this block is
written *to be read by the masked model*, so it goes through the same gazetteer
as the articles, and the gazetteer does not know it is looking at a controlled
vocabulary rather than at prose. The World Bank's own region names do not
survive the trip. That is the failure these tests exist to keep fixed, and to
keep fixed for the forty-three countries nobody has filled in yet.
"""

import pathlib

import pytest
import yaml

from backend.utils import constants, data_retrieval
from backend.utils.data_fetching import curated_loader
from backend.utils.masking import gazetteer, rewrite

ROSTER = [c["iso2"] for c in constants.COUNTRY_ROSTER]


@pytest.fixture(scope="module")
def facts():
    return curated_loader.load_structural_facts()


class TestTheFileItself:
    def test_the_pilot_five_are_filled(self, facts):
        assert set(facts) == {"US", "TR", "BR", "PT", "KR"}

    def test_every_value_carries_a_source_and_a_date(self):
        """The point of the file is that a fact can be checked, not that it is
        present. A bare scalar is a value somebody pasted without saying where
        from, which is the fabrication this is supposed to prevent."""
        raw = yaml.safe_load(curated_loader.STRUCTURAL_FACTS.read_text(encoding="utf-8"))
        for iso2, block in raw.items():
            for field, entry in block.items():
                assert isinstance(entry, dict), f"{iso2}.{field} is not a cited entry"
                assert entry.get("source"), f"{iso2}.{field} has no source"
                assert entry.get("retrieved"), f"{iso2}.{field} has no retrieval date"

    def test_an_uncited_value_is_dropped_rather_than_trusted(self, tmp_path):
        path = tmp_path / "f.yaml"
        path.write_text("PT:\n  income_group: high\n", encoding="utf-8")
        assert curated_loader.load_structural_facts(path) == {}

    def test_a_value_outside_its_vocabulary_is_dropped(self, tmp_path):
        path = tmp_path / "f.yaml"
        path.write_text("PT:\n  monetary_sovereignty:\n    value: constrainted\n"
                        "    source: s\n    retrieved: 2026-01-01\n", encoding="utf-8")
        assert curated_loader.load_structural_facts(path) == {}

    def test_an_absent_file_is_silent(self, tmp_path):
        assert curated_loader.load_structural_facts(tmp_path / "nope.yaml") == {}

    def test_an_unparseable_file_costs_the_block_not_the_run(self, tmp_path):
        """Read on the live path for every country: one bad file must not cost
        forty-seven countries their scores."""
        path = tmp_path / "f.yaml"
        path.write_text("PT: [unclosed\n", encoding="utf-8")
        assert curated_loader.load_structural_facts(path) == {}


class TestItSurvivesTheMask:
    """The trap. These values are shown to a model that must not learn the name."""

    @pytest.mark.parametrize("iso2", ["US", "TR", "BR", "PT", "KR"])
    def test_a_filled_block_passes_the_gate(self, facts, iso2):
        masked = rewrite.mask_payload({"structural": facts[iso2]}, iso2)
        rewrite.assert_clean(masked)

    @pytest.mark.parametrize("iso2", ["US", "TR", "BR", "PT", "KR"])
    def test_no_field_is_mangled_on_the_way_through(self, facts, iso2):
        """Not just "no leak" — no *damage*.

        "Latin America and Caribbean" masking to "another country and Caribbean"
        passes the gate and is still wrong: it reads as though a country were
        named, and it teaches the scorer that the text has been tampered with.
        """
        masked = rewrite.mask_payload({"structural": facts[iso2]}, iso2)
        assert masked["structural"] == facts[iso2]

    @pytest.mark.parametrize("region", sorted(curated_loader._STRUCTURAL_VOCAB["region"]))
    def test_every_allowed_region_survives_for_every_roster_country(self, region):
        """The guard for the forty-three countries not yet filled in.

        A region value is only safe if it survives being masked *as* every
        country, because the foreign pass flattens every roster country but the
        one being scored — which is how "Latin America" became "another country"
        while scoring Brazil rather than while scoring the US.
        """
        for iso2 in ROSTER:
            assert rewrite.mask_text(region, iso2) == region, f"{region!r} mangled for {iso2}"
            assert not gazetteer.scan(region, ROSTER), f"{region!r} scans as a roster term"


class TestThePayload:
    def test_a_filled_country_gets_the_block(self, facts):
        import datetime
        payload = data_retrieval.build_evidence_payload(
            "PT", as_of=datetime.date(2019, 6, 3), structural=facts)
        assert payload["structural"]["monetary_sovereignty"] == "constrained"

    def test_an_unfilled_country_has_no_block_at_all(self, facts):
        """Absent, not empty. An empty `structural` key would read to the model
        as "this country has no structure", which is false and worse than
        silence — the same rule the payload already follows for an indicator
        with no observation."""
        import datetime
        payload = data_retrieval.build_evidence_payload(
            "IN", as_of=datetime.date(2019, 6, 3), structural=facts)
        assert "structural" not in payload

    def test_the_time_varying_three_are_registered_not_hardcoded(self):
        """They move every year, so a single current value on a 2016 snapshot
        would be a future leak. They belong in indicator_series, where the
        vintage bound already governs them."""
        for code in ("GOV.DEBT.FX.SHARE", "GOV.DEBT.DOMESTIC.SHARE", "NIIP.GDP"):
            assert code in constants.INDICATOR_REGISTRY
            assert constants.INDICATOR_REGISTRY[code]["freq"] == "A"
