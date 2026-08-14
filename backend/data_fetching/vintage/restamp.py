"""Re-date the rows already stored, from when we fetched them to when they landed.

Every ``indicator_series`` row written before this module carries ``as_of`` = the
bulk-fetch date, because that is what the fetchers stamp and for the daily run it
is true. It is also, for a backfill, catastrophic and silent:
``data_retrieval._resolve``'s vintage bound drops any observation published after
the anchor, so with a single fetch date on everything **a 2019 snapshot sees zero
rows from this table** — no CPI, no exchange rate, no policy rate, none of the
curated annual friction series. Not an error, not a warning; the payload simply
arrives thinner and nothing says why.

The World Bank parquet panel escapes this because ``_panel_observations`` stamps
each value with its own year end. This module gives ``indicator_series`` the same
property, using the publication lags in :mod:`lags`.

Three things make it safe to run against live data:

* **It dumps first.** Every affected row is written to a CSV under
  ``backend/data/backups/`` before anything changes, and ``revert`` reads that
  file back. A migration over live rows that cannot be undone does not run.
* **It leaves real vintages alone.** Rows stamped ``as-published-edition`` come
  from a WEO edition and already carry a publisher's own release date, which
  beats any estimate here. Only ``as-published-latest`` rows are touched.
* **It checks the invariant before writing**, not after: a row whose new date
  would precede its own period end, or sit more than two years past it, is
  reported and skipped rather than written and found later.

This does change the live path. Today's payload will report a different
``staleness_days`` for old periods — larger, and correct, where it used to say
every value arrived the morning of the last bulk fetch. That is a correction, and
``--diff`` prints its size for one country before anybody commits to it.

Usage:
    python -m backend.util.run restamp --diff      # show, change nothing
    python -m backend.util.run restamp             # dump, then write
    python -m backend.util.run restamp --revert F  # put a dump back
"""

import csv
import datetime
import logging
import pathlib
from typing import Any, Dict, List, Optional, Tuple

from backend.data_upsert import data_push
from backend.data_fetching.vintage import lags

logger = logging.getLogger(__name__)

BACKUP_DIR = (pathlib.Path(__file__).resolve().parents[2]
              / "data" / "backups")

# The scheme a row carries when nothing better is known about its vintage —
# `data_push.upsert_indicator_series`'s own default, and therefore the mark of a
# row stamped with a fetch date. Rows carrying anything else were dated by
# somebody who knew when the value was published.
_FETCH_DATED = "as-published-latest"

_DUMP_COLUMNS = ("country_iso2", "indicator_code", "freq", "period",
                 "value", "as_of", "source", "vintage_scheme")


def read_all() -> List[Dict[str, Any]]:
    """Every ``indicator_series`` row, across every country.

    Deliberately not the pilot roster: this is a live-path migration, and
    re-dating five countries out of forty-eight would leave the table
    disagreeing with itself about what ``as_of`` means.
    """
    with data_push._transaction() as cur:
        cur.execute(data_push._INDICATOR_SERIES_DDL)
        cur.execute(f"SELECT {', '.join(_DUMP_COLUMNS)} FROM indicator_series "
                    "ORDER BY country_iso2, indicator_code, freq, period")
        columns = [c[0] for c in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def plan(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split stored rows into (what to rewrite, what to leave and why).

    A row is rewritten only if it is fetch-dated, its period parses, its new date
    satisfies the invariant, and that date actually differs from the stored one.
    Everything else is returned in the second list with a ``skip_reason``, so the
    caller can print what it is not doing — a migration that silently covers 60%
    of a table reads afterwards as one that covered all of it.
    """
    changed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for row in rows:
        period, freq = str(row.get("period")), str(row.get("freq"))

        if row.get("vintage_scheme") != _FETCH_DATED:
            skipped.append({**row, "skip_reason": f"vintage {row.get('vintage_scheme')}"})
            continue

        stamp = lags.published_on(period, freq, str(row.get("indicator_code") or ""))
        if stamp is None:
            skipped.append({**row, "skip_reason": "unparseable period"})
            continue

        # The stored date is a fetch date, and a fetch date is *proof* the value
        # was public by then. So it caps the estimate: erring long is right until
        # it contradicts something observed. Without this the annual default
        # pushes a 2025 figure to 2026-12-31 — a value claiming to have been
        # published four months from now, which reads as negative staleness in
        # the live payload and is plainly false, since it is already in the
        # table. Same rule as preferring a WEO edition date, applied to the one
        # release-date evidence every row already carries.
        fetched = row.get("as_of")
        if isinstance(fetched, datetime.date) and stamp > fetched:
            stamp = fetched
        if not lags.within_bounds(stamp, period, freq):
            # Unreachable unless the lag table goes negative or a period label
            # parses into the wrong century. Reported rather than trusted.
            skipped.append({**row, "skip_reason": f"invariant: {stamp} vs {period}"})
            continue
        if stamp == row.get("as_of"):
            skipped.append({**row, "skip_reason": "already dated"})
            continue

        changed.append({**row, "as_of": stamp, "vintage_scheme": lags.SCHEME})

    return changed, skipped


def dump(rows: List[Dict[str, Any]], directory: Optional[pathlib.Path] = None
         ) -> pathlib.Path:
    """Write the rows as they stand now, so the migration can be undone.

    Returns the path, which the caller prints. The dump holds the *pre-change*
    values, so reverting is re-upserting the file verbatim.
    """
    directory = directory or BACKUP_DIR
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    path = directory / f"indicator_series_{stamp}.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_DUMP_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def revert(path: pathlib.Path) -> int:
    """Put a dump back, exactly as it was written.

    Reads the CSV, restores the ``as_of`` and ``vintage_scheme`` each row had
    before the migration, and returns the count. The upsert is keyed on
    ``(country, indicator, freq, period)``, so this overwrites in place rather
    than duplicating.
    """
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = [
            {**row,
             "as_of": datetime.date.fromisoformat(row["as_of"]),
             "value": float(row["value"]) if row["value"] else None}
            for row in csv.DictReader(handle)
        ]
    data_push.upsert_indicator_series(rows)
    return len(rows)


def apply(dry_run: bool = False) -> Dict[str, Any]:
    """Dump, then re-date. Returns what happened, for the CLI to print."""
    stored = read_all()
    changed, skipped = plan(stored)

    reasons: Dict[str, int] = {}
    for row in skipped:
        key = str(row["skip_reason"]).split(":")[0]
        reasons[key] = reasons.get(key, 0) + 1

    result: Dict[str, Any] = {"read": len(stored), "changed": len(changed),
                              "skipped": reasons, "backup": None}
    if dry_run or not changed:
        return result

    # The dump carries the rows as they are *now*, so it is taken from `stored`
    # rather than from `changed` — same rows, original dates.
    keys = {(r["country_iso2"], r["indicator_code"], r["freq"], r["period"])
            for r in changed}
    result["backup"] = dump([r for r in stored
                             if (r["country_iso2"], r["indicator_code"],
                                 r["freq"], r["period"]) in keys])
    data_push.upsert_indicator_series(changed)
    return result
