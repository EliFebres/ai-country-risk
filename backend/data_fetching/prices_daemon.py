"""
Prices daemon — long-running market-data poller for the bottom-bar "Prices" pane.

Reached as ``python -m backend.main prices``: a persistent loop that, every
``PRICES_POLL_SECONDS``, pulls live prices from FMP and upserts the latest
snapshot into the ``market_price`` table the frontend reads. ``main.py run``
drives the same tick on its own schedule alongside the ETL.

Cost control:
  • FMP live quotes are fetched in ONE batched call per tick, and only for asset
    classes whose market is currently open (``market_hours.is_open``) — crypto is
    24/7, US equities follow the NYSE session, commodities the Globex window.
  • The 1Q/YTD reference closes (FMP history) and the US Treasury yields (FMP
    treasury-rates) refresh at most once per (ET) day; both are skipped on every
    other tick.

Resilience: each tick is wrapped so a failure never kills the loop, and SIGINT/
SIGTERM trigger a clean shutdown. Run ``python -m backend.main prices --once``
to execute a single tick (used for verification).
"""

import signal
import logging
import threading
from datetime import datetime, timezone, date
from typing import Any, Dict, List, Optional, Tuple

from backend.util import constants
from backend.data_fetching import market_hours
from backend.data_upsert import data_push
from backend.data_fetching import fmp_prices_fetch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [prices] %(levelname)s %(message)s",
)
logger = logging.getLogger("prices_daemon")

#: Per-ET-day cap on each daily refresh, so a down upstream is retried
#: without being hammered every poll interval.
MAX_REFRESH_ATTEMPTS_PER_DAY = 3

# --- Precomputed asset lookups ----------------------------------------------
_FMP_ASSETS: List[Dict[str, Any]] = [a for a in constants.PRICE_ASSETS if a["source"] == "fmp"]
_BOND_ASSETS: List[Dict[str, Any]] = [a for a in constants.PRICE_ASSETS if a["source"] == "fmp_treasury"]
_SORT_ORDER: Dict[str, int] = {a["symbol"]: i for i, a in enumerate(constants.PRICE_ASSETS)}
_SRC_TO_INTERNAL: Dict[str, str] = {a["source_symbol"]: a["symbol"] for a in _FMP_ASSETS}


def _pct(px: Optional[float], ref: Optional[float]) -> Optional[float]:
    """Percentage move of ``px`` vs reference close ``ref`` (None-safe)."""
    if px is None or ref in (None, 0):
        return None
    return round((px / ref - 1.0) * 100.0, 2)


def _today_et(now_utc: datetime) -> date:
    """ET calendar date — daily rollovers align to the US trading day."""
    return market_hours.eastern_now(now_utc).date()


