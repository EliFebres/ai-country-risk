"""
The durable record: evidence in, model output cached, the week written down.

This module owns the tables a reader in three years needs — `article`,
`llm_artifact`, `indicator_series`, `risk_snapshot`, `run_ledger`,
`snapshot_diagnostic`, `country_outlook` and `watch_item`. `data_push` keeps
the dashboard-facing feeds (prices, alerts, calendar) and the roster seed,
because those have a different lifetime: they are overwritten every tick and
nobody will ever reconstruct one. The calendar is read here all the same,
because the watch list needs the rows as they stood on a date.

Two rules are enforced here rather than left to convention:

**A snapshot without a manifest is refused.** The manifest is what makes a score
comparable with another score — the evidence it was built from and the version
of everything that could have changed the answer. A row without one is a number
with no provenance, and the write path will not create one.

**Stamps are immutable.** Once a row records which prompt, which models, which
commit and which payload produced it, a later write cannot change those. It is
refused and both values are logged. The old branch lost its ability to detect a
stale reference because a later commit quietly restamped a `git_sha`, and by the
time anyone noticed there was no way to tell which rows were affected.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import socket
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg2.extras as extras

from backend.data_upsert import schema
from backend.util import db

logger = logging.getLogger(__name__)

__all__ = [
    "ensure_schema",
    "read_artifacts",
    "write_artifacts",
    "upsert_articles",
    "read_articles",
    "read_articles_by_hash",
    "upsert_indicator_series",
    "read_indicator_series",
    "read_first_seen",
    "upsert_snapshot",
    "read_snapshot",
    "read_recent_snapshots",
    "read_snapshot_series",
    "read_roster_scores",
    "first_snapshot_on_or_after",
    "write_ledger",
    "read_ledger",
    "read_latest_ledger",
    "write_diagnostic",
    "read_diagnostics",
    "read_latest_artifact",
    "write_outlook",
    "read_outlook",
    "read_latest_outlook",
    "read_recent_outlooks",
    "read_all_outlooks",
    "upsert_watch_items",
    "read_watch_items",
    "resolve_watch_item",
    "update_watch_importance",
    "mark_watch_shown",
    "read_calendar_events",
    "read_calendar_rows_by_id",
    "dump_table_csv",
    "StampConflict",
]


def _json(value: Any) -> extras.Json:
    """A JSONB parameter that survives dates.

    Manifests and censuses carry `as_of` and `period` as `datetime.date`, and
    psycopg2's default encoder raises on them. The first week-one run lost every
    country it reached to exactly that, after the model had been paid. `str` is
    what the rest of the codebase already uses when it serialises a payload.
    """
    return extras.Json(value, dumps=lambda v: json.dumps(v, default=str))


class StampConflict(RuntimeError):
    """A write would have changed a field that records how a row was produced."""


def ensure_schema() -> Dict[str, bool]:
    """Provision the ten tables, then confirm they exist."""
    conn = db.connect()
    try:
        return schema.bootstrap(conn)
    finally:
        conn.close()


# --- The model-output cache -------------------------------------------------


def read_artifacts(
    hashes: Sequence[str],
    *,
    kind: str,
    version: str,
    mode: str = "named",
    country_iso2: str = "",
) -> Dict[str, Any]:
    """Cached payloads for `hashes`, keyed by hash.

    A miss is an absent key, never a null value: "we have no answer" and "the
    model answered null" are different facts, and collapsing them makes a failed
    call look like a cached one.
    """
    wanted = [h for h in dict.fromkeys(hashes) if h]
    if not wanted:
        return {}

    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT content_sha256, payload
                  FROM llm_artifact
                 WHERE kind = %s AND version = %s AND mode = %s
                   AND country_iso2 = %s AND content_sha256 = ANY(%s)
                """,
                (kind, version, mode, country_iso2, wanted),
            )
            return {r[0]: r[1] for r in cur.fetchall()}
    finally:
        conn.close()


