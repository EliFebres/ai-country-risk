"""Two Neon projects, one accessor each, roles fixed by the function name.

    prod_readonly()  ->  PROD_DATABASE_URL  production, READ-ONLY, enforced
    dev()            ->  DEV_DATABASE_URL   dev project, read-write

The split exists so that "let me just look at prod" cannot become "I just
wrote to prod". ``prod_readonly`` opens every transaction as ``BEGIN READ
ONLY``, so Postgres rejects an INSERT/UPDATE/DELETE/DDL server-side rather
than trusting the caller to have meant it. Per-transaction rather than a
session GUC on purpose — Neon's pooled endpoint recycles sessions and a plain
``SET`` would not survive that. ``dev()`` has no such guard: the scratch
project is yours to break.

This module is for humans — notebooks, one-off queries, exploration. The daily
pipeline does not import it. Production writes still go through
:mod:`backend.data_upsert.data_push`, which owns the real write layer and is
the only thing that should ever write to production.

``DATABASE_URL`` used to be the production name, and it is gone. The argument
for keeping it was that renaming here would rename it in the frontend, which
reads ``DATABASE_URL`` in ``frontend/app/lib/risk-server.ts`` — but that is a
different process reading a different ``frontend/.env`` with its own
deployment environment, so the two names were never the same variable. What
the shared name did buy was a default: every tool that wanted "the database"
reached for the bare name without deciding anything, and what it reached was
production. A session went into establishing which of these two projects held
the scores and which held the corpus, because that choice had never been
written down anywhere. ``RISK_DB_TARGET`` now has to be said out loud.

Usage::

    from backend.util.db import dev, prod_readonly

    with prod_readonly() as cur:              # look, never touch
        cur.execute("SELECT iso2, score FROM country_snapshot LIMIT 5")

    with dev() as cur:                        # scribble freely
        cur.execute("CREATE TABLE experiment (x int)")

Check both strings with ``python -m backend.util.db``.
"""

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg2

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _require(var: str, hint: str) -> str:
    """Read a connection string at call time (never cached at import)."""
    url = os.getenv(var)
    if not url:
        raise RuntimeError(f"{var} is not set in the environment. {hint}")
    return url


@contextmanager
def _cursor(url: str, readonly: bool) -> Iterator["psycopg2.extensions.cursor"]:
    """One connection, one transaction, closed on the way out.

    Read-only blocks always roll back — there is nothing to commit, and a
    rollback is the honest end to a transaction that was never allowed to
    change anything. Read-write blocks commit on clean exit and roll back on
    any exception, matching ``data_push._transaction``.
    """
    conn = psycopg2.connect(url)
    try:
        conn.set_session(readonly=readonly, autocommit=False)
        with conn.cursor() as cur:
            yield cur
        conn.rollback() if readonly else conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def prod_readonly() -> Iterator["psycopg2.extensions.cursor"]:
    """Cursor on PRODUCTION that Postgres will not let you write through."""
    url = _require(
        "PROD_DATABASE_URL",
        "It is the production Neon project; see backend/.env.example.",
    )
    with _cursor(url, readonly=True) as cur:
        yield cur


@contextmanager
def dev() -> Iterator["psycopg2.extensions.cursor"]:
    """Cursor on the DEVELOPMENT project. Read-write; break what you like."""
    url = _require(
        "DEV_DATABASE_URL",
        "Paste the second Neon project's string into backend/.env.",
    )
    if url == os.getenv("PROD_DATABASE_URL"):
        raise RuntimeError(
            "DEV_DATABASE_URL is the same database as PROD_DATABASE_URL. This "
            "handle is read-write — point it at the scratch project, not "
            "production."
        )
    with _cursor(url, readonly=False) as cur:
        yield cur


def _describe(label: str, cur) -> None:
    cur.execute("SELECT current_database()")
    cur.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
        "ORDER BY tablename"
    )
    tables = [r[0] for r in cur.fetchall()]
    print(f"{label}: {len(tables)} public table(s): {', '.join(tables) or '(none)'}")


def _self_check() -> None:
    """Prove each string reaches its database and that prod refuses writes."""
    with prod_readonly() as cur:
        _describe("prod  (read-only)", cur)
    try:
        with prod_readonly() as cur:
            cur.execute("CREATE TABLE db_write_probe (x int)")
    except psycopg2.errors.ReadOnlySqlTransaction:
        print("prod  (read-only): write blocked by Postgres — correct")
    else:
        raise AssertionError("a CREATE TABLE succeeded on prod — NOT read-only")

    if not os.getenv("DEV_DATABASE_URL"):
        print("dev   (read-write): DEV_DATABASE_URL unset, skipped")
        return
    with dev() as cur:
        _describe("dev   (read-write)", cur)
    with dev() as cur:
        cur.execute("CREATE TEMP TABLE db_write_probe (x int)")
    print("dev   (read-write): write accepted — correct")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / "backend" / ".env")
    load_dotenv()
    _self_check()
