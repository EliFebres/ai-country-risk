"""Deterministic arithmetic for the three ledgers: pure functions, no judgement.

The scoring model judges; this module does the sums it should not have to do in
its head. Every function here is **pure** in the strictest sense the project
uses elsewhere (``ai.policy``, ``provenance``): plain numbers in, plain numbers
out, no network, no database, no clock, no module state. That is what lets the
whole layer be re-run over history for free and pinned by hand-computed tests.

Two conventions run through all of it:

* **Absent means absent.** Any missing or non-numeric input yields ``None``, not
  a zero and not an exception. A ``None`` is a fact about our knowledge that the
  payload stamps and the model reads; a fabricated zero is a lie the model
  cannot detect. Nothing here ever imputes a missing half of a sum.
* **No cross-country context.** Every metric is computed from one country's own
  numbers against fixed, documented scales. Roster-relative normalization was
  the obvious alternative and is deliberately rejected: min-maxing across
  whoever happened to report today makes a country's own history depend on its
  peers' publication lags, so the same country would score differently on two
  days its own data never changed. Where a reference level is genuinely needed
  (:func:`rome_gap`) it is passed in as a frozen, versioned constant.

Units are the source's own throughout — percentage points stay percentage
points, indices stay indices. Nothing here rescales to 0-1; that boundary lives
in ``ai.langchain_llm`` and applies only to model scores.
"""

import logging
import statistics
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


# --- Fixed scales and advisory thresholds -----------------------------------
# The World Bank publishes Governance Indicators as z-scores on a roughly
# [-2.5, +2.5] band by construction. Mapping with those published bounds rather
# than the roster's observed range is what keeps `conversion_loss` comparable
# across days: the denominator is a property of the source, not of today's data.
_WGI_Z_MIN = -2.5
_WGI_Z_MAX = 2.5

# A managed or pegged currency that is being actively defended shows near-zero
# measured volatility in absolute terms — it does not need peers to be
# recognized. 1.5% monthly-return stdev is roughly a fifth of a typical floating
# EM currency and comfortably above the noise of a hard peg.
#
# Advisory tripwire, not policy: this threshold feeds an *input* the model reads
# and weighs. It never modifies a score. Same standing as the constants in
# ``util.lint``.
_SUPPRESSED_FX_VOL_MAX = 1.5

# `rolling_vol` needs most of its window present before a stdev means anything.
# Below this fraction the answer is "we don't know", not a number computed from
# whatever survived.
_VOL_MIN_COVERAGE = 0.75

# Currency regimes for which suppressed volatility is a meaningful reading. A
# free float has nothing to suppress.
_MANAGED_REGIMES = ("peg", "managed")


