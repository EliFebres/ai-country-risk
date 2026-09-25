"""
The weekly quality report: state, then what changed.

Runs at the end of every `etl` and standalone as `main.py report`. It reads the
record rather than the run, so it works on a week that finished hours ago and
cannot be fooled by a variable the run happened to be holding.

**State and flags are separate, and that is the whole design.**

State is what each country looks like: the article funnel, the pass rate, the
per-ledger counts, the body mix, indicators resolved, coverage. Taiwan's zeros
appear in every single report, because state that is always the same is still
information and somebody reading the table should see the gap.

Flags are what *changed*, measured against that country's own recent median
rather than an absolute floor. Taiwan resolving zero raises nothing, because
Taiwan always resolved zero. Taiwan resolving zero after resolving five would.
An absolute threshold cannot tell those apart, and an alarm that fires every
week stops being read — which is the failure this is built against, so the
flags print last, in a block that is hard to skip.

The one exception is a schema violation, which is flagged whenever it happens,
history or not: it is a defect in the answer a score was made from, and a first
run can have one. Otherwise the first run has no history and reports state only.
"""

from __future__ import annotations

import argparse
import datetime as dt
from statistics import median
from typing import Any, Dict, List, Optional, Sequence

from backend.data_upsert import store
from backend.util import constants, db, provenance

__all__ = ["collect", "flags_for", "run_report"]


# How far a measure may fall against a country's own baseline before it is a
# break rather than a quiet week.
LEDGER_DROP = 1 / 3          # indicators resolved in any one ledger
POOL_DROP = 1 / 2            # candidates after dedupe
PASS_RATE_DROP = 1 / 2       # share of the pool clearing the gate

# How many prior runs make the baseline.
BASELINE_RUNS = 4


