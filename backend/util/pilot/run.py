"""CLI for the History Machine: harvest, vintage, score, report.

``main.py`` is the only *process* the backend runs — the weekly ETL, the prices
tick, the panel rebuild. None of that is this. Harvesting ten years of articles
takes days of somebody else's rate limit and scoring them costs real money, so
both get their own entry point rather than a job in the scheduler's tuple.

Nothing here computes a score. `score` and `diagnostic` drive
``pipeline._process_country`` with ``as_of`` pinned — the same code path the
daily run takes, which is the entire premise of the project. If the backfill had
its own scoring path, the series would be measuring the backfill.

Every command that spends money prints its projection and waits for a yes.

Usage:
    python -m backend.util.pilot.run guardian     # step 2
    python -m backend.util.pilot.run gdelt        # step 3 (dormant, needs --anyway)
    python -m backend.util.pilot.run wayback      # step 4 (asks before spending)
    python -m backend.util.pilot.run nyt          # step 5
    python -m backend.util.pilot.run weo          # step 7 (per-edition macro)
    python -m backend.util.pilot.run monthly      # step 7 (IMF monthly, back-dated)
    python -m backend.util.pilot.run restamp      # step 7 (re-date stored rows)
    python -m backend.util.pilot.run report       # counts, evenness, recovery curve

    python -m backend.util.pilot.run score ...    # step 9/10 (asks before spending)
    python -m backend.util.pilot.run diagnostic   # step 10 (the named arms)
    python -m backend.util.pilot.run pilot-report # step 10/11 (the five meters)
"""

import argparse
import datetime
import logging
import os
import pathlib
import sys

import pandas as pd

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / "backend" / ".env")
load_dotenv()

from backend.data_upsert import data_push  # noqa: E402
from backend.data_upsert import store  # noqa: E402
from backend.news_fetching import wayback  # noqa: E402
from backend.util.pilot import reports, score  # noqa: E402
from backend.util import config  # noqa: E402
from backend.news_fetching.adapters import gdelt, guardian, nyt  # noqa: E402
from backend.data_fetching.vintage import lags, monthly, restamp, weo  # noqa: E402

logger = logging.getLogger("history")


def _report() -> None:
    """Print every deliverable the harvest steps owe."""
    print("\n=== articles by source x country x year ===")
    counts = pd.DataFrame(store.counts_by_year())
    if counts.empty:
        print("  Nothing harvested yet.")
        return
    grid = counts.pivot_table(index=["source_system", "country_iso2"],
                              columns="year", values="n",
                              aggfunc="sum", fill_value=0)
    grid["total"] = grid.sum(axis=1)
    print(grid.to_string())

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
    print("  (abstract-only is a tier, not a failure: the NYT archive returns no "
          "bodies to recover)")
    totals: dict = {}
    for row in store.recovery_curve():
        key = (row["source_system"], row["year"])
        buckets = totals.setdefault(key, {"n": 0, "recovered": 0, "live": 0,
                                          "flagged": 0, "abstract": 0})
        buckets["n"] += row["n"]
        if row["tier"] == "abstract-only":
            # Never had a body to lose. Counting it as a recovery failure would
            # read afterwards as "the archive went dark", which is the opposite
            # of what this curve is for.
            buckets["abstract"] += row["n"]
        elif row["body_status"] == "recovered":
            buckets["recovered"] += row["n"]
            buckets["live"] += row["n"] if row["body_vintage"] == "live-refetch" else 0
        elif str(row["body_status"]).startswith("degraded"):
            buckets["flagged"] += row["n"]
    # Percentages against the articles that could have had a body, so a source
    # with none does not dilute the number that matters.
    curve = [{"source": source, "year": year, "n": b["n"],
              **({"recovered": f"{100*b['recovered']/full:.1f}%",
                  "live-refetch": f"{100*b['live']/full:.1f}%",
                  "flagged": f"{100*b['flagged']/full:.1f}%"} if (full := b["n"] - b["abstract"])
                 else {"recovered": "-", "live-refetch": "-", "flagged": "-"})}
             for (source, year), b in sorted(totals.items())]
    print(pd.DataFrame(curve).to_string(index=False))


