"""Environment, read in one place and validated at startup.

Before v2.0 this happened eight times across six modules, in three different
styles, all of them at import time. They all in fact resolved backend/.env -
``find_dotenv()`` walks up from the *calling file's* directory rather than the
working directory - so centralising it changes which file is read: not at all.
What changes is when a missing key is noticed.

A missing key used to surface at the first call that needed it, which for the
ETL meant inside the per-country ``except`` clause, 57 times, as a caught and
printed error with a successful-looking exit. Now it fails at launch, naming the
key.

Ordering matters and is the reason this is a function rather than an import side
effect: ``data_upsert.data_push`` snapshots ``DATABASE_URL`` into a module-level
constant when it is imported, so the .env must be loaded before that import, and
mutating os.environ afterwards has no effect on it.
"""

import os
import sys
from typing import Iterable, List

from dotenv import load_dotenv

from backend.util import paths

#: The .env the project has always used. Named, not searched for.
ENV_FILE = paths.BACKEND_DIR / ".env"

#: Without these a command cannot do its work at all.
#:
#: `RISK_DB_TARGET` rather than a connection string: the URL itself is resolved
#: by `util.db` from the chosen target, and there is deliberately no default. A
#: bare `DATABASE_URL` is the variable every tool and stray script picks up
#: without being told to, and on this project that bare name pointed at the
#: database everything had been writing to for weeks.
REQUIRED = {
    "etl": ("RISK_DB_TARGET", "OPENAI_API_KEY", "FMP_API_KEY"),
    "prices": ("RISK_DB_TARGET", "FMP_API_KEY"),
    "run": ("RISK_DB_TARGET", "OPENAI_API_KEY", "FMP_API_KEY"),
    "bootstrap": ("RISK_DB_TARGET",),
    "report": ("RISK_DB_TARGET",),
}

#: Absent, these degrade a feature rather than stopping the run. Reported, not enforced.
OPTIONAL = ("CRAWLBASE_JS_TOKEN", "CRAWLBASE_TOKEN")


def load() -> None:
    """Load backend/.env without overriding anything already exported."""
    load_dotenv(ENV_FILE, override=False)


def missing(names: Iterable[str]) -> List[str]:
    return [n for n in names if not (os.getenv(n) or "").strip()]


def require(command: str) -> None:
    """Fail at launch, naming every missing key at once.

    Naming all of them matters: discovering three missing keys one run at a time
    is three failed runs.
    """
    absent = missing(REQUIRED.get(command, ()))
    if absent:
        raise SystemExit(
            f"{command}: missing required environment variable(s): "
            f"{', '.join(absent)}\n"
            f"Set them in {ENV_FILE} (see backend/.env.example) or export them."
        )


def redact_database_url(url: str | None) -> str:
    """A DSN safe to log: scheme://user@host/dbname, never the password.

    The target is worth printing on every run - two databases with confusable
    names cost the old branch a day - but the credential is not.
    """
    if not url:
        return "<unset>"
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        user = f"{parts.username}@" if parts.username else ""
        host = parts.hostname or "?"
        port = f":{parts.port}" if parts.port else ""
        name = parts.path.lstrip("/") or "?"
        return f"{parts.scheme}://{user}{host}{port}/{name}"
    except Exception:
        return "<unparseable>"


def describe_optional() -> str:
    """Which optional tokens are present, for the startup line."""
    present = [n for n in OPTIONAL if (os.getenv(n) or "").strip()]
    return ", ".join(present) if present else "none"


def announce(command: str, project_root) -> None:
    """Say what was resolved, before doing any work.

    The database line names the target and the database it resolved to, because
    "which database did this write to" should be answerable from the run output
    rather than from memory.
    """
    from backend.util import db

    print(f"[main] command      : {command}")
    print(f"[main] project root : {project_root}")
    try:
        print(f"[main] db target    : {db.resolve()}")
        print(f"[main] database     : {db.redact()}")
    except db.DbTargetError as exc:
        raise SystemExit(f"[main] {exc}")
    print(f"[main] env file     : {ENV_FILE} ({'found' if ENV_FILE.exists() else 'MISSING'})")
    print(f"[main] optional keys: {describe_optional()}")


def utf8_console() -> None:
    """Make stdout UTF-8 regardless of the platform's default codepage.

    A Windows console defaults to cp1252, so a single non-ASCII character in a
    progress line — an arrow, an accented country name — raises
    UnicodeEncodeError and takes the whole run down with it. The run should not
    be able to fail on a print.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
