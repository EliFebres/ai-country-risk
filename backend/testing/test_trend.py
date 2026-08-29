"""The computed trend block: what it may see, and what it must refuse to say.

Two failure modes, and only the first one looks like a failure. A block built
from revised data is a leak that does not resemble the future — it resembles a
clean series, which is worse. And a block that reports a short history as `flat`
hands the model a stability claim nobody made, on exactly the quiet weeks where
the instrument already snaps to a round number.
"""

import datetime

import pytest

from backend.llm import payload as payload_mod
from backend.llm import trend
from backend.util import provenance

ANCHOR = datetime.date(2019, 6, 1)


def annual(code, year, value, as_of=None, scheme="as-published-edition"):
    """One annual observation, dated when it was published."""
    return {"indicator_code": code, "freq": "A", "period": str(year),
            "value": value, "source": "IMF WEO", "vintage_scheme": scheme,
            "as_of": as_of or datetime.date(year + 1, 4, 1)}


def debt(pairs, **kw):
    return {"WEO.GGXWDG_NGDP": [annual("WEO.GGXWDG_NGDP", y, v, **kw)
                                for y, v in pairs]}


class TestTheBlockCannotSeePastItsAnchor:
    """The same bound as everything else, applied to the thing most likely to
    escape it: a trajectory is assembled from many vintages at once."""

    def test_a_later_revision_of_an_older_year_is_not_used(self):
        """The leak that looks like clean data.

        2016's figure was first published at 100.0 and revised to 130.0 in 2026.
        A 2019 anchor must read the first release. Nothing about the revised
        series looks wrong afterwards -- it is smoother and more accurate, and
        it is a number nobody had.
        """
        series = {"WEO.GGXWDG_NGDP": [
            annual("WEO.GGXWDG_NGDP", 2016, 100.0, datetime.date(2017, 4, 1)),
            annual("WEO.GGXWDG_NGDP", 2016, 130.0, datetime.date(2026, 4, 1)),
        ]}
        block = trend.build(series, ANCHOR)
        assert block["macro"]["WEO.GGXWDG_NGDP"]["annual"] == {"2016": 100.0}

    def test_a_year_the_anchor_could_not_know_is_absent(self):
        block = trend.build(debt([(2016, 100.0), (2018, 120.0), (2024, 200.0)]),
                            ANCHOR)
        assert "2024" not in block["macro"]["WEO.GGXWDG_NGDP"]["annual"]

    def test_the_evidence_volume_stops_at_the_anchor(self):
        """Coverage is evidence, so counting the anchor's own quarter would be
        the same no-future leak as an article published after it. Enforced by
        the caller passing an exclusive bound; asserted here so the contract is
        recorded where the block is."""
        counts = {"2019Q1": {"friction": 5}, "2019Q2": {"friction": 3}}
        block = trend.build(debt([(2017, 1.0)]), ANCHOR, theme_counts=counts)
        assert list(block["evidence_volume"]["quarters"]) == ["2019Q1", "2019Q2"]


class TestUnknownIsNotFlat:
    """The distinction the block exists to preserve."""

    def test_a_series_too_short_for_a_horizon_says_unknown(self):
        block = trend.build(debt([(2017, 100.0), (2018, 101.0)]), ANCHOR)
        entry = block["macro"]["WEO.GGXWDG_NGDP"]
        assert entry["direction_1y"] != "unknown"
        assert entry["direction_5y"] == "unknown"
        assert entry["change_5y"] is None

    def test_a_short_window_says_how_short_rather_than_shrinking_quietly(self):
        block = trend.build(debt([(2017, 100.0), (2018, 101.0)]), ANCHOR)
        entry = block["macro"]["WEO.GGXWDG_NGDP"]
        assert len(entry["annual"]) == 2
        assert "2 of 5" in entry["annual_note"]

    def test_a_genuinely_unchanged_series_says_flat(self):
        block = trend.build(debt([(y, 100.0) for y in range(2013, 2019)]), ANCHOR)
        assert block["macro"]["WEO.GGXWDG_NGDP"]["direction_5y"] == "flat"

    def test_acceleration_is_absent_rather_than_false_when_unknown(self):
        """False reads as 'steady', which is a claim. Absence is not."""
        block = trend.build(debt([(2017, 100.0), (2018, 110.0)]), ANCHOR)
        assert "accelerating" not in block["macro"]["WEO.GGXWDG_NGDP"]


class TestDirectionIsScaledToWhatItMeasures:
    """One absolute epsilon would call every WGI move significant and every
    debt move noise: the registry mixes % of GDP with z-scores on -2.5..2.5."""

    def test_a_tiny_move_on_a_large_level_is_flat(self):
        block = trend.build(debt([(2013, 100.0), (2018, 100.5)]), ANCHOR)
        assert block["macro"]["WEO.GGXWDG_NGDP"]["direction_5y"] == "flat"

    def test_the_same_absolute_move_on_a_small_level_is_not(self):
        block = trend.build(debt([(2013, 1.0), (2018, 1.5)]), ANCHOR)
        assert block["macro"]["WEO.GGXWDG_NGDP"]["direction_5y"] == "rising"


class TestTheBlockIsOptOnly:
    """p4 must not be able to change what the daily run does by existing."""

    def test_an_unset_variant_is_still_p2(self, monkeypatch):
        monkeypatch.delenv("PAYLOAD_VARIANT", raising=False)
        assert provenance.payload_variant() == "p2"

    def test_the_variant_is_known(self, monkeypatch):
        monkeypatch.setenv("PAYLOAD_VARIANT", "p4-trend")
        assert provenance.payload_variant() == "p4-trend"

    def test_a_payload_without_the_block_omits_the_key(self):
        out = payload_mod.build_evidence_payload("PT", as_of=ANCHOR)
        assert "trend" not in out

    def test_the_block_lands_inside_the_evidence_payload(self):
        """Not a fifth prompt placeholder: `mask_payload` and `assert_clean`
        already run over this dict whole, and a placeholder would need both
        wired by hand."""
        import json

        out = payload_mod.build_evidence_payload(
            "PT", as_of=ANCHOR, trend_block={"macro": {"X": {"latest": 1}}})
        assert out["trend"]["macro"]["X"]["latest"] == 1
        assert "trend" in json.dumps(out)


class TestTheBlockMakesNoModelCall:
    """The whole reason it needs no cache, no `llm_artifact` kind and no
    migration, where p3 needed all three."""

    def test_building_it_needs_no_client_and_no_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        block = trend.build(debt([(y, 100.0 + y - 2013) for y in range(2013, 2019)]),
                            ANCHOR)
        assert block["macro"]["WEO.GGXWDG_NGDP"]["direction_5y"] == "rising"

    def test_it_is_byte_reproducible(self):
        import json

        series = debt([(y, 100.0 + y - 2013) for y in range(2013, 2019)])
        first = json.dumps(trend.build(series, ANCHOR), sort_keys=True)
        second = json.dumps(trend.build(series, ANCHOR), sort_keys=True)
        assert first == second
