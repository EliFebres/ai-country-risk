"""Tiny shared datetime formatters.

Only formats used by more than one module live here; API-specific parsers
(FMP's three timestamp formats, IMF SDMX periods) stay in their own modules
because they encode that API's contract, not shared knowledge.
"""

from datetime import datetime, timezone
from typing import Any


def utc_minute_iso(dt: datetime) -> str:
    """Minute-precision UTC timestamp with trailing 'Z' (e.g. 2026-07-26T14:03Z).

    Naive datetimes are treated as UTC. This exact format is the ETL's
    ``generated_at`` contract — ``data_push`` parses it back into ``as_of``.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def date_prefix(value: Any) -> str:
    """Best-effort 'YYYY-MM-DD' from a datetime or ISO string ('' otherwise).

    Used to compress timestamps for LLM prompts, where only the day matters.
    """
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, str):
        return value[:10]
    return ""
