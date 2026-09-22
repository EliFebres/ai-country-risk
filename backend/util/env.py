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
from typing import Iterable, List

from dotenv import load_dotenv

from backend.util import paths

#: The .env the project has always used. Named, not searched for.
ENV_FILE = paths.BACKEND_DIR / ".env"

#: Without these a command cannot do its work at all.
REQUIRED = {
    "etl": ("DATABASE_URL", "OPENAI_API_KEY", "FMP_API_KEY"),
    "prices": ("DATABASE_URL", "FMP_API_KEY"),
    "run": ("DATABASE_URL", "OPENAI_API_KEY", "FMP_API_KEY"),
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
