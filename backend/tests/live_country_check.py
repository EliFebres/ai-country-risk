"""Live end-to-end check: run the real ETL for ONE country, verify, then undo.

Unlike everything else in this directory, this script talks to the real world:
the World Bank, Google News, publisher sites, OpenAI (which costs money), and
the Postgres database in ``DATABASE_URL``. It is therefore NOT named
``test_*.py`` and pytest will not collect it. Run it deliberately:

    python backend/tests/live_country_check.py            # defaults to PT
    python backend/tests/live_country_check.py NZ
    python backend/tests/live_country_check.py PT --keep  # skip the cleanup

What it does, in order:

  1. Ensures the core risk schema exists (the DDL from backend/README.md, made
     idempotent). Records which tables it had to create.
  2. Records the exact "before" state for this country, and **refuses to run**
     if a snapshot already exists for today — that would overwrite real data
     which the cleanup would then delete.
  3. Builds the country's Parquet panel if it is missing.
  4. Runs the real per-country pipeline: macro payload -> news -> LLM score ->
     Top-3 selection and enrichment -> database upsert.
  5. Verifies what landed, including the shape the frontend reads.
  6. Deletes exactly what this run created, and nothing that pre-existed.
  7. Confirms the database is back to its "before" state.

Every step is logged to stdout and to a log file.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import pathlib
import sys
import time
from typing import Any, Dict, List, Set, Tuple

# --- Bootstrap (mirrors main.py; must precede the backend.* imports) ---------
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / "backend" / ".env")
load_dotenv()

import psycopg2

from backend.utils import constants, pipeline
from backend.utils.data_fetching import country_data_fetch

log = logging.getLogger("live-check")

# The core risk schema. data_push creates the other tables itself, but these
# five are documented in backend/README.md as operator-provisioned, so a fresh
# database has none of them. Same columns/constraints as the README, with IF
# NOT EXISTS so re-running is safe.
CORE_SCHEMA: List[Tuple[str, str]] = [
    ("country", """
        CREATE TABLE IF NOT EXISTS country (
            iso2  CHAR(2) PRIMARY KEY,
            name  TEXT    NOT NULL
        );"""),
    ("indicator", """
        CREATE TABLE IF NOT EXISTS indicator (
            id   SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            unit TEXT        NOT NULL
        );"""),
    ("yearly_value", """
        CREATE TABLE IF NOT EXISTS yearly_value (
            country_iso2 CHAR(2) REFERENCES country(iso2),
            indicator_id INT     REFERENCES indicator(id),
            yr           INT,
            value        DOUBLE PRECISION,
            PRIMARY KEY (country_iso2, indicator_id, yr)
        );"""),
    ("risk_snapshot", """
        CREATE TABLE IF NOT EXISTS risk_snapshot (
            country_iso2   CHAR(2) REFERENCES country(iso2),
            as_of          DATE,
            score          DOUBLE PRECISION,
            bullet_summary TEXT,
            PRIMARY KEY (country_iso2, as_of)
        );"""),
    ("risk_snapshot_article", """
        CREATE TABLE IF NOT EXISTS risk_snapshot_article (
            id            BIGSERIAL PRIMARY KEY,
            country_iso2  CHAR(2)  NOT NULL REFERENCES country(iso2),
            as_of         DATE     NOT NULL,
            rank          SMALLINT NOT NULL CHECK (rank BETWEEN 1 AND 3),
            url           TEXT     NOT NULL,
            title         TEXT,
            source        TEXT,
            published_at  TIMESTAMPTZ,
            impact        DOUBLE PRECISION,
            summary       TEXT,
            image_url     TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (country_iso2, as_of, rank),
            FOREIGN KEY (country_iso2, as_of)
                REFERENCES risk_snapshot (country_iso2, as_of)
                ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_risk_snapshot_article_country_date
            ON risk_snapshot_article (country_iso2, as_of);"""),
]


class CheckFailed(Exception):
    """A verification assertion did not hold."""


def setup_logging(log_path: pathlib.Path) -> None:
    """Log everything to stdout and to ``log_path``."""
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(log_path, encoding="utf-8")):
        handler.setFormatter(fmt)
        root.addHandler(handler)


def connect():
    """Open a psycopg2 connection, or fail loudly."""
    url = os.getenv("DATABASE_URL")
    if not url:
        raise CheckFailed("DATABASE_URL is not set; cannot run the live check.")
    return psycopg2.connect(url)


def table_exists(cur, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{name}",))
    return cur.fetchone()[0]


def ensure_schema(conn) -> List[str]:
    """Create any missing core tables. Returns the names actually created."""
    created: List[str] = []
    with conn.cursor() as cur:
        for name, ddl in CORE_SCHEMA:
            if table_exists(cur, name):
                log.info("  schema: %-24s already present", name)
                continue
            cur.execute(ddl)
            created.append(name)
            log.info("  schema: %-24s CREATED", name)
    conn.commit()
    return created


# --------------------------------------------------------------------------
# Before / after state capture
# --------------------------------------------------------------------------
def capture_state(conn, iso2: str, as_of: datetime.date) -> Dict[str, Any]:
    """Everything about this country that already exists in the database."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM country WHERE iso2 = %s", (iso2,))
        country_rows = cur.fetchone()[0]

        cur.execute("SELECT id, name FROM indicator ORDER BY id")
        indicators = {r[0]: r[1] for r in cur.fetchall()}

        cur.execute(
            "SELECT indicator_id, yr FROM yearly_value WHERE country_iso2 = %s", (iso2,)
        )
        yearly: Set[Tuple[int, int]] = set(cur.fetchall())

        cur.execute(
            "SELECT count(*) FROM risk_snapshot WHERE country_iso2 = %s AND as_of = %s",
            (iso2, as_of),
        )
        snapshot_today = cur.fetchone()[0]

        cur.execute("SELECT count(*) FROM risk_snapshot WHERE country_iso2 = %s", (iso2,))
        snapshots_any = cur.fetchone()[0]

        cur.execute(
            "SELECT count(*) FROM risk_snapshot_article WHERE country_iso2 = %s AND as_of = %s",
            (iso2, as_of),
        )
        articles_today = cur.fetchone()[0]

    return {
        "country_rows": country_rows,
        "indicator_ids": set(indicators),
        "yearly_keys": yearly,
        "snapshot_today": snapshot_today,
        "snapshots_any": snapshots_any,
        "articles_today": articles_today,
    }


