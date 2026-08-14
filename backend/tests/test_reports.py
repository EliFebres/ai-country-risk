"""The meters, and the one that was reporting zeros as if they were findings.

`evidence_texture` reads the article manifests the masked runs already store, so
it costs a query rather than a re-assembly of the pilot. It was reading `tier`
and `source_system` — two keys `snapshot_select.to_item` sets on the item and
`provenance.article_manifest_entry` never copied into the manifest. So it
returned `abstract_share 0.000, guardian 0, nyt 0` for every country-year, which
reads as "the corpus is uniform across the roster" and means "nothing was
measured".

That is the failure mode this whole branch keeps finding: a plausible number, no
error, and an answer to a question nobody asked. It is worth one test that
crosses the seam rather than two that each pass on their own side, because both
sides were individually correct — `to_item` set the keys and `evidence_texture`
read them, and the manifest between them dropped them on the floor.
"""

import datetime

import pytest

from backend.utils import provenance
from backend.utils.history import reports, snapshot_select, store


def _row(iso2, as_of, items):
    """A ledger row shaped the way `score.score_one` writes one."""
    return {"country_iso2": iso2, "as_of": as_of,
            "manifest": {"articles": [provenance.article_manifest_entry(i)
                                      for i in items]}}


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


class TestEvidenceTexture:
    def test_the_manifest_carries_what_the_meter_reads(self, _corpus, monkeypatch):
        monkeypatch.setattr(
            store, "read_runs",
            lambda mode=None: [_row("PT", datetime.date(2019, 6, 3), _corpus)])
        got = reports.evidence_texture(["PT"])["PT 2019"]

        assert got["articles"] == 3
        assert got["guardian"] == 2, "the source mix came back empty"
        assert got["nyt"] == 1
        assert got["abstract"] == 1
        assert got["abstract_share"] == round(1 / 3, 3)
        assert got["articles_per_snapshot"] == 3.0

    def test_a_country_outside_the_roster_is_skipped_not_an_error(self, _corpus,
                                                                 monkeypatch):
        """BR left the roster and kept its harvest. Its rows are simply not in
        this report — the one thing that must not happen is a raise."""
        monkeypatch.setattr(
            store, "read_runs",
            lambda mode=None: [_row("BR", datetime.date(2019, 6, 3), _corpus)])
        assert reports.evidence_texture(["PT"]) == {}

    def test_an_empty_week_does_not_divide_by_zero(self, monkeypatch):
        monkeypatch.setattr(
            store, "read_runs",
            lambda mode=None: [{"country_iso2": "PT",
                                "as_of": datetime.date(2019, 6, 3),
                                "manifest": {"articles": 0}}])
        assert reports.evidence_texture(["PT"]) == {}
