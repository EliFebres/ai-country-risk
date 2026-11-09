"""
The forecast ledger: what the outlook said, what happened, and what that says
about the instrument.

Runs at the end of every `etl` and standalone as `main.py forecast-report`.
Like the quality report it reads the record, never the run, so it works on a
week that finished hours ago and cannot be fooled by a variable the run was
holding.

**An evaluation is a measurement of the instrument, not a fact about the
country**, so it goes in `snapshot_diagnostic`:

- `kind='outlook-eval'`, `variant='4w'|'12w'`: a production forecast graded
  against the first snapshot on or after its horizon, at most seven days late.
- `variant='4w-late'|'12w-late'`: the same grade when the realization arrived
  eight to forty-five days late. Written so the plumbing can be exercised on a
  record with a skipped week in it, printed in its own table with the lag on
  every row, and excluded from the calibration memo, the API's track record
  and the training export. A 43-day move is not a 4-week outcome, and nothing
  downstream may learn as if it were.
- `variant='<arm>|4w'` and so on: the diagnostic prompt variants, graded the
  same way, printed side by side with production, promoted by nobody.
- `kind='watch-eval'`: the watch list's own week, per country.
- `kind='repeat'`: the scoring instrument's repeat runs, the noise floor every
  direction call is judged against.

**The calibration memo** (`write_lessons`) is computed, not opined: plain
sentences with numbers from the systematic-errors block, under 1,200
characters, stored as an `llm_artifact` of kind `lessons` keyed by its own
hash. The outlook reads the newest one. The memo changes only when a new
evaluation has matured, so most weeks the hash, and the row's
`lessons_version`, stays put.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import pathlib
import statistics
from typing import Any, Dict, List, Optional, Sequence, Tuple

import backend.llm.constants as ai_constants
from backend.data_upsert import store
from backend.llm.outlook_validate import band_of
from backend.util import constants, db, paths, provenance
from backend.util.hashing import content_hash

__all__ = [
    "EVAL_KIND", "ARM_KIND", "WATCH_EVAL_KIND", "REPEAT_KIND", "PRODUCTION_VARIANTS",
    "load_step5e_repeats", "noise_floor", "matured", "evaluate_one", "run",
    "watch_eval", "systematic_errors", "report", "write_lessons", "export",
    "run_report", "run_export",
]

S = ai_constants.OUTLOOK_SCORING
EVAL_KIND = "outlook-eval"
ARM_KIND = "outlook-arm"
WATCH_EVAL_KIND = "watch-eval"
REPEAT_KIND = "repeat"
PRODUCTION_VARIANTS = tuple(S["horizons_days"])            # ("4w", "12w")
LATE_SUFFIX = "-late"
LEDGERS = ("friction", "order", "information", "edge")
#: The scorer whose repeat runs make the noise floor.
REPEAT_SOURCE_MODEL = "gpt-4o-2024-08-06"
MERGES_DIR = paths.BACKEND_DIR / "measurements" / "step5e" / "merges"
EXPORT_DIR = paths.DATA_DIR / "exports"


def _d(value: Any) -> Optional[dt.date]:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _mean(values: Sequence[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def _rate(flags: Sequence[Any]) -> Optional[float]:
    vals = [bool(f) for f in flags if f is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


# --- the noise floor --------------------------------------------------------


def load_step5e_repeats(merges_dir: pathlib.Path = MERGES_DIR, arm: str = "new_masked") -> int:
    """Write the step 5e masked merges as `repeat` rows, one per country.

    The measurement session left them in files; the ledger needs them as rows
    so the floor is a query. Idempotent. Returns how many repeat rows exist
    afterwards.
    """
    for path in sorted(merges_dir.glob(f"{arm}_*.jsonl")):
        iso2 = path.stem.split("_")[-1].upper()
        lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        scorings = [l.get("scoring") or {} for l in lines if l.get("scoring")]
        if not scorings:
            continue
        s12 = [s["score_12m"] for s in scorings]
        s3 = [s["score_3m"] for s in scorings]
        ledgers = {led: [(s.get("ledger_ints") or {}).get(led) for s in scorings] for led in LEDGERS}
        payload_path = merges_dir.parent / f"payload_{iso2}.json"
        as_of = None
        if payload_path.exists():
            try:
                frozen = json.loads(payload_path.read_text(encoding="utf-8"))
                as_of = _d((frozen.get("payload") or frozen).get("as_of")) or _d(frozen.get("as_of"))
            except (OSError, ValueError):
                as_of = None
        run_date = as_of or dt.date(2026, 11, 8)
        store.write_diagnostic(
            country_iso2=iso2, run_date=run_date, kind=REPEAT_KIND, model=REPEAT_SOURCE_MODEL,
            variant=f"step5e-{arm}",
            payload={
                "source": str(path.relative_to(paths.PROJECT_ROOT)).replace("\\", "/"),
                "n": len(scorings),
                "score_12m": s12, "score_3m": s3, "ledgers": ledgers,
                "spread_12m": max(s12) - min(s12),
                "sd_12m": round(statistics.pstdev(s12), 3) if len(s12) > 1 else 0.0,
                "prompt_version": lines[0].get("prompt_version"),
            },
        )
    return len(store.read_diagnostics(kind=REPEAT_KIND))


def noise_floor() -> Dict[str, Any]:
    """The scoring instrument's repeat spread, from `repeat` rows.

    Floor = the median over repeat rows of (max - min) of `score_12m` across
    that row's runs; per ledger likewise. Reported as measured: the masked
    merges of step 5e were near-deterministic, so the floor is small, and a
    small floor is a fact about the scorer, not a parameter to tune.
    """
    rows = store.read_diagnostics(kind=REPEAT_KIND)
    if not rows:
        return {"floor_12m": None, "floor_by_ledger": {}, "n_rows": 0,
                "source": "no repeat rows in snapshot_diagnostic", "phrase": "not yet measured"}
    spreads = []
    by_ledger: Dict[str, List[float]] = {led: [] for led in LEDGERS}
    for r in rows:
        p = r["payload"] or {}
        s12 = p.get("score_12m") or []
        if len(s12) >= 2:
            spreads.append(max(s12) - min(s12))
        for led in LEDGERS:
            vals = [v for v in (p.get("ledgers") or {}).get(led, []) if v is not None]
            if len(vals) >= 2:
                by_ledger[led].append(max(vals) - min(vals))
    floor = round(statistics.median(spreads), 2) if spreads else None
    floors = {led: (round(statistics.median(v), 2) if v else None) for led, v in by_ledger.items()}
    worst = max(spreads) if spreads else None
    return {
        "floor_12m": floor,
        "floor_by_ledger": floors,
        "worst_spread_12m": worst,
        "n_rows": len(rows),
        "runs_per_row": sorted({(r["payload"] or {}).get("n") for r in rows}),
        "source": f"{len(rows)} repeat rows ({', '.join(sorted({r['variant'] for r in rows}))})",
        "phrase": (f"a median absolute spread of {floor:g} points on score_12m across {len(rows)} "
                   f"countries run {max((r['payload'] or {}).get('n', 0) for r in rows)} times each "
                   f"(worst spread {worst:g})" if floor is not None else "not yet measured"),
    }


# --- maturity and grading ---------------------------------------------------


def matured(iso2: str, run_date: dt.date, horizon: str) -> Optional[Dict[str, Any]]:
    """The snapshot a forecast is graded against, or None when none qualifies.

    Returns ``{"variant", "snapshot", "days_late", "target"}``: the bare
    horizon when the realization is at most `maturity_tolerance_days` late, the
    `-late` variant up to `late_tolerance_days`, None beyond that or when no
    snapshot exists yet.
    """
    target = run_date + dt.timedelta(days=S["horizons_days"][horizon])
    snap = store.first_snapshot_on_or_after(iso2, target)
    if not snap:
        return None
    late = (snap["run_date"] - target).days
    if late <= S["maturity_tolerance_days"]:
        return {"variant": horizon, "snapshot": snap, "days_late": late, "target": target}
    if late <= S["late_tolerance_days"]:
        return {"variant": horizon + LATE_SUFFIX, "snapshot": snap, "days_late": late, "target": target}
    return None


def _direction(change: Optional[float], floor: Optional[float]) -> Optional[str]:
    if change is None:
        return None
    if abs(change) <= (floor or 0):
        return "flat"
    return "up" if change > 0 else "down"


def evaluate_one(
    outlook: Dict[str, Any], horizon: str, realized: Dict[str, Any], origin: Dict[str, Any], *,
    noise: Dict[str, Any], days_late: int, arm: Optional[str] = None,
) -> Dict[str, Any]:
    """Grade one horizon of one forecast against the realized snapshot.

    All formulas, so the doc can quote them:
      change        = realized.score_12m - origin.score_12m
      realized dir  = flat if |change| <= floor else up/down
      range_hit     = low <= realized <= high
      direction_hit = forecast direction == realized direction
      within_noise  = direction_hit or |change| <= floor
      abs_error     = |realized - (low + high) / 2|
      brier         = sum over bands of (p_band - 1[realized band])^2
      log_loss      = -ln(max(p[realized band], 1e-3))
    """
    h = outlook[f"horizon_{horizon}"]
    floor = noise.get("floor_12m") or 0
    floors = noise.get("floor_by_ledger") or {}
    score = realized["score_12m"]
    change = score - origin["score_12m"]
    realized_dir = _direction(change, floor)
    lo, hi = h["score_low"], h["score_high"]
    mid = (lo + hi) / 2
    band = band_of(score)
    p = h.get("p_band") or {}
    brier = sum((float(p.get(b, 0.0)) - (1.0 if b == band else 0.0)) ** 2 for b in S["bands"])
    log_loss = -math.log(max(float(p.get(band, 0.0)), 1e-3))
    ledger_dirs, ledger_hits = {}, {}
    for led in LEDGERS:
        r, o = realized.get(f"{led}_score"), origin.get(f"{led}_score")
        d = _direction(None if r is None or o is None else r - o, floors.get(led) if floors.get(led) is not None else floor)
        ledger_dirs[led] = d
        ledger_hits[led] = None if d is None else ((outlook.get("ledger_direction") or {}).get(led) == d)
    return {
        "horizon": horizon,
        "arm": arm,
        "origin_run_date": _d(outlook["run_date"]).isoformat(),
        "origin_score_12m": origin["score_12m"],
        "target_date": (_d(outlook["run_date"]) + dt.timedelta(days=S["horizons_days"][horizon])).isoformat(),
        "realized_run_date": realized["run_date"].isoformat(),
        "days_late": days_late,
        "forecast": {"direction": h["direction"], "score_low": lo, "score_high": hi, "p_band": p},
        "realized": {"score_12m": score, "change": change, "band": band, "ledger_directions": ledger_dirs,
                     "ledgers": {led: realized.get(f"{led}_score") for led in LEDGERS}},
        "range_hit": lo <= score <= hi,
        "range_width": hi - lo,
        "direction_hit": h["direction"] == realized_dir,
        "direction_hit_within_noise": h["direction"] == realized_dir or abs(change) <= floor,
        "abs_error_to_midpoint": round(abs(score - mid), 2),
        "signed_error": round(score - mid, 2),
        "brier": round(brier, 4),
        "log_loss": round(log_loss, 4),
        "confidence": outlook.get("confidence"),
        "ledger_direction_hits": ledger_hits,
        "driver_ledgers": sorted({d.get("ledger") for d in (outlook.get("drivers") or []) if d.get("ledger")}),
        "noise_floor": floor,
        "outlook_prompt_version": outlook.get("outlook_prompt_version"),
        "outlook_model": outlook.get("outlook_model"),
        "lessons_version": outlook.get("lessons_version"),
    }


def _origin(iso2: str, run_date: dt.date) -> Optional[Dict[str, Any]]:
    rows = store.read_snapshot_series(iso2, since=run_date, until=run_date)
    return rows[0] if rows else None


def _evaluate_rows(rows: Sequence[Dict[str, Any]], *, noise: Dict[str, Any], arm: Optional[str]) -> Dict[str, int]:
    counts = {"evaluated": 0, "late": 0, "pending": 0}
    for o in rows:
        iso2, run_date = o["country_iso2"], _d(o["run_date"])
        origin = _origin(iso2, run_date)
        if not origin:
            continue
        for horizon in PRODUCTION_VARIANTS:
            m = matured(iso2, run_date, horizon)
            if not m:
                counts["pending"] += 1
                continue
            payload = evaluate_one(o, horizon, m["snapshot"], origin, noise=noise,
                                   days_late=m["days_late"], arm=arm)
            variant = m["variant"] if arm is None else f"{arm}|{m['variant']}"
            store.write_diagnostic(country_iso2=iso2, run_date=run_date, kind=EVAL_KIND,
                                   model=o.get("outlook_model") or "?", variant=variant, payload=payload)
            counts["evaluated"] += 1
            if m["variant"].endswith(LATE_SUFFIX):
                counts["late"] += 1
    return counts


def run(run_date: Optional[dt.date] = None) -> Dict[str, Any]:
    """Grade every forecast whose horizon has matured, production and arms,
    then write each country's watch-eval for `run_date`. Idempotent: the first
    matured snapshot is deterministic, so re-grading rewrites the same row."""
    noise = noise_floor()
    production = _evaluate_rows(store.read_all_outlooks(), noise=noise, arm=None)
    arms: Dict[str, Dict[str, int]] = {}
    for d in store.read_diagnostics(kind=ARM_KIND):
        row = dict(d["payload"] or {})
        row["country_iso2"], row["run_date"] = d["country_iso2"], d["run_date"]
        c = _evaluate_rows([row], noise=noise, arm=d["variant"])
        slot = arms.setdefault(d["variant"], {"evaluated": 0, "late": 0, "pending": 0})
        for k, v in c.items():
            slot[k] += v
    watch_rows = 0
    if run_date:
        for entry in constants.COUNTRY_ROSTER:
            if store.read_watch_items(entry["iso2"]):
                watch_eval(entry["iso2"], run_date)
                watch_rows += 1
    out = {"production": production, "arms": arms, "watch_evals": watch_rows, "noise_floor": noise.get("floor_12m")}
    print(f"[forecast-eval] production: {production['evaluated']} graded ({production['late']} late), "
          f"{production['pending']} pending; arms: "
          + (", ".join(f"{k} {v['evaluated']} graded" for k, v in arms.items()) or "none")
          + f"; watch-eval rows {watch_rows}")
    return out


# --- the watch list's grade -------------------------------------------------


def watch_eval(iso2: str, run_date: dt.date) -> Dict[str, Any]:
    """One country's watch-list week: what resolved, the trailing precision,
    the lead time, and whether what happened had been shown."""
    items = store.read_watch_items(iso2)
    this_week = [i for i in items if i.get("resolved_run_date") == run_date]
    since = run_date - dt.timedelta(days=84)
    trailing = [i for i in items if i.get("resolved_run_date") and since < i["resolved_run_date"] <= run_date]
    terminal = ("happened", "did_not_happen", "expired", "superseded")

    def by_status(rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        return {s: sum(1 for i in rows if i["status"] == s) for s in terminal}

    t = by_status(trailing)
    denom = t["happened"] + t["did_not_happen"] + t["expired"]
    happened = [i for i in trailing if i["status"] == "happened"]
    leads = [(i["resolved_run_date"] - i["first_seen_run_date"]).days for i in happened]
    payload = {
        "run_date": run_date.isoformat(),
        "resolved_this_week": by_status(this_week),
        "trailing_12w": t,
        "precision_12w": round(t["happened"] / denom, 3) if denom else None,
        "median_lead_days": statistics.median(leads) if leads else None,
        "happened_shown_share": _rate([i.get("ever_shown") for i in happened]) if happened else None,
        "open": sum(1 for i in items if i["status"] == "open"),
        "by_kind_open": {k: sum(1 for i in items if i["status"] == "open" and i["kind"] == k)
                         for k in ai_constants.WATCH_KINDS},
        "total": len(items),
    }
    store.write_diagnostic(country_iso2=iso2, run_date=run_date, kind=WATCH_EVAL_KIND, model="code", payload=payload)
    return payload


# --- aggregates -------------------------------------------------------------

_CONF_BUCKETS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 1.01))


def _aggregate(evals: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not evals:
        return {"n": 0}
    out = {
        "n": len(evals),
        "range_hit_rate": _rate([e["range_hit"] for e in evals]),
        "direction_hit_rate": _rate([e["direction_hit"] for e in evals]),
        "direction_hit_within_noise_rate": _rate([e["direction_hit_within_noise"] for e in evals]),
        "mean_brier": _mean([e["brier"] for e in evals]),
        "mean_log_loss": _mean([e["log_loss"] for e in evals]),
        "mae": _mean([e["abs_error_to_midpoint"] for e in evals]),
        "mean_signed_error": _mean([e["signed_error"] for e in evals]),
        "mean_width": _mean([e["range_width"] for e in evals]),
        "mean_confidence": _mean([e.get("confidence") for e in evals]),
        "ledger_direction_hit_rate": {
            led: _rate([(e.get("ledger_direction_hits") or {}).get(led) for e in evals]) for led in LEDGERS
        },
        "calibration_by_confidence": [],
    }
    for lo, hi in _CONF_BUCKETS:
        bucket = [e for e in evals if e.get("confidence") is not None and lo <= e["confidence"] < hi]
        if bucket:
            out["calibration_by_confidence"].append({
                "bucket": f"{lo:.2f}-{min(hi, 1.0):.2f}", "n": len(bucket),
                "mean_confidence": _mean([e["confidence"] for e in bucket]),
                "range_hit_rate": _rate([e["range_hit"] for e in bucket]),
                "direction_hit_rate": _rate([e["direction_hit"] for e in bucket]),
            })
    return out


def _cut(evals: Sequence[Dict[str, Any]], key) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for e in evals:
        groups.setdefault(str(key(e)), []).append(e)
    return {k: _aggregate(v) for k, v in sorted(groups.items())}


def systematic_errors(evals: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Computed, not opined: where the instrument leans."""
    if not evals:
        return {"n": 0, "note": "insufficient: no production-matured evaluations"}
    by_driver: Dict[str, List[float]] = {}
    for e in evals:
        for led in e.get("driver_ledgers") or ["(none)"]:
            by_driver.setdefault(led, []).append(e["signed_error"])
    ups = [e for e in evals if e["forecast"]["direction"] == "up"]
    downs = [e for e in evals if e["forecast"]["direction"] == "down"]
    flats = [e for e in evals if e["forecast"]["direction"] == "flat"]
    widths = ((0, 5), (5, 10), (10, 20), (20, 101))
    width_vs_hit = []
    for lo, hi in widths:
        bucket = [e for e in evals if lo <= e["range_width"] < hi]
        if bucket:
            width_vs_hit.append({"width": f"{lo}-{hi - 1 if hi <= 100 else 100}", "n": len(bucket),
                                 "range_hit_rate": _rate([e["range_hit"] for e in bucket])})
    overall_mae = _mean([e["abs_error_to_midpoint"] for e in evals]) or 0
    by_country = {}
    for iso2, group in _cut(evals, lambda e: e["country_iso2"]).items():
        by_country[iso2] = {"n": group["n"], "mean_signed_error": group["mean_signed_error"],
                            "range_hit_rate": group["range_hit_rate"],
                            "clustered": abs(group["mean_signed_error"] or 0) > overall_mae}
    return {
        "n": len(evals),
        "mean_signed_error_by_driver_ledger": {k: {"n": len(v), "mean_signed_error": _mean(v)}
                                                for k, v in sorted(by_driver.items())},
        "direction_bias": {
            "up_calls": len(ups),
            "up_realized_not_up": _rate([e["realized"]["change"] <= e["noise_floor"] for e in ups]) if ups else None,
            "down_calls": len(downs),
            "down_realized_not_down": _rate([e["realized"]["change"] >= -e["noise_floor"] for e in downs]) if downs else None,
            "flat_calls": len(flats),
            "flat_realized_moved": _rate([abs(e["realized"]["change"]) > e["noise_floor"] for e in flats]) if flats else None,
        },
        "range_width_vs_hit_rate": width_vs_hit,
        "mean_width": _mean([e["range_width"] for e in evals]),
        "overall_range_hit_rate": _rate([e["range_hit"] for e in evals]),
        "by_country": by_country,
        "countries_clustered": sorted(k for k, v in by_country.items() if v["clustered"]),
    }


