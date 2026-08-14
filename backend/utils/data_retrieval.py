"""Read side of the macro panel: Parquet in, LLM-ready payload out.

``main.py`` writes each country's World Bank panel to
``backend/data/wb_panel_wide/country_code=XX/*.parquet`` (see
``data_fetching/country_data_fetch.ingest_panel_wide``); this module reads it
back with DuckDB and shapes it into the compact JSON payload the risk prompt
and the database upsert both consume.

The payload is deliberately "pretty": indicators carry their display names and
units, values are rounded, and only a short recent window plus a couple of
change horizons are included — enough for the model to reason about trend and
level without spending context on a full history.

Everything per-indicator is anchored on that indicator's own newest
observation. The World Bank publishes these on different lags, so the panel's
last row is populated for the fastest series only, and anchoring on it would
report a null ``latest`` for the slower half of the set.
"""

import calendar
import json
import logging
import re
import duckdb
import pathlib
import pandas as pd

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, NamedTuple, Optional

from dateutil.relativedelta import relativedelta

from backend.utils import constants, metrics, provenance
from backend.utils.dates import utc_minute_iso

logger = logging.getLogger(__name__)


# Anchor all data paths to the backend/ folder (this file lives in backend/utils/)
BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]   # .../backend
DATA_DIR    = BACKEND_DIR / "data" / "wb_panel_wide"        # .../backend/data/wb_panel_wide

_ISO_CODE_RE = re.compile(r"[A-Z]{2,3}")


def _validate_iso_code(value: object, param: str) -> str:
    """Return ``value`` if it is a 2- or 3-letter uppercase ISO code.

    Args:
        value: the candidate code.
        param: parameter name, used in the error message.

    Returns:
        The validated code.

    Raises:
        TypeError: if ``value`` is not a string.
        ValueError: if it is not 2-3 uppercase letters.
    """
    if not isinstance(value, str):
        raise TypeError(f"`{param}` must be a str, got {type(value).__name__}: {value!r}")
    if not _ISO_CODE_RE.fullmatch(value):
        raise ValueError(
            f"`{param}` must be a 2- or 3-letter uppercase ISO code, got {value!r}"
        )
    return value


def query_macro_panel(country_iso_code: str) -> pd.DataFrame:
    """Load one country's macro panel (years >= 2000) from Parquet.

    Args:
        country_iso_code: 2- or 3-letter uppercase ISO code naming the
            partition to read.

    Returns:
        Year-ordered DataFrame with one column per indicator.

    Raises:
        TypeError: if ``country_iso_code`` is not a string.
        ValueError: if it is not a valid ISO code.
        FileNotFoundError: if the country has no Parquet partition yet — the
            backfill in ``main.ensure_missing_country_panels`` has not run for it.
    """
    _validate_iso_code(country_iso_code, "country_iso_code")

    part_dir = DATA_DIR / f"country_code={country_iso_code}"
    parquet_files = sorted(part_dir.glob("*.parquet"))

    if not parquet_files:
        raise FileNotFoundError(
            f"No parquet files found for {country_iso_code} at {part_dir}/*.parquet\n"
            f"HINTS:\n"
            f"  • Ensure writes go to {DATA_DIR}\n"
            f"  • Run the backfill (main.ensure_missing_country_panels) or confirm\n"
            f"    the country exists in constants.COUNTRY_ROSTER\n"
            f"  • Check permissions / paths in your runtime environment"
        )

    # Use glob form so DuckDB can read the full partition if multiple files exist
    parquet_glob = (part_dir / "*.parquet").as_posix()

    sql = f"""
        SELECT *
        FROM read_parquet('{parquet_glob}')
        WHERE year >= 2000
        ORDER BY year
    """
    return duckdb.sql(sql).df()