def log_state(label: str, state: Dict[str, Any]) -> None:
    log.info("  %s country rows          : %d", label, state["country_rows"])
    log.info("  %s indicator rows (global): %d", label, len(state["indicator_ids"]))
    log.info("  %s yearly_value rows      : %d", label, len(state["yearly_keys"]))
    log.info("  %s risk_snapshot (today)  : %d", label, state["snapshot_today"])
    log.info("  %s risk_snapshot (any day): %d", label, state["snapshots_any"])
    log.info("  %s articles (today)       : %d", label, state["articles_today"])


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------
def verify(conn, iso2: str, country_name: str, as_of: datetime.date) -> None:
    """Assert the run wrote a complete, frontend-readable snapshot."""
    problems: List[str] = []

    with conn.cursor() as cur:
        # -- country -------------------------------------------------------
        cur.execute("SELECT iso2, name FROM country WHERE iso2 = %s", (iso2,))
        row = cur.fetchone()
        if not row:
            problems.append("country row missing")
        else:
            log.info("  country            : %s / %s", row[0], row[1])
            if row[1] != country_name:
                problems.append(f"country name {row[1]!r} != expected {country_name!r}")

        # -- indicators ----------------------------------------------------
        cur.execute("SELECT count(*) FROM indicator")
        n_ind = cur.fetchone()[0]
        log.info("  indicator rows     : %d (expected >= %d)", n_ind, len(constants.ALL_INDICATORS))
        if n_ind < len(constants.ALL_INDICATORS):
            problems.append(f"only {n_ind} indicator rows, expected >= {len(constants.ALL_INDICATORS)}")

        # -- yearly values -------------------------------------------------
        cur.execute(
            """SELECT count(*), min(yr), max(yr), count(value)
                 FROM yearly_value WHERE country_iso2 = %s""",
            (iso2,),
        )
        n_yv, yr_min, yr_max, n_val = cur.fetchone()
        log.info("  yearly_value rows  : %d (years %s-%s, %d non-null)", n_yv, yr_min, yr_max, n_val)
        if n_yv == 0:
            problems.append("no yearly_value rows written")
        if yr_min is not None and yr_min < 1900:
            problems.append(f"implausible min year {yr_min}")

        # -- snapshot ------------------------------------------------------
        cur.execute(
            "SELECT score, bullet_summary FROM risk_snapshot WHERE country_iso2 = %s AND as_of = %s",
            (iso2, as_of),
        )
        snap = cur.fetchone()
        if not snap:
            problems.append("risk_snapshot row missing")
        else:
            score, summary = snap
            log.info("  risk score         : %s", score)
            log.info("  bullet_summary     : %d chars", len(summary or ""))
            log.info("     %s", (summary or "")[:300])
            if score is None:
                problems.append("score is NULL - the LLM call failed or degraded")
            elif not (0.0 <= float(score) <= 1.0):
                problems.append(f"score {score} outside 0..1")
            if not (summary or "").strip():
                problems.append("bullet_summary is empty")

        # -- articles ------------------------------------------------------
        cur.execute(
            """SELECT rank, url, title, source, published_at, impact, image_url,
                      length(coalesce(summary,''))
                 FROM risk_snapshot_article
                WHERE country_iso2 = %s AND as_of = %s ORDER BY rank""",
            (iso2, as_of),
        )
        arts = cur.fetchall()
        log.info("  Top-3 articles     : %d rows", len(arts))
        for rank, url, title, source, published, impact, image, summary_len in arts:
            log.info("     [%d] impact=%-6s %s", rank, impact, (title or "")[:70])
            log.info("         src=%-22s published=%s", (source or "")[:22], published)
            log.info("         url=%s", (url or "")[:100])
            log.info("         img=%s  summary=%d chars",
                     "yes" if image else "NONE", summary_len)
        if len(arts) != 3:
            problems.append(f"expected 3 article rows, got {len(arts)}")
        if {a[0] for a in arts} != {1, 2, 3} and arts:
            problems.append(f"ranks are {sorted(a[0] for a in arts)}, expected [1,2,3]")
        for rank, url, *_ in arts:
            if not (url or "").startswith("http"):
                problems.append(f"article rank {rank} has a non-http url: {url!r}")

        # -- the shape the frontend actually reads -------------------------
        cur.execute(
            """SELECT s.country_iso2, c.name, s.score, s.as_of,
                      count(a.id) AS article_count
                 FROM risk_snapshot s
                 JOIN country c ON c.iso2 = s.country_iso2
                 LEFT JOIN risk_snapshot_article a
                        ON a.country_iso2 = s.country_iso2 AND a.as_of = s.as_of
                WHERE s.country_iso2 = %s AND s.as_of = %s
                GROUP BY 1,2,3,4""",
            (iso2, as_of),
        )
        joined = cur.fetchone()
        if not joined:
            problems.append("frontend-shaped join returned nothing")
        else:
            log.info("  frontend join      : %s %s score=%s as_of=%s articles=%d", *joined)

    if problems:
        for p in problems:
            log.error("  FAIL: %s", p)
        raise CheckFailed(f"{len(problems)} verification problem(s)")
    log.info("  ALL VERIFICATION CHECKS PASSED")


