"""The only process the AI Country Risk backend runs.

A single loop that ticks every ``TICK_SECONDS``, asks Postgres when each job
last finished, and runs whatever is overdue:

  • prices  — every tick. Live FMP quotes for whichever markets are open right
              now; a no-op outside session hours.
  • etl     — the first tick of a new ISO week. The full run: seed the roster,
              backfill any missing macro panel, refresh the economic calendar,
              the IMF's fresher-than-annual indicators and the three-ledger
              sources, then score every country in the roster and publish the
              global alerts.
  • panels  — every 30 days. Refetches every country's World Bank annuals so
              revisions and newly published years land, which the incremental
              backfill alone would never pick up.

Because "when did this last run" lives in the ``job_run`` table rather than in
memory, the schedule survives a restart or a redeploy: a box that was down for
ten days comes back up and immediately catches up on the week it missed. A job
is only stamped when it succeeds, so a failure retries on the next tick instead
of waiting out its whole interval.

Everything is written to Postgres, which the Next.js frontend reads directly —
there is no API layer between them. Resilience lives with the phases: each one
logs failures with a full traceback and returns, so a flaky upstream or one bad
country costs a single phase rather than the whole run.

The scheduler is the default, not a subcommand — bare ``main.py`` is what runs
in production. Everything else that used to live in ``scripts/`` is a
subcommand here, so there is one executable rather than five.

Usage:
    python backend/main.py                       # run forever
    python backend/main.py --once                # one pass over every due job
    python backend/main.py backfill score --help # the pilot CLI
    python backend/main.py rebuild PT 2019-06-03 # re-derive a stored snapshot
    python backend/main.py probe --recorded      # re-probe stored bundles
    python backend/main.py census PT             # registry vs what arrives
    python backend/main.py weo-fetch             # download WEO editions
"""

import os
import sys
import signal
import logging
import pathlib
import importlib
import threading

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from dotenv import load_dotenv

# --- Resolve project root so "backend/" is importable ------------------------
# Must run before the backend.* imports below.
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Single .env load for the whole process (modules read env at call time).
load_dotenv(PROJECT_ROOT / "backend" / ".env")
load_dotenv()  # also pick up a repo-root/cwd .env, without overriding

# --- Internal Imports --------------------------------------------------------
from backend.util import constants
from backend.util import pipeline, prices
from backend.util.dates import utc_minute_iso
from backend.data_fetching import country_data_fetch
from backend.data_upsert import data_push

logger = logging.getLogger("main")

TICK_SECONDS = constants.PRICES_POLL_SECONDS  # the shortest cadence we schedule
STOP = threading.Event()

# Subcommand -> the module whose main() owns its arguments. Each already had a
# working argparse when it was a script, so this dispatches rather than
# re-declaring them; --help on a subcommand is that module's own help.
SUBCOMMANDS = {
    "bootstrap": ("backend.util.tools.bootstrap", "build an empty database into a working one"),
    "backfill":  ("backend.util.pilot.run", "harvest, score and report the backfill"),
    "rebuild":   ("backend.util.tools.rebuild_snapshot", "re-derive a stored snapshot and diff it"),
    "probe":     ("backend.util.tools.probe_bundles", "re-probe stored bundles for identifiability"),
    "census":    ("backend.util.tools.payload_census", "every registry indicator vs what arrives"),
    "weo-fetch": ("backend.data_fetching.vintage.fetch_editions", "download IMF WEO editions"),
}


def _dispatch(argv: list) -> int:
    """Run a subcommand, handing it the rest of the arguments as its own."""
    module = importlib.import_module(SUBCOMMANDS[argv[0]][0])
    sys.argv = [f"main.py {argv[0]}", *argv[1:]]
    return module.main() or 0


def _usage() -> None:
    """Print the scheduler's own usage plus the subcommand list."""
    print(__doc__.split("Usage:")[0].strip())
    print("\nUsage:\n    python backend/main.py [--once]        run the scheduler")
    for name, (_, blurb) in SUBCOMMANDS.items():
        print(f"    python backend/main.py {name:<10} {blurb}")
    print("\nAdd --help after a subcommand for its own arguments.")


# --- Due checks --------------------------------------------------------------
# Each takes (last_run or None, now) and answers "should this run?". A job that
# has never run is always due, which is what makes a fresh database bootstrap
# itself on first boot.