def _num(value: object) -> Optional[float]:
    """Coerce a value to float, or None if it is not a usable number.

    Booleans are rejected on purpose: ``True`` is not a rate of 1.0, and Python
    would happily float() it into one.

    Args:
        value: the candidate reading.

    Returns:
        The value as a float, or None if it is None, a bool, or non-numeric.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    # NaN propagates silently through arithmetic and lands in the payload
    # looking like a real number. Treat it as the absence it actually is.
    return None if result != result else result


def _clip01(value: float) -> float:
    """Clamp a normalized value into [0, 1]."""
    return max(0.0, min(1.0, value))


# --- Friction ledger --------------------------------------------------------
def conversion_loss(
    gov_effectiveness_z: Optional[float],
    corruption_idx: Optional[float],
) -> Optional[float]:
    """How much of what the state takes fails to come back as capability.

    Both inputs are mapped onto a common [0, 1] "conversion quality" scale using
    each source's own published bounds, then averaged (a simple mean — the two
    are treated as equally informative because there is no defensible basis for
    weighting one over the other, and a weighted blend would be a judgement call
    smuggled into arithmetic). The loss is the complement of that quality.

    Args:
        gov_effectiveness_z: World Bank ``GE.EST``, a z-score on the published
            [-2.5, +2.5] band, higher = more effective government.
        corruption_idx: V-Dem political corruption index, already on [0, 1],
            higher = more corrupt.

    Returns:
        Conversion loss on [0, 1], where 0 means every unit extracted converts
        into capability and 1 means none of it does. None if either input is
        missing — this is the denominator of the headline friction measure, and
        guessing half of it would misstate the number that matters most.
    """
    ge = _num(gov_effectiveness_z)
    corruption = _num(corruption_idx)
    if ge is None or corruption is None:
        return None

    # Align directions first: both components must mean "quality", or the mean
    # of them means nothing.
    ge_quality = _clip01((ge - _WGI_Z_MIN) / (_WGI_Z_MAX - _WGI_Z_MIN))
    corruption_quality = 1.0 - _clip01(corruption)
    quality = (ge_quality + corruption_quality) / 2.0
    return round(1.0 - quality, 4)


def frictional_extraction(
    tax_rev_pct_gdp: Optional[float],
    conversion_loss_value: Optional[float],
) -> Optional[float]:
    """The wedge: how much is taken, discounted by how badly it converts.

    This is the headline friction measure. The point of multiplying rather than
    looking at either half alone is that a large take which converts well is not
    friction, and a small take which converts badly is not much of one either.

    Args:
        tax_rev_pct_gdp: tax revenue as a percent of GDP.
        conversion_loss_value: the output of :func:`conversion_loss`, on [0, 1].

    Returns:
        Percent of GDP extracted and lost, in the same percentage-point unit as
        the revenue input. None if either input is missing.
    """
    take = _num(tax_rev_pct_gdp)
    loss = _num(conversion_loss_value)
    if take is None or loss is None:
        return None
    return round(take * loss, 4)


def doom_loop(
    burden_5y_delta: Optional[float],
    conversion_quality_5y_delta: Optional[float],
) -> Optional[Dict[str, object]]:
    """Whether the burden is rising while the state's conversion is decaying.

    Trajectory, not level: a country can carry a heavy but stable burden
    indefinitely, whereas rising burden alongside falling capability is the
    pattern that compounds.

    Args:
        burden_5y_delta: five-year change in the extraction burden, in its own
            unit. Positive = burden rising.
        conversion_quality_5y_delta: five-year change in conversion quality.
            Negative = quality falling.

    Returns:
        ``{burden_5y_delta, conversion_quality_5y_delta, burden_up_quality_down}``
        with the two deltas echoed alongside the boolean, so a reader never has
        to re-derive which direction produced the flag. None if either delta is
        missing — the flag is a statement about both trends together and cannot
        be made from one.
    """
    burden = _num(burden_5y_delta)
    quality = _num(conversion_quality_5y_delta)
    if burden is None or quality is None:
        return None
    return {
        "burden_5y_delta": burden,
        "conversion_quality_5y_delta": quality,
        "burden_up_quality_down": burden > 0.0 and quality < 0.0,
    }


def rome_gap(
    top_statutory_rate: Optional[float],
    tax_rev_pct_gdp: Optional[float],
    reference_ratio: Optional[float],
) -> Optional[Dict[str, Optional[float]]]:
    """Statutory tax intensity against revenue actually collected.

    A high headline rate paired with thin collection says the rate is aspirational
    and the real economy has routed around it — informal share, avoidance, or
    simple non-enforcement. The ratio alone has no natural scale, so it is
    reported against a frozen reference level.

    The reference is deliberately **not** a live roster median. It is computed
    once from filled ``STAT.TAX.TOP.RATE`` rows, then frozen as
    ``constants.ROME_REFERENCE_RATIO``, so the same country-year always produces
    the same gap no matter which peers reported.

    Args:
        top_statutory_rate: top statutory rate, in percent.
        tax_rev_pct_gdp: tax revenue as a percent of GDP.
        reference_ratio: the frozen reference for ``rate / revenue``. None until
            the curated constant is filled in.

    Returns:
        ``{ratio, reference_ratio, gap}`` where ``gap`` is ``ratio -
        reference_ratio``, or None if the ratio itself cannot be computed.
        ``gap`` alone is None when the reference is absent — the ratio is still
        worth reporting without it.
    """
    rate = _num(top_statutory_rate)
    revenue = _num(tax_rev_pct_gdp)
    if rate is None or revenue is None or revenue == 0.0:
        return None

    ratio = rate / revenue
    reference = _num(reference_ratio)
    return {
        "ratio": round(ratio, 4),
        "reference_ratio": reference,
        "gap": None if reference is None else round(ratio - reference, 4),
    }


def monetary_dilution(
    broad_money_growth: Optional[float],
    real_gdp_growth: Optional[float],
) -> Optional[float]:
    """Money growth in excess of real output growth.

    Annual figures are acceptable here: the measure is about a persistent gap,
    and a single quarter of it says little.

    Args:
        broad_money_growth: broad money growth, percent per year.
        real_gdp_growth: real GDP growth, percent per year.

    Returns:
        The difference in percentage points, or None if either input is missing.
    """
    money = _num(broad_money_growth)
    output = _num(real_gdp_growth)
    if money is None or output is None:
        return None
    return round(money - output, 4)


def real_policy_rate(
    policy_rate: Optional[float],
    cpi_yoy: Optional[float],
) -> Optional[float]:
    """The policy rate net of measured inflation.

    Args:
        policy_rate: central bank policy rate, percent.
        cpi_yoy: CPI inflation, percent year-over-year.

    Returns:
        The difference in percentage points, or None if either input is missing.
    """
    rate = _num(policy_rate)
    cpi = _num(cpi_yoy)
    if rate is None or cpi is None:
        return None
    return round(rate - cpi, 4)


def precommitted_share(
    interest_pct_rev: Optional[float],
    social_protection_pct_rev: Optional[float],
) -> Optional[Dict[str, object]]:
    """Share of revenue already committed before any discretionary choice.

    Interest and social protection are the two components that cannot be
    reallocated within a budget year, so their sum is what remains unavailable
    to a government under pressure.

    When social protection is missing the interest-only figure is returned and
    marked ``partial``. It is never imputed: a made-up social protection number
    would understate or overstate the constraint by more than the whole interest
    line in most countries, and the model can weigh a partial reading honestly
    if it is told that is what it has.

    Args:
        interest_pct_rev: interest payments as a percent of revenue.
        social_protection_pct_rev: social protection spending as a percent of
            revenue, or None.

    Returns:
        ``{value, partial}`` where ``partial`` is True when the social
        protection half was absent. None if interest itself is missing, since
        there is then nothing to report.
    """
    interest = _num(interest_pct_rev)
    if interest is None:
        return None

    social = _num(social_protection_pct_rev)
    if social is None:
        return {"value": round(interest, 4), "partial": True}
    return {"value": round(interest + social, 4), "partial": False}


def wage_productivity_gap(
    real_wage_growth: Optional[float],
    output_per_worker_growth: Optional[float],
) -> Optional[float]:
    """Real wage growth in excess of productivity growth.

    Supplementary to the friction ledger: a sustained positive gap is a claim on
    output that has not been produced yet.

    Args:
        real_wage_growth: real wage growth, percent per year.
        output_per_worker_growth: output per worker growth, percent per year.

    Returns:
        The difference in percentage points, or None if either input is missing.
    """
    wages = _num(real_wage_growth)
    productivity = _num(output_per_worker_growth)
    if wages is None or productivity is None:
        return None
    return round(wages - productivity, 4)


def dependency_trajectory(
    current_ratio: Optional[float],
    projected_ratio_10y: Optional[float],
) -> Optional[Dict[str, Optional[float]]]:
    """Old-age dependency now and where the projection puts it in ten years.

    Args:
        current_ratio: current old-age dependency ratio.
        projected_ratio_10y: the ten-year projected ratio, from the curated UN
            WPP file. None until that file is filled.

    Returns:
        ``{current, projected_10y, delta}``, with ``delta`` None when the
        projection is absent. None if the current level is missing, since the
        level is the part that is actually measured.
    """
    current = _num(current_ratio)
    if current is None:
        return None

    projected = _num(projected_ratio_10y)
    return {
        "current": round(current, 4),
        "projected_10y": projected,
        "delta": None if projected is None else round(projected - current, 4),
    }


# --- Uncertainty ledger -----------------------------------------------------
def rolling_vol(series: Optional[Sequence[Optional[float]]], window: int) -> Optional[float]:
    """Sample standard deviation of the last ``window`` observations.

    Used for CPI over 36 months and FX monthly returns over 24. The coverage
    floor matters: a "36-month volatility" computed from four surviving prints
    is not a volatility, and reporting one would let a country with a broken
    statistics office look calm.

    Args:
        series: observations in chronological order. None entries are dropped
            (they are gaps in publication, not zeros), but they still count
            against the window's coverage.
        window: how many trailing observations the measure is defined over.

    Returns:
        The sample stdev in the series' own unit, or None if fewer than
        ``window * 0.75`` of the trailing observations are present or the window
        is not a usable size.

    Raises:
        Nothing. A malformed ``window`` yields None like any other absent input.
    """
    if not isinstance(window, int) or isinstance(window, bool) or window < 2:
        return None
    if not series:
        return None

    tail = list(series)[-window:]
    observed = [v for v in (_num(x) for x in tail) if v is not None]
    if len(observed) < window * _VOL_MIN_COVERAGE:
        return None
    # stdev needs two points even when the coverage floor is satisfied by a
    # window of 2 with one gap.
    if len(observed) < 2:
        return None
    return round(statistics.stdev(observed), 6)


def fx_monthly_returns(
    fx_series: Optional[Sequence[Optional[float]]],
) -> Optional[List[Optional[float]]]:
    """Month-over-month **simple** returns of an exchange rate series.

    Simple returns, not log returns, chosen deliberately: a bad or
    placeholder FX print of zero or a negative number makes ``log`` undefined
    and would take down the whole series, while simple returns degrade to a
    single None at that step. Over monthly moves the two agree closely enough
    that the robustness is worth more than the additivity.

    Args:
        fx_series: exchange rates in chronological order. None entries are
            preserved as gaps so positions stay aligned with the input.

    Returns:
        A list one shorter than the input, holding the fractional change at each
        step in percent, with None where either endpoint was missing or the
        earlier rate was zero. None if the input has fewer than two points.
    """
    if not fx_series or len(fx_series) < 2:
        return None

    values = [_num(x) for x in fx_series]
    returns: List[Optional[float]] = []
    for prev, curr in zip(values, values[1:]):
        if prev is None or curr is None or prev == 0.0:
            returns.append(None)
        else:
            returns.append(round((curr - prev) / prev * 100.0, 6))
    return returns


def suppressed_vol_flag(
    regime: Optional[str],
    fx_vol: Optional[float],
    reserves_trend_6m: Optional[float],
) -> Optional[bool]:
    """Whether measured calm in the currency is being paid for out of reserves.

    True only when all three hold: the regime is managed or pegged, measured FX
    volatility is below :data:`_SUPPRESSED_FX_VOL_MAX`, and reserves have
    trended down over six months. A float has nothing to suppress; a managed
    rate that is calm *and* not costing reserves is simply a credible one.

    This is an **input the model reads and weighs**, never a score modifier. The
    threshold is an absolute level rather than a peer percentile so that a
    country's own flag history does not shift when its peers' data does.

    Args:
        regime: currency regime from the curated ``fx_regimes.yaml`` — one of
            ``peg``, ``managed``, ``float`` (case-insensitive). None until that
            file is filled.
        fx_vol: measured FX volatility, from :func:`rolling_vol` over
            :func:`fx_monthly_returns`, in percent.
        reserves_trend_6m: six-month change in reserves. Negative = falling.

    Returns:
        The flag, or None if any input needed to decide it is missing. None is
        not False: "we have no regime file" and "this is a free float" are
        different facts and the payload stamps them differently.
    """
    if not isinstance(regime, str):
        return None
    vol = _num(fx_vol)
    trend = _num(reserves_trend_6m)
    if vol is None or trend is None:
        return None

    if regime.strip().lower() not in _MANAGED_REGIMES:
        return False
    return vol < _SUPPRESSED_FX_VOL_MAX and trend < 0.0


# --- Information ledger -----------------------------------------------------
def instrument_quality(
    spi: Optional[float],
    press_freedom: Optional[float],
    obs: Optional[float] = None,
    egdi: Optional[float] = None,
) -> Optional[Dict[str, object]]:
    """How much the country's own instruments can be trusted to measure it.

    The statistical system and the press are the two instruments that cannot be
    substituted: without the first there are no numbers, without the second
    there is no one to contest them. Both are required. Open Budget Survey and
    the UN e-government index sharpen the reading where available but cannot
    stand in for the core pair.

    All four inputs are 0-100 scores where higher = better instruments, on the
    scales their publishers use. The blend is a simple mean of whatever is
    present, and the component count is returned alongside it so the payload can
    say how much of the picture the number covers.

    Args:
        spi: World Bank Statistical Performance Indicators overall score.
        press_freedom: RSF press freedom score.
        obs: Open Budget Survey score, optional.
        egdi: UN E-Government Development Index, rescaled to 0-100, optional.

    Returns:
        ``{value, components}`` with the blend and how many inputs went into it,
        or None if either core input is missing.
    """
    spi_value = _num(spi)
    press_value = _num(press_freedom)
    if spi_value is None or press_value is None:
        return None

    parts = [spi_value, press_value]
    parts += [v for v in (_num(obs), _num(egdi)) if v is not None]
    return {"value": round(sum(parts) / len(parts), 4), "components": len(parts)}