# --- the report -------------------------------------------------------------


def _eval_rows(variant_filter) -> List[Dict[str, Any]]:
    rows = []
    for d in store.read_diagnostics(kind=EVAL_KIND):
        if variant_filter(d["variant"]):
            e = dict(d["payload"] or {})
            e["country_iso2"], e["variant"], e["run_date"] = d["country_iso2"], d["variant"], d["run_date"]
            rows.append(e)
    return rows


def _first_maturation(outlooks: Sequence[Dict[str, Any]], graded: set, horizon: str, today: dt.date) -> Optional[Dict[str, Any]]:
    """The earliest date a not-yet-graded production forecast can mature."""
    pending = []
    for o in outlooks:
        key = (o["country_iso2"], _d(o["run_date"]), horizon)
        if key in graded:
            continue
        target = _d(o["run_date"]) + dt.timedelta(days=S["horizons_days"][horizon])
        pending.append((target, o["country_iso2"]))
    if not pending:
        return None
    future = [(t, c) for t, c in pending if t >= today]
    passed = sum(1 for t, _ in pending if t < today)
    if not future:
        return {"date": None, "countries": [], "passed": passed}
    target = min(t for t, _ in future)
    return {"date": target.isoformat(), "countries": sorted({c for t, c in future if t == target}), "passed": passed}


