"""CLI for the History Machine's harvest phase.

``main.py`` is the only *process* the backend runs — the weekly ETL, the prices
tick, the panel rebuild. None of that is this. Harvesting ten years of articles
is a one-off backfill that takes days of somebody else's rate limit, so it gets
its own entry point rather than a job in the scheduler's tuple.

Nothing here scores anything. It fills the article store and reports on what it
filled.

Usage:
    python -m backend.utils.history.run guardian     # step 2
    python -m backend.utils.history.run gdelt        # step 3 (dormant, needs --anyway)
    python -m backend.utils.history.run wayback      # step 4 (asks before spending)
    python -m backend.utils.history.run nyt          # step 5
    python -m backend.utils.history.run weo          # step 7 (per-edition macro)
    python -m backend.utils.history.run report       # counts, evenness, recovery curve
"""

import argparse
import logging
import os
import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / "backend" / ".env")
load_dotenv()

from backend.utils.data_upsert import data_push  # noqa: E402
from backend.utils.history import config, store, wayback  # noqa: E402
from backend.utils.history.adapters import gdelt, guardian, nyt  # noqa: E402
from backend.utils.history.vintage import weo  # noqa: E402

logger = logging.getLogger("history")


def _report() -> None:
    """Print every deliverable the harvest steps owe."""
    print("\n=== articles by source x country x year ===")
    for row in store.counts_by_year():
        print(f"  {row['source_system']:<9} {row['country_iso2']}  {row['year']}  {row['n']:>6}")

    print("\n=== evenness check: months with no articles ===")
    months = store.counts_by_month()
    have = {(r["country_iso2"], r["month"]) for r in months if r["n"] > 0}
    holes = 0
    for iso2 in config.PILOT_ROSTER:
        seen = sorted(m for c, m in have if c == iso2)
        if not seen:
            print(f"  {iso2}: nothing harvested")
            continue
        cursor = seen[0]
        while cursor <= seen[-1]:
            if (iso2, cursor) not in have:
                print(f"  {iso2} {cursor:%Y-%m}: 0 articles")
                holes += 1
            cursor = cursor.replace(year=cursor.year + cursor.month // 12,
                                    month=cursor.month % 12 + 1)
    print(f"  {holes} hole(s) — each one is an empty snapshot later")

    print("\n=== recovery curve: body outcomes by source x year ===")
    totals: dict = {}
    for row in store.recovery_curve():
        key = (row["source_system"], row["year"])
        totals.setdefault(key, {})
        label = f"{row['body_status']}/{row['body_vintage'] or '-'}"
        totals[key][label] = totals[key].get(label, 0) + row["n"]
    for (source, year), buckets in sorted(totals.items()):
        total = sum(buckets.values())
        recovered = sum(n for k, n in buckets.items() if k.startswith("recovered"))
        live = sum(n for k, n in buckets.items() if k.endswith("live-refetch"))
        degraded = sum(n for k, n in buckets.items() if k.startswith("degraded"))
        print(f"  {source:<9} {year}  n={total:<6} recovered={100*recovered/total:5.1f}%  "
              f"live-refetch={100*live/total:5.1f}%  flagged={100*degraded/total:5.1f}%")


def _wayback(args) -> None:
    """Drain the recovery queue, asking before any billable scan."""
    pending = store.read_pending(limit=args.limit)
    if not pending:
        print("Nothing pending.")
        return

    if args.no_scan:
        print(f"{len(pending)} pending; live refetches will be skipped "
              f"(--no-scan), so uncaptured articles will be marked failed.")
        wayback.drain(limit=args.limit, api_key=None)
        return

    # A rough upper bound: every article needing a live refetch, at a full-size
    # body. The real spend is far lower — most articles have a capture and never
    # reach the scan — but a projection should overstate, not understate.
    worst_case = wayback.scan_cost_usd(["x" * 24000] * len(pending))
    capped = min(worst_case, config.LEAKAGE_SCAN_BUDGET_USD)
    print(f"\n{len(pending)} pending article(s).")
    print(f"Leakage scan worst case: ${worst_case:.2f} (every one needing a live "
          f"refetch at full body size).")
    print(f"Hard cap: ${config.LEAKAGE_SCAN_BUDGET_USD:.2f} — the drain aborts there, "
          f"so at most ${capped:.2f} is spent.")
    # --scan-approved is the same consent given ahead of time, for a run whose
    # stdin is not a terminal. It is deliberately not a default: nothing here
    # spends money without someone having said so once.
    if not args.scan_approved:
        try:
            answer = input("Proceed with the billable leakage scan? [y/N] ")
        except EOFError:
            print("No terminal to ask on. Re-run with --scan-approved to consent "
                  "up front, or --no-scan to recover captures only.")
            return
        if answer.strip().lower() != "y":
            print("Not scanning. Re-run with --no-scan to recover captures only.")
            return

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY is not set; cannot scan.")
        return
    wayback.drain(limit=args.limit, api_key=api_key)


def main() -> None:
    """Dispatch one harvest command."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("guardian", "gdelt", "nyt"):
        p = sub.add_parser(name)
        p.add_argument("--since", help="ISO date overriding the configured floor")
        p.add_argument("--country", action="append", dest="roster",
                       help="ISO2 code; repeatable. Defaults to PILOT_ROSTER.")
        if name == "gdelt":
            p.add_argument("--anyway", action="store_true",
                           help="harvest anyway, knowing what it costs")

    p = sub.add_parser("wayback")
    p.add_argument("--limit", type=int, help="stop after this many articles")
    p.add_argument("--no-scan", action="store_true",
                   help="recover archive captures only; never refetch live pages")
    p.add_argument("--scan-approved", action="store_true",
                   help="consent to the billable leakage scan up front, for a "
                        "run with no terminal to ask on")

    sub.add_parser("weo")
    sub.add_parser("report")
    args = parser.parse_args()

    if args.command == "guardian":
        guardian.harvest(roster=args.roster, since=args.since)
    elif args.command == "gdelt":
        # Dormant, and the reason is a measurement rather than an opinion — see
        # the module docstring. The guard exists because the harvest is
        # resumable and polite and looks perfectly safe to start, and starting
        # it costs about twelve days.
        if not args.anyway:
            print("GDELT is dormant: the DOC API answers ~1 call per multi-minute "
                  "window from one IP, making this harvest ~12 days with most "
                  "windows failing. See backend/utils/history/adapters/gdelt.py.\n"
                  "Pass --anyway if you mean it.")
            return
        gdelt.harvest(roster=args.roster, since=args.since)
    elif args.command == "nyt":
        nyt.harvest(roster=args.roster, since=args.since)
    elif args.command == "wayback":
        _wayback(args)
    elif args.command == "weo":
        rows = weo.load_all()
        written = data_push.upsert_indicator_series(rows) if rows else 0
        print(f"\n{written} WEO vintage row(s) written to indicator_series.")
        if not rows:
            print("Drop the editions into backend/data/curated/weo_vintages/ "
                  "— see the README there.")
        return
    _report()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [history] %(levelname)s %(message)s")
    main()