class PricesDaemon:
    """Holds the small amount of cross-tick state (daily-refresh bookkeeping)."""

    def __init__(self) -> None:
        # internal_symbol -> {ref_q, ref_ytd, ...}. A cache of values, not of
        # whether the day's work is done - that question goes to the database.
        self.refs: Dict[str, Dict[str, Any]] = {}
        self._attempts: Dict[str, Tuple[date, int]] = {}
        self._stop = threading.Event()

    # -- attempt limiting ---------------------------------------------------
    def _may_attempt(self, job: str, today: date) -> bool:
        """Cap retries per ET day.

        Asking the data means a failed refresh leaves the stored date stale, so
        the next tick tries again - correct, but every five minutes forever if
        the upstream is down. The old code avoided that for references by
        stamping the day even on total failure, which also meant a transient
        blip cost a whole day of 1Q/YTD accuracy. A cap keeps the retry and
        drops the hammering.
        """
        day, count = self._attempts.get(job, (today, 0))
        if day != today:
            return True
        return count < MAX_REFRESH_ATTEMPTS_PER_DAY

    def _record_attempt(self, job: str, today: date) -> None:
        day, count = self._attempts.get(job, (today, 0))
        self._attempts[job] = (today, count + 1 if day == today else 1)

    # -- startup ------------------------------------------------------------
    def load_state(self) -> None:
        """Hydrate reference *values* from the DB so a tick can compute 1Q/YTD.

        Whether today's refresh has already happened is no longer inferred here.
        It is asked of the database each time it matters, so a restart cannot
        lose the answer and a partially-written table cannot hide it.
        """
        try:
            self.refs = data_push.read_price_references()
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not load stored price references: %s", e)
            self.refs = {}
        logger.info("Loaded %d stored reference values.", len(self.refs))

    # -- daily refreshes ----------------------------------------------------
    def maybe_refresh_references(self, now: datetime) -> None:
        """Once/day: read quarter-/year-start closes for the FMP assets."""
        today = _today_et(now)
        try:
            stored = data_push.read_reference_refreshed_on()
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not read the stored reference date (%s); skipping refresh.", e)
            return
        if stored == today:
            logger.debug("References already refreshed for %s; skipping.", today)
            return
        if not self._may_attempt("references", today):
            logger.warning(
                "References: attempt cap %d reached today; holding off. Stored date "
                "is %s, so 1Q/YTD are being computed against stale closes.",
                MAX_REFRESH_ATTEMPTS_PER_DAY, stored,
            )
            return
        self._record_attempt("references", today)
        symbols = [a["source_symbol"] for a in _FMP_ASSETS]
        fetched = fmp_prices_fetch.fetch_reference_closes(symbols, now_utc=now)
        # Re-key from source symbol to internal symbol for storage + lookups.
        by_internal: Dict[str, Dict[str, Any]] = {}
        for src, ref in fetched.items():
            internal = _SRC_TO_INTERNAL.get(src)
            if internal:
                by_internal[internal] = ref
        if by_internal:
            self.refs.update(by_internal)
            try:
                data_push.upsert_price_references(by_internal, today)
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not persist price references: %s", e)
        # No day to stamp: the stored rows are the record. A failed fetch leaves
        # the stored date stale, so the next tick retries until the cap.
        logger.info("Reference refresh complete (%d symbols, stored date was %s).",
                    len(by_internal), stored)

    def maybe_refresh_yields(self, now: datetime) -> None:
        """Once/day: fetch US Treasury yields from FMP and upsert them."""
        today = _today_et(now)
        try:
            stored_at = data_push.read_yields_updated_at()
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not read the stored yield timestamp (%s); skipping refresh.", e)
            return
        stored_day = _today_et(stored_at) if stored_at else None
        if stored_day == today:
            logger.debug("Yields already refreshed for %s; skipping.", today)
            return
        if not self._may_attempt("yields", today):
            logger.warning(
                "Yields: attempt cap %d reached today; holding off. Stored day is %s.",
                MAX_REFRESH_ATTEMPTS_PER_DAY, stored_day,
            )
            return
        self._record_attempt("yields", today)
        metrics = fmp_prices_fetch.fetch_treasury_yields(_BOND_ASSETS, now_utc=now)
        rows: List[Dict[str, Any]] = []
        for a in _BOND_ASSETS:
            m = metrics.get(a["symbol"])
            if not m:
                continue
            rows.append(self._row(a, px=m.get("px"), chg=m.get("chg"), q=m.get("q"), ytd=m.get("ytd")))
        if rows:
            try:
                data_push.upsert_market_prices(rows)
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not upsert yield rows: %s", e)
                return
        # Nothing to stamp: the upserted rows carry their own updated_at, which is
        # what the next tick reads. A fully-failed fetch writes nothing and so
        # retries, up to the cap.
        logger.info("Yield refresh complete (%d/%d symbols).", len(rows), len(_BOND_ASSETS))

    # -- live tick ----------------------------------------------------------
    def _row(self, asset: Dict[str, Any], *, px, chg, q, ytd) -> Dict[str, Any]:
        """Build a ``market_price`` upsert row from an asset + its metrics."""
        return {
            "symbol": asset["symbol"],
            "label": asset["label"],
            "asset_class": asset["asset_class"],
            "source_symbol": asset["source_symbol"],
            "is_yield": asset["is_yield"],
            "px": px,
            "chg": chg,
            "q": q,
            "ytd": ytd,
            "sort_order": _SORT_ORDER[asset["symbol"]],
        }

    def tick(self, now: Optional[datetime] = None) -> None:
        """One poll cycle: daily refreshes (if due) + live FMP quotes for open markets."""
        now = now or datetime.now(timezone.utc)

        self.maybe_refresh_references(now)
        self.maybe_refresh_yields(now)

        # Only poll FMP classes whose market is open right now.
        open_assets = [a for a in _FMP_ASSETS if market_hours.is_open(a["asset_class"], now)]
        if not open_assets:
            logger.info("No FMP markets open; skipping live fetch this tick.")
            return

        quotes = fmp_prices_fetch.fetch_live_quotes([a["source_symbol"] for a in open_assets])

        rows: List[Dict[str, Any]] = []
        for a in open_assets:
            q_data = quotes.get(a["source_symbol"])
            if not q_data:
                continue  # absent from this batch — leave the prior DB value intact
            px = q_data.get("px")
            if px is None:
                continue
            ref = self.refs.get(a["symbol"]) or {}
            rows.append(
                self._row(
                    a,
                    px=px,
                    chg=q_data.get("chg_1d"),
                    q=_pct(px, ref.get("ref_q")),
                    ytd=_pct(px, ref.get("ref_ytd")),
                )
            )

        if rows:
            data_push.upsert_market_prices(rows)
        logger.info(
            "Tick done: %d open assets, %d rows upserted.", len(open_assets), len(rows)
        )

    # -- loop ---------------------------------------------------------------
    def run(self) -> None:
        """Run the poll loop until a stop signal is received."""
        self._install_signals()
        self.load_state()
        poll = constants.PRICES_POLL_SECONDS
        logger.info("Prices daemon started (poll=%ss). Running until stopped - press Ctrl-C to quit.", poll)
        while not self._stop.is_set():
            started = datetime.now(timezone.utc)
            try:
                self.tick(started)
            except Exception as e:  # noqa: BLE001 - never let one tick kill the loop
                logger.exception("Tick failed: %s", e)
            # Sleep to the next wall-clock boundary; wake early on a stop signal.
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            sleep_s = max(1.0, poll - elapsed)
            if not self._stop.is_set():
                logger.info("Idle - next refresh in %ds (Ctrl-C to stop).", round(sleep_s))
            self._stop.wait(sleep_s)
        logger.info("Prices daemon stopped.")

    def _install_signals(self) -> None:
        def _handler(signum, _frame):
            logger.info("Received signal %s; shutting down.", signum)
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass  # not in main thread / unsupported on this platform


def run_daemon(once: bool = False) -> None:
    """One tick, or the poll loop until a signal stops it.

    The DATABASE_URL guard that used to live here has moved to the launch-time
    check in main.py, which runs for every command and names every missing key
    at once rather than the first one reached.
    """
    daemon = PricesDaemon()
    if once:
        daemon.load_state()
        daemon.tick()
    else:
        daemon.run()