def _wayback(args) -> None:
    """Drain the recovery queue, asking before any billable scan."""
    pending = store.read_pending(limit=args.limit)
    if not pending:
        print("Nothing pending.")
        return

    if args.no_scan:
        print(f"{len(pending)} in the queue; live refetches will be skipped "
              f"(--no-scan), so an article with no archive capture is marked "
              f"'no-capture' and offered again in "
              f"{config.WAYBACK_RECHECK_DAYS} days rather than being written off.")
        counts = wayback.drain(limit=args.limit, api_key=None)
        print(f"attempted {counts['attempted']}, recovered {counts['recovered']}, "
              f"no capture {counts['no-capture']}, transient {counts['transient']}")
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


def _restamp_diff(iso2: str) -> None:
    """What the migration would do to one country's staleness, before it runs.

    Prints the latest observation of each indicator with its stored ``as_of``,
    its planned one, and how much older the payload will report it to be. The
    numbers grow, and that is the point: the stored date says every value
    arrived the morning of the last bulk fetch, which is when *we* got it, not
    when anyone could have.
    """
    stored = [r for r in restamp.read_all() if r["country_iso2"] == iso2]
    changed, _ = restamp.plan(stored)
    if not changed:
        print(f"{iso2}: nothing to re-date.")
        return

    today = datetime.date.today()
    latest: dict = {}
    for row in changed:
        code = row["indicator_code"]
        if code not in latest or row["period"] > latest[code]["period"]:
            latest[code] = row
    before = {(r["country_iso2"], r["indicator_code"], r["freq"], r["period"]): r["as_of"]
              for r in stored}

    print(f"\n=== {iso2}: staleness of the newest observation per indicator ===")
    print(f"  {'indicator':<24} {'period':<8} {'stored':<12} {'planned':<12} "
          f"{'stale before':>12} {'after':>7}")
    for code, row in sorted(latest.items()):
        was = before[(row["country_iso2"], code, row["freq"], row["period"])]
        print(f"  {code:<24} {str(row['period']):<8} {was!s:<12} {row['as_of']!s:<12} "
              f"{(today - was).days:>12} {(today - row['as_of']).days:>7}")
    print(f"\n  {len(changed)} row(s) would change for {iso2}. Nothing written.")


def _restamp(args) -> None:
    """Re-date every fetch-dated row, having dumped it first."""
    if args.revert:
        print(f"{restamp.revert(pathlib.Path(args.revert))} row(s) restored.")
        return
    if args.diff:
        _restamp_diff(args.diff.upper())
        return

    result = restamp.apply(dry_run=args.dry_run)
    print(f"\nread {result['read']} row(s); {result['changed']} to re-date.")
    for reason, count in sorted(result["skipped"].items()):
        print(f"  skipped {count:>6}: {reason}")
    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return
    if result["backup"]:
        print(f"\nbacked up to {result['backup']}")
        print(f"revert with: python -m backend.util.pilot.run restamp "
              f"--revert {result['backup']}")
    print(f"\nLags: {lags.SCHEME}. WEO editions keep their own dates.")


def _frozen(args) -> bool:
    """Check the version freeze, before anything asks anyone for money.

    ``score.run`` checks it too and that is the load-bearing one — every caller
    routes through it, including a notebook that never sees this file. This is
    only about the order the operator experiences: being asked to approve a
    spend and *then* being refused is the wrong way round.
    """
    try:
        state = score.freeze(override=args.override_version_drift)
    except score.VersionDrift as exc:
        print(f"\nRefusing to resume.\n\n  {exc}")
        return False
    sha = (state["versions"].get("git_sha") or "")[:8]
    if state["first"]:
        print(f"\nversion set pinned for this pilot at {sha or 'an unknown commit'}")
    elif state["moved"]:
        print(f"\nre-pinned across a version move by --override-version-drift: "
              f"{', '.join(state['moved'])}")
    elif state["sha_moved"]:
        print(f"\nnote: the tree moved to {sha} since this pilot started; every "
              f"frozen version held, so the series is still one series.")
    return True


