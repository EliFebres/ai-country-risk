"""Scoring a decade one week at a time, on the live code path.

Everything below this module is the daily pipeline with ``as_of`` pinned. That
is the whole premise: if the backfill had its own scoring path, the series it
produced would be measuring the backfill rather than the instrument, and no
amount of care afterwards could separate the two. So this file drives, and does
not compute.

What it adds to `_process_country` is only the four things a multi-thousand-call
run needs and a single call does not:

* **Anchors.** Weekly Mondays, the same cadence the full 48-country backfill
  will use, so the pilot is a scale model rather than a different experiment.
* **Resume.** The ledger records every completed (date, country, mode), and a
  restart skips them. Only ``complete`` counts — a country that died half way
  through is retried, never silently skipped.
* **A budget that survives restarts.** Spend is metered from the API's own usage
  fields and summed out of the ledger, not held in memory, so stopping and
  resuming a multi-hour pilot cannot reset the governor to zero and quietly
  spend the budget twice.
* **Three arms, two of which must not touch `risk_snapshot`.** The masked series
  is production and writes through the ordinary upsert. ``named`` and
  ``masked_nostructural`` share ``(country, as_of)`` with their masked twin, so
  they write to `history_run_ledger` and nowhere else.

A snapshot that fails is recorded as failed and the run continues. One bad week
costs that week.
"""

import datetime
import logging
import random
from typing import Any, Dict, List, Optional

import pandas as pd

from backend.utils import pipeline
from backend.utils.history import config, snapshot_select, store, usage

logger = logging.getLogger(__name__)


def anchors(start: datetime.date, end: datetime.date) -> List[datetime.date]:
    """Every scoring date in ``[start, end]``, on the configured cadence.

    Mondays, via pandas rather than a hand-rolled loop, because ``CADENCE`` is
    already a pandas frequency string and two ways of saying "weekly" is one
    more than this project needs.
    """
    return [d.date() for d in pd.date_range(start, end, freq=config.CADENCE)]


def score_one(iso2: str, as_of: datetime.date, mode: str,
              country_name: Optional[str] = None) -> Dict[str, Any]:
    """One country, one week, one regime. Writes its own ledger row.

    Args:
        iso2: pilot country.
        as_of: the anchor. Nothing published on or after it is read.
        mode: one of :data:`config.SCORING_MODES`.
        country_name: display name; looked up when omitted.

    Returns:
        ``{status, spend_usd, llm_score}``. A failure is a return value rather than
        an exception: the caller is a loop over thousands of these, and one
        country's bad week must not end the run.

    Raises:
        usage.BudgetExhausted: propagated deliberately. Everything else is
            caught, but the budget governor is the one condition where
            continuing is the wrong answer.
    """
    country_name = country_name or config.country_name(iso2)
    items = snapshot_select.select(iso2, as_of)
    if not items:
        # A real answer for a thin week, not an error. Recorded as complete so
        # a resume does not retry it forever, and visible in the report.
        logger.info("[%s %s %s] no articles in window; recorded empty", iso2, as_of, mode)
        store.write_run(as_of, iso2, mode, status="complete", spend_usd=0.0,
                        manifest={"articles": 0})
        return {"status": "complete", "spend_usd": 0.0, "llm_score": None}

    spent_before = store.total_spend_usd()
    with usage.meter(already_spent_usd=spent_before) as meter:
        try:
            llm_output, manifest = pipeline._process_country(
                country_name, iso2, [], as_of=as_of, items=items,
                scoring_mode=mode,
                # The two diagnostic arms would overwrite their own masked twin
                # on `risk_snapshot`'s primary key. They live in the ledger.
                upsert=mode not in config.DIAGNOSTIC_MODES,
                # Weekly anchors and a 30-day window put the same article in
                # about four consecutive snapshots. The daily run's cache is
                # keyed on `as_of`, so all four are misses and the pilot pays
                # four times for one digest; this one is keyed on content.
                #
                # It is also what makes `masked` and `masked_nostructural` share
                # their digests: the two arms differ only in the structural
                # block, which is nowhere near the digest, so they hash the same
                # and the second arm is nearly free.
                digest_content_cache=store,
            )
        except usage.BudgetExhausted:
            store.write_run(as_of, iso2, mode, status="failed",
                            spend_usd=meter.spend_usd,
                            manifest={"error": "budget exhausted"})
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s %s %s] failed", iso2, as_of, mode)
            store.write_run(as_of, iso2, mode, status="failed",
                            spend_usd=meter.spend_usd,
                            manifest={"error": str(exc)[:500]})
            return {"status": "failed", "spend_usd": meter.spend_usd,
                    "llm_score": None}

    store.write_run(
        as_of, iso2, mode, status="complete", spend_usd=meter.spend_usd,
        manifest=manifest,
        # Masked rows are in `risk_snapshot` where the front end reads them; the
        # diagnostic arms have nowhere else to live, so their output is the
        # ledger row.
        result=llm_output if mode in config.DIAGNOSTIC_MODES else None,
    )
    # The governor, and this is the only place it can fire. `_confirm_spend`
    # checks a *projection* before the run starts; a run whose real per-snapshot
    # cost lands at three times that projection would otherwise sail past
    # `PILOT_BUDGET_USD` with nothing to stop it, because `Meter` deliberately
    # never raises from its callback — a handler that throws mid-`.batch()`
    # aborts eight concurrent digests at an arbitrary point.
    #
    # After the ledger row, not before: the snapshot is paid for either way, and
    # a budget stop that also loses the work it just bought is the worst of both.
    meter.check()
    # `llm_score`, not `score`: this is the scorer's number reported back, not
    # a second module assigning one. The name keeps that distinction visible to
    # the tripwire in test_observe_only, which greps for exactly that.
    return {"status": "complete", "spend_usd": meter.spend_usd,
            "llm_score": llm_output.get("score")}


