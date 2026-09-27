"""
What a run costs, projected before it spends and metered while it does.

Two jobs, deliberately kept apart:

`project` answers "what is this about to cost" from a token estimate, and is
printed before every paid step so a run that is about to be expensive says so
first rather than afterwards.

`Meter` answers "what did it actually cost" from the usage the provider
reported. Estimates drift; a meter does not. The gap between the two is itself
worth watching, which is why both are printed.

An unknown model id raises rather than costing zero. A price table that silently
returns nothing for a model nobody added turns a budget cap into decoration, and
"the run was free" is the one wrong answer a cost meter must never give.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "PRICES_USD_PER_1M",
    "price_of",
    "cost_usd",
    "project",
    "Meter",
    "BudgetExhausted",
]


# USD per 1,000,000 tokens, (input, output). Dated model ids only: an alias
# moves under you, and a step that silently changes model is a step that
# silently changes both its cost and its answers.
PRICES_USD_PER_1M: Dict[str, Tuple[float, float]] = {
    "gpt-4o-2024-08-06":       (2.50, 10.00),
    "gpt-4o-mini-2024-07-18":  (0.15,  0.60),
    "gpt-4.1-2025-04-14":      (2.00,  8.00),
    "gpt-4.1-mini-2025-04-14": (0.40,  1.60),
    "gpt-4.1-nano-2025-04-14": (0.10,  0.40),
}


class BudgetExhausted(RuntimeError):
    """Raised when metered spend passes the cap the caller set."""


def price_of(model: str) -> Tuple[float, float]:
    """Return ``(input, output)`` USD per 1M tokens for `model`.

    Raises:
        KeyError: if the model is not in the table. Deliberate — a model with no
            price must not be assumed free.
    """
    try:
        return PRICES_USD_PER_1M[model]
    except KeyError:
        raise KeyError(
            f"no price for {model!r}. Add it to usage.PRICES_USD_PER_1M rather "
            f"than letting the run bill an unknown amount."
        ) from None


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return the USD cost of a given number of tokens on `model`."""
    inp, out = price_of(model)
    return (input_tokens / 1_000_000) * inp + (output_tokens / 1_000_000) * out


def project(
    label: str,
    *,
    model: str,
    calls: int,
    input_tokens_per_call: int,
    output_tokens_per_call: int,
    cap_usd: Optional[float] = None,
) -> float:
    """Print and return the projected cost of a paid step.

    Args:
        label: What is about to run, in a few words.
        model: The dated model id.
        calls: How many calls the step will make at most.
        input_tokens_per_call: Estimated prompt tokens per call.
        output_tokens_per_call: Estimated completion tokens per call.
        cap_usd: If given and the projection exceeds it, this raises rather than
            printing a warning nobody reads.

    Raises:
        BudgetExhausted: if the projection exceeds `cap_usd`.
    """
    total = cost_usd(model, calls * input_tokens_per_call, calls * output_tokens_per_call)
    print(
        f"[cost] {label}: {calls} calls on {model} "
        f"(~{input_tokens_per_call} in / {output_tokens_per_call} out each) "
        f"-> projected ${total:.4f}"
    )
    if cap_usd is not None and total > cap_usd:
        raise BudgetExhausted(
            f"{label} projects ${total:.2f}, over the ${cap_usd:.2f} cap"
        )
    return total


@dataclass
class Meter:
    """Accumulates real token usage as reported by the provider.

    `add_response` reads LangChain's `usage_metadata`, which is only present on
    the raw `AIMessage`. A structured-output call made without `include_raw=True`
    discards that message, so the meter would report zero on a run that spent
    money — which is why every metered call site asks for the raw response.
    """

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_model: Dict[str, Dict[str, int]] = field(default_factory=dict)
    unmetered_calls: int = 0

    def add(self, model: str, input_tokens: int, output_tokens: int) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        slot = self.by_model.setdefault(
            model, {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        )
        slot["calls"] += 1
        slot["input_tokens"] += input_tokens
        slot["output_tokens"] += output_tokens

    def add_response(self, model: str, response: Any) -> bool:
        """Meter one LangChain response. Returns whether usage was found.

        A response that carries no usage metadata is counted as unmetered rather
        than as free, so the summary can say how much of the bill it could not
        see.
        """
        usage = None
        raw = response
        if isinstance(response, dict):
            raw = response.get("raw", response)
        usage = getattr(raw, "usage_metadata", None)
        if not usage and isinstance(raw, dict):
            usage = raw.get("usage_metadata")
        if not usage:
            self.unmetered_calls += 1
            self.calls += 1
            return False
        self.add(model, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)))
        return True

    @property
    def spend_usd(self) -> float:
        return sum(
            cost_usd(model, s["input_tokens"], s["output_tokens"])
            for model, s in self.by_model.items()
        )

    def check(self, cap_usd: float) -> None:
        """Raise if metered spend has passed `cap_usd`.

        Worth stating plainly: a budget check that is never called is not a
        budget. Every metered loop calls this.
        """
        if self.spend_usd > cap_usd:
            raise BudgetExhausted(
                f"spent ${self.spend_usd:.2f}, over the ${cap_usd:.2f} cap"
            )

    def summary(self) -> str:
        parts = [
            f"{m}: {s['calls']} calls, "
            f"{s['input_tokens']}+{s['output_tokens']} tok, "
            f"${cost_usd(m, s['input_tokens'], s['output_tokens']):.4f}"
            for m, s in sorted(self.by_model.items())
        ]
        tail = f" ({self.unmetered_calls} calls reported no usage)" if self.unmetered_calls else ""
        return f"[cost] actual ${self.spend_usd:.4f} — " + "; ".join(parts) + tail
