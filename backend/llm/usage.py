"""The budget governor: what the pilot has actually spent, from actual usage.

A projection is a hope. This module reads the ``usage_metadata`` OpenAI returns
on every call and prices it, so the number the pilot aborts on is the number the
invoice will show.

The hard part is that nothing on the scoring path exposes usage. Every LLM call
in this backend goes through ``.with_structured_output(schema=..., strict=True)``,
which returns a plain dict — the ``AIMessage`` carrying ``usage_metadata`` is
parsed and discarded before any call site sees it. Threading a usage argument
down through ``digest_engine`` and ``langchain_llm`` would mean editing the
daily run's code to serve a backfill, which is exactly backwards.

So this meters from underneath instead. ``register_configure_hook`` is the same
mechanism LangChain's own ``get_openai_callback`` uses: a context variable that
LangChain consults when it builds the callback list for *any* runnable. Set it,
and every model call made inside the ``with`` block is metered — including calls
inside ``.batch()``, inside structured-output wrappers, in code that has never
heard of this module. Leave the block and nothing is metered. The live daily run
is untouched, because it never enters the block.

Usage:

    with usage.meter() as m:
        ...score a country...
    m.spend_usd            # what that country cost
    m.check()              # raises BudgetExhausted past the cap
"""

import contextlib
import contextvars
import logging
from typing import Any, Dict, Iterator, Optional

from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.tracers.context import register_configure_hook

from backend.utils.history import config

logger = logging.getLogger(__name__)

# USD per 1M tokens, (input, output). Hardcoded for the same reason
# ``ai_client.MODEL_NAME`` is: a price that lives in three places disagrees in
# two of them. Update when OpenAI's pricing page does — and note that a wrong
# price here makes the governor wrong in a way no test can catch, so it is
# checked against the invoice, not against itself.
PRICES_USD_PER_1M: Dict[str, tuple] = {
    "gpt-4o-2024-08-06": (2.50, 10.00),
    "gpt-4o-mini-2024-07-18": (0.15, 0.60),
}

# What an unrecognised model costs, for pricing purposes. Deliberately the
# expensive model's rate: an unknown model should make the governor stop early,
# not spend freely because nobody added it to the table.
_FALLBACK_PRICE = PRICES_USD_PER_1M["gpt-4o-2024-08-06"]


class BudgetExhausted(RuntimeError):
    """Cumulative spend has passed the pilot's cap. Stops the run, keeps the work."""


def price(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """What one call cost, in dollars.

    Args:
        model_id: as OpenAI reported it. Version suffixes matter — an unversioned
            alias falls back to the expensive rate rather than guessing.
    """
    rate_in, rate_out = PRICES_USD_PER_1M.get(model_id, _FALLBACK_PRICE)
    return (input_tokens * rate_in + output_tokens * rate_out) / 1_000_000


class Meter(BaseCallbackHandler):
    """Accumulates token spend across every model call made inside its block.

    Deliberately does **not** raise from the callback. A handler that throws
    mid-``.batch()`` aborts eight concurrent digests at an arbitrary point,
    leaving the ledger unable to say what was paid for and what was not.
    Instead the meter records, and :meth:`check` is called at a snapshot
    boundary where stopping is clean.
    """

    def __init__(self, budget_usd: float, already_spent_usd: float = 0.0) -> None:
        """
        Args:
            budget_usd: the cap this meter measures against.
            already_spent_usd: spend from earlier runs, read out of the ledger,
                so a resumed pilot cannot restart its budget at zero.
        """
        self.budget_usd = budget_usd
        self.already_spent_usd = already_spent_usd
        self.spend_usd = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0

    @property
    def total_usd(self) -> float:
        """Everything the pilot has spent, this run and every earlier one."""
        return self.already_spent_usd + self.spend_usd

    def check(self) -> None:
        """Raise if the cap has been passed. Call at snapshot boundaries.

        Raises:
            BudgetExhausted: with the numbers, because a governor that stops a
                multi-hour run owes an explanation of why.
        """
        if self.total_usd > self.budget_usd:
            raise BudgetExhausted(
                f"pilot spend ${self.total_usd:.2f} passed "
                f"${self.budget_usd:.2f} (this run ${self.spend_usd:.2f}, "
                f"{self.calls} calls, {self.input_tokens:,} in / "
                f"{self.output_tokens:,} out)"
            )

    # -- the LangChain side --------------------------------------------------

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        """Meter one completed model call.

        Reads ``usage_metadata`` off the generation's message first — it is the
        shape LangChain normalises across providers — and falls back to the
        raw ``token_usage`` block. A call whose usage is unreadable is counted
        as a call but priced at zero, and says so in the log: silently pricing
        it at zero would be a governor that undercounts, which is the one
        failure mode that matters.
        """
        self.calls += 1
        usage, model_id = _extract_usage(response)
        if not usage:
            logger.warning("[usage] a model call reported no usage; spend undercounted")
            return
        self.input_tokens += usage[0]
        self.output_tokens += usage[1]
        self.spend_usd += price(model_id, usage[0], usage[1])


def _extract_usage(response: Any) -> tuple:
    """Pull (input, output) tokens and the model id off an LLMResult.

    Returns:
        ``((input, output), model_id)``, or ``(None, "")`` when the response
        carries no usage at all.
    """
    llm_output = getattr(response, "llm_output", None) or {}
    model_id = llm_output.get("model_name") or llm_output.get("model") or ""

    for batch in getattr(response, "generations", None) or []:
        for generation in batch or []:
            message = getattr(generation, "message", None)
            meta = getattr(message, "usage_metadata", None)
            if meta:
                model_id = model_id or (getattr(message, "response_metadata", {}) or {}).get(
                    "model_name", "")
                return (int(meta.get("input_tokens") or 0),
                        int(meta.get("output_tokens") or 0)), model_id

    token_usage = llm_output.get("token_usage") or {}
    if token_usage:
        return (int(token_usage.get("prompt_tokens") or 0),
                int(token_usage.get("completion_tokens") or 0)), model_id
    return None, model_id


# The context variable LangChain consults when assembling callbacks. Registered
# once at import; setting it is what arms the meter.
_meter_var: contextvars.ContextVar[Optional[Meter]] = contextvars.ContextVar(
    "history_usage_meter", default=None)
register_configure_hook(_meter_var, True)


@contextlib.contextmanager
def meter(budget_usd: Optional[float] = None,
          already_spent_usd: float = 0.0) -> Iterator[Meter]:
    """Meter every model call made inside this block.

    Args:
        budget_usd: defaults to :data:`config.PILOT_BUDGET_USD`.
        already_spent_usd: prior spend, normally ``store.total_spend_usd()``.

    Yields:
        The :class:`Meter`, readable during the block and after it.
    """
    m = Meter(config.PILOT_BUDGET_USD if budget_usd is None else budget_usd,
              already_spent_usd)
    token = _meter_var.set(m)
    try:
        yield m
    finally:
        _meter_var.reset(token)