def _fmt_rate(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:.0%}"


def _fmt_num(v: Optional[float], nd: int = 2) -> str:
    return "-" if v is None else f"{v:.{nd}f}"


def _table(title: str, cuts: Dict[str, Dict[str, Any]]) -> List[str]:
    lines = [f"  {title}", f"  {'cut':<22} {'n':>3} {'range':>6} {'dir':>5} {'dir~':>5} {'brier':>6} {'mae':>6} {'width':>6} {'conf':>5}"]
    for name, a in cuts.items():
        if a["n"] == 0:
            lines.append(f"  {name:<22} {0:>3}")
            continue
        lines.append(f"  {name[:22]:<22} {a['n']:>3} {_fmt_rate(a['range_hit_rate']):>6} "
                     f"{_fmt_rate(a['direction_hit_rate']):>5} {_fmt_rate(a['direction_hit_within_noise_rate']):>5} "
                     f"{_fmt_num(a['mean_brier'], 3):>6} {_fmt_num(a['mae'], 1):>6} {_fmt_num(a['mean_width'], 1):>6} "
                     f"{_fmt_num(a['mean_confidence']):>5}")
    return lines


def report(run_date: Optional[dt.date] = None, *, record: bool = True) -> Dict[str, Any]:
    """Print the ledger and return it. Order: coverage, 4w, 12w, arms, the
    late plumbing table, the watch list, systematic errors."""
    today = run_date or dt.date.today()
    outlooks = store.read_all_outlooks()
    production = _eval_rows(lambda v: v in PRODUCTION_VARIANTS)
    late = _eval_rows(lambda v: v.endswith(LATE_SUFFIX) and "|" not in v)
    arms = _eval_rows(lambda v: "|" in v)
    noise = noise_floor()
    graded = {(e["country_iso2"], _d(e["run_date"]), e["horizon"]) for e in production}

    coverage = {
        "outlooks": len(outlooks),
        "countries": len({o["country_iso2"] for o in outlooks}),
        "run_dates": sorted({_d(o["run_date"]).isoformat() for o in outlooks}),
        "matured": {h: sum(1 for e in production if e["horizon"] == h) for h in PRODUCTION_VARIANTS},
        "late": {h: sum(1 for e in late if e["horizon"] == h) for h in PRODUCTION_VARIANTS},
        "first_maturation": {h: _first_maturation(outlooks, graded, h, today) for h in PRODUCTION_VARIANTS},
        "noise_floor": noise.get("floor_12m"),
        "noise_source": noise.get("source"),
    }
    tables = {}
    for h in PRODUCTION_VARIANTS:
        evals = [e for e in production if e["horizon"] == h]
        tables[h] = {
            "overall": _aggregate(evals),
            "by_country": _cut(evals, lambda e: e["country_iso2"]),
            "by_prompt_version": _cut(evals, lambda e: (e.get("outlook_prompt_version") or "?")[:12]),
            "by_lessons_version": _cut(evals, lambda e: (e.get("lessons_version") or "none")[:12]),
        }
    arm_table = {}
    for h in PRODUCTION_VARIANTS:
        arm_table[h] = {"production": _aggregate([e for e in production if e["horizon"] == h])}
        for e in arms:
            name = e["variant"].split("|")[0]
            if e["variant"].endswith(h):
                arm_table[h].setdefault(name, [])
        for name in [k for k in arm_table[h] if k != "production"]:
            arm_table[h][name] = _aggregate([e for e in arms if e["variant"] == f"{name}|{h}"])
    arm_late = [e for e in arms if e["variant"].endswith(LATE_SUFFIX)]
    watch_rows = []
    for entry in constants.COUNTRY_ROSTER:
        rows = store.read_diagnostics(kind=WATCH_EVAL_KIND, country_iso2=entry["iso2"])
        if rows:
            watch_rows.append({"iso2": entry["iso2"], **(rows[-1]["payload"] or {})})
    errors = systematic_errors(production)

    lines = ["", f"=== Forecast ledger - {today} ===", ""]
    lines.append(f"coverage: {coverage['outlooks']} outlooks over {coverage['countries']} countries and "
                 f"{len(coverage['run_dates'])} run dates ({', '.join(coverage['run_dates'])})")
    for h in PRODUCTION_VARIANTS:
        fm = coverage["first_maturation"][h]
        nxt = ("none pending" if not fm else
               (f"first possible {fm['date']} ({', '.join(fm['countries'])})" if fm["date"] else "no future target")
               + (f"; {fm['passed']} target(s) already passed without a qualifying snapshot" if fm["passed"] else ""))
        lines.append(f"  {h}: {coverage['matured'][h]} production-matured, {coverage['late'][h]} late (plumbing); {nxt}")
    lines.append(f"  noise floor: {noise.get('phrase')}")
    for h in PRODUCTION_VARIANTS:
        label = "short term (4 weeks)" if h == "4w" else "long term (12 weeks)"
        lines.append("")
        if tables[h]["overall"]["n"] == 0:
            lines.append(f"  {label}: no production-matured forecasts yet")
            continue
        lines += _table(f"{label}: overall", {"all": tables[h]["overall"]})
        lines += _table("  by country", tables[h]["by_country"])
        lines += _table("  by outlook_prompt_version", tables[h]["by_prompt_version"])
        lines += _table("  by lessons_version", tables[h]["by_lessons_version"])
        for b in tables[h]["overall"]["calibration_by_confidence"]:
            lines.append(f"    confidence {b['bucket']}: n {b['n']}, mean conf {b['mean_confidence']}, "
                         f"range hit {_fmt_rate(b['range_hit_rate'])}, direction hit {_fmt_rate(b['direction_hit_rate'])}")
    lines.append("")
    if any(len(v) > 1 for v in arm_table.values()):
        for h in PRODUCTION_VARIANTS:
            lines += _table(f"arms side by side, {h} (production-matured only)", arm_table[h])
    else:
        lines.append("  arms: no matured arm evaluations yet")
    lines.append("")
    lines.append(f"  PLUMBING CHECK: realizations 8 to {S['late_tolerance_days']} days late. Excluded from the "
                 f"memo, the API track record and the export; they prove the eval path, not the instrument.")
    if not late and not arm_late:
        lines.append("    none")
    for e in sorted(late, key=lambda e: (e["horizon"], e["country_iso2"])):
        f = e["forecast"]
        lines.append(f"    {e['country_iso2']} {e['origin_run_date']} {e['horizon']}: forecast {f['direction']} "
                     f"{f['score_low']}-{f['score_high']} | realized {e['realized']['score_12m']} "
                     f"({e['realized']['change']:+d}) on {e['realized_run_date']}, {e['days_late']} days late | "
                     f"range {'hit' if e['range_hit'] else 'miss'}, direction {'hit' if e['direction_hit'] else 'miss'}, "
                     f"brier {e['brier']:.3f}")
    for e in sorted(arm_late, key=lambda e: (e["variant"], e["country_iso2"])):
        f = e["forecast"]
        lines.append(f"    arm {e['variant']} {e['country_iso2']} {e['origin_run_date']}: forecast {f['direction']} "
                     f"{f['score_low']}-{f['score_high']} | realized {e['realized']['score_12m']} "
                     f"({e['realized']['change']:+d}), {e['days_late']} days late | "
                     f"range {'hit' if e['range_hit'] else 'miss'}, brier {e['brier']:.3f}")
    lines.append("")
    lines.append(f"  watch list (latest week per country)")
    lines.append(f"  {'':<4} {'open':>4} {'wk +':>4} {'wk -':>4} {'wk exp':>6} {'wk sup':>6} {'prec12w':>7} {'lead':>5} {'shown':>5}")
    for w in watch_rows:
        r = w.get("resolved_this_week") or {}
        lines.append(f"  {w['iso2']:<4} {w.get('open', 0):>4} {r.get('happened', 0):>4} {r.get('did_not_happen', 0):>4} "
                     f"{r.get('expired', 0):>6} {r.get('superseded', 0):>6} {_fmt_rate(w.get('precision_12w')):>7} "
                     f"{_fmt_num(w.get('median_lead_days'), 0):>5} {_fmt_rate(w.get('happened_shown_share')):>5}")
    if not watch_rows:
        lines.append("    no watch-eval rows yet")
    lines.append("")
    lines.append("  systematic errors (production-matured only)")
    if errors.get("n", 0) == 0:
        lines.append(f"    {errors.get('note')}")
    else:
        for led, v in errors["mean_signed_error_by_driver_ledger"].items():
            lines.append(f"    driver {led}: n {v['n']}, mean signed error {v['mean_signed_error']:+.2f}")
        db_ = errors["direction_bias"]
        lines.append(f"    direction bias: up calls {db_['up_calls']} realized not-up {_fmt_rate(db_['up_realized_not_up'])}; "
                     f"down calls {db_['down_calls']} realized not-down {_fmt_rate(db_['down_realized_not_down'])}; "
                     f"flat calls {db_['flat_calls']} moved {_fmt_rate(db_['flat_realized_moved'])}")
        for b in errors["range_width_vs_hit_rate"]:
            lines.append(f"    width {b['width']}: n {b['n']}, range hit {_fmt_rate(b['range_hit_rate'])}")
        lines.append(f"    clustered by country: {', '.join(errors['countries_clustered']) or 'none'}")
    lines.append("")
    for line in lines:
        print(line)

    out = {"run_date": today.isoformat(), "coverage": coverage, "tables": tables, "arms": arm_table,
           "late": late, "watch": watch_rows, "systematic_errors": errors,
           "outlook_prompt_version": ai_constants.OUTLOOK_PROMPT_VERSION}
    if record:
        try:
            store.write_ledger(job_type="forecast-report", run_date=today, status="ok",
                               git_sha=provenance.git_sha(), detail=json.loads(json.dumps(out, default=str)))
        except Exception as exc:
            print(f"[forecast-report] could not record the report: {exc}")
    return out


