"""Trajectory as computed fact, not as prose the model has to read.

The second attempt at the thing :mod:`backend.llm.context` tried. That one
summarised four quarters of articles into a masked paragraph each and made the
instrument *coarser* — an ordinary quarter in an ordinary country reads "the
government continued its programme, the central bank held rates", which is the
same input that already produces 0.50, at four times the length.

So this block computes instead of summarising, and **makes no model call
anywhere**. Numbers, dates, and directions stated in words. A language model
should not be asked to infer a slope from four essays when the pipeline can
compute the slope and say what it is: LLMs are unreliable at arithmetic over
tables, and every inference moved into deterministic code is one that can be
tested.

Determinism buys more than accuracy here. No generation cost, no
nondeterminism, no cache to invalidate, and the block is byte-reproducible from
``indicator_series`` and ``article`` — so the rebuild check covers it for free,
where the p3 block needed its own cache table and a new ``llm_artifact`` kind.

Three parts.

**Macro trajectory** — the five headline series, their last five annual
observations knowable at the anchor, the direction, and whether the change is
accelerating. The payload has always shown that debt is 68%; it has never shown
that it was 51% five years ago and has risen every year since.

**Ledger trajectory** — for each resolvable constituent, the 1-, 3- and 5-year
direction. This is the part meant to do the work: the difference between "this
country is quiet" and "this country is quiet while its rule-of-law score has
fallen three years running", which is the discrimination missing on exactly the
weeks where the model snaps to a round number.

**Evidence volume** — articles per theme per quarter over eight quarters, a
pure count. Coverage collapsing in a theme is a fact about the reporting, and it
separates "quiet because stable" from "quiet because nobody wrote anything".

Two rules the whole block is built to keep.

**One vintage bound.** Everything resolves through ``payload._resolve`` at the
anchor, so a 2019 anchor sees the 2014–2018 trajectory *as it was knowable in
2019* — first releases, not values revised later. A trend built from revised
data is the same leak the vintage store exists to prevent and a harder one to
see: it does not look like the future, it looks like a clean series.

**Absent is absent.** Where a series does not reach back far enough at that
vintage the block says ``unknown`` rather than shortening the window or
interpolating. The model has to be able to tell flat from absent, and a silently
shortened window tells it the opposite of the truth.
"""

import datetime
import logging
from typing import Any, Dict, List, Optional

from backend.llm import payload as llm_payload
from backend.util import constants

logger = logging.getLogger(__name__)

TREND_VERSION = "t1"

# The five the brief names, and the five that survive the vintage bound with a
# decade behind them: 21 WEO editions reaching back to 1980, plus CPI.
MACRO_CODES = ("WEO.GGXWDG_NGDP", "WEO.GGXCNL_NGDP", "WEO.BCA_NGDPD",
               "WEO.NGDP_RPCH", "CPI.YOY")

ANNUAL_POINTS = 5
QUARTERS = 8

# Below this a change is reported as flat rather than as a direction. Relative
# to the level, because the registry mixes percentages of GDP with WGI z-scores
# on a -2.5..2.5 scale, and one absolute epsilon would call every WGI move
# significant and every debt move noise. The absolute floor catches indicators
# sitting near zero, where a ratio of the level means nothing.
_FLAT_RELATIVE = 0.01
_FLAT_ABSOLUTE = 0.01

# How much faster than its own five-year average a one-year move has to be
# before it is worth calling acceleration rather than continuation.
_ACCELERATING_RATIO = 1.2


def _direction(delta: Optional[float], level: Optional[float]) -> str:
    """Which way, in words, or ``unknown`` when there is nothing to compare."""
    if delta is None:
        return "unknown"
    threshold = max(_FLAT_ABSOLUTE, _FLAT_RELATIVE * abs(level or 0.0))
    if abs(delta) <= threshold:
        return "flat"
    return "rising" if delta > 0 else "falling"


def _accelerating(trend_1y: Optional[float],
                  trend_5y: Optional[float]) -> Optional[bool]:
    """Whether the last year moved faster than the five-year average pace.

    None when either horizon is missing. An unknown must not be reported as
    False, which would read as "steady" — a different and unearned claim.
    """
    if trend_1y is None or trend_5y is None:
        return None
    pace = abs(trend_5y) / 5.0
    if pace == 0:
        return abs(trend_1y) > _FLAT_ABSOLUTE
    return (trend_1y * trend_5y > 0) and abs(trend_1y) > pace * _ACCELERATING_RATIO


