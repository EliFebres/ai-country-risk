"""Daily ETL entry point for the AI Country Risk dashboard.

One run, in order: backfill any missing macro panels, refresh the economic
calendar and the IMF's fresher-than-annual indicators, then for every country
in the roster assemble a macro payload, gather and rank news, score the
country with the LLM, and upsert the snapshot plus its Top-3 articles. Finally
the pooled Top-3s across all countries are ranked once more for the global
alerts table.

Everything is written to Postgres, which the Next.js frontend reads directly —
there is no API layer between them.

This module is only the running order; each phase is implemented in
``backend.utils.pipeline``. Resilience lives with the phases: every one of them
logs failures with a full traceback and returns, so a flaky upstream or one bad
country costs a single phase rather than the whole day's run.

Usage:
    python backend/main.py
"""

import logging
import pathlib
import sys

from datetime import datetime, timezone

from dotenv import load_dotenv

# --- Resolve project root so "backend/" is importable ------------------------
# Must run before the backend.* imports below.
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Single .env load for the whole ETL process (modules read env at call time).
load_dotenv(PROJECT_ROOT / "backend" / ".env")
load_dotenv()  # also pick up a repo-root/cwd .env, without overriding

# --- Internal Imports --------------------------------------------------------
from backend.utils import constants, pipeline
from backend.utils.dates import utc_minute_iso
from backend.utils.data_fetching import country_data_fetch
from backend.utils.data_upsert import data_push

logger = logging.getLogger("main")


def main() -> None:
    """Run the full daily ETL, in order."""
    logger.info("=== AI Country Risk run started at %s UTC ===", utc_minute_iso(datetime.now(timezone.utc)))

    data_push.upsert_countries(constants.COUNTRY_ROSTER)    # 0)  seed the roster
    country_data_fetch.backfill_missing_panels()            # 0a) macro panels (incremental)
    pipeline.refresh_calendar()                             # 0b) econ calendar + AI ranking
    pipeline.refresh_imf_indicators()                       # 0c) fresher-than-annual indicators
    pool = pipeline.process_all_countries()                 # 1-7) per-country risk snapshots
    pipeline.publish_global_alerts(pool)                    # 8)  global news alerts

    logger.info("=== Run finished at %s UTC ===", utc_minute_iso(datetime.now(timezone.utc)))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [etl] %(levelname)s %(message)s",
    )
    main()