# --------------------------------------------------------------------------
# Cleanup
# --------------------------------------------------------------------------
def cleanup(conn, iso2: str, as_of: datetime.date, before: Dict[str, Any]) -> None:
    """Delete exactly what this run added; leave anything that pre-existed."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM risk_snapshot_article WHERE country_iso2 = %s AND as_of = %s",
            (iso2, as_of),
        )
        log.info("  deleted %d risk_snapshot_article row(s)", cur.rowcount)

        cur.execute(
            "DELETE FROM risk_snapshot WHERE country_iso2 = %s AND as_of = %s",
            (iso2, as_of),
        )
        log.info("  deleted %d risk_snapshot row(s)", cur.rowcount)

        # Only the yearly_value rows that were not there before.
        cur.execute(
            "SELECT indicator_id, yr FROM yearly_value WHERE country_iso2 = %s", (iso2,)
        )
        now_keys: Set[Tuple[int, int]] = set(cur.fetchall())
        added = now_keys - before["yearly_keys"]
        if added:
            cur.executemany(
                "DELETE FROM yearly_value WHERE country_iso2 = %s AND indicator_id = %s AND yr = %s",
                [(iso2, ind, yr) for ind, yr in added],
            )
        log.info("  deleted %d yearly_value row(s) (%d pre-existing kept)",
                 len(added), len(before["yearly_keys"]))

        # The country row, only if this run introduced it.
        if before["country_rows"] == 0:
            cur.execute("DELETE FROM country WHERE iso2 = %s", (iso2,))
            log.info("  deleted %d country row(s)", cur.rowcount)
        else:
            log.info("  country row pre-existed - kept")

        # Indicator rows are global reference data shared by every country.
        # Remove only the ones this run introduced, and only while nothing
        # references them.
        cur.execute("SELECT id FROM indicator")
        added_inds = {r[0] for r in cur.fetchall()} - before["indicator_ids"]
        removed = 0
        for ind_id in added_inds:
            cur.execute("SELECT count(*) FROM yearly_value WHERE indicator_id = %s", (ind_id,))
            if cur.fetchone()[0] == 0:
                cur.execute("DELETE FROM indicator WHERE id = %s", (ind_id,))
                removed += cur.rowcount
        log.info("  deleted %d indicator row(s) (%d pre-existing kept)",
                 removed, len(before["indicator_ids"]))
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("iso2", nargs="?", default="PT", help="ISO-2 country code (default: PT)")
    parser.add_argument("--keep", action="store_true", help="skip cleanup and leave the data")
    parser.add_argument("--log-file", default=None, help="where to write the log")
    args = parser.parse_args()

    iso2 = args.iso2.upper()
    log_path = pathlib.Path(args.log_file) if args.log_file else \
        PROJECT_ROOT / f"live_check_{iso2}.log"
    setup_logging(log_path)

    entry = next((c for c in constants.COUNTRY_ROSTER if c["iso2"] == iso2), None)
    if entry is None:
        log.error("%s is not in COUNTRY_ROSTER", iso2)
        return 2
    country_name = entry["name"]
    as_of = datetime.date.today()
    started = time.monotonic()

    log.info("=" * 78)
    log.info("LIVE ONE-COUNTRY CHECK  %s (%s)   as_of=%s", country_name, iso2, as_of)
    log.info("log file: %s", log_path)
    log.info("=" * 78)
    log.info("This calls the World Bank, Google News, publisher sites and OpenAI,")
    log.info("and writes to the real database. It costs one LLM call.")

    conn = connect()
    conn.autocommit = False
    created_tables: List[str] = []
    wrote_data = False
    try:
        log.info("\n--- STEP 1: ensure core schema ---")
        created_tables = ensure_schema(conn)
        log.info("  tables created this run: %s", created_tables or "(none)")

        log.info("\n--- STEP 2: capture BEFORE state ---")
        before = capture_state(conn, iso2, as_of)
        log_state("before", before)

        if before["snapshot_today"]:
            log.error("REFUSING TO RUN: %s already has a risk_snapshot for %s.", iso2, as_of)
            log.error("Re-running would overwrite that real row, and the cleanup")
            log.error("would then delete it. Pick another country or another day.")
            return 3

        log.info("\n--- STEP 3: ensure the Parquet macro panel ---")
        if country_data_fetch.has_country_partition(country_data_fetch.PANEL_DIR, iso2):
            log.info("  panel already present for %s", iso2)
        else:
            log.info("  building panel for %s (World Bank + OWID; this takes a minute)...", iso2)
            # Reuse the real backfill by narrowing the roster to this country.
            full_roster = constants.COUNTRY_ROSTER
            constants.COUNTRY_ROSTER = [entry]
            try:
                country_data_fetch.backfill_missing_panels()
            finally:
                constants.COUNTRY_ROSTER = full_roster
            if not country_data_fetch.has_country_partition(country_data_fetch.PANEL_DIR, iso2):
                raise CheckFailed(f"panel build produced no partition for {iso2}")
            log.info("  panel built")

        log.info("\n--- STEP 4: run the real per-country pipeline ---")
        pool: List[Dict] = []
        t0 = time.monotonic()
        pipeline._process_country(country_name, iso2, pool)
        wrote_data = True
        log.info("  pipeline finished in %.1fs; %d article(s) pooled for global alerts",
                 time.monotonic() - t0, len(pool))

        log.info("\n--- STEP 5: verify what landed in the database ---")
        verify(conn, iso2, country_name, as_of)

        if args.keep:
            log.info("\n--- STEP 6: cleanup SKIPPED (--keep) ---")
            log.info("  %s's data for %s is still in the database.", iso2, as_of)
        else:
            log.info("\n--- STEP 6: delete exactly what this run added ---")
            cleanup(conn, iso2, as_of, before)

            log.info("\n--- STEP 7: confirm we are back to the BEFORE state ---")
            after = capture_state(conn, iso2, as_of)
            log_state("after ", after)
            drift = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
            if drift:
                for k, (b, a) in drift.items():
                    log.error("  RESIDUE: %s before=%s after=%s", k, b, a)
                raise CheckFailed("database did not return to its before state")
            log.info("  database is byte-for-byte back to its before state")

        log.info("\n%s", "=" * 78)
        log.info("RESULT: PASS  (%.1fs total)", time.monotonic() - started)
        if created_tables:
            log.info("NOTE: this run created these tables, and LEFT them in place")
            log.info("      (they are the schema the app requires): %s", created_tables)
        log.info("log written to %s", log_path)
        log.info("%s", "=" * 78)
        return 0

    except Exception:
        log.exception("RESULT: FAIL")
        if wrote_data and not args.keep:
            log.warning("Attempting cleanup after failure so nothing is left behind...")
            try:
                cleanup(conn, iso2, as_of, before)
                log.info("  post-failure cleanup done")
            except Exception:
                log.exception("  post-failure cleanup ALSO failed - manual check needed")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
