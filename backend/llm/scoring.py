"""
How many times the scorer is asked, and how its answers become one.

One function, used by production (`pipeline.run_etl`) and by every measurement
(`step5e`), so the two cannot drift: a measurement that merged differently from
the weekly run would be measuring an instrument nobody runs.

The rule (step 5e, Fix 7). Three calls. If every question is unanimous across
them, the per-question median of three is the answer and that is the end of
it. Otherwise two more calls are made and all five are merged by plurality
(`combine.vote_merge`): a question is non-null once three calls answered it,
and its level is the most common one. A tie for most common gets one more call
and a recount, to six and then seven at most; a tie that survives seven calls
is settled as the median of the answered levels, upper middle on an even
count. The old rule, the lower middle of an even split, leaned toward less
stress: [0, 1, null] read as 0.

`call_fn(i)` makes the i-th call and returns what `langchain_llm.score_country`
returns. A failed call raises, as production has always done: a country with
no score is better than one scored on fewer readings than the rule says.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from backend.llm import combine
from backend.llm import constants as ai_constants

__all__ = ["run_scoring"]


def run_scoring(
    call_fn: Callable[[int], Dict[str, Any]],
    *,
    on_call: Optional[Callable[[int, Dict[str, Any]], None]] = None,
    min_calls: int = ai_constants.CALLS_PER_COUNTRY,
    vote_calls: int = ai_constants.VOTE_CALLS,
    max_calls: int = ai_constants.VOTE_MAX_CALLS,
) -> Dict[str, Any]:
    """Make calls until the rule stops, and merge them.

    Returns:
        ``{"answer", "calls", "merge"}``: the merged answer (whose own
        ``"merge"`` block carries ``k``, the per-question ``votes``,
        ``extended`` and the questions still ``tied`` before the final
        recount), the raw call results in order, and a copy of the merge block.
    """
    calls: List[Dict[str, Any]] = []

    def make(i: int) -> None:
        scored = call_fn(i)
        if not scored or scored.get("failed") or not scored.get("answer"):
            raise RuntimeError(f"scoring failed: {(scored or {}).get('failed')}")
        calls.append(scored)
        if on_call is not None:
            on_call(i, scored)

    for i in range(min_calls):
        make(i)
    answers = [c["answer"] for c in calls]
    if is_unanimous := combine.is_unanimous(answers):
        merged = combine.merge_answers(answers)
    else:
        while len(calls) < vote_calls:
            make(len(calls))
        answers = [c["answer"] for c in calls]
        merged = combine.vote_merge(answers)
        while merged["merge"]["tied"] and len(calls) < max_calls:
            make(len(calls))
            answers = [c["answer"] for c in calls]
            merged = combine.vote_merge(answers)
        if merged["merge"]["tied"]:
            still = merged["merge"]["tied"]
            merged = combine.vote_merge(answers, final=True)
            merged["merge"]["tied"] = still
    merged["merge"]["unanimous"] = is_unanimous
    return {"answer": merged, "calls": calls, "merge": dict(merged["merge"])}