def prepare_llm_payload_pretty(
    country_iso: str,
    indicators: dict[str, str],
    *,
    since: int = 2015,
    lookback: int = 10,
    deltas: tuple[int, ...] = (1, 5),
) -> dict:
    """Build the compact macro payload sent to the risk prompt and the DB.

    Args:
        country_iso: 2- or 3-letter uppercase ISO code.
        indicators: raw column name -> World Bank code. Only the keys are used
            here (to select columns); display names come from
            ``constants.NICE_NAME``.
        since: earliest year to include.
        lookback: how many recent values to keep per indicator series.
        deltas: change horizons in years, emitted as ``Δ{h}y`` keys. Each is an
            absolute difference in the indicator's own unit, measured from that
            indicator's newest observation.

    Returns:
        ``{country, latest_year, indicators: {pretty_name: {latest, Δ..y,
        series}}, _meta: {units, source, generated_at, series_lookback,
        data_dir}}``. ``_meta.generated_at`` is the timestamp
        ``data_push.upsert_snapshot`` parses back into the snapshot's ``as_of``.

    Raises:
        TypeError: if ``country_iso`` or ``indicators`` has the wrong type.
        ValueError: if the ISO code, ``since``, ``lookback``, or ``deltas``
            are out of range.
        FileNotFoundError: if the country has no Parquet partition.
    """
    _validate_iso_code(country_iso, "country_iso")

    if not isinstance(indicators, dict):
        raise TypeError(f"`indicators` must be a dict, got {type(indicators).__name__}")
    if not indicators:
        raise ValueError("`indicators` must not be empty")
    bad_keys = [k for k in indicators if not (isinstance(k, str) and k)]
    if bad_keys:
        raise ValueError(f"indicator keys must be non-empty str, got {bad_keys!r}")

    this_year = datetime.now().year
    if not isinstance(since, int) or not 1900 <= since <= this_year:
        raise ValueError(f"`since` must be a year between 1900 and {this_year}, got {since!r}")
    if not isinstance(lookback, int) or lookback <= 0:
        raise ValueError(f"`lookback` must be a positive int, got {lookback!r}")
    bad_deltas = [h for h in deltas if not (isinstance(h, int) and h > 0)]
    if bad_deltas:
        raise ValueError(f"`deltas` must contain positive ints, got {bad_deltas!r}")

    # ---- load & filter panel ----------------------------------------------
    df = query_macro_panel(country_iso)
    df = df[df.year >= since]

    latest_year = int(df["year"].max())

    # ---- per-indicator build ----------------------------------------------
    year_indexed = df.set_index("year")
    ind_payload: dict[str, dict] = {}
    for raw_col in indicators.keys():
        pretty_name = constants.NICE_NAME.get(raw_col, raw_col)
        column = year_indexed[raw_col]

        # Anchor on this indicator's own newest observation, not on the panel's
        # last row: the World Bank publishes these on different lags (WGI
        # z-scores trail a year, Gini two), so the newest row is populated only
        # for the fastest of them and reading `latest` off it nulls the rest.
        observed = column.dropna()
        latest = None if observed.empty else round(float(observed.iloc[-1]), 2)

        # last `lookback` values
        series = observed.tail(lookback).round(2).to_dict()

        # Δ-changes, as absolute differences in the indicator's own unit. Every
        # indicator here is a rate, ratio, or index, and percent-change breaks
        # on the ones that cross zero: PT inflation going -0.01 -> 2.34 is
        # +2.35pp, but pct_change reports -188.8 (huge, and sign-flipped by the
        # negative base).
        delta_vals: dict[str, float | None] = {}
        for h in deltas:
            base = None if observed.empty else column.get(int(observed.index[-1]) - h)
            delta_vals[f"Δ{h}y"] = (
                None if base is None or pd.isna(base)
                else round(float(observed.iloc[-1]) - float(base), 3)
            )

        ind_payload[pretty_name] = {
            "latest": latest,
            **delta_vals,
            "series": series,
        }

    return {
        "country": country_iso,
        "latest_year": latest_year,
        "indicators": ind_payload,
        "_meta": {
            "units": constants.UNITS,
            "delta_basis": "Δ values are absolute changes in the indicator's own unit, not percent changes",
            "source": "World Bank",
            "generated_at": utc_minute_iso(datetime.now(timezone.utc)),
            "series_lookback": lookback,
            "data_dir": str(DATA_DIR),
        },
    }


# ---------------------------------------------------------------------------
# Evidence payload v2 — the three-ledger view the scoring model receives
# ---------------------------------------------------------------------------
# This is a *second* payload, deliberately not a replacement for
# ``prepare_llm_payload_pretty``. That one is still the panel/DB payload:
# ``data_push.upsert_snapshot`` reads its ``indicators`` and ``_meta.units`` to
# write the ``indicator`` and ``yearly_value`` tables the front-end's indicator
# pane reads, and ``provenance.macro_vintages`` reads its ``series``. Reshaping
# it into ledgers would have broken both.
#
# So: the panel payload keeps its job, and this builds the evidence the model
# sees. They share the same parquet read.

