"""Loader for ``backend/data/curated.csv``, the hand-maintained series file.

Some of what the three ledgers need has no free, stable, no-auth API: statutory
tax rates, press freedom scores, informal-economy estimates, reserves. Those
arrive as rows an operator types or pastes into one CSV, documented source by
source in ``backend/README.md`` under "Curated inputs".

The loader's contract is three rules, and the asymmetry between them is the
point:

* **An absent file is silent.** The file is expected to be empty until someone
  fills it. Warning every run would train the operator to ignore the log.
* **Malformed rows are loud** — they raise. A row that is *present* is a row
  someone meant to be used, so a wrong indicator code or an unparseable number is
  a mistake to surface immediately, not to degrade into silently-missing evidence
  that looks exactly like the absent case.
* **A header-only file is neither.** It loads zero rows and is not an error;
  that is the shipped state.

Raising is safe because the pipeline calls this behind a ``try``: the operator
sees the failure in the log and the run still scores. Loud at the source,
isolated at the boundary.

``freq`` and ``source`` are not typed per row — they are read from
``constants.INDICATOR_REGISTRY``, which is already the single source of indicator
truth. Adding a source is one registry entry plus rows in the CSV.

``as_of`` is a column rather than the file's modification time: one file now
carries eleven sources with different refresh cadences, and a single mtime would
stamp a triennial PISA row with the same known-on date as a monthly reserves row.
"""

import csv
import datetime as _dt
import logging
import pathlib
import re
from typing import Any, Dict, List, Optional

from backend.utils import constants

logger = logging.getLogger(__name__)

CURATED_CSV = pathlib.Path(__file__).resolve().parents[2] / "data" / "curated.csv"

_COLUMNS = ["country_iso2", "indicator_code", "period", "value", "as_of"]

_PERIOD_PATTERNS = {
    "A": re.compile(r"^\d{4}$"),
    "Q": re.compile(r"^\d{4}Q[1-4]$"),
    "M": re.compile(r"^\d{4}-(0[1-9]|1[0-2])$"),
}


class CuratedFileError(ValueError):
    """The curated file is present but cannot be trusted.

    Carries the line number and the specific problem so the operator can fix the
    row without reading the loader.
    """


def _roster_iso2() -> set:
    """The ISO-2 codes a curated row may refer to."""
    return {c["iso2"] for c in constants.COUNTRY_ROSTER}


def load_curated_series(path: Optional[pathlib.Path] = None) -> List[Dict[str, Any]]:
    """Load ``curated.csv`` into ``indicator_series`` rows.

    Off-roster countries are skipped with a logged count — a published dataset
    covering 190 countries is not malformed for being wider than this project.
    Everything else wrong raises.

    Args:
        path: the file to read. Defaults to :data:`CURATED_CSV`; overridable so
            tests can point at a fixture.

    Returns:
        Rows ready for ``data_push.upsert_indicator_series``. Empty when the file
        is absent or header-only — the shipped state.

    Raises:
        CuratedFileError: if the file exists and any row is malformed.
    """
    csv_path = path or CURATED_CSV
    if not csv_path.exists():
        return []  # expected until filled; silence is the contract

    try:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = [h.strip() for h in (reader.fieldnames or [])]
            if header != _COLUMNS:
                raise CuratedFileError(
                    f"{csv_path.name}: expected columns {_COLUMNS}, got {header}. "
                    f"See the 'Curated inputs' section of backend/README.md."
                )
            raw_rows = list(reader)
    except OSError as exc:
        raise CuratedFileError(f"{csv_path.name}: cannot read ({exc})") from exc

    roster = _roster_iso2()
    rows: List[Dict[str, Any]] = []
    off_roster = 0

    for line_no, row in enumerate(raw_rows, start=2):  # start=2: line 1 is the header
        iso2 = (row.get("country_iso2") or "").strip().upper()
        code = (row.get("indicator_code") or "").strip()
        period = (row.get("period") or "").strip()
        raw_value = (row.get("value") or "").strip()
        raw_as_of = (row.get("as_of") or "").strip()

        if not any((iso2, code, period, raw_value, raw_as_of)):
            continue  # a trailing blank line is not an error
        if iso2 not in roster:
            off_roster += 1
            continue

        spec = constants.INDICATOR_REGISTRY.get(code)
        if not spec:
            # A typo'd code must not become a silently-absent indicator: nothing
            # downstream would ever ask for it, so nothing would ever complain.
            raise CuratedFileError(
                f"{csv_path.name} line {line_no}: indicator_code {code!r} is not in "
                f"INDICATOR_REGISTRY."
            )

        freq = str(spec["freq"])
        pattern = _PERIOD_PATTERNS[freq]
        if not pattern.match(period):
            raise CuratedFileError(
                f"{csv_path.name} line {line_no}: period {period!r} is not valid for "
                f"{code} at frequency {freq!r} (expected {pattern.pattern})."
            )

        value: Optional[float] = None
        if raw_value:
            try:
                value = float(raw_value)
            except ValueError as exc:
                raise CuratedFileError(
                    f"{csv_path.name} line {line_no}: value {raw_value!r} is not a number."
                ) from exc

        try:
            as_of = _dt.date.fromisoformat(raw_as_of)
        except ValueError as exc:
            raise CuratedFileError(
                f"{csv_path.name} line {line_no}: as_of {raw_as_of!r} is not a "
                f"YYYY-MM-DD date."
            ) from exc

        rows.append({
            "country_iso2": iso2,
            "indicator_code": code,
            "freq": freq,
            "period": period,
            "value": value,          # blank stays NULL: "reported unavailable"
            "as_of": as_of,
            "source": str(spec["source"]),
        })

    if off_roster:
        logger.info("[curated] skipped %d off-roster row(s)", off_roster)
    return rows


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    logging.basicConfig(level=logging.INFO)
    for loaded in load_curated_series():
        print(loaded)