def _weekly(last: Optional[datetime], now: datetime) -> bool:
    """True on the first tick of an ISO week we have not run in yet."""
    return last is None or last.isocalendar()[:2] != now.isocalendar()[:2]


def _every(days: int) -> Callable[[Optional[datetime], datetime], bool]:
    """Due check for a plain interval: at least ``days`` since the last run."""
    def due(last: Optional[datetime], now: datetime) -> bool:
        """True once ``days`` have elapsed (or the job has never run)."""
        return last is None or now - last >= timedelta(days=days)
    return due


# --- Jobs --------------------------------------------------------------------

def _run_etl() -> None:
    """The full weekly run, in order."""
    data_push.upsert_countries(constants.COUNTRY_ROSTER)    # 0)  seed the roster
    country_data_fetch.backfill_missing_panels()            # 0a) macro panels (incremental)
    pipeline.refresh_calendar()                             # 0b) econ calendar + AI ranking
    pipeline.refresh_imf_indicators()                       # 0c) fresher-than-annual indicators
    pipeline.refresh_ledger_sources()                       # 0d) WB extras, BIS, curated.csv
    pool = pipeline.process_all_countries()                 # 1-7) per-country risk snapshots
    pipeline.publish_global_alerts(pool)                    # 8)  global news alerts


def _refresh_panels() -> None:
    """Rebuild every macro panel so World Bank revisions land."""
    country_data_fetch.backfill_missing_panels(force=True)


JOBS = (
    ("etl",    _weekly,    _run_etl),
    ("panels", _every(30), _refresh_panels),
)


def run_pass(daemon: prices.PricesDaemon, now: datetime) -> None:
    """One tick: refresh prices, then run every job whose interval has passed."""
    try:
        daemon.tick(now)
    except Exception:  # noqa: BLE001 - a bad tick must not skip the scheduled jobs
        logger.exception("Prices tick failed")

    last_runs = data_push.read_job_runs()
    for name, is_due, run in JOBS:
        last = last_runs.get(name)
        if not is_due(last, now):
            continue
        logger.info("=== %s started at %s UTC (last run: %s) ===", name, utc_minute_iso(now), last or "never")
        try:
            run()
            data_push.mark_job_run(name)  # only on success, so a failure retries next tick
            logger.info("=== %s finished at %s UTC ===", name, utc_minute_iso(datetime.now(timezone.utc)))
        except Exception:  # noqa: BLE001 - one bad job must not kill the loop
            logger.exception("%s failed; will retry next tick", name)


def _install_signals() -> None:
    """Trap SIGINT/SIGTERM so a stop request ends the loop cleanly."""
    def _handler(signum: int, _frame: object) -> None:
        """Signal the loop to finish its current pass and exit."""
        logger.info("Received signal %s; shutting down.", signum)
        STOP.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass  # not in main thread / unsupported on this platform


def main() -> None:
    """Dispatch a subcommand, or run the scheduler — which is the default."""
    argv = sys.argv[1:]
    if argv and argv[0] in SUBCOMMANDS:
        sys.exit(_dispatch(argv))
    if argv and argv[0] in ("-h", "--help"):
        _usage()
        return
    if argv and not argv[0].startswith("-"):
        print(f"unknown subcommand {argv[0]!r}\n")
        _usage()
        sys.exit(2)

    if not os.getenv("DATABASE_URL"):
        logger.error("DATABASE_URL is not set; every job writes, so there is nothing to do.")
        sys.exit(1)

    daemon = prices.PricesDaemon()
    daemon.load_state()

    if "--once" in sys.argv[1:]:
        run_pass(daemon, datetime.now(timezone.utc))
        return

    _install_signals()
    logger.info("Scheduler started (tick=%ss). Running until stopped - press Ctrl-C to quit.", TICK_SECONDS)
    while not STOP.is_set():
        started = datetime.now(timezone.utc)
        try:
            run_pass(daemon, started)
        except Exception:  # noqa: BLE001 - a dropped DB connection must not end the process
            logger.exception("Pass failed; retrying next tick")
        # Sleep to the next tick boundary; a long ETL just shortens the wait.
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        sleep_s = max(1.0, TICK_SECONDS - elapsed)
        if not STOP.is_set():
            logger.info("Idle - next tick in %ds (Ctrl-C to stop).", round(sleep_s))
        STOP.wait(sleep_s)
    logger.info("Scheduler stopped.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [etl] %(levelname)s %(message)s",
    )
    main()