def write_artifacts(
    rows: Iterable[Tuple[str, Any]],
    *,
    kind: str,
    version: str,
    model: str,
    mode: str = "named",
    country_iso2: str = "",
) -> int:
    """Cache model output. Returns the number of rows offered.

    `ON CONFLICT DO NOTHING`: the first answer for a given key wins. Two runs
    racing on one article must not be able to disagree about what the cache
    holds, and a later overwrite would silently change the evidence behind a
    score that was already written.
    """
    batch: List[Tuple] = [
        (h, kind, version, mode, country_iso2, model, _json(payload))
        for h, payload in rows
        if h
    ]
    if not batch:
        return 0

    conn = db.connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            extras.execute_values(
                cur,
                """
                INSERT INTO llm_artifact
                  (content_sha256, kind, version, mode, country_iso2, model, payload)
                VALUES %s
                ON CONFLICT (content_sha256, kind, version, mode, country_iso2)
                DO NOTHING
                """,
                batch,
            )
        conn.commit()
        return len(batch)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- The article corpus -----------------------------------------------------

_ARTICLE_COLUMNS = (
    "url", "content_sha256", "wrapper_url", "source_system", "publisher",
    "published_at", "page_published_at", "title", "abstract", "body",
    "body_chars_original", "body_clipped", "harvested_at", "created_at",
)
_ARTICLE_WRITTEN = _ARTICLE_COLUMNS[:-2]