# Long history is expensive in context and only two indicators earn it — the two
# whose *path* matters as much as their level.
_LONG_HISTORY_CODES = ("CPI.YOY", "NY.GDP.PCAP.KD.ZG")
_LONG_HISTORY_YEARS = 10

# Rolling windows, in observations.
_CPI_VOL_MONTHS = 36
_FX_VOL_MONTHS = 24
_RESERVES_TREND_MONTHS = 6

# Roughly four characters per token. Deliberately an estimate rather than a real
# tokenizer: `tiktoken` is pinned but downloads a BPE file on first use, and a
# payload builder that reaches the network to count its own tokens is a payload
# builder that fails offline.
_CHARS_PER_TOKEN = 4
# Raised from 2500 with payload version p2, which added the four WEO indicators.
# A fully-populated country came to ~2514. Sized against the contract rather
# than trimmed to fit the old number: four annual series cost about 1% more
# tokens for about 19% more indicators, which is the trade worth making.
_TOKEN_BUDGET = 2800


def _period_to_date(period: str, freq: str) -> Optional[date]:
    """End-of-period date for an ``indicator_series`` period string.

    Args:
        period: ``'2025'``, ``'2026Q1'`` or ``'2026-06'``.
        freq: the declared frequency, used to pick the parse.

    Returns:
        The last day of the period, or None if it does not parse.
    """
    try:
        if freq == "M":
            year, month = period.split("-")
            return _end_of_month(int(year), int(month))
        if freq == "Q":
            year, quarter = period.split("Q")
            return _end_of_month(int(year), int(quarter) * 3)
        if freq == "A":
            return date(int(period), 12, 31)
    except (TypeError, ValueError):
        return None
    return None


def _end_of_month(year: int, month: int) -> date:
    """Last calendar day of ``month`` in ``year``."""
    return date(year, month, calendar.monthrange(year, month)[1])


class _Observation(NamedTuple):
    """One resolved reading, with everything needed to stamp its provenance."""
    value: float
    period: str
    freq: str
    period_end: date
    as_of: date
    source: str
    # Whether `as_of` is a real publication date or a stand-in. The panel has no
    # record of when the World Bank published a figure, so it uses the year's
    # end; that is a placeholder wearing the shape of a vintage, and it must
    # never outrank an edition that really was published on its date.
    dated: bool = False


def _panel_observations(panel: pd.DataFrame, panel_col: str) -> List[_Observation]:
    """Read one indicator's annual history out of the parquet panel.

    ``as_of`` for a panel value is the end of its own year: the panel carries no
    record of when the World Bank published it, and claiming the fetch date
    would understate the staleness the model is supposed to see.
    """
    if panel_col not in panel.columns:
        return []
    observations: List[_Observation] = []
    for year, value in panel.set_index("year")[panel_col].dropna().items():
        try:
            period = str(int(year))
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        period_end = date(int(year), 12, 31)
        observations.append(_Observation(
            value=numeric, period=period, freq="A", period_end=period_end,
            as_of=period_end, source="World Bank panel",
        ))
    return observations


def _series_observations(rows: List[dict]) -> List[_Observation]:
    """Convert ``indicator_series`` rows into observations, dropping unusable ones."""
    observations: List[_Observation] = []
    for row in rows or []:
        value, period, freq = row.get("value"), row.get("period"), row.get("freq")
        if value is None or not period or not freq:
            continue
        period_end = _period_to_date(str(period), str(freq))
        as_of = row.get("as_of")
        if period_end is None or not isinstance(as_of, date):
            continue
        observations.append(_Observation(
            value=float(value), period=str(period), freq=str(freq),
            period_end=period_end, as_of=as_of, source=str(row.get("source") or "unknown"),
            # `indicator_series` rows carry an as_of that was decided when the
            # row was written — a WEO edition date, or a publication-lag stamp —
            # rather than derived from the period. That is what `dated` means,
            # and it is what lets a real vintage outrank the panel's year-end.
            dated=True,
        ))
    return observations