def _manifest_state(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the comparable measures out of one run's manifest."""
    census = (manifest or {}).get("census") or {}
    articles = census.get("articles") or {}
    indicators = census.get("indicators") or {}
    deduped = articles.get("after_dedupe", 0) or 0
    passed = articles.get("passed_gate", 0) or 0
    return {
        "fetched": articles.get("fetched", 0) or 0,
        "after_dedupe": deduped,
        "passed_gate": passed,
        "pass_rate": (passed / deduped) if deduped else 0.0,
        "selected": articles.get("selected", 0) or 0,
        "budget": articles.get("budget", 20) or 20,
        "per_ledger_selected": articles.get("per_ledger_selected") or {},
        "body_status": articles.get("body_status") or {},
        "digests_failed": articles.get("digests_failed", 0) or 0,
        "digests_truncated_retry": articles.get("digests_truncated_retry", 0) or 0,
        "resolved_by_ledger": indicators.get("resolved_by_ledger") or {},
        "expected_by_ledger": indicators.get("expected_by_ledger") or {},
        "empty_ledgers": indicators.get("empty_ledgers") or [],
        "schema_violations": (census.get("versions") or {}).get("schema_violations", 0),
    }


def collect(run_date: dt.date) -> List[Dict[str, Any]]:
    """State for every country scored on `run_date`, with its own baseline."""
    rows: List[Dict[str, Any]] = []
    for entry in constants.COUNTRY_ROSTER:
        iso2 = entry["iso2"]
        snapshot = store.read_snapshot(iso2, run_date)
        if not snapshot:
            rows.append({"iso2": iso2, "name": entry["name"], "scored": False,
                         "state": None, "history": [], "coverage": None})
            continue

        history = [
            _manifest_state(h["manifest"])
            for h in store.read_recent_snapshots(
                iso2, before=run_date, limit=BASELINE_RUNS
            )
        ]
        rows.append({
            "iso2": iso2,
            "name": entry["name"],
            "scored": True,
            "state": _manifest_state(snapshot.get("manifest") or {}),
            "coverage": snapshot.get("evidence_coverage"),
            "coverage_components": snapshot.get("coverage_components"),
            "history": history,
        })
    return rows


def _baseline(history: Sequence[Dict[str, Any]], path: str, ledger: str = "") -> Optional[float]:
    """Median of a measure across a country's recent runs, or None."""
    values = []
    for h in history:
        v = h.get(path)
        if ledger:
            v = (v or {}).get(ledger)
        if v is not None:
            values.append(v)
    return median(values) if values else None


def flags_for(row: Dict[str, Any], consecutive_failures: int = 0) -> List[str]:
    """What changed for one country. Empty on a first run."""
    out: List[str] = []
    iso2 = row["iso2"]

    if consecutive_failures >= 2:
        out.append(f"{iso2}: failed {consecutive_failures} runs in a row")

    if not row["scored"]:
        return out

    # The one absolute flag. A schema violation is a defect in the answer the
    # score was made from, not a change against history, so it fires on a first
    # run too: HK's week one printed "NO FLAGS" with ten articles unscored.
    state = row["state"]
    violations = state.get("schema_violations") or 0
    if violations > 0:
        out.append(f"{iso2}: {violations} schema violation(s) in the scorer's answer")

    history = row["history"]
    if not history:
        return out   # first run: nothing else to compare against

    for ledger in constants.LEDGERS:
        base = _baseline(history, "resolved_by_ledger", ledger)
        now = (state["resolved_by_ledger"] or {}).get(ledger, 0)
        if base is None or base <= 0:
            continue    # always been zero here; that is state, not change
        if now == 0:
            out.append(
                f"{iso2}: ledger '{ledger}' resolved nothing, against a recent "
                f"median of {base:g}"
            )
        elif now < base * (1 - LEDGER_DROP):
            out.append(
                f"{iso2}: ledger '{ledger}' resolved {now} indicators, against a "
                f"recent median of {base:g}"
            )

    base_pool = _baseline(history, "after_dedupe")
    if base_pool and state["after_dedupe"] < base_pool * POOL_DROP:
        out.append(
            f"{iso2}: pool fell to {state['after_dedupe']} from a recent median "
            f"of {base_pool:g}"
        )

    base_rate = _baseline(history, "pass_rate")
    if base_rate and state["pass_rate"] < base_rate * PASS_RATE_DROP:
        out.append(
            f"{iso2}: gate pass rate fell to {state['pass_rate']:.0%} from a "
            f"recent median of {base_rate:.0%}"
        )

    return out


def _format(rows: Sequence[Dict[str, Any]], run_date: dt.date) -> List[str]:
    lines = [
        "",
        f"=== Weekly quality report — {run_date} ===",
        "",
        f"{'':<4} {'pool':>5} {'pass':>5} {'sel':>7} {'ind':>7} {'cov':>4}  "
        f"{'bodies':<28} ledgers with no articles",
    ]
    for row in rows:
        if not row["scored"]:
            lines.append(f"{row['iso2']:<4} {'—':>5} {'—':>5} {'not scored':>7}")
            continue
        s = row["state"]
        resolved = sum((s["resolved_by_ledger"] or {}).values())
        expected = sum((s["expected_by_ledger"] or {}).values())
        no_articles = [
            led for led in constants.LEDGERS
            if not (s["per_ledger_selected"] or {}).get(led)
        ]
        bodies = ", ".join(f"{k}:{v}" for k, v in sorted(s["body_status"].items()))
        lines.append(
            f"{row['iso2']:<4} {s['after_dedupe']:>5} {s['pass_rate']:>4.0%} "
            f"{s['selected']:>3}/{s['budget']:<3} {resolved:>3}/{expected:<3} "
            f"{row['coverage'] or 0:>4}  {bodies:<28} "
            f"{', '.join(no_articles) or '—'}"
        )
    return lines


def run_report(run_date: Optional[dt.date] = None) -> Dict[str, Any]:
    """Print the report, record it, and return it.

    Returns:
        ``{"run_date", "rows", "flags"}``.
    """
    db.announce()
    run_date = run_date or store_latest_run_date()
    if run_date is None:
        print("[report] no scored runs to report on")
        return {"run_date": None, "rows": [], "flags": []}

    rows = collect(run_date)
    scored = [r for r in rows if r["scored"]]
    all_flags: List[str] = []
    for row in rows:
        all_flags.extend(flags_for(row))

    for line in _format(rows, run_date):
        print(line)

    print("")
    print(f"[report] {len(scored)}/{len(rows)} countries scored")
    if scored:
        covs = [r["coverage"] or 0 for r in scored]
        print(f"[report] coverage: min {min(covs)}, median "
              f"{median(covs):g}, max {max(covs)}")
        violations = sum(r["state"]["schema_violations"] or 0 for r in scored)
        failures = sum(r["state"]["digests_failed"] for r in scored)
        retries = sum(r["state"]["digests_truncated_retry"] for r in scored)
        print(f"[report] schema violations {violations} | digest failures "
              f"{failures} | truncated retries {retries}")

    # Flags last, and hard to skip. An alarm nobody reads is the failure mode
    # this whole report is built against.
    print("")
    if all_flags:
        print("!" * 72)
        print(f"!! {len(all_flags)} FLAG(S) — a defect in an answer, or a change "
              f"against a country's own history")
        print("!" * 72)
        for flag in all_flags:
            print(f"!! {flag}")
        print("!" * 72)
    else:
        first_run = all(not r["history"] for r in scored)
        print("=" * 72)
        print("== NO FLAGS" + (" — first run for every country, so state only, "
                               "nothing to compare against" if first_run else ""))
        print("=" * 72)

    try:
        store.write_ledger(
            job_type="report", run_date=run_date, status="ok",
            git_sha=provenance.git_sha(),
            detail={"countries_scored": len(scored), "flags": all_flags},
        )
    except Exception as exc:
        print(f"[report] could not record the report: {exc}")

    return {"run_date": run_date, "rows": rows, "flags": all_flags}


def store_latest_run_date() -> Optional[dt.date]:
    """The most recent run that produced scores."""
    from backend.data_upsert import data_push

    return data_push.read_latest_snapshot_date()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default="", help="run date, default the latest scored")
    args = ap.parse_args()
    run_report(dt.date.fromisoformat(args.date) if args.date else None)


if __name__ == "__main__":
    main()