# --- the calibration memo ---------------------------------------------------


def _memo_text(errors: Dict[str, Any], aggregates: Dict[str, Dict[str, Any]]) -> str:
    parts: List[str] = []
    n = errors["n"]
    parts.append(f"Over {n} matured production outlooks:")
    for h, a in aggregates.items():
        if a.get("n"):
            parts.append(f"{h}: ranges hit {a['range_hit_rate']:.0%} of {a['n']} at a mean width of "
                         f"{a['mean_width']:.1f} points; direction calls hit {a['direction_hit_rate']:.0%}; "
                         f"mean error to midpoint {a['mae']:.1f}; mean Brier {a['mean_brier']:.3f}.")
    db_ = errors["direction_bias"]
    if db_["up_calls"]:
        parts.append(f"Of {db_['up_calls']} up calls, {db_['up_realized_not_up']:.0%} realized flat or down.")
    if db_["down_calls"]:
        parts.append(f"Of {db_['down_calls']} down calls, {db_['down_realized_not_down']:.0%} realized flat or up.")
    if db_["flat_calls"]:
        parts.append(f"Of {db_['flat_calls']} flat calls, {db_['flat_realized_moved']:.0%} moved beyond the noise floor.")
    for led, v in errors["mean_signed_error_by_driver_ledger"].items():
        if v["n"] >= 2 and v["mean_signed_error"] is not None and abs(v["mean_signed_error"]) >= 2:
            lean = "under" if v["mean_signed_error"] > 0 else "over"
            parts.append(f"With {led} as a driver ({v['n']}), midpoints {lean}-predicted the realized score by "
                         f"{abs(v['mean_signed_error']):.1f} points on average.")
    for b in errors["range_width_vs_hit_rate"]:
        parts.append(f"Ranges of width {b['width']} hit {b['range_hit_rate']:.0%} of {b['n']}.")
    if errors["countries_clustered"]:
        parts.append(f"Errors cluster in {', '.join(errors['countries_clustered'])}.")
    text = " ".join(parts)
    limit = S["max_chars"]["memo"]
    if len(text) > limit:
        cut = text[:limit].rfind(". ")
        text = text[: cut + 1] if cut > limit // 2 else text[:limit]
    return text