def _recent_observation(entry: Optional[dict]) -> Optional[_Observation]:
    """Convert a ``recent_indicator`` row into an observation, or None."""
    if not isinstance(entry, dict):
        return None
    value, period, freq = entry.get("value"), entry.get("period"), entry.get("freq")
    if value is None or not isinstance(period, date) or freq not in ("M", "Q", "A"):
        return None
    label = (period.strftime("%Y-%m") if freq == "M"
             else f"{period.year}Q{(period.month - 1) // 3 + 1}" if freq == "Q"
             else str(period.year))
    return _Observation(
        value=float(value), period=label, freq=str(freq), period_end=period,
        as_of=period, source=str(entry.get("source") or "IMF"),
    )


def _resolve(observations: List[_Observation],
             as_of: Optional[date] = None) -> List[_Observation]:
    """Merge one indicator's copies from every store, freshest copy winning.

    An indicator can live in three places at once — the annual panel, the
    monthly latest print, and the series store. Same-period duplicates are
    collapsed to the copy with the newer ``as_of``; the result is sorted oldest
    first so the last entry is the freshest and a rolling window is a tail slice.

    This is what puts a monthly CPI print in front of the model instead of an
    annual average up to two years stale.

    Args:
        as_of: when scoring a past date, the newest vintage that may be used.
            "Freshest wins" becomes "freshest that existed yet wins", and both
            observations *published* after the date and periods *covering* time
            after it are dropped. Without this a 2018 snapshot is scored on
            2026's revisions of 2018 — the macro twin of reading tomorrow's
            news, and quieter, because a revised number looks exactly like an
            unrevised one.
    """
    if as_of is not None:
        observations = [o for o in observations
                        if o.as_of <= as_of and o.period_end <= as_of]

    # A real vintage always outranks a synthesized one, whatever the dates say.
    # The panel stamps every annual figure with 31 December of its own year, so
    # between the year end and the next WEO edition — January to March, a
    # quarter of the anchors — that placeholder was beating the edition that
    # actually existed, and the snapshot silently read today's revision of last
    # year instead of the number a reader could have had.
    best: Dict[str, _Observation] = {}
    for obs in observations:
        key = f"{obs.freq}:{obs.period}"
        current = best.get(key)
        if current is None or (obs.dated, obs.as_of) > (current.dated, current.as_of):
            best[key] = obs

    # Sort by the period the value describes, not by when we learned it: an
    # annual 2025 figure and a monthly 2026-06 print must order by coverage.
    return sorted(best.values(), key=lambda o: (o.period_end, o.freq))


def _trend(observations: List[_Observation], years: int) -> Optional[float]:
    """Change over ``years``, in the indicator's own unit.

    Absolute difference, not percent change: every indicator here is a rate,
    ratio, or index, and percent change breaks on the ones that cross zero.

    Returns None when no observation sits near enough to the target date — the
    tolerance is half a year, so a gappy annual series still yields a trend but
    a five-year gap does not silently become a one-year one.
    """
    if not observations:
        return None
    latest = observations[-1]
    # `relativedelta`, not `.replace(year=...)`: a monthly observation for
    # February ends on the 29th in a leap year, and `date(2020, 2, 29).replace(
    # year=2019)` raises rather than returning anything. It is not hypothetical
    # — it is every snapshot anchored in the months after a leap February, which
    # in the pilot window is 2016, 2020 and 2024. `relativedelta` clamps to the
    # 28th, which is what "a year before this" means for a month-end.
    target = latest.period_end - relativedelta(years=years)

    tolerance = timedelta(days=183)
    candidates = [o for o in observations if abs((o.period_end - target).days) <= tolerance.days]
    if not candidates:
        return None
    base = min(candidates, key=lambda o: abs((o.period_end - target).days))
    return round(latest.value - base.value, 4)


