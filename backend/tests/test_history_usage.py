"""The budget governor.

These tests care about two things, and both are about money rather than about
LangChain: that the meter *catches* calls it never asked to be told about, and
that it fails in the safe direction when it cannot read usage.

No network, no key. `FakeMessagesListChatModel` is a real LangChain runnable, so
the hook under test is genuinely exercised — if `register_configure_hook` stops
working the way this module assumes, this file goes red rather than the pilot
silently metering zero.
"""

import pytest
from langchain_core.language_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from backend.utils.history import usage


def _message(input_tokens: int = 1000, output_tokens: int = 200,
             model: str = "gpt-4o-mini-2024-07-18") -> AIMessage:
    """An assistant reply carrying the usage block OpenAI really returns."""
    return AIMessage(
        content="ok",
        usage_metadata={"input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": input_tokens + output_tokens},
        response_metadata={"model_name": model},
    )


class TestPricing:
    def test_a_known_model_is_priced_from_the_table(self):
        # 1M in + 1M out on mini = $0.15 + $0.60
        assert usage.price("gpt-4o-mini-2024-07-18", 1_000_000, 1_000_000) == pytest.approx(0.75)

    def test_the_scoring_model_costs_what_openai_charges(self):
        assert usage.price("gpt-4o-2024-08-06", 1_000_000, 0) == pytest.approx(2.50)

    def test_an_unknown_model_is_priced_at_the_expensive_rate(self):
        # Failing safe: a model nobody added to the table must make the governor
        # stop early, never spend freely.
        unknown = usage.price("gpt-5-does-not-exist", 1_000_000, 0)
        assert unknown == usage.price("gpt-4o-2024-08-06", 1_000_000, 0)


class TestTheMeterCatchesCallsItWasNeverToldAbout:
    """The whole point: no call site knows the meter exists."""

    def test_calls_inside_the_block_are_metered(self):
        llm = FakeMessagesListChatModel(responses=[_message(), _message()])
        with usage.meter(budget_usd=10.0) as m:
            llm.invoke("a")
            llm.invoke("b")
        assert m.calls == 2
        assert m.input_tokens == 2000
        assert m.output_tokens == 400
        assert m.spend_usd == pytest.approx(usage.price("gpt-4o-mini-2024-07-18", 2000, 400))

    def test_calls_outside_the_block_are_not(self):
        """The live daily run never enters the block, and must never be metered."""
        llm = FakeMessagesListChatModel(responses=[_message(), _message()])
        with usage.meter(budget_usd=10.0) as m:
            llm.invoke("inside")
        llm.invoke("outside")
        assert m.calls == 1

    def test_batched_calls_are_metered_individually(self):
        """`digest_engine` digests via `.batch()`; each article must be counted."""
        llm = FakeMessagesListChatModel(responses=[_message()] * 3)
        with usage.meter(budget_usd=10.0) as m:
            llm.batch(["a", "b", "c"])
        assert m.calls == 3
        assert m.input_tokens == 3000


class TestTheCap:
    def test_check_is_quiet_under_budget(self):
        with usage.meter(budget_usd=10.0) as m:
            m.spend_usd = 9.99
            m.check()

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
        llm = FakeMessagesListChatModel(responses=[_message(9_000_000, 9_000_000)])
        with usage.meter(budget_usd=0.01) as m:
            llm.invoke("expensive")          # must not raise
        assert m.total_usd > 0.01
        with pytest.raises(usage.BudgetExhausted):
            m.check()


class TestUnreadableUsage:
    def test_a_call_with_no_usage_is_still_counted_as_a_call(self, caplog):
        """Undercounting spend is the one failure the governor must announce."""
        llm = FakeMessagesListChatModel(responses=[AIMessage(content="no usage block")])
        with usage.meter(budget_usd=10.0) as m:
            llm.invoke("x")
        assert m.calls == 1
        assert m.spend_usd == 0.0
        assert "undercounted" in caplog.text
