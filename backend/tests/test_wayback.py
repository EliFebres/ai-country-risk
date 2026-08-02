"""Tests for body recovery — mostly about the leak.

Choosing the right capture is arithmetic and is tested as such. The part worth
being careful about is the fallback: when the archive has nothing, the module
refetches the live page, and a 2018 page refetched today can carry a correction
or an "as it turned out" paragraph added years later. Future text wearing a past
date is the subtlest leak in the whole machine — it never shows up in a count,
and it would teach the scorer to be right about things nobody knew yet.

So the tests here pin the *policy* rather than the model: a flagged body is
discarded and the article is demoted, a scan that errors is treated as a leak,
a live refetch never counts as recovered without a scan, and the dollar cap
stops the drain rather than being exceeded.

No network, no model call. Every boundary is monkeypatched.
"""

import datetime

import pytest

from backend.utils.history import config, wayback

PUBLISHED = datetime.datetime(2018, 3, 14, 9, 30, tzinfo=datetime.timezone.utc)
URL = "https://www.reuters.com/article/x"

# A CDX `output=json` response: header row, then captures. Three of these sit
# inside the window; one is a redirect the filter should never have let through
# but sometimes does.
CDX = [
    ["timestamp", "statuscode"],
    ["20180420120000", "200"],   # five weeks later
    ["20180314181500", "200"],   # same evening — the winner
    ["20180316090000", "200"],   # two days later
]


@pytest.fixture()
def marked(monkeypatch):
    """Capture what recover_one writes instead of writing it."""
    calls = []
    monkeypatch.setattr(wayback.store, "mark_body",
                        lambda url, **kw: calls.append({"url": url, **kw}))
    return calls


def row():
    return {"url": URL, "published_at": PUBLISHED, "country_iso2": "TR",
            "source_system": "gdelt", "title": "Lira slides"}


class TestChooseCapture:
    def test_the_nearest_capture_to_publication_wins(self):
        # Not the first in the window: a page captured the day it ran is the
        # page that ran, while one captured weeks later has had time to pick up
        # corrections — the thing the live-refetch scan exists to catch.
        assert wayback.choose_capture(CDX, PUBLISHED) == "20180314181500"

    def test_a_non_200_capture_is_ignored(self):
        rows = [CDX[0], ["20180314181500", "302"], ["20180316090000", "200"]]
        assert wayback.choose_capture(rows, PUBLISHED) == "20180316090000"

    def test_an_empty_response_has_no_capture(self):
        assert wayback.choose_capture([], PUBLISHED) is None
        assert wayback.choose_capture([CDX[0]], PUBLISHED) is None

    def test_a_malformed_timestamp_is_skipped_not_fatal(self):
        rows = [CDX[0], ["not-a-timestamp", "200"], ["20180316090000", "200"]]
        assert wayback.choose_capture(rows, PUBLISHED) == "20180316090000"

    def test_an_unexpected_header_is_survivable(self):
        assert wayback.choose_capture([["urlkey", "digest"], ["x", "y"]], PUBLISHED) is None

    def test_the_window_is_bounded_at_six_months(self):
        # Beyond this a page has usually been re-templated and later edits start
        # reading as if they were original.
        assert config.WAYBACK_WINDOW_DAYS == 180


class TestCaptureRecovery:
    def test_a_capture_is_stamped_with_its_own_date(self, monkeypatch, marked):
        monkeypatch.setattr(wayback, "find_capture", lambda *a: "20180314181500")
        monkeypatch.setattr(wayback, "fetch_capture", lambda *a: "The lira fell.")
        assert wayback.recover_one(row(), "sk-test", [0.0]) == "recovered"
        assert marked[0]["body_vintage"] == "wayback-20180314"
        assert marked[0]["body_status"] == "recovered"
        assert "20180314181500id_" in marked[0]["wayback_url"]

    def test_a_capture_that_extracts_to_nothing_falls_through(self, monkeypatch, marked):
        monkeypatch.setattr(wayback, "find_capture", lambda *a: "20180314181500")
        monkeypatch.setattr(wayback, "fetch_capture", lambda *a: "")
        monkeypatch.setattr(wayback, "fetch_live", lambda *a: "Live text.")
        monkeypatch.setattr(wayback, "references_future", lambda *a: False)
        assert wayback.recover_one(row(), "sk-test", [0.0]) == "recovered-live"


