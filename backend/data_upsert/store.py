"""
The durable record: evidence in, model output cached, the week written down.

This module owns the tables a reader in three years needs — `article`,
`llm_artifact`, `indicator_series`, `risk_snapshot`, `run_ledger` and
`snapshot_diagnostic`. `data_push` keeps the dashboard-facing feeds (prices,
alerts, calendar) and the roster seed, because those have a different lifetime:
they are overwritten every tick and nobody will ever reconstruct one.

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
    "upsert_indicator_series",
    "read_indicator_series",
    "upsert_snapshot",
    "read_snapshot",
    "read_recent_snapshots",
    "write_ledger",
    "read_ledger",
    "write_diagnostic",
    "StampConflict",
]


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
        (h, kind, version, mode, country_iso2, model, extras.Json(payload))
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
    "url", "wrapper_url", "country_iso2", "source_system", "publisher",
    "published_at", "page_published_at", "title", "abstract", "body",
    "content_sha256", "body_status", "body_chars_original", "body_clipped",
    "themes",
)


def upsert_articles(rows: Iterable[Dict[str, Any]]) -> int:
    """Store articles, keyed by resolved URL. Returns the number offered.

    A body beats a stub: the update clause keeps whatever text it already has
    when a row arrives without one, so a later pass that only knows the headline
    cannot erase a body an earlier pass paid to fetch.
    """
    batch: List[Tuple] = []
    for r in rows:
        if not r.get("url") or not r.get("country_iso2"):
            continue
        batch.append(tuple(r.get(c) for c in _ARTICLE_COLUMNS))
    if not batch:
        return 0

    conn = db.connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            extras.execute_values(
                cur,
                f"""
                INSERT INTO article ({", ".join(_ARTICLE_COLUMNS)})
                VALUES %s
                ON CONFLICT (url) DO UPDATE SET
                    title               = COALESCE(EXCLUDED.title, article.title),
                    abstract            = COALESCE(EXCLUDED.abstract, article.abstract),
                    body                = COALESCE(EXCLUDED.body, article.body),
                    content_sha256      = COALESCE(EXCLUDED.content_sha256, article.content_sha256),
                    page_published_at   = COALESCE(EXCLUDED.page_published_at, article.page_published_at),
                    body_chars_original = COALESCE(EXCLUDED.body_chars_original, article.body_chars_original),
                    body_clipped        = EXCLUDED.body_clipped,
                    themes              = EXCLUDED.themes,
                    body_status         = CASE
                        WHEN EXCLUDED.body IS NOT NULL THEN EXCLUDED.body_status
                        ELSE article.body_status
                    END
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


def read_articles(country_iso2: str, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Stored articles for one country, newest first.

    What makes a scoring re-run possible: last week's evidence as it stood,
    rather than a re-fetch of a different week.
    """
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {", ".join(_ARTICLE_COLUMNS)}
                  FROM article
                 WHERE country_iso2 = %s
                 ORDER BY COALESCE(page_published_at, published_at) DESC NULLS LAST
                 {"LIMIT %s" if limit else ""}
                """,
                (country_iso2, limit) if limit else (country_iso2,),
            )
            return [dict(zip(_ARTICLE_COLUMNS, r)) for r in cur.fetchall()]
    finally:
        conn.close()


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


def _check_stamps(cur, country_iso2: str, run_date: Any, incoming: Dict[str, Any]) -> None:
    """Refuse a write that would change how an existing row was produced.

    Raises:
        StampConflict: naming the field and both values.
    """
    cur.execute(
        f"""
        SELECT {", ".join(schema.STAMPED_FIELDS)}
          FROM risk_snapshot WHERE country_iso2 = %s AND run_date = %s
        """,
        (country_iso2, run_date),
    )
    row = cur.fetchone()
    if not row:
        return

    for field, existing in zip(schema.STAMPED_FIELDS, row):
        if existing is None:
            continue  # a stamp may be filled in later; it may not be altered
        new = incoming.get(field)
        if new is not None and new != existing:
            logger.error(
                "stamp conflict on %s %s: %s is %r, refusing to overwrite with %r",
                country_iso2, run_date, field, existing, new,
            )
            raise StampConflict(
                f"{country_iso2} {run_date}: {field} is already {existing!r} and "
                f"the write would set it to {new!r}. Stamps record how a row was "
                f"produced and are not rewritable; a new run writes a new row."
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
        values.append(extras.Json(v) if col in _JSON_COLUMNS and v is not None else v)

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
                 extras.Json(detail or {}), error),
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
                (country_iso2, run_date, kind, model, variant, extras.Json(payload)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
