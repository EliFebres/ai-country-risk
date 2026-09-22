"""
Read the hand-acquired series that no API serves.

Two indicators in the registry have no machine-readable feed anyone can poll:
RSF's World Press Freedom Index and the OECD's PISA means. Both are published as
tables on a page, once a year and once every three years. They are the reason
the `information` and `edge` ledgers are not one-indicator ledgers, so leaving
them out is not a small omission — it is two of four ledgers scoring on almost
nothing while the registry claims otherwise.

They live in `backend/data/curated.csv`, one row per (country, indicator,
period), and **every row carries its own citation**: the URL, the table it came
from, and the date it was read. Written at the time of acquisition, not
reconstructed later — a number whose provenance is remembered rather than
recorded is a number nobody can check.

The contract:

* An absent file is silent. A country with no curated rows still scores; the
  census says which indicators were missing and why.
* A malformed row raises. A silently skipped row is how a file that loads
  "successfully" ends up holding half the data someone thought they put in it.
* `freq` and `source` are NOT in the file. They come from the registry, so a
  CSV row cannot quietly disagree with the registry about what it is.
"""

from __future__ import annotations

import csv
import datetime as dt
from typing import Any, Dict, Optional

from backend.util import constants, paths

__all__ = ["CURATED_CSV", "CuratedFileError", "load_curated", "load_for_country"]


CURATED_CSV = paths.DATA_DIR / "curated.csv"

REQUIRED_COLUMNS = (
    "country_iso2",
    "indicator_code",
    "period",
    "value",
    "as_of",
    "source_url",
    "source_table",
    "retrieved_at",
)


class CuratedFileError(ValueError):
    """The curated file exists but a row in it cannot be trusted."""


def _fail(line_no: int, message: str) -> None:
    raise CuratedFileError(f"{CURATED_CSV.name} line {line_no}: {message}")


def load_curated(path: Optional[Any] = None) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Load every curated row, keyed by country then indicator code.

    Returns:
        ``{iso2: {code: {"value", "period", "as_of", "series", "source_url",
        "source_table", "retrieved_at"}}}``. Empty if the file is absent or
        holds only a header.

    Raises:
        CuratedFileError: on a malformed row, an unknown indicator code, or a
            code the registry does not mark as curated.
    """
    path = path or CURATED_CSV
    try:
        handle = open(path, "r", encoding="utf-8", newline="")
    except FileNotFoundError:
        return {}

    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    with handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise CuratedFileError(
                f"{path} is missing column(s): {', '.join(missing)}"
            )

        for line_no, row in enumerate(reader, start=2):
            if not any((v or "").strip() for v in row.values()):
                continue

            iso2 = (row["country_iso2"] or "").strip().upper()
            code = (row["indicator_code"] or "").strip()

            if not iso2:
                _fail(line_no, "no country_iso2")
            if code not in constants.INDICATOR_REGISTRY:
                _fail(line_no, f"{code!r} is not in INDICATOR_REGISTRY")
            if code not in constants.CURATED_CODES:
                _fail(
                    line_no,
                    f"{code!r} is fetched from {constants.INDICATOR_REGISTRY[code]['source']}, "
                    f"not curated — a curated row would shadow the real source",
                )

            try:
                period = int(str(row["period"]).strip())
            except (TypeError, ValueError):
                _fail(line_no, f"period {row['period']!r} is not a year")
            try:
                value = float(str(row["value"]).strip())
            except (TypeError, ValueError):
                _fail(line_no, f"value {row['value']!r} is not a number")
            try:
                as_of = dt.date.fromisoformat(str(row["as_of"]).strip())
            except (TypeError, ValueError):
                _fail(line_no, f"as_of {row['as_of']!r} is not an ISO date")

            if not (row["source_url"] or "").strip():
                _fail(line_no, "no source_url — a curated number without a citation "
                               "is a number nobody can check")

            slot = out.setdefault(iso2, {})
            existing = slot.get(code)
            series = existing["series"] if existing else {}
            series[period] = value

            # The freshest period wins, so a file that grows a new year does not
            # need its older rows removed.
            if existing is None or period >= existing["period"]:
                slot[code] = {
                    "value": value,
                    "period": period,
                    "as_of": as_of.isoformat(),
                    "series": series,
                    "source_url": (row["source_url"] or "").strip(),
                    "source_table": (row["source_table"] or "").strip(),
                    "retrieved_at": (row["retrieved_at"] or "").strip(),
                }
            else:
                existing["series"] = series

    return out


def load_for_country(iso2: str, path: Optional[Any] = None) -> Dict[str, Dict[str, Any]]:
    """Curated rows for one country, in the shape the payload builder wants."""
    return load_curated(path).get((iso2 or "").upper(), {})
