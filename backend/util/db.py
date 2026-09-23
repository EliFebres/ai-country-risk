"""
Which database, named out loud, every time.

There is no default. `RISK_DB_TARGET` must say `prod` or `dev`, and a command
that does not set it fails before it connects to anything.

That is deliberate and it is the whole point of this module. A bare
`DATABASE_URL` is the variable every tool, notebook and stray script picks up
without being told to, and on this project that bare name pointed at the
database everything had been writing to for weeks. "Which database did this row
come from" should be a question with an answer, and the answer should be in the
row.

Production does not exist yet. `PROD_DATABASE_URL` is declared and blank, so
asking for `prod` fails loudly and names the missing variable rather than
falling back to something that happens to be configured.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

__all__ = [
    "TARGETS",
    "resolve",
    "url_for",
    "where",
    "redact",
    "connect",
    "announce",
    "DbTargetError",
]


# target -> the environment variable that holds its connection string
TARGETS: Dict[str, str] = {
    "prod": "PROD_DATABASE_URL",
    "dev": "DEV_DATABASE_URL",
}

_TARGET_VAR = "RISK_DB_TARGET"


class DbTargetError(RuntimeError):
    """The target is missing, unknown, or has no connection string."""


def resolve() -> str:
    """Return the chosen target, or raise.

    Raises:
        DbTargetError: if `RISK_DB_TARGET` is unset or is not a known target.
    """
    raw = (os.getenv(_TARGET_VAR) or "").strip().lower()
    if not raw:
        raise DbTargetError(
            f"{_TARGET_VAR} is not set. It must be one of "
            f"{', '.join(sorted(TARGETS))} — there is deliberately no default, "
            f"because the default would be whichever database happened to be "
            f"configured."
        )
    if raw not in TARGETS:
        raise DbTargetError(
            f"{_TARGET_VAR}={raw!r} is not a known target. "
            f"Expected one of {', '.join(sorted(TARGETS))}."
        )
    return raw


def url_for(target: Optional[str] = None) -> str:
    """Return the connection string for `target`, or raise.

    Raises:
        DbTargetError: if the target's variable is unset or empty.
    """
    target = target or resolve()
    var = TARGETS[target]
    url = (os.getenv(var) or "").strip()
    if not url:
        raise DbTargetError(
            f"{_TARGET_VAR}={target} but {var} is empty. "
            + (
                "There is no production database yet; set PROD_DATABASE_URL "
                "when there is one."
                if target == "prod"
                else f"Set {var} in backend/.env."
            )
        )
    return url


def where(url: Optional[str] = None) -> Tuple[str, str]:
    """Return ``(host, database)`` for a connection string.

    Credentials are never returned and never logged. This is what goes on every
    ledger row, because two Neon projects with confusable names cost a day once
    and the fix is to make the answer a column rather than a memory.
    """
    parsed = urlparse(url or url_for())
    return (parsed.hostname or "?", (parsed.path or "").lstrip("/") or "?")


def redact(url: Optional[str] = None) -> str:
    """A connection string safe to print: scheme, user, host, database."""
    parsed = urlparse(url or url_for())
    user = f"{parsed.username}@" if parsed.username else ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{user}{parsed.hostname or '?'}{port}/{(parsed.path or '').lstrip('/')}"


@lru_cache(maxsize=1)
def announce() -> str:
    """Log the resolved target once, before anything connects, and return it."""
    target = resolve()
    host, database = where()
    logger.info("database target %s -> %s / %s", target, host, database)
    print(f"[db] target={target}  host={host}  database={database}")
    return target


def connect(target: Optional[str] = None):
    """Open a connection to the resolved target.

    Every connection in the backend comes through here, so there is exactly one
    place that decides which database is being written to.
    """
    import psycopg2

    return psycopg2.connect(url_for(target))
