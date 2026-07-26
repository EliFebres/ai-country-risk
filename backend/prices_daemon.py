"""
Prices daemon — long-running market-data poller for the bottom-bar "Prices" pane.

Runs SEPARATELY from the daily ``main.py`` ETL: a persistent loop (run it
directly, e.g. under Task Scheduler at boot) that, every ``PRICES_POLL_SECONDS``,
pulls live prices from FMP and upserts the latest snapshot into the
``market_price`` table the frontend reads.

Cost control:
  • FMP live quotes are fetched in ONE batched call per tick, and only for asset
    classes whose market is currently open (``market_hours.is_open``) — crypto is
    24/7, US equities follow the NYSE session, commodities the Globex window.
  • The 1Q/YTD reference closes (FMP history) and the US Treasury yields (FMP
    treasury-rates) refresh at most once per (ET) day; both are skipped on every
    other tick.

Resilience: each tick is wrapped so a failure never kills the loop, and SIGINT/
SIGTERM trigger a clean shutdown. Run ``python backend/prices_daemon.py --once``
to execute a single tick (used for verification).
"""

import os
import sys
import signal
import logging
import pathlib
import threading
from datetime import datetime, timezone, date
from typing import Any, Dict, List, Optional

# --- Resolve project root so "backend/" is importable (mirrors main.py) -------
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

# Single .env load for the whole daemon process (modules read env at call time).
load_dotenv(PROJECT_ROOT / "backend" / ".env")
load_dotenv()  # also pick up a repo-root/cwd .env, without overriding

from backend.utils import constants
from backend.utils import market_hours
from backend.utils.data_upsert import data_push
from backend.utils.data_fetching import fmp_prices_fetch

logger = logging.getLogger("prices_daemon")

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
        """Start with empty references and no refresh recorded for today."""
        # internal_symbol -> {ref_q, ref_ytd, ...}
        self.refs: Dict[str, Dict[str, Any]] = {}
        self.refs_day: Optional[date] = None
        self.yields_day: Optional[date] = None
        self._stop = threading.Event()

    # -- startup ------------------------------------------------------------
    def load_state(self) -> None:
        """Hydrate references from the DB so a restart skips a same-day refetch."""
        try:
            self.refs = data_push.read_price_references()
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not load stored price references: %s", e)
            self.refs = {}
        # If everything stored shares a refresh date, treat that as today's marker.
        days = {r.get("reference_refreshed_on") for r in self.refs.values() if r.get("reference_refreshed_on")}
        self.refs_day = next(iter(days)) if len(days) == 1 else None
        logger.info("Loaded %d stored references (refs_day=%s).", len(self.refs), self.refs_day)

    # -- daily refreshes ----------------------------------------------------
    def maybe_refresh_references(self, now: datetime) -> None:
        """Once/day: read quarter-/year-start closes for the FMP assets."""
        today = _today_et(now)
        if self.refs_day == today:
            return
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
        # Stamp the day regardless so we attempt at most once per day.
        self.refs_day = today
        logger.info("Reference refresh complete (%d symbols).", len(by_internal))

    def maybe_refresh_yields(self, now: datetime) -> None:
        """Once/day: fetch US Treasury yields from FMP and upsert them."""
        today = _today_et(now)
        if self.yields_day == today:
            return
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
        # Stamp only when at least one yield resolved, so a fully-failed fetch retries.
        if rows:
            self.yields_day = today
        logger.info("Yield refresh complete (%d/%d symbols).", len(rows), len(_BOND_ASSETS))

    # -- live tick ----------------------------------------------------------
    def _row(
        self,
        asset: Dict[str, Any],
        *,
        px: Optional[float],
        chg: Optional[float],
        q: Optional[float],
        ytd: Optional[float],
    ) -> Dict[str, Any]:
        """Build a ``market_price`` upsert row from an asset + its metrics.

        Args:
            asset: a ``constants.PRICE_ASSETS`` entry (symbol, label, class, ...).
            px: latest price, or yield in points for bond assets.
            chg: 1-day change; percent for prices, points for yields.
            q: quarter-to-date change, same units as ``chg``.
            ytd: year-to-date change, same units as ``chg``.
        """
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
        """Trap SIGINT/SIGTERM so a stop request ends the loop cleanly."""
        def _handler(signum: int, _frame: object) -> None:
            """Signal the run loop to finish its current tick and exit."""
            logger.info("Received signal %s; shutting down.", signum)
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass  # not in main thread / unsupported on this platform


def main() -> None:
    """Entry point: configure logging, then run one tick or the full loop.

    Exits with status 1 if ``DATABASE_URL`` is unset, since every tick writes.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [prices] %(levelname)s %(message)s",
    )
    if not os.getenv("DATABASE_URL"):
        logger.error("DATABASE_URL is not set; cannot run the prices daemon.")
        sys.exit(1)

    daemon = PricesDaemon()
    if "--once" in sys.argv[1:]:
        daemon.load_state()
        daemon.tick()
    else:
        daemon.run()


if __name__ == "__main__":
    main()