def _stamp(observations: List[_Observation], code: str, as_of: date) -> Optional[dict]:
    """Build one indicator's payload entry: value plus its full provenance.

    Returns None when there is nothing to report, so the caller can omit the key
    entirely. Absent indicators are absent from the payload rather than padded
    with nulls — a null the model has to interpret is noise, and a missing key
    is unambiguous.
    """
    if not observations:
        return None
    spec = constants.INDICATOR_REGISTRY.get(code, {})
    latest = observations[-1]
    entry = {
        "value": round(latest.value, 4),
        "period": latest.period,
        "freq": latest.freq,
        # Two different dates, and conflating them is what makes a stale reading
        # look fresh. `as_of` is when the value became known to us — provenance,
        # matching `indicator_series.as_of`. `staleness_days` measures from the
        # end of the period the value *describes*, which is what "how old is
        # this reading" actually means.
        #
        # The difference is not cosmetic: a 2020 Human Capital Index reading
        # fetched today has as_of = today, and reporting staleness against that
        # would tell the model a six-year-old number is current.
        "as_of": latest.as_of.isoformat(),
        "staleness_days": (as_of - latest.period_end).days,
        "source": latest.source,
        "unit": spec.get("unit"),
    }
    for years, key in ((1, "trend_1y"), (5, "trend_5y")):
        trend = _trend(observations, years)
        if trend is not None:
            entry[key] = trend
    if code in _LONG_HISTORY_CODES:
        # The two indicators whose path matters as much as their level.
        # Same leap-day trap as `_trend`, one step further out: this one takes
        # the anchor rather than an observation, so it fires on any run whose
        # own date is 29 February — which for the daily run is the whole roster,
        # every leap year.
        cutoff = as_of - relativedelta(years=_LONG_HISTORY_YEARS)
        entry["history"] = {
            o.period: round(o.value, 2) for o in observations if o.period_end >= cutoff
        }
    return entry


def _values(observations: List[_Observation], freq: Optional[str] = None) -> List[float]:
    """Just the numbers, oldest first — what the metrics functions consume.

    Args:
        observations: a resolved series.
        freq: keep only observations at this frequency. **Required for anything
            windowed.** A resolved series deliberately mixes frequencies — an
            annual history behind a monthly print is exactly what
            freshest-value-wins produces — and a "36-month volatility" computed
            over ten annual values and six monthly ones is a number with no
            meaning. Passing None keeps everything and is only correct for
            consumers that do not care about the spacing.
    """
    return [o.value for o in observations if freq is None or o.freq == freq]


