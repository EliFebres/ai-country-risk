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
  • panels  — every 30 days. Rebuilds every ``wb_panel_wide`` partition so
              World Bank revisions and newly published years land, which the
              incremental backfill alone would never pick up.

Because "when did this last run" lives in the ``job_run`` table rather than in
memory, the schedule survives a restart or a redeploy: a box that was down for
ten days comes back up and immediately catches up on the week it missed. A job
is only stamped when it succeeds, so a failure retries on the next tick instead
of waiting out its whole interval.

Everything is written to Postgres, which the Next.js frontend reads directly —
there is no API layer between them. Resilience lives with the phases: each one
logs failures with a full traceback and returns, so a flaky upstream or one bad
country costs a single phase rather than the whole run.

Usage:
    python backend/main.py           # run forever
    python backend/main.py --once    # one pass over every due job, then exit
"""

import os
import sys
import signal
import logging
import pathlib
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
from backend.utils import constants, pipeline, prices
from backend.utils.dates import utc_minute_iso
from backend.utils.data_fetching import country_data_fetch
from backend.utils.data_upsert import data_push

logger = logging.getLogger("main")

TICK_SECONDS = constants.PRICES_POLL_SECONDS  # the shortest cadence we schedule
STOP = threading.Event()


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
    """Run the scheduler loop until stopped, or a single pass with ``--once``."""
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