def run(roster: Optional[List[str]] = None,
        start: Optional[datetime.date] = None,
        end: Optional[datetime.date] = None,
        mode: str = "masked",
        dates: Optional[Dict[str, List[datetime.date]]] = None) -> Dict[str, Any]:
    """Score a roster across a date range, resumably and inside the budget.

    Args:
        roster: countries. Defaults to :data:`config.PILOT_ROSTER`.
        start, end: the anchor range. Defaults to ``PILOT_START`` .. today.
        mode: which regime.
        dates: explicit per-country anchors, overriding ``start``/``end``. This
            is how the diagnostic arms run — they score a chosen dozen dates
            rather than a range.

    Returns:
        ``{scored, skipped, failed, spend_usd}``.
    """
    if mode not in config.SCORING_MODES:
        raise ValueError(f"mode must be one of {config.SCORING_MODES}, got {mode!r}")

    roster = roster or list(config.PILOT_ROSTER)
    start = start or datetime.date.fromisoformat(config.PILOT_START)
    end = end or datetime.date.today()

    totals = {"scored": 0, "skipped": 0, "failed": 0, "spend_usd": 0.0}
    for iso2 in roster:
        wanted = (dates or {}).get(iso2) if dates else anchors(start, end)
        if not wanted:
            continue
        # Read once per country rather than once per anchor: this is a set
        # membership test run thousands of times.
        done = store.completed_runs(mode, iso2)
        country_name = config.country_name(iso2)
        logger.info("[%s %s] %d anchor(s), %d already complete",
                    iso2, mode, len(wanted), len(set(wanted) & done))

        for as_of in wanted:
            if as_of in done:
                totals["skipped"] += 1
                continue
            try:
                result = score_one(iso2, as_of, mode, country_name)
            except usage.BudgetExhausted:
                logger.error("budget exhausted at %s %s; stopping the run", iso2, as_of)
                totals["failed"] += 1
                return totals
            totals["spend_usd"] += result["spend_usd"]
            totals["scored" if result["status"] == "complete" else "failed"] += 1

    return totals