def build_evidence_payload(
    country_iso2: str,
    *,
    as_of: date,
    panel: Optional[pd.DataFrame] = None,
    series: Optional[Dict[str, List[dict]]] = None,
    recent: Optional[Dict[str, dict]] = None,
    fx_regimes: Optional[Dict[str, str]] = None,
    elections: Optional[Dict[str, List[dict]]] = None,
    vintage_as_of: Optional[date] = None,
    structural: Optional[Dict[str, Dict[str, Any]]] = None,
) -> dict:
    """Build the three-ledger evidence payload the scoring model receives.

    Every store is passed in rather than read here, so this function is
    testable without a database and re-runnable over history: the caller decides
    which snapshot of the world it sees.

    Args:
        country_iso2: the country being scored.
        as_of: the date being scored. Staleness is measured against this, never
            against the clock, so re-running an old date reports the staleness
            that was true then.
        panel: the parquet macro panel, from :func:`query_macro_panel`.
        series: ``indicator_series`` rows, from ``data_push.read_indicator_series``.
        recent: latest prints, from ``data_push.read_recent_indicators``.
        fx_regimes: currency regimes, from ``constants.FX_REGIMES``.
        elections: election calendar, from ``constants.ELECTIONS``.
        vintage_as_of: for a historical backfill, the newest data vintage this
            snapshot is allowed to see. Deliberately separate from ``as_of``
            and defaulting to None, so the daily run is unaffected: passing
            today's date would drop the current year's annual figures, whose
            period ends in December. Only a historical run wants that, and a
            historical run wants it badly — otherwise a 2018 score is built on
            2026's revisions of 2018.
        structural: static per-country facts, from
            ``curated_loader.load_structural_facts``. Masking removes the
            country's name and with it the priors the name carried; this puts
            the structural ones back as stated evidence. Deliberately not
            time-varying — anything that moves year to year is an
            ``indicator_series`` row with its own vintage, or it would be a
            future leak on every historical snapshot.

    Returns:
        ``{_meta, friction_inputs, uncertainty_inputs, information_inputs,
        edge_inputs, computed}``. Indicators with no observation are omitted
        entirely. Every present value carries ``period``, ``freq``, ``as_of``,
        ``staleness_days`` and ``source`` so the model can weigh a fresh reading
        differently from a stale one.
    """
    panel = panel if panel is not None else pd.DataFrame()
    series = series or {}
    recent = recent or {}

    # --- resolve every registry indicator across all three stores ------------
    resolved: Dict[str, List[_Observation]] = {}
    for code, spec in constants.INDICATOR_REGISTRY.items():
        observations: List[_Observation] = []
        panel_col = spec.get("panel_col")
        if panel_col and not panel.empty:
            observations += _panel_observations(panel, str(panel_col))
        observations += _series_observations(series.get(code, []))
        recent_name = spec.get("recent_name")
        if recent_name:
            fresh = _recent_observation(recent.get(str(recent_name)))
            if fresh:
                observations.append(fresh)
        merged = _resolve(observations, vintage_as_of)
        if merged:
            resolved[code] = merged

    def latest_value(code: str) -> Optional[float]:
        """The freshest number for one indicator, or None."""
        observations = resolved.get(code)
        return observations[-1].value if observations else None

    # --- ledger sections -----------------------------------------------------
    sections: Dict[str, Dict[str, dict]] = {
        "friction_inputs": {}, "uncertainty_inputs": {},
        "information_inputs": {}, "edge_inputs": {},
    }
    ledger_to_section = {
        "friction": "friction_inputs", "uncertainty": "uncertainty_inputs",
        "information": "information_inputs", "edge": "edge_inputs",
    }
    for code, observations in resolved.items():
        ledger = constants.INDICATOR_REGISTRY[code].get("ledger")
        section = ledger_to_section.get(str(ledger))
        if not section:
            continue  # no ledger: a denominator or a helper, not evidence
        entry = _stamp(observations, code, as_of)
        if entry:
            sections[section][str(constants.INDICATOR_REGISTRY[code]["label"])] = entry

    # --- computed metrics ----------------------------------------------------
    computed: Dict[str, object] = {}

    loss = metrics.conversion_loss(
        latest_value("GOV_WGI_GE.EST"), latest_value("POL_CORRUPTION")
    )
    extraction = metrics.frictional_extraction(latest_value("GC.TAX.TOTL.GD.ZS"), loss)
    if loss is not None:
        computed["conversion_loss"] = loss
    if extraction is not None:
        computed["frictional_extraction"] = extraction

    # The doom loop needs the same two five-year deltas, one of which is the
    # conversion quality — the complement of the loss, so it is differenced from
    # the two underlying trends rather than re-derived from a single number.
    tax_trend = _trend(resolved.get("GC.TAX.TOTL.GD.ZS", []), 5)
    ge_trend = _trend(resolved.get("GOV_WGI_GE.EST", []), 5)
    corruption_trend = _trend(resolved.get("POL_CORRUPTION", []), 5)
    if ge_trend is not None and corruption_trend is not None:
        # Both mapped onto the same [0,1] quality scale conversion_loss uses.
        quality_trend = round((ge_trend / 5.0 + -corruption_trend) / 2.0, 4)
        loop = metrics.doom_loop(tax_trend, quality_trend)
        if loop:
            computed["doom_loop"] = loop

    gap = metrics.rome_gap(
        latest_value("STAT.TAX.TOP.RATE"), latest_value("GC.TAX.TOTL.GD.ZS"),
        constants.ROME_REFERENCE_RATIO,
    )
    if gap:
        computed["rome_gap"] = gap

    dilution = metrics.monetary_dilution(
        latest_value("FM.LBL.BMNY.ZG"), latest_value("NY.GDP.PCAP.KD.ZG")
    )
    if dilution is not None:
        computed["monetary_dilution"] = dilution

    real_rate = metrics.real_policy_rate(
        latest_value("BIS.POLICY.RATE"), latest_value("CPI.YOY")
    )
    if real_rate is not None:
        computed["real_policy_rate"] = real_rate

    # Monthly observations only: the resolved CPI series carries an annual
    # history behind its monthly prints, and a stdev over both would measure the
    # frequency change rather than the inflation.
    cpi_vol = metrics.rolling_vol(_values(resolved.get("CPI.YOY", []), "M"), _CPI_VOL_MONTHS)
    if cpi_vol is not None:
        computed["cpi_volatility_36m"] = cpi_vol

    fx_returns = metrics.fx_monthly_returns(_values(resolved.get("BIS.FX.USD", []), "M"))
    fx_vol = metrics.rolling_vol(fx_returns, _FX_VOL_MONTHS)
    if fx_vol is not None:
        computed["fx_volatility_24m"] = fx_vol

    precommitted = metrics.precommitted_share(
        latest_value("GC.XPN.INTP.RV.ZS"),
        # No free source for social protection as a share of revenue; the metric
        # reports the interest-only figure marked partial rather than imputing.
        None,
    )
    if precommitted:
        computed["precommitted_share"] = precommitted

    quality = metrics.instrument_quality(
        latest_value("IQ.SPI.OVRL"), latest_value("RSF.PRESS.SCORE"),
        latest_value("OBS.SCORE"), latest_value("UN.EGDI"),
    )
    if quality:
        computed["instrument_quality"] = quality

    dependency = metrics.dependency_trajectory(
        latest_value("SP.POP.DPND.OL"), latest_value("UNWPP.DPND.OL.PROJ")
    )
    if dependency:
        computed["dependency_trajectory"] = dependency

    # Suppressed volatility: reported under uncertainty because it is evidence
    # the model weighs, not a computed score. `regime: None` is not `float` —
    # an absent regime file and a genuine free float are different facts.
    # Monthly only, for the same reason the volatilities are: a six-month trend
    # must be six months, not six observations of mixed spacing.
    reserves = _values(resolved.get("RESERVES.USD", []), "M")
    reserves_trend = None
    if len(reserves) > _RESERVES_TREND_MONTHS:
        reserves_trend = round(reserves[-1] - reserves[-1 - _RESERVES_TREND_MONTHS], 4)
    regime = (fx_regimes or {}).get(country_iso2)
    flag = metrics.suppressed_vol_flag(regime, fx_vol, reserves_trend)
    sections["uncertainty_inputs"]["suppressed_vol_flag"] = {
        "value": flag,
        "regime": regime,
        "fx_volatility_24m": fx_vol,
        "reserves_trend_6m": reserves_trend,
        "note": (
            "True means measured calm is being bought with reserves under a managed "
            "or pegged regime. null means one of the three inputs is unavailable — "
            "not that the flag is false."
        ),
    }

    next_election = None
    for entry in (elections or {}).get(country_iso2, []):
        if entry["date"] >= as_of.isoformat():
            next_election = entry
            break

    payload = {
        "_meta": {
            "country": country_iso2,
            "as_of": as_of.isoformat(),
            # Which regime built this payload, not a constant. A vintage-bounded
            # build is point-in-time; reporting it as "as-published-latest" told
            # the audit record the exact opposite of what happened, and the
            # manifest is the only place that difference is ever visible.
            "vintage_scheme": ("point-in-time" if vintage_as_of is not None
                               else provenance._VINTAGE_SCHEME),
            "staleness_basis": (
                "staleness_days counts from the end of the period a value describes "
                "to as_of: how old the reading is. `as_of` on each value is a "
                "separate fact — when it became known to us. A large "
                "staleness_days means the reading is old, not that it is wrong."
            ),
            "next_scheduled_election": next_election,
        },
        **sections,
        "computed": computed,
    }

    # The facts identity used to imply, stated because masking took identity
    # away. Omitted entirely when the country has no block, exactly as an
    # indicator with no observation is omitted: an empty `structural` key would
    # read to the model as "this country has no structure", which is false and
    # is worse than silence.
    country_structural = (structural or {}).get(country_iso2)
    if country_structural:
        payload["structural"] = {
            **country_structural,
            "note": (
                "Facts about this country that do not change year to year, "
                "supplied because the country is not named. Reason from these "
                "rather than from any guess about which country this is."
            ),
        }

    estimated_tokens = len(json.dumps(payload, ensure_ascii=False)) // _CHARS_PER_TOKEN
    log = logger.warning if estimated_tokens > _TOKEN_BUDGET else logger.info
    log("[%s] evidence payload ~%d tokens (budget %d)",
        country_iso2, estimated_tokens, _TOKEN_BUDGET)
    return payload