def write_lessons(run_date: Optional[dt.date] = None) -> Optional[Dict[str, Any]]:
    """Turn the systematic-errors block into the calibration memo and store it.

    Production-matured rows only: the late plumbing rows never teach the
    instrument anything. With nothing matured there is no memo, and the
    outlook keeps reading "no evaluated history yet".
    """
    production = _eval_rows(lambda v: v in PRODUCTION_VARIANTS)
    if not production:
        print("[lessons] no production-matured evaluations; the memo is not written")
        return None
    errors = systematic_errors(production)
    aggregates = {h: _aggregate([e for e in production if e["horizon"] == h]) for h in PRODUCTION_VARIANTS}
    text = _memo_text(errors, aggregates)
    digest = content_hash(text)
    latest = store.read_latest_artifact(kind="lessons")
    if latest and latest["content_sha256"] == digest:
        print(f"[lessons] memo unchanged ({digest[:12]})")
        return {"hash": digest, "text": text, "changed": False}
    store.write_artifacts(
        [(digest, {"text": text, "n_evaluations": len(production),
                   "run_date": (run_date or dt.date.today()).isoformat(),
                   "outlook_prompt_version": ai_constants.OUTLOOK_PROMPT_VERSION})],
        kind="lessons", version=ai_constants.OUTLOOK_PROMPT_VERSION, model="code",
    )
    print(f"[lessons] memo written ({digest[:12]}, {len(text)} chars, {len(production)} evaluations)")
    return {"hash": digest, "text": text, "changed": True}