# --- the diagnostic sample --------------------------------------------------

def diagnostic_dates(iso2: str, per_country: Optional[int] = None,
                     seed: int = 0) -> List[datetime.date]:
    """The dates to score named, chosen from the finished masked series.

    Stratified two ways, and both matter.

    **Either side of the model's knowledge cutoff**, because "can the model
    identify this country" means something different when it might simply
    remember the week. Half the dates before ``CUTOFF_DATE`` and half after.

    **Extremes and calm**, because they fail differently. The largest |Δscore|
    weeks are where masking either survives or does not — a week whose whole
    story is one named institution. The random calm weeks are the control: if
    masked and named agree on the quiet weeks and diverge on the loud ones, the
    divergence is about events rather than about the method.

    Returns fewer than ``per_country`` when the series is short; that is honest
    rather than padded.
    """
    per_country = per_country or config.NAMED_SAMPLE_PER_COUNTRY
    cutoff = datetime.date.fromisoformat(config.CUTOFF_DATE)
    series = _masked_series(iso2)
    if len(series) < 4:
        logger.warning("[%s] masked series has %d point(s); no diagnostic sample",
                       iso2, len(series))
        return []

    # |Δscore| against the previous week. The first point has no predecessor and
    # is dropped rather than given a delta of zero, which would file it as calm.
    deltas = [(abs(score - prev), day)
              for (_, prev), (day, score) in zip(series, series[1:])
              if score is not None and prev is not None]

    picked: List[datetime.date] = []
    half = per_country // 2
    for era_dates in (
        [(d, day) for d, day in deltas if day < cutoff],
        [(d, day) for d, day in deltas if day >= cutoff],
    ):
        if not era_dates:
            continue
        loud = [day for _, day in sorted(era_dates, reverse=True)[:half // 2]]
        # Calm weeks drawn from what is left, seeded so the sample is
        # reproducible — a diagnostic that picks different dates on every run
        # cannot be compared with its own previous run.
        rest = [day for _, day in era_dates if day not in set(loud)]
        rng = random.Random(f"{iso2}:{seed}:{era_dates[0][1]}")
        calm = rng.sample(rest, min(half - len(loud), len(rest))) if rest else []
        picked += loud + calm

    return sorted(set(picked))


def _masked_series(iso2: str) -> List[tuple]:
    """One country's finished masked scores, oldest first, as (date, score).

    Read from ``risk_snapshot`` rather than the ledger, because that is where
    the masked arm actually writes — the ledger holds its manifest and spend,
    not its number.
    """
    from backend.utils.data_upsert import data_push
    with data_push._transaction() as cur:
        cur.execute(
            "SELECT as_of, score FROM risk_snapshot "
            "WHERE country_iso2 = %s AND scoring_mode = 'masked' ORDER BY as_of",
            (iso2,))
        return [(row[0], float(row[1]) if row[1] is not None else None)
                for row in cur.fetchall()]


def diagnostic_plan(roster: Optional[List[str]] = None) -> Dict[str, List[datetime.date]]:
    """The full diagnostic sample, per country."""
    return {iso2: diagnostic_dates(iso2) for iso2 in (roster or config.PILOT_ROSTER)}


# --- cost projection --------------------------------------------------------

def projection(n_snapshots: int) -> float:
    """What ``n_snapshots`` should cost, from what the run has already spent.

    Deliberately derived from the ledger rather than from a constant: a
    projection that does not move when the observed per-snapshot cost moves is
    a number nobody should be asked to approve a budget against. Falls back to
    a documented estimate before there is anything to measure.
    """
    rows = [r for r in store.read_runs() if r["status"] == "complete"
            and (r["spend_usd"] or 0) > 0]
    if not rows:
        # From the dry run's own measurement when it exists; until then, the
        # figure the pilot was budgeted on.
        return n_snapshots * 0.036
    per = sum(r["spend_usd"] for r in rows) / len(rows)
    return n_snapshots * per