def _confirm_spend(label: str, n_snapshots: int, args) -> bool:
    """Print the projection and the observed per-unit cost, then ask.

    The one place money is committed, so the number shown is derived from what
    the ledger has actually spent rather than from a constant — a projection
    that does not move when the real cost moves is not worth approving against.
    """
    already = store.total_spend_usd()
    try:
        projected = score.projection(n_snapshots, mode=getattr(args, "mode", None)
                                     if isinstance(getattr(args, "mode", None), str)
                                     else None)
    except score.NoObservedCost as exc:
        # The first run of all has nothing to project from, and that is exactly
        # when a fabricated number is most dangerous — it is the number somebody
        # approves a budget against. Say so, and make the operator carry the
        # estimate rather than dressing one up as a measurement.
        print(f"\n=== {label} ===")
        print(f"  snapshots        : {n_snapshots}")
        print(f"  projection       : UNAVAILABLE — {exc}")
        print(f"  already spent    : ${already:.2f}")
        print(f"  budget           : ${config.PILOT_BUDGET_USD:.2f}")
        if args.approved:
            print("\n  --approved given: proceeding without a projection. The "
                  "budget governor still stops the run at the cap.")
            return True
        try:
            return input("\nProceed with no projection? [y/N] ").strip().lower() == "y"
        except EOFError:
            print("No terminal to ask on. Re-run with --approved to consent up front.")
            return False
    per = projected / n_snapshots if n_snapshots else 0.0

    print(f"\n=== {label} ===")
    print(f"  snapshots        : {n_snapshots}")
    print(f"  observed per unit: ${per:.4f}")
    print(f"  projection       : ${projected:.2f}")
    print(f"  already spent    : ${already:.2f}")
    print(f"  budget           : ${config.PILOT_BUDGET_USD:.2f} "
          f"(hard: the run aborts rather than exceeding it)")
    print(f"  would leave      : ${config.PILOT_BUDGET_USD - already - projected:.2f}")

    if already + projected > config.PILOT_BUDGET_USD:
        print("\nThis would exceed the budget. Raise PILOT_BUDGET_USD deliberately "
              "or narrow the run.")
        return False
    if args.approved:
        return True
    try:
        return input("\nProceed? [y/N] ").strip().lower() == "y"
    except EOFError:
        print("No terminal to ask on. Re-run with --approved to consent up front.")
        return False


def _score(args) -> None:
    """Score a range of anchors in one mode, after showing what it costs."""
    if not _frozen(args):
        return
    roster = args.roster or list(config.PILOT_ROSTER)
    start = (datetime.date.fromisoformat(args.since) if args.since
             else datetime.date.fromisoformat(config.PILOT_START))
    end = datetime.date.fromisoformat(args.until) if args.until else datetime.date.today()

    dates = score.anchors(start, end)
    outstanding = sum(len([d for d in dates if d not in store.completed_runs(args.mode, c)])
                      for c in roster)
    if not outstanding:
        print("Nothing outstanding — every anchor in this range is already complete.")
        return
    if not _confirm_spend(f"{args.mode}: {len(roster)} country/ies x {len(dates)} anchors",
                          outstanding, args):
        print("Not scoring.")
        return

    totals = score.run(roster=roster, start=start, end=end, mode=args.mode,
                       override_version_drift=args.override_version_drift)
    print(f"\nscored {totals['scored']}, skipped {totals['skipped']}, "
          f"failed {totals['failed']}, spent ${totals['spend_usd']:.2f}")
    print(f"cumulative: ${store.total_spend_usd():.2f} of ${config.PILOT_BUDGET_USD:.2f}")


def _diagnostic(args) -> None:
    """Score the named and no-structural arms on dates chosen from the series."""
    if not _frozen(args):
        return
    roster = args.roster or list(config.PILOT_ROSTER)
    # Bounded to the range the masked arm actually scored. Unbounded is right for
    # the pilot and wrong for a one-year dry run: one leftover snapshot from
    # another year sits on the far side of the cutoff and pulls a date nothing
    # scored into the sample, so a correctly half-size six-date plan comes back
    # with seven and reads as a stratification bug.
    plan = score.diagnostic_plan(
        roster,
        since=datetime.date.fromisoformat(args.since) if args.since else None,
        until=datetime.date.fromisoformat(args.until) if args.until else None)
    total = sum(len(v) for v in plan.values())
    if not total:
        print("No masked series to sample from yet. Run `score` first.")
        return

    print("\n=== diagnostic sample ===")
    for iso2, days in sorted(plan.items()):
        print(f"  {iso2}: {len(days)} date(s)  {', '.join(str(d) for d in days[:4])}"
              f"{' …' if len(days) > 4 else ''}")

    modes = args.mode or list(config.DIAGNOSTIC_MODES)
    if not _confirm_spend(f"diagnostic arms {modes}", total * len(modes), args):
        print("Not scoring.")
        return

    for mode in modes:
        totals = score.run(roster=roster, mode=mode, dates=plan,
                           override_version_drift=args.override_version_drift)
        print(f"\n{mode}: scored {totals['scored']}, skipped {totals['skipped']}, "
              f"failed {totals['failed']}, spent ${totals['spend_usd']:.2f}")
    print(f"\ncumulative: ${store.total_spend_usd():.2f} of ${config.PILOT_BUDGET_USD:.2f}")


