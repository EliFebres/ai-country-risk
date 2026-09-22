"""
Trajectory, computed here so the model does not have to do it in its head.

The scorer used to see levels: inflation is 7.2%. A level says almost nothing
about risk on its own — 7.2% on the way down from 20% is a different country
from 7.2% on the way up from 2%. So the payload carries the last five annual
observations, and beside them the direction **in words**, worked out in code.

Trend detection is not the model's job. Asked to infer a direction from five
numbers it will usually get it right, sometimes not, and never reproducibly;
and every token spent doing arithmetic is a token not spent on judgement.

Two rules that matter more than the arithmetic:

**Nothing is interpolated.** A year with no observation reads `"unknown"`. A
filled gap is a number the model will treat as measured, and it never was.

**A direction needs enough to be a direction.** Two points is a line, not a
trend. Fewer than three observations reports `"unknown"` rather than a
confident-sounding word derived from almost nothing.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["FLAT_BAND", "YEARS", "direction_of", "annual_history", "describe"]


# How much a series has to move, relative to its own level, before the movement
# is called rather than described as flat. A tenth of the level over the window
# is a real move on a percentage series and on an index alike; below that, the
# honest word is "flat" rather than a direction invented from noise.
FLAT_BAND = 0.10

# The window the payload carries.
YEARS = 5

# Below this many observations there is no trend to state.
MIN_POINTS = 3


def direction_of(values: Sequence[Optional[float]]) -> str:
    """Return `rising`, `falling`, `flat`, or `unknown` for a series.

    Args:
        values: Observations oldest-first; None for a year with no value.
    """
    seen = [v for v in values if v is not None]
    if len(seen) < MIN_POINTS:
        return "unknown"

    first, last = seen[0], seen[-1]
    move = last - first
    scale = max(abs(first), abs(last), 1e-9)
    if abs(move) / scale < FLAT_BAND:
        return "flat"
    return "rising" if move > 0 else "falling"


def _accelerating(values: Sequence[Optional[float]]) -> Optional[bool]:
    """Whether the movement in the later half outpaced the earlier half.

    Returns None when there is not enough to say — which is most short series,
    and is a better answer than a coin flip.
    """
    seen = [v for v in values if v is not None]
    if len(seen) < 4:
        return None
    mid = len(seen) // 2
    early = seen[mid] - seen[0]
    late = seen[-1] - seen[mid]
    if early == 0:
        return abs(late) > 0
    # Same direction, and faster.
    if (early > 0) != (late > 0):
        return False
    return abs(late) > abs(early)


def annual_history(
    series: Dict[Any, Optional[float]],
    *,
    years: int = YEARS,
    latest_year: Optional[int] = None,
) -> List[Tuple[int, Optional[float]]]:
    """Return the last `years` calendar years as ``(year, value_or_None)``.

    Years the series does not cover are present and carry None, rather than
    being absent. The gap is the point: a five-year window with two holes in it
    is a different statement about the evidence than a three-year window, and
    silently shortening the window hides that.
    """
    numeric = {}
    for k, v in (series or {}).items():
        try:
            numeric[int(k)] = None if v is None else float(v)
        except (TypeError, ValueError):
            continue

    if latest_year is None:
        latest_year = max(numeric) if numeric else None
    if latest_year is None:
        return []

    return [(y, numeric.get(y)) for y in range(latest_year - years + 1, latest_year + 1)]


def describe(
    series: Dict[Any, Optional[float]],
    *,
    years: int = YEARS,
    latest_year: Optional[int] = None,
) -> Dict[str, Any]:
    """Return the trajectory block for one indicator.

    Returns:
        ``{"history": [{"year", "value"}...], "direction", "accelerating",
        "observed", "missing"}``. `value` is the string ``"unknown"`` where a
        year has no observation, so the model reads the gap in the same words it
        reads everything else.
    """
    pairs = annual_history(series, years=years, latest_year=latest_year)
    values = [v for _, v in pairs]
    observed = sum(1 for v in values if v is not None)

    return {
        "history": [
            {"year": y, "value": ("unknown" if v is None else v)} for y, v in pairs
        ],
        "direction": direction_of(values),
        "accelerating": _accelerating(values),
        "observed": observed,
        "missing": len(pairs) - observed,
    }
