"""Pytest configuration for the backend characterization suite.

These tests pin the *current* behavior of the backend's pure functions so the
refactor can prove it changed nothing. They touch no network and no database.

The repo root is added to ``sys.path`` so ``backend.*`` imports resolve no
matter where pytest is invoked from.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def pytest_configure(config):
    """Refuse to run the database tests against the production database.

    `HISTORY_TEST_DATABASE_URL` is deliberately not `DATABASE_URL` — `test.py`
    says so — but "deliberately named differently" is a convention, and a
    convention is not a guard. Point one at the other and the `db` fixture in
    `test_data_upsert.py` opens with

        for table in ("article", "run_ledger", "llm_artifact",
                      "snapshot_diagnostic"):
            cur.execute(f"DELETE FROM {table}")

    which is correct for a scratch database and catastrophic for the real one:
    it silently destroys the entire harvested corpus — days of somebody else's
    rate limit — and every harvest checkpoint, while `risk_snapshot` survives so
    the damage does not announce itself.

    That happened on 2026-08-28. This is the guard that was missing, and it
    compares the resolved values rather than the variable names, because the
    mistake is pointing them at the same place.
    """
    import os

    test_db = os.getenv("HISTORY_TEST_DATABASE_URL")
    if not test_db:
        return
    live_db = os.getenv("PROD_DATABASE_URL") or os.getenv("DEV_DATABASE_URL")
    if live_db and test_db.strip() == live_db.strip():
        raise SystemExit(
            "HISTORY_TEST_DATABASE_URL is set to the same database as a real "
            "one. The db fixture truncates article, run_ledger, "
            "llm_artifact and snapshot_diagnostic before every test. Point it "
            "at a scratch database, or unset it to skip the database tests."
        )