# --- the training export ----------------------------------------------------


def export(run_date: Optional[dt.date] = None, out_dir: pathlib.Path = EXPORT_DIR) -> pathlib.Path:
    """One JSONL line per production-matured outlook and horizon: the masked
    payload that was sent, the structured forecast, the realized outcome and
    the evaluation. Deletes nothing. Late rows are not in it."""
    today = run_date or dt.date.today()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"outlook-{today.isoformat()}.jsonl"
    production = _eval_rows(lambda v: v in PRODUCTION_VARIANTS)
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        for e in production:
            o = store.read_outlook(e["country_iso2"], _d(e["run_date"]))
            if not o:
                continue
            manifest = o.get("manifest") or {}
            line = {
                "iso2": e["country_iso2"], "run_date": _d(o["run_date"]).isoformat(), "horizon": e["horizon"],
                "payload": manifest.get("payload"),
                "forecast": {k: o.get(k) for k in ("horizon_4w", "horizon_12w", "ledger_direction", "drivers",
                                                   "base_case", "upside_risk", "downside_risk", "headed_text",
                                                   "confidence", "key_uncertainties", "changes_since_last")},
                "realized": e["realized"],
                "evaluation": {k: e[k] for k in ("range_hit", "range_width", "direction_hit",
                                                  "direction_hit_within_noise", "abs_error_to_midpoint",
                                                  "brier", "log_loss", "ledger_direction_hits", "days_late")},
                "stamps": {k: o.get(k) for k in ("outlook_prompt_version", "outlook_model", "lessons_version",
                                                 "git_sha", "payload_fingerprint")},
            }
            fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
            n += 1
    print(f"[forecast-export] wrote {n} line(s) to {path}")
    return path


# --- entry points -----------------------------------------------------------


def run_report() -> Dict[str, Any]:
    """`main.py forecast-report`: grade what has matured, then print the ledger."""
    db.announce()
    run()
    return report()


def run_export() -> pathlib.Path:
    """`main.py forecast-export`: the training set as it stands today."""
    db.announce()
    return export()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--export", action="store_true")
    args = ap.parse_args()
    run_export() if args.export else run_report()


if __name__ == "__main__":
    main()
