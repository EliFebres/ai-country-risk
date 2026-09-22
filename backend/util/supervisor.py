"""The supervisor: one process, started once and left alone.

This is what a deployment runs. A single loop in the main thread ticks the
prices poller on its existing cadence and, on every tick, asks whether the ETL
is due.

Whether it is due is read from the data, not from the clock and not from when
the process happened to start. Every tick reads the newest ``risk_snapshot``
date - the table the ETL actually writes - and runs the ETL when it is older
than ``ETL_MAX_AGE_DAYS`` or missing entirely. Startup is not an event; it is
just the first tick, asking the same question every other tick asks. That gives,
without a marker table and without a schema change:

  * a fresh deploy against an empty database runs immediately
  * a restart after a successful run this week does nothing
  * a restart after a crash that missed last week's run catches up on the first tick
  * a machine that was off for a month runs once, not four times

Asking the data rather than recording an attempt also means a failed ETL leaves
the age growing, so the next tick retries - which is correct, and is why the
per-day attempt cap exists: a persistently failing ETL must not hammer the
upstream APIs, and its failure must be visible in the log rather than silent.

The loop is in the main thread because the prices daemon installs its
SIGINT/SIGTERM handlers there, and signal.signal is a no-op anywhere else. The
ETL runs inline, so a long ETL is a gap in the price feed - the deliberate
trade for one loop, one thread, and no shared state to get wrong.
"""

import logging
import signal
import threading
from datetime import date, datetime, timezone
from typing import Optional

from backend.data_fetching import prices_daemon
from backend.data_upsert import data_push
from backend.util import constants, pipeline

logger = logging.getLogger("supervisor")

#: Ratings older than this mean the ETL is due. The ETL is a weekly job.
ETL_MAX_AGE_DAYS = 7

#: A persistently failing ETL retries, but not without limit: each attempt is a
#: full pass over the roster against paid APIs.
MAX_ETL_ATTEMPTS_PER_DAY = 3


class Supervisor:
    """Cross-tick state: only the attempt cap, which is a rate limit, not a schedule."""

    def __init__(self) -> None:
        self.prices = prices_daemon.PricesDaemon()
        self._stop = threading.Event()
        self._attempts_day: Optional[date] = None
        self._attempts_today = 0

    def _install_signals(self) -> None:
        """Own stop signal, rather than reaching into the daemon's.

        The same cooperative shutdown the prices command uses: the handler only
        sets the event, the tick in flight finishes, then the loop exits. Kept
        here rather than borrowed from PricesDaemon so that the two commands
        stay independent - `prices` still runs its own loop, untouched.
        """
        def _handler(signum, _frame):
            logger.info("Received signal %s; shutting down after this tick.", signum)
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass  # not in the main thread / unsupported on this platform

    # -- the ETL decision ---------------------------------------------------
    def _record_attempt(self, today: date) -> None:
        if self._attempts_day != today:
            self._attempts_day = today
            self._attempts_today = 0
        self._attempts_today += 1

    def etl_is_due(self, now: datetime) -> bool:
        """Ask the database how old the ratings are, and say so either way.

        Logged on every check, not only when it acts: a supervisor that silently
        decides to do nothing is indistinguishable from one that is stuck.
        """
        today = now.date()  # UTC, the same calendar the ETL stamps as_of with
        if self._attempts_day == today and self._attempts_today >= MAX_ETL_ATTEMPTS_PER_DAY:
            logger.warning(
                "ETL: %d attempts already today (cap %d); holding off until tomorrow. "
                "If ratings are still stale, the ETL is failing - check the log above.",
                self._attempts_today, MAX_ETL_ATTEMPTS_PER_DAY,
            )
            return False

        try:
            last = data_push.read_latest_snapshot_date()
        except Exception as e:  # noqa: BLE001 - a DB blip must not kill the loop
            logger.warning("ETL: could not read the last snapshot date (%s); skipping this tick.", e)
            return False

        if last is None:
            logger.info("ETL: no ratings in the database at all; due (threshold %dd).", ETL_MAX_AGE_DAYS)
            return True

        age = (now.date() - last).days
        due = age > ETL_MAX_AGE_DAYS
        logger.info(
            "ETL: last ratings %s (%dd old), threshold %dd -> %s.",
            last, age, ETL_MAX_AGE_DAYS, "RUN" if due else "skip",
        )
        return due

    def maybe_run_etl(self, now: datetime) -> None:
        if not self.etl_is_due(now):
            return
        self._record_attempt(now.date())
        logger.info("ETL: starting (attempt %d today). The price feed pauses until it finishes.",
                    self._attempts_today)
        started = datetime.now(timezone.utc)
        try:
            pipeline.run_etl()
        except Exception as e:  # noqa: BLE001 - one failed ETL must not kill the loop
            logger.exception("ETL: failed after %ds: %s",
                             round((datetime.now(timezone.utc) - started).total_seconds()), e)
            return
        logger.info("ETL: finished in %ds.",
                    round((datetime.now(timezone.utc) - started).total_seconds()))

    # -- the loop -----------------------------------------------------------
    def tick(self, now: Optional[datetime] = None) -> None:
        """Prices first, then the ETL question.

        Prices first because it is the cheap half, and because it means every
        ETL run is preceded by a fresh price tick rather than followed by a
        stale one.
        """
        now = now or datetime.now(timezone.utc)
        try:
            self.prices.tick(now)
        except Exception as e:  # noqa: BLE001
            logger.exception("prices tick failed: %s", e)
        self.maybe_run_etl(now)

    def run(self) -> None:
        self._install_signals()
        self.prices.load_state()
        poll = constants.PRICES_POLL_SECONDS
        logger.info(
            "Supervisor started (poll=%ss, ETL when ratings exceed %dd old). "
            "Running until stopped - press Ctrl-C to quit.",
            poll, ETL_MAX_AGE_DAYS,
        )
        while not self._stop.is_set():
            started = datetime.now(timezone.utc)
            try:
                self.tick(started)
            except Exception as e:  # noqa: BLE001 - never let one tick kill the loop
                logger.exception("Tick failed: %s", e)
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            sleep_s = max(1.0, poll - elapsed)
            if not self._stop.is_set():
                logger.info("Idle - next tick in %ds (Ctrl-C to stop).", round(sleep_s))
            self._stop.wait(sleep_s)
        logger.info("Supervisor stopped.")


def run_supervisor() -> None:
    """The dispatch target for ``python -m backend.main run``."""
    Supervisor().run()