def upsert_articles(rows: Iterable[Dict[str, Any]]) -> int:
    """Store articles, keyed by resolved URL and the hash of the body read.
    Returns the number offered.

    A body is never overwritten. The same URL with a different body is a new
    row, so the text an earlier manifest points at is still there when that
    week is reconstructed. On a repeat of the same (url, hash) only the
    metadata is refreshed, and only where the new row knows something.
    """
    batch: List[Tuple] = []
    seen = set()
    for r in rows:
        if not r.get("url"):
            continue
        r = {**r, "content_sha256": r.get("content_sha256") or ""}
        key = (r["url"], r["content_sha256"])
        if key in seen:
            continue  # one statement cannot touch the same row twice
        seen.add(key)
        batch.append(tuple(r.get(c) for c in _ARTICLE_WRITTEN))
    if not batch:
        return 0

    conn = db.connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            extras.execute_values(
                cur,
                f"""
                INSERT INTO article ({", ".join(_ARTICLE_WRITTEN)})
                VALUES %s
                ON CONFLICT (url, content_sha256) DO UPDATE SET
                    title             = COALESCE(EXCLUDED.title, article.title),
                    abstract          = COALESCE(EXCLUDED.abstract, article.abstract),
                    publisher         = COALESCE(EXCLUDED.publisher, article.publisher),
                    page_published_at = COALESCE(EXCLUDED.page_published_at, article.page_published_at)
                """,
                batch,
            )
        conn.commit()
        return len(batch)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _select_articles(where: str, params: Tuple) -> List[Dict[str, Any]]:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_ARTICLE_COLUMNS)} FROM article WHERE {where}",
                params,
            )
            return [dict(zip(_ARTICLE_COLUMNS, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def read_articles(urls: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """The latest stored version of each URL, preferring one with a body.

    Keyed by URL. The list of URLs comes from a manifest or a census — the
    article table no longer knows which country a story was evidence for.
    """
    urls = sorted({u for u in urls if u})
    if not urls:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for row in _select_articles("url = ANY(%s)", (urls,)):
        have = out.get(row["url"])
        rank = (row["body"] is not None, row["created_at"])
        if have is None or rank > (have["body"] is not None, have["created_at"]):
            out[row["url"]] = row
    return out


def read_articles_by_hash(hashes: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """Stored bodies keyed by the hash of the text that was read.

    What reconstruction uses: a manifest names the hash of each body the model
    read, and that exact text is what has to still exist.
    """
    hashes = sorted({h for h in hashes if h})
    if not hashes:
        return {}
    return {
        row["content_sha256"]: row
        for row in _select_articles("content_sha256 = ANY(%s)", (hashes,))
    }


# --- The macro record -------------------------------------------------------


def upsert_indicator_series(rows: Iterable[Dict[str, Any]]) -> int:
    """Store macro observations. Returns the number offered.

    `ON CONFLICT DO NOTHING`: `as_of` is in the key, so a revision arrives as a
    new row rather than overwriting the old one. The series keeps its own
    history, and a score stays re-readable against the data as it stood rather
    than as it was later corrected.
    """
    batch: List[Tuple] = []
    for r in rows:
        if r.get("value") is None or not r.get("indicator_code"):
            continue
        batch.append((
            r["country_iso2"], r["indicator_code"], int(r["period"]), r["as_of"],
            r["as_of_scheme"], float(r["value"]), r.get("unit"), r["source"],
            r["ledger"], r.get("freq", "A"),
        ))
    if not batch:
        return 0

    conn = db.connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            extras.execute_values(
                cur,
                """
                INSERT INTO indicator_series
                  (country_iso2, indicator_code, period, as_of, as_of_scheme,
                   value, unit, source, ledger, freq)
                VALUES %s
                ON CONFLICT (country_iso2, indicator_code, period, as_of)
                DO NOTHING
                """,
                batch,
            )
        conn.commit()
        return len(batch)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_indicator_series(country_iso2: str, code: Optional[str] = None) -> List[Dict[str, Any]]:
    """Stored observations for a country, newest period first."""
    cols = ("country_iso2", "indicator_code", "period", "as_of", "as_of_scheme",
            "value", "unit", "source", "ledger", "freq")
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {", ".join(cols)} FROM indicator_series
                 WHERE country_iso2 = %s AND (%s::text IS NULL OR indicator_code = %s)
                 ORDER BY indicator_code, period DESC, as_of DESC
                """,
                (country_iso2, code, code),
            )
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def read_first_seen(country_iso2: str) -> Dict[tuple, dt.date]:
    """The first day each stored value was held, keyed as `payload.seen_key`.

    Having fetched a value proves it was published by then, so this caps a
    publication-lag estimate that would land later. Keyed on the value, so a
    revision is first seen when it arrived.
    """
    from backend.llm.payload import seen_key

    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT indicator_code, period, value, (MIN(created_at) AT TIME ZONE 'UTC')::date
                  FROM indicator_series WHERE country_iso2 = %s
                 GROUP BY indicator_code, period, value
                """,
                (country_iso2,),
            )
            return {seen_key(c, p, v): d for c, p, v, d in cur.fetchall()}
    finally:
        conn.close()


# --- The product ------------------------------------------------------------

_SNAPSHOT_COLUMNS = (
    "country_iso2", "run_date", "score_12m", "score_3m", "friction_score",
    "order_score", "information_score", "edge_score", "condition_flags",
    "bullet_summary", "subscore_evidence", "article_scores", "top_articles",
    "evidence_coverage", "coverage_components", "payload_fingerprint",
    "prompt_version", "scoring_model", "digest_model", "gate_model", "seed",
    "git_sha", "payload_tokens", "restricted_badge", "manifest",
)

_JSON_COLUMNS = {
    "condition_flags", "subscore_evidence", "article_scores", "top_articles",
    "coverage_components", "restricted_badge", "manifest",
}


def _check_stamps_on(
    cur, *, table: str, key: Dict[str, Any], fields: Sequence[str], incoming: Dict[str, Any],
) -> None:
    """Refuse a write that would change how an existing row of `table` was produced.

    `key` is the row's primary key as column -> value; `fields` the stamped
    columns. Shared by the snapshot and the outlook, which have different
    stamps but the same rule.

    Raises:
        StampConflict: naming the field and both values.
    """
    where = " AND ".join(f"{c} = %s" for c in key)
    cur.execute(
        f"SELECT {', '.join(fields)} FROM {table} WHERE {where}",
        tuple(key.values()),
    )
    row = cur.fetchone()
    if not row:
        return

    label = " ".join(str(v) for v in key.values())
    for field, existing in zip(fields, row):
        if existing is None:
            continue  # a stamp may be filled in later; it may not be altered
        new = incoming.get(field)
        if new is not None and new != existing:
            logger.error(
                "stamp conflict on %s %s: %s is %r, refusing to overwrite with %r",
                table, label, field, existing, new,
            )
            raise StampConflict(
                f"{label}: {field} is already {existing!r} and "
                f"the write would set it to {new!r}. Stamps record how a row was "
                f"produced and are not rewritable; a new run writes a new row."
            )


def _check_stamps(cur, country_iso2: str, run_date: Any, incoming: Dict[str, Any]) -> None:
    """The snapshot's stamp check. See `_check_stamps_on`."""
    _check_stamps_on(
        cur, table="risk_snapshot",
        key={"country_iso2": country_iso2, "run_date": run_date},
        fields=schema.STAMPED_FIELDS, incoming=incoming,
    )


def upsert_snapshot(row: Dict[str, Any]) -> None:
    """Write one country's score for one run.

    Raises:
        ValueError: if the manifest is missing. A score with no record of the
            evidence and versions behind it is not a valid row.
        StampConflict: if the write would alter an existing row's provenance.
    """
    if not row.get("manifest"):
        raise ValueError(
            f"{row.get('country_iso2')}: refusing to write a snapshot with no "
            f"manifest. The manifest is what makes the score comparable with "
            f"another score."
        )
    for required in ("country_iso2", "run_date", "score_12m", "score_3m"):
        if row.get(required) is None:
            raise ValueError(f"snapshot is missing {required}")

    values = []
    for col in _SNAPSHOT_COLUMNS:
        v = row.get(col)
        values.append(_json(v) if col in _JSON_COLUMNS and v is not None else v)

    assignments = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in _SNAPSHOT_COLUMNS
        if c not in ("country_iso2", "run_date")
    )

    conn = db.connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            _check_stamps(cur, row["country_iso2"], row["run_date"], row)
            cur.execute(
                f"""
                INSERT INTO risk_snapshot ({", ".join(_SNAPSHOT_COLUMNS)})
                VALUES ({", ".join(["%s"] * len(_SNAPSHOT_COLUMNS))})
                ON CONFLICT (country_iso2, run_date) DO UPDATE SET {assignments}
                """,
                values,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_snapshot(country_iso2: str, run_date: Any) -> Optional[Dict[str, Any]]:
    """One snapshot back, whole. The consumer side of `upsert_snapshot`."""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {", ".join(_SNAPSHOT_COLUMNS)} FROM risk_snapshot
                 WHERE country_iso2 = %s AND run_date = %s
                """,
                (country_iso2, run_date),
            )
            r = cur.fetchone()
            return dict(zip(_SNAPSHOT_COLUMNS, r)) if r else None
    finally:
        conn.close()


def read_recent_snapshots(
    country_iso2: str, *, before: Any = None, limit: int = 4
) -> List[Dict[str, Any]]:
    """A country's own recent runs, newest first.

    What the quality report compares against. The baseline is the country's own
    history rather than an absolute floor: Taiwan resolving zero indicators is
    expected because Taiwan always resolved zero, while Portugal going from
    twenty to twelve is a source break, and no fixed threshold tells those apart.
    """
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT run_date, evidence_coverage, manifest
                  FROM risk_snapshot
                 WHERE country_iso2 = %s
                   AND (%s::date IS NULL OR run_date < %s::date)
                 ORDER BY run_date DESC LIMIT %s
                """,
                (country_iso2, before, before, limit),
            )
            return [
                {"run_date": r[0], "evidence_coverage": r[1], "manifest": r[2]}
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


_SERIES_COLUMNS = (
    "country_iso2", "run_date", "score_12m", "score_3m", "friction_score",
    "order_score", "information_score", "edge_score", "evidence_coverage",
)


def read_snapshot_series(
    country_iso2: str, *, since: Any = None, until: Any = None
) -> List[Dict[str, Any]]:
    """A country's scores over a window, oldest first, without the manifests.

    What the outlook's trend block is computed from. `until` is inclusive and
    is how a backfill stays honest: nothing after the as-of date is returned.
    """
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {", ".join(_SERIES_COLUMNS)} FROM risk_snapshot
                 WHERE country_iso2 = %s
                   AND (%s::date IS NULL OR run_date >= %s::date)
                   AND (%s::date IS NULL OR run_date <= %s::date)
                 ORDER BY run_date
                """,
                (country_iso2, since, since, until, until),
            )
            return [dict(zip(_SERIES_COLUMNS, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def read_roster_scores(*, until: Any = None) -> List[Dict[str, Any]]:
    """Every country's scores up to `until`, for the base rates."""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {", ".join(_SERIES_COLUMNS)} FROM risk_snapshot
                 WHERE (%s::date IS NULL OR run_date <= %s::date)
                 ORDER BY country_iso2, run_date
                """,
                (until, until),
            )
            return [dict(zip(_SERIES_COLUMNS, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def first_snapshot_on_or_after(country_iso2: str, target: Any) -> Optional[Dict[str, Any]]:
    """The first snapshot at or after `target`, or None. How a forecast matures."""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {", ".join(_SERIES_COLUMNS)} FROM risk_snapshot
                 WHERE country_iso2 = %s AND run_date >= %s
                 ORDER BY run_date LIMIT 1
                """,
                (country_iso2, target),
            )
            r = cur.fetchone()
            return dict(zip(_SERIES_COLUMNS, r)) if r else None
    finally:
        conn.close()


# --- The forecast -----------------------------------------------------------

_OUTLOOK_COLUMNS = (
    "country_iso2", "run_date", "horizon_4w", "horizon_12w", "ledger_direction",
    "drivers", "base_case", "upside_risk", "downside_risk", "headed_text",
    "confidence", "key_uncertainties", "changes_since_last",
    "outlook_prompt_version", "outlook_model", "git_sha", "payload_fingerprint",
    "lessons_version", "inputs_fingerprint", "payload_tokens", "manifest",
)

_OUTLOOK_JSON = {
    "horizon_4w", "horizon_12w", "ledger_direction", "drivers",
    "key_uncertainties", "manifest",
}


def write_outlook(row: Dict[str, Any]) -> None:
    """Write one country's forecast for one run date.

    Raises:
        ValueError: if the manifest or a scoreable horizon is missing.
        StampConflict: if the write would alter an existing row's provenance.
    """
    if not row.get("manifest"):
        raise ValueError(
            f"{row.get('country_iso2')}: refusing to write an outlook with no "
            f"manifest. A forecast with no record of what it was made from "
            f"cannot be graded."
        )
    for required in ("country_iso2", "run_date", "horizon_4w", "horizon_12w",
                     "ledger_direction", "drivers"):
        if row.get(required) is None:
            raise ValueError(f"outlook is missing {required}")

    values = []
    for col in _OUTLOOK_COLUMNS:
        v = row.get(col)
        values.append(_json(v) if col in _OUTLOOK_JSON and v is not None else v)
    assignments = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in _OUTLOOK_COLUMNS
        if c not in ("country_iso2", "run_date")
    )

    conn = db.connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            _check_stamps_on(
                cur, table="country_outlook",
                key={"country_iso2": row["country_iso2"], "run_date": row["run_date"]},
                fields=schema.OUTLOOK_STAMPED_FIELDS, incoming=row,
            )
            cur.execute(
                f"""
                INSERT INTO country_outlook ({", ".join(_OUTLOOK_COLUMNS)})
                VALUES ({", ".join(["%s"] * len(_OUTLOOK_COLUMNS))})
                ON CONFLICT (country_iso2, run_date) DO UPDATE SET {assignments}
                """,
                values,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _select_outlooks(where: str, params: Tuple, order: str = "run_date DESC", limit: Optional[int] = None) -> List[Dict[str, Any]]:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_OUTLOOK_COLUMNS)} FROM country_outlook "
                f"WHERE {where} ORDER BY {order}"
                + (f" LIMIT {int(limit)}" if limit else ""),
                params,
            )
            return [dict(zip(_OUTLOOK_COLUMNS, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def read_outlook(country_iso2: str, run_date: Any) -> Optional[Dict[str, Any]]:
    """One forecast back, whole."""
    rows = _select_outlooks("country_iso2 = %s AND run_date = %s", (country_iso2, run_date))
    return rows[0] if rows else None


def read_latest_outlook(country_iso2: str, *, before: Any = None) -> Optional[Dict[str, Any]]:
    """The newest forecast strictly before `before` (or the newest at all)."""
    rows = _select_outlooks(
        "country_iso2 = %s AND (%s::date IS NULL OR run_date < %s::date)",
        (country_iso2, before, before), limit=1,
    )
    return rows[0] if rows else None


def read_recent_outlooks(country_iso2: str, *, before: Any = None, limit: int = 4) -> List[Dict[str, Any]]:
    """A country's recent forecasts, newest first."""
    return _select_outlooks(
        "country_iso2 = %s AND (%s::date IS NULL OR run_date < %s::date)",
        (country_iso2, before, before), limit=limit,
    )


def read_all_outlooks(*, until: Any = None) -> List[Dict[str, Any]]:
    """Every forecast made on or before `until`, oldest first. The ledger's input."""
    return _select_outlooks(
        "(%s::date IS NULL OR run_date <= %s::date)", (until, until),
        order="run_date, country_iso2",
    )


# --- The watch list ---------------------------------------------------------

_WATCH_COLUMNS = (
    "country_iso2", "item_id", "kind", "title", "due_date", "window_end",
    "question", "expected_direction", "importance", "source",
    "first_seen_run_date", "last_seen_run_date", "seen_count", "ever_shown",
    "status", "resolved_run_date", "resolution_note", "resolution_evidence",
    "superseded_by",
)
_WATCH_INSERTED = (
    "country_iso2", "item_id", "kind", "title", "due_date", "window_end",
    "question", "expected_direction", "importance", "source",
    "first_seen_run_date", "last_seen_run_date",
)


def upsert_watch_items(rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Insert new items and re-see existing ones. Returns inserted and re-seen counts.

    On a hit the item's `last_seen_run_date`, `seen_count`, `importance` and
    `source` move; nothing else does, and a closed item is left closed. The
    model proposing an item that already happened does not reopen it.
    """
    batch: List[Tuple] = []
    seen = set()
    for r in rows:
        key = (r["country_iso2"], r["item_id"])
        if key in seen:
            continue
        seen.add(key)
        batch.append(tuple(
            _json(r.get(c)) if c == "source" and r.get(c) is not None else r.get(c)
            for c in _WATCH_INSERTED
        ))
    if not batch:
        return {"inserted": 0, "reseen": 0}

    conn = db.connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            got = extras.execute_values(
                cur,
                f"""
                INSERT INTO watch_item ({", ".join(_WATCH_INSERTED)})
                VALUES %s
                ON CONFLICT (country_iso2, item_id) DO UPDATE SET
                    last_seen_run_date = GREATEST(watch_item.last_seen_run_date, EXCLUDED.last_seen_run_date),
                    -- A week re-run is the same sighting, not a second one.
                    seen_count         = CASE WHEN watch_item.last_seen_run_date < EXCLUDED.last_seen_run_date
                                              THEN watch_item.seen_count + 1 ELSE watch_item.seen_count END,
                    importance         = CASE WHEN watch_item.status = 'open'
                                              THEN EXCLUDED.importance ELSE watch_item.importance END,
                    source             = CASE WHEN watch_item.status = 'open'
                                              THEN EXCLUDED.source ELSE watch_item.source END
                RETURNING (xmax = 0) AS inserted
                """,
                batch, fetch=True,
            )
        conn.commit()
        inserted = sum(1 for (flag,) in got if flag)
        return {"inserted": inserted, "reseen": len(got) - inserted}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_watch_items(
    country_iso2: str, *, status: Optional[str] = None, first_seen_until: Any = None,
) -> List[Dict[str, Any]]:
    """A country's watch items, most important first.

    `first_seen_until` keeps a backfill honest: an item first seen after the
    as-of date did not exist yet.
    """
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {", ".join(_WATCH_COLUMNS)} FROM watch_item
                 WHERE country_iso2 = %s
                   AND (%s::text IS NULL OR status = %s)
                   AND (%s::date IS NULL OR first_seen_run_date <= %s::date)
                 ORDER BY importance DESC NULLS LAST, due_date NULLS LAST, title
                """,
                (country_iso2, status, status, first_seen_until, first_seen_until),
            )
            return [dict(zip(_WATCH_COLUMNS, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def resolve_watch_item(
    country_iso2: str, item_id: str, *, status: str, resolved_run_date: Any,
    note: Optional[str] = None, evidence: Any = None, superseded_by: Optional[str] = None,
) -> bool:
    """Close an open item. Returns whether a row changed.

    Only an open item can be resolved: the terminal states are final, and a
    second verdict on a closed item is ignored rather than rewriting history.
    """
    if status not in schema.WATCH_STATUSES or status == "open":
        raise ValueError(f"not a terminal watch status: {status!r}")
    conn = db.connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE watch_item
                   SET status = %s, resolved_run_date = %s, resolution_note = %s,
                       resolution_evidence = %s, superseded_by = %s
                 WHERE country_iso2 = %s AND item_id = %s AND status = 'open'
                """,
                (status, resolved_run_date, note,
                 _json(evidence) if evidence is not None else None,
                 superseded_by, country_iso2, item_id),
            )
            changed = cur.rowcount
        conn.commit()
        return changed > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_watch_importance(country_iso2: str, scores: Dict[str, float]) -> None:
    """Store this week's computed importance for a country's open items."""
    if not scores:
        return
    conn = db.connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            # One statement per item: a country has a few dozen open items at
            # most, and execute_values cannot bind the country beside the list.
            for item_id, importance in scores.items():
                cur.execute(
                    "UPDATE watch_item SET importance = %s WHERE country_iso2 = %s "
                    "AND item_id = %s AND status = 'open'",
                    (float(importance), country_iso2, item_id),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_watch_shown(country_iso2: str, item_ids: Sequence[str]) -> None:
    """Record that these items made the displayed top five."""
    ids = [i for i in item_ids if i]
    if not ids:
        return
    conn = db.connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE watch_item SET ever_shown = TRUE "
                "WHERE country_iso2 = %s AND item_id = ANY(%s)",
                (country_iso2, ids),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_CALENDAR_COLUMNS = (
    "event_id", "event_time", "country_code", "event", "importance",
    "currency", "previous", "estimate", "actual", "ai_importance", "created_at",
)


def read_calendar_events(
    country_code: str, *, start: Any, end: Any,
    importance: Sequence[str] = ("h", "m"), created_before: Any = None,
) -> List[Dict[str, Any]]:
    """Calendar rows for a country inside a window, soonest first.

    `created_before` is the as-of filter for a backfill: a row the database
    first held after the as-of date was not knowable then. The table keeps no
    history of its own, so this is as far back as it can honestly go.
    """
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {", ".join(_CALENDAR_COLUMNS)} FROM economic_calendar_event
                 WHERE country_code = %s
                   AND event_time >= %s::date AND event_time < (%s::date + 1)
                   AND importance = ANY(%s)
                   AND (%s::date IS NULL OR created_at < (%s::date + 1))
                 ORDER BY event_time
                """,
                (country_code, start, end, list(importance), created_before, created_before),
            )
            return [dict(zip(_CALENDAR_COLUMNS, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def read_calendar_rows_by_id(event_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Calendar rows by event id, for confirming a watch item's release happened.
    A pruned row is simply absent."""
    ids = [e for e in event_ids if e]
    if not ids:
        return {}
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_CALENDAR_COLUMNS)} FROM economic_calendar_event "
                f"WHERE event_id = ANY(%s)",
                (ids,),
            )
            return {r[0]: dict(zip(_CALENDAR_COLUMNS, r)) for r in cur.fetchall()}
    finally:
        conn.close()


def dump_table_csv(table: str, path: Any) -> int:
    """COPY one table to a CSV file with a header. Returns the row count.

    The backup taken before a schema change. A table name is interpolated, so
    it is checked against the schema first.
    """
    if table not in schema.TABLES:
        raise ValueError(f"{table!r} is not a table in schema.py")
    conn = db.connect()
    try:
        with conn.cursor() as cur, open(path, "w", encoding="utf-8", newline="") as fh:
            cur.copy_expert(f"COPY {table} TO STDOUT WITH CSV HEADER", fh)
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table}")
            return int(cur.fetchone()[0])
    finally:
        conn.close()


# --- What ran, where, and what it cost --------------------------------------


def write_ledger(
    *,
    job_type: str,
    run_date: Any,
    status: str,
    country_iso2: str = "",
    started_at: Optional[dt.datetime] = None,
    finished_at: Optional[dt.datetime] = None,
    git_sha: Optional[str] = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    spend_usd: float = 0.0,
    detail: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    """Record one unit of work, with where it ran and what it wrote to.

    `host` and the database's host and name come from the environment rather
    than from the caller, because the point of the row is to answer "which
    machine, against which database" without anyone having to remember.
    """
    db_host, db_name = db.where()
    conn = db.connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO run_ledger
                  (job_type, country_iso2, run_date, started_at, finished_at,
                   status, host, db_host, db_name, git_sha, input_tokens,
                   output_tokens, spend_usd, detail, error)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (job_type, country_iso2, run_date) DO UPDATE SET
                    finished_at   = EXCLUDED.finished_at,
                    status        = EXCLUDED.status,
                    input_tokens  = EXCLUDED.input_tokens,
                    output_tokens = EXCLUDED.output_tokens,
                    spend_usd     = EXCLUDED.spend_usd,
                    detail        = EXCLUDED.detail,
                    error         = EXCLUDED.error
                """,
                (job_type, country_iso2, run_date, started_at, finished_at,
                 status, socket.gethostname(), db_host, db_name, git_sha,
                 input_tokens, output_tokens, spend_usd,
                 _json(detail or {}), error),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_ledger(run_date: Any) -> List[Dict[str, Any]]:
    """Every unit of work recorded for one run date."""
    cols = ("job_type", "country_iso2", "run_date", "started_at", "finished_at",
            "status", "host", "db_host", "db_name", "git_sha", "input_tokens",
            "output_tokens", "spend_usd", "detail", "error")
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(cols)} FROM run_ledger WHERE run_date = %s "
                f"ORDER BY job_type, country_iso2",
                (run_date,),
            )
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def read_latest_ledger(job_type: str, *, country_iso2: str = "") -> Optional[Dict[str, Any]]:
    """The most recent ledger row of one job type."""
    cols = ("job_type", "country_iso2", "run_date", "started_at", "finished_at",
            "status", "git_sha", "spend_usd", "detail", "error")
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(cols)} FROM run_ledger "
                f"WHERE job_type = %s AND country_iso2 = %s "
                f"ORDER BY run_date DESC, finished_at DESC NULLS LAST LIMIT 1",
                (job_type, country_iso2),
            )
            r = cur.fetchone()
            return dict(zip(cols, r)) if r else None
    finally:
        conn.close()


def read_diagnostics(
    *, kind: str, variant: Optional[str] = None, country_iso2: Optional[str] = None,
    run_date: Any = None,
) -> List[Dict[str, Any]]:
    """Measurements of one kind, oldest first. The ledger and the report read these."""
    cols = ("country_iso2", "run_date", "kind", "model", "variant", "payload", "created_at")
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {", ".join(cols)} FROM snapshot_diagnostic
                 WHERE kind = %s
                   AND (%s::text IS NULL OR variant = %s)
                   AND (%s::text IS NULL OR country_iso2 = %s)
                   AND (%s::date IS NULL OR run_date = %s::date)
                 ORDER BY run_date, country_iso2, variant
                """,
                (kind, variant, variant, country_iso2, country_iso2, run_date, run_date),
            )
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def read_latest_artifact(
    *, kind: str, mode: str = "named", country_iso2: str = "", created_before: Any = None,
) -> Optional[Dict[str, Any]]:
    """The newest artifact of a kind, or None. How the calibration memo is found.

    `read_artifacts` needs the hashes; the memo is looked up by recency, with
    `created_before` so a backfill sees the memo that existed on its date.
    """
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT content_sha256, version, model, payload, created_at
                  FROM llm_artifact
                 WHERE kind = %s AND mode = %s AND country_iso2 = %s
                   AND (%s::date IS NULL OR created_at < (%s::date + 1))
                 ORDER BY created_at DESC LIMIT 1
                """,
                (kind, mode, country_iso2, created_before, created_before),
            )
            r = cur.fetchone()
            if not r:
                return None
            return {"content_sha256": r[0], "version": r[1], "model": r[2],
                    "payload": r[3], "created_at": r[4]}
    finally:
        conn.close()


def write_diagnostic(
    *, country_iso2: str, run_date: Any, kind: str, model: str,
    payload: Dict[str, Any], variant: str = "",
) -> None:
    """Record something that measures the instrument rather than the country.

    Repeat runs, benchmark arms, probe results. The product never reads this
    table; it exists so a measurement has somewhere to go that is not the score.
    """
    conn = db.connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO snapshot_diagnostic
                  (country_iso2, run_date, kind, model, variant, payload)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (country_iso2, run_date, kind, model, variant)
                DO UPDATE SET payload = EXCLUDED.payload
                """,
                (country_iso2, run_date, kind, model, variant, _json(payload)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