def main() -> None:
    """Dispatch one harvest command."""
    # Resolve the commit once for the whole process. `provenance` reads this
    # variable and documents that it never shells out — correctly, it is a pure
    # module — but nothing ever set it, so every manifest ever written records a
    # null `git_sha`. Exporting it here fixes that for every writer at once.
    sha = score.git_sha()
    if sha:
        os.environ.setdefault("GIT_SHA", sha)

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

    p = sub.add_parser("monthly")
    p.add_argument("--since", help="ISO date overriding HARVEST_FLOOR")
    p.add_argument("--country", action="append", dest="roster",
                   help="ISO2 code; repeatable. Defaults to PILOT_ROSTER.")

    p = sub.add_parser("restamp")
    p.add_argument("--dry-run", action="store_true",
                   help="count what would change; write nothing")
    p.add_argument("--diff", metavar="ISO2",
                   help="show one country's staleness before/after; write nothing")
    p.add_argument("--revert", metavar="CSV",
                   help="restore a dump written by an earlier run")

    p = sub.add_parser("score")
    p.add_argument("--country", action="append", dest="roster",
                   help="ISO2 code; repeatable. Defaults to PILOT_ROSTER.")
    p.add_argument("--since", help="first anchor; defaults to PILOT_START")
    p.add_argument("--until", help="last anchor; defaults to today")
    p.add_argument("--mode", default="masked", choices=config.SCORING_MODES)
    p.add_argument("--approved", action="store_true",
                   help="consent to the projected spend up front")
    p.add_argument("--override-version-drift", action="store_true",
                   help="score on despite a moved masking/prompt/payload "
                        "version, re-pinning the set and recording the move")

    p = sub.add_parser("diagnostic")
    p.add_argument("--country", action="append", dest="roster")
    p.add_argument("--since", help="draw the sample from anchors on or after this")
    p.add_argument("--until", help="draw the sample from anchors on or before this")
    p.add_argument("--mode", action="append",
                   choices=config.DIAGNOSTIC_MODES,
                   help="repeatable; defaults to both diagnostic arms")
    p.add_argument("--approved", action="store_true")
    p.add_argument("--override-version-drift", action="store_true",
                   help="score on despite a moved masking/prompt/payload version")

    sub.add_parser("report")

    p = sub.add_parser("pilot-report")
    p.add_argument("--country", action="append", dest="roster")
    p.add_argument("--export", action="store_true",
                   help="also write GATE2_BASELINE.json/.md to the repo root, so "
                        "the next run is a regression check rather than an opinion")
    p.add_argument("--note", default="",
                   help="what this capture was for, for the top of the markdown")

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
                  "windows failing. See backend/news_fetching/adapters/gdelt.py.\n"
                  "Pass --anyway if you mean it.")
            return
        gdelt.harvest(roster=args.roster, since=args.since)
    elif args.command == "nyt":
        nyt.harvest(roster=args.roster, since=args.since)
    elif args.command == "wayback":
        _wayback(args)
    elif args.command == "restamp":
        _restamp(args)
        return
    elif args.command == "score":
        _score(args)
        return
    elif args.command == "diagnostic":
        _diagnostic(args)
        return
    elif args.command == "pilot-report":
        reports.render(args.roster)
        if args.export:
            written = reports.export(PROJECT_ROOT, args.roster, note=args.note)
            print(f"\nbaseline written:\n  {written['json']}\n  {written['markdown']}")
        return
    elif args.command == "monthly":
        rows = monthly.backfill(roster=args.roster, since=args.since)
        data_push.upsert_indicator_series(rows) if rows else None
        print(f"\n{len(rows)} monthly row(s) written, dated by publication lag.")
        return
    elif args.command == "weo":
        rows = weo.load_all()
        if rows:
            # upsert_indicator_series returns None, so count the rows we handed
            # it. Printing its return said "None row(s) written" after a
            # successful load of nineteen editions, which reads as a failure.
            data_push.upsert_indicator_series(rows)
        print(f"\n{len(rows)} WEO vintage row(s) written to indicator_series.")
        if not rows:
            print("Drop the editions into backend/data/curated/weo_vintages/ "
                  "— see the README there.")
        return
    _report()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [history] %(levelname)s %(message)s")
    main()