class TestNoCaptureFallback:
    def test_a_clean_live_refetch_is_stamped_live(self, monkeypatch, marked):
        monkeypatch.setattr(wayback, "find_capture", lambda *a: None)
        monkeypatch.setattr(wayback, "fetch_live", lambda *a: "Contemporaneous text.")
        monkeypatch.setattr(wayback, "references_future", lambda *a: False)
        assert wayback.recover_one(row(), "sk-test", [0.0]) == "recovered-live"
        assert marked[0]["body_vintage"] == "live-refetch"
        assert marked[0]["body"] == "Contemporaneous text."

    def test_a_flagged_body_is_demoted_and_discarded(self, monkeypatch, marked):
        monkeypatch.setattr(wayback, "find_capture", lambda *a: None)
        monkeypatch.setattr(wayback, "fetch_live", lambda *a: "As it turned out, the bank cut in June.")
        monkeypatch.setattr(wayback, "references_future", lambda *a: True)
        assert wayback.recover_one(row(), "sk-test", [0.0]) == "degraded-title-only"
        assert marked[0]["body"] is None
        assert marked[0]["body_status"] == "degraded-title-only"

    def test_without_a_scan_a_live_refetch_is_never_attempted(self, monkeypatch, marked):
        # "The scan was not approved" must not become "store it unverified".
        monkeypatch.setattr(wayback, "find_capture", lambda *a: None)
        monkeypatch.setattr(wayback, "fetch_live",
                            lambda *a: pytest.fail("refetched without a scan"))
        assert wayback.recover_one(row(), None, [0.0]) == "failed"
        assert marked[0]["body_status"] == "failed"

    def test_a_dead_link_is_a_permanent_failure(self, monkeypatch, marked):
        monkeypatch.setattr(wayback, "find_capture", lambda *a: None)
        monkeypatch.setattr(wayback, "fetch_live", lambda *a: "")
        assert wayback.recover_one(row(), "sk-test", [0.0]) == "failed"
        assert marked[0]["body"] is None


class TestLeakageScanPolicy:
    def test_a_scan_that_errors_counts_as_a_leak(self, monkeypatch):
        # Fails closed. A body nobody could verify must not be scored as if it
        # had been: being short one article costs a week a little evidence, one
        # leaked body costs the series its honesty.
        class Boom:
            def with_structured_output(self, **_):
                raise RuntimeError("no key")
        monkeypatch.setattr(wayback.ai_client, "build_digest_chat", lambda k: Boom())
        assert wayback.references_future("text", PUBLISHED, "sk-test") is True

    def test_a_malformed_answer_counts_as_a_leak(self, monkeypatch):
        class Chain:
            def with_structured_output(self, **_):
                return self

            def invoke(self, _):
                return "not a dict"
        monkeypatch.setattr(wayback.ai_client, "build_digest_chat", lambda k: Chain())
        assert wayback.references_future("text", PUBLISHED, "sk-test") is True

    def test_a_clean_answer_passes(self, monkeypatch):
        class Chain:
            def with_structured_output(self, **_):
                return self

            def invoke(self, _):
                return {"references_future": False, "evidence": ""}
        monkeypatch.setattr(wayback.ai_client, "build_digest_chat", lambda k: Chain())
        assert wayback.references_future("text", PUBLISHED, "sk-test") is False

    def test_the_prompt_carries_the_publication_date(self, monkeypatch):
        seen = {}

        class Chain:
            def with_structured_output(self, **_):
                return self

            def invoke(self, prompt):
                seen["prompt"] = prompt
                return {"references_future": False, "evidence": ""}
        monkeypatch.setattr(wayback.ai_client, "build_digest_chat", lambda k: Chain())
        wayback.references_future("body text", PUBLISHED, "sk-test")
        assert "2018-03-14" in seen["prompt"] and "body text" in seen["prompt"]

    def test_it_uses_the_cheap_model(self):
        # The scan runs over thousands of bodies. The scoring model would be an
        # order of magnitude more expensive for a boolean.
        from backend.utils.ai import client as ai_client
        assert wayback.ai_client.build_digest_chat is ai_client.build_digest_chat


class TestBudget:
    def test_the_cap_stops_the_drain_rather_than_being_exceeded(self, monkeypatch, marked):
        monkeypatch.setattr(wayback, "find_capture", lambda *a: None)
        monkeypatch.setattr(wayback, "fetch_live", lambda *a: "x" * 24000)
        monkeypatch.setattr(wayback, "references_future", lambda *a: False)
        with pytest.raises(wayback.BudgetExhausted):
            wayback.recover_one(row(), "sk-test", [config.LEAKAGE_SCAN_BUDGET_USD])
        assert marked == [], "nothing may be written once the budget is gone"

    def test_spend_accumulates_across_articles(self, monkeypatch, marked):
        monkeypatch.setattr(wayback, "find_capture", lambda *a: None)
        monkeypatch.setattr(wayback, "fetch_live", lambda *a: "x" * 24000)
        monkeypatch.setattr(wayback, "references_future", lambda *a: False)
        spent = [0.0]
        wayback.recover_one(row(), "sk-test", spent)
        first = spent[0]
        wayback.recover_one(row(), "sk-test", spent)
        assert spent[0] == pytest.approx(2 * first) and first > 0

    def test_a_capture_costs_nothing(self, monkeypatch, marked):
        monkeypatch.setattr(wayback, "find_capture", lambda *a: "20180314181500")
        monkeypatch.setattr(wayback, "fetch_capture", lambda *a: "Archived text.")
        spent = [0.0]
        wayback.recover_one(row(), "sk-test", spent)
        assert spent[0] == 0.0

    def test_the_projection_scales_with_volume(self):
        one = wayback.scan_cost_usd(["x" * 24000])
        ten = wayback.scan_cost_usd(["x" * 24000] * 10)
        assert one > 0 and ten == pytest.approx(10 * one)

    def test_the_cap_is_the_briefed_three_dollars(self):
        assert config.LEAKAGE_SCAN_BUDGET_USD == 3.0
