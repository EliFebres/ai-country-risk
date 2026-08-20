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

from backend.util import config

logger = logging.getLogger(__name__)

# USD per 1M tokens, (uncached input, cached input, output). Hardcoded for the
# same reason ``ai_client.MODEL_NAME`` is: a price that lives in three places
# disagrees in two of them. Update when the vendor's pricing page does — and note
# that a wrong price here makes the governor wrong in a way no test can catch, so
# it is checked against the invoice, not against itself.
#
# The middle column is new and is not cosmetic. Prompt v4 is a long constant
# prefix on every call, so the cached share of input is large and the cache rates
# differ by an order of magnitude between vendors — MiniMax bills cached input at
# a fifth of OpenAI's discount rate. Pricing every input token at the cache-miss
# rate, which is what this table did, overstates every model's cost by a
# different amount and so ranks them wrongly rather than merely pessimistically.
#
# The candidate ids are what each vendor is *expected* to echo back in
# `model_name`, and that is an assumption until a real response is read. It fails
# safe: an id that does not match falls through to `_FALLBACK_PRICE` below, which
# is gpt-4o's rate, so a mispriced candidate stops the run early and loudly
# rather than spending against a rate nobody chose.
PRICES_USD_PER_1M: Dict[str, tuple] = {
    "gpt-4o-2024-08-06": (2.50, 1.25, 10.00),
    "gpt-4o-mini-2024-07-18": (0.15, 0.075, 0.60),
    # Bake-off candidates. DeepSeek is at its **peak** rate deliberately: it
    # bills 01:00-04:00 and 06:00-10:00 UTC at double, a long run can straddle
    # the boundary, and a governor quoting the cheap half of a run it has not
    # finished is the failure mode this file exists to prevent. `offpeak_price`
    # below reports the other half for the write-up.
    "deepseek-v4-pro": (1.32, 0.044, 3.96),
    "deepseek-v4-flash": (0.44, 0.014, 1.32),
    # MiniMax's <=512K context tier, which is where our ~11.6k-token payloads
    # sit. The >512K tier is priced differently and we never reach it.
    "MiniMax-M3": (0.30, 0.06, 1.20),
    "openai/gpt-oss-120b": (0.15, 0.075, 0.60),
}

# When DeepSeek bills at half. UTC hours 01,02,03 and 06,07,08,09 are peak; the
# other seventeen are off-peak. Stated as the peak set because that is the one
# the table above is priced at, so the report derives the discount rather than
# the penalty.
DEEPSEEK_PEAK_HOURS_UTC = frozenset({1, 2, 3, 6, 7, 8, 9})

# What an unrecognised model costs, for pricing purposes. Deliberately the
# expensive model's rate: an unknown model should make the governor stop early,
# not spend freely because nobody added it to the table.
_FALLBACK_PRICE = PRICES_USD_PER_1M["gpt-4o-2024-08-06"]


class BudgetExhausted(RuntimeError):
    """Cumulative spend has passed the pilot's cap. Stops the run, keeps the work."""


def price(model_id: str, input_tokens: int, output_tokens: int,
          cached_tokens: int = 0) -> float:
    """What one call cost, in dollars.

    Args:
        model_id: as the provider reported it. Version suffixes matter — an
            unversioned alias falls back to the expensive rate rather than
            guessing.
        input_tokens: the whole prompt, cached part included. That is what
            ``usage_metadata.input_tokens`` means, so ``cached_tokens`` is
            subtracted here rather than expected to arrive pre-netted.
        cached_tokens: the subset served from the provider's prefix cache.
            Defaults to zero, which prices a call at the cache-miss rate — the
            conservative reading, and the right one when the provider reports
            no cache detail at all.
    """
    rate_in, rate_cached, rate_out = PRICES_USD_PER_1M.get(model_id, _FALLBACK_PRICE)
    # Clamped: a provider that reports more cached tokens than input tokens is
    # reporting something this function does not understand, and the cheap
    # reading of it is the one that would understate the bill.
    cached = max(0, min(cached_tokens, input_tokens))
    return ((input_tokens - cached) * rate_in
            + cached * rate_cached
            + output_tokens * rate_out) / 1_000_000


def offpeak_price(model_id: str, input_tokens: int, output_tokens: int,
                  cached_tokens: int = 0) -> Optional[float]:
    """The same call billed at DeepSeek's off-peak half, or None if not DeepSeek.

    Reporting-only. The governor runs on :func:`price`, which is the peak rate,
    because a run that straddles 06:00 UTC must not have been quoted at the
    cheap half.
    """
    if not model_id.startswith("deepseek-"):
        return None
    return price(model_id, input_tokens, output_tokens, cached_tokens) / 2.0


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
        # The cached subset of `input_tokens`, not a separate pool. Kept so the
        # bake-off can report a realised cache-hit share instead of assuming
        # one — prompt v4 is a long constant prefix and the vendors' cache rates
        # differ by an order of magnitude, so an assumed share picks the winner.
        self.cached_tokens = 0
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
        self.cached_tokens += usage[2]
        self.spend_usd += price(model_id, usage[0], usage[1], usage[2])


def _extract_usage(response: Any) -> tuple:
    """Pull (input, output, cached) tokens and the model id off an LLMResult.

    ``cached`` is the prefix-cache-served subset of ``input``, read from
    LangChain's normalised ``input_token_details.cache_read`` and falling back to
    the raw OpenAI-compatible ``prompt_tokens_details.cached_tokens``. A provider
    that reports neither yields zero, which prices the whole prompt at the
    cache-miss rate — an overstatement rather than an understatement, and the
    bake-off says "not reported" rather than "0%" so the two are never confused.

    Returns:
        ``((input, output, cached), model_id)``, or ``(None, "")`` when the
        response carries no usage at all.
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
                details = meta.get("input_token_details") or {}
                return (int(meta.get("input_tokens") or 0),
                        int(meta.get("output_tokens") or 0),
                        int(details.get("cache_read") or 0)), model_id

    token_usage = llm_output.get("token_usage") or {}
    if token_usage:
        details = token_usage.get("prompt_tokens_details") or {}
        return (int(token_usage.get("prompt_tokens") or 0),
                int(token_usage.get("completion_tokens") or 0),
                int(details.get("cached_tokens") or 0)), model_id
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