def _annual_points(observations: List[Any], count: int) -> Dict[str, float]:
    """The last ``count`` *annual* observations, oldest first.

    Annual only, deliberately. ``CPI.YOY`` carries a monthly series behind its
    annual one, and a "last five observations" returning five months for one
    indicator and five years for another would be two different questions
    answered under one key.
    """
    annual = [o for o in observations if o.freq == "A"]
    return {o.period: round(o.value, 2) for o in annual[-count:]}


def _series_entry(code: str, observations: List[Any], *,
                  full: bool) -> Optional[Dict[str, Any]]:
    """One indicator's trajectory, or None when it has no usable history.

    Horizons come from ``payload._trend``, which the payload already uses for
    ``trend_1y`` and ``trend_5y``. Reusing it rather than recomputing is what
    keeps this block and the entries beside it from disagreeing about the same
    number — and it inherits the leap-day handling and the half-year tolerance
    that function already got right.

    ``full`` is a token decision, not a design one. Every indicator here already
    appears in the evidence payload above with its label, unit, value and
    period, so repeating all four in the ledger section costs about 900 tokens
    to say nothing new — and p3's lesson was that a payload which grows without
    adding information gets *worse* answers, not merely more expensive ones. The
    macro five keep the long form because their annual path is the thing this
    block exists to add; the ledger constituents carry directions only, which is
    the part that is genuinely not already on the page.
    """
    if not observations:
        return None
    latest = observations[-1]
    horizons = {years: llm_payload._trend(observations, years)
                for years in (1, 3, 5)}
    entry: Dict[str, Any] = {}
    if full:
        spec = constants.INDICATOR_REGISTRY.get(code, {})
        entry.update({
            "label": spec.get("label"),
            "unit": spec.get("unit"),
            "latest": round(latest.value, 4),
            "period": latest.period,
        })
    for years, delta in horizons.items():
        if full:
            entry[f"change_{years}y"] = (round(delta, 4) if delta is not None
                                         else None)
        entry[f"direction_{years}y"] = _direction(delta, latest.value)
    accelerating = _accelerating(horizons[1], horizons[5])
    if accelerating is not None:
        entry["accelerating"] = accelerating
    return entry


def build(series: Dict[str, List[dict]], as_of: datetime.date, *,
          theme_counts: Optional[Dict[str, Dict[str, int]]] = None,
          ) -> Dict[str, Any]:
    """The trend block for one anchor. Pure: no model call, no database read.

    Args:
        series: ``indicator_series`` rows by code, as ``build_evidence_payload``
            takes them. Resolved here through the same vintage bound rather than
            trusted, so this block cannot see anything the payload beside it
            could not.
        as_of: the anchor. Every horizon is measured back from here.
        theme_counts: ``store.counts_by_theme_quarter`` output, already bounded
            at the anchor by its caller. Omitted, the evidence-volume part is
            left out rather than reported empty — an empty count reads as "no
            coverage", which is a claim, and absence is not one.
    """
    resolved = {
        code: llm_payload._resolve(llm_payload._series_observations(rows), as_of)
        for code, rows in (series or {}).items()
    }

    macro: Dict[str, Any] = {}
    for code in MACRO_CODES:
        entry = _series_entry(code, resolved.get(code) or [], full=True)
        if entry is None:
            continue
        points = _annual_points(resolved[code], ANNUAL_POINTS)
        entry["annual"] = points or "unknown"
        # Said out loud rather than left to be counted. A four-year window and a
        # five-year one look identical unless the block states which it had.
        if len(points) < ANNUAL_POINTS:
            entry["annual_note"] = (
                f"only {len(points)} of {ANNUAL_POINTS} years knowable at this date")
        macro[code] = entry

    ledgers: Dict[str, Dict[str, Any]] = {}
    for code, observations in resolved.items():
        if code in MACRO_CODES:
            continue
        ledger = str(constants.INDICATOR_REGISTRY.get(code, {}).get("ledger"))
        if ledger == "None":
            continue
        entry = _series_entry(code, observations, full=False)
        if entry is not None:
            ledgers.setdefault(ledger, {})[code] = entry

    block: Dict[str, Any] = {
        "note": ("Computed from the same vintage-bounded series as the evidence "
                 "above. Directions are stated rather than left to be inferred: "
                 "read each against what its indicator measures. `unknown` means "
                 "the series does not reach back that far at this date; it does "
                 "not mean flat."),
        "macro": macro,
        # Directions only. Every one of these appears above with its value; what
        # is new here is which way it has been going.
        "ledgers": ledgers,
    }
    if theme_counts:
        quarters = sorted(theme_counts)[-QUARTERS:]
        block["evidence_volume"] = {
            "note": ("Articles retrieved per theme per quarter. A theme whose "
                     "coverage collapses is evidence about the reporting, not "
                     "about the country."),
            "quarters": {q: theme_counts[q] for q in quarters},
        }
    return block
