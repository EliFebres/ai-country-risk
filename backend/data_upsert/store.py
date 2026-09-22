"""
Read and write the evidence store: `article` bodies and `llm_artifact` caches.

Kept apart from `data_push`, which upserts the finished product (scores, alerts,
prices). This module handles the inputs to scoring rather than its outputs, and
the two have different lifetimes: a snapshot is written once a week and read by
the dashboard, whereas these rows are read on the same run that writes them.

Every function here opens and closes its own connection, matching the convention
in `data_push`. `ensure_schema` is the exception worth noting: it verifies after
it provisions, because a `CREATE TABLE IF NOT EXISTS` that returns without
raising is not evidence that the table is there.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg2
import psycopg2.extras as extras

from backend.data_upsert import schema
from backend.data_upsert.data_push import DB_URL

__all__ = [
    "ensure_schema",
    "read_artifacts",
    "write_artifacts",
    "upsert_articles",
    "read_articles",
    "write_census",
    "read_census",
    "read_recent_resolution",
]


def _connect():
    if not DB_URL:
        raise RuntimeError("DATABASE_URL is not set in the environment")
    return psycopg2.connect(DB_URL)


def ensure_schema() -> Dict[str, bool]:
    """Provision `article` and `llm_artifact`, then confirm they exist.

    Returns:
        ``{table_name: exists}`` — every value True, or this raises.

    Raises:
        RuntimeError: if a table is still absent after provisioning.
    """
    conn = _connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            schema.create_all(cur)
            present = schema.verify(cur)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    missing = [name for name, ok in present.items() if not ok]
    if missing:
        raise RuntimeError(f"schema provisioning did not create: {', '.join(missing)}")
    return present


# --- The model-output cache -------------------------------------------------


def read_artifacts(
    hashes: Sequence[str],
    *,
    kind: str,
    version: str,
    mode: str = "named",
) -> Dict[str, Any]:
    """Return cached payloads for `hashes`, keyed by hash.

    A miss is an absent key, never a null value: the caller distinguishes "we
    have no answer" from "the model answered null", and collapsing the two would
    make a failed call look like a cached one.

    Args:
        hashes: Content hashes to look up.
        kind: ``'relevance'`` or ``'digest'``.
        version: The prompt-version hash the answer must have been produced under.
        mode: ``'named'`` or ``'masked'``.

    Returns:
        ``{content_sha256: payload}`` for the rows that exist.
    """
    wanted = [h for h in dict.fromkeys(hashes) if h]
    if not wanted:
        return {}

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT content_sha256, payload
                  FROM llm_artifact
                 WHERE kind = %s AND version = %s AND mode = %s
                   AND content_sha256 = ANY(%s)
                """,
                (kind, version, mode, wanted),
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
) -> int:
    """Cache model output. Returns the number of rows offered.

    `ON CONFLICT DO NOTHING`: the first answer for a given (text, kind, version,
    mode) wins. Two concurrent runs racing on the same article should not be able
    to disagree about what the cache holds, and a later overwrite would silently
    change the evidence behind a score that was already published.

    Args:
        rows: ``(content_sha256, payload)`` pairs. `payload` is JSON-serializable.
        kind: ``'relevance'`` or ``'digest'``.
        version: The hash of the prompt text that produced these.
        model: The dated model ID that produced these, for the census.
        mode: ``'named'`` or ``'masked'``.
    """
    batch: List[Tuple] = [
        (h, kind, version, mode, model, extras.Json(payload))
        for h, payload in rows
        if h
    ]
    if not batch:
        return 0

    conn = _connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            extras.execute_values(
                cur,
                """
                INSERT INTO llm_artifact
                  (content_sha256, kind, version, mode, model, payload)
                VALUES %s
                ON CONFLICT (content_sha256, kind, version, mode) DO NOTHING
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
    "url",
    "wrapper_url",
    "country_iso2",
    "source_system",
    "publisher",
    "published_at",
    "page_published_at",
    "title",
    "abstract",
    "body",
    "content_sha256",
    "body_status",
    "body_chars_original",
    "body_clipped",
    "themes",
)


def upsert_articles(rows: Iterable[Dict[str, Any]]) -> int:
    """Store articles, keyed by resolved URL. Returns the number of rows offered.

    A body beats a stub: the update clause keeps whatever text it already has if
    this row arrives without one, so a second pass that only knows the headline
    cannot erase a body an earlier pass paid to fetch.

    Args:
        rows: Dicts using the column names in ``_ARTICLE_COLUMNS``; `url`,
            `country_iso2` and `body_status` are required.
    """
    batch: List[Tuple] = []
    for r in rows:
        if not r.get("url") or not r.get("country_iso2"):
            continue
        batch.append(tuple(r.get(c) for c in _ARTICLE_COLUMNS))
    if not batch:
        return 0

    conn = _connect()
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
    """Read stored articles for one country, newest first.

    The consumer side of :func:`upsert_articles`, and the thing that makes a
    scoring re-run possible: last week's evidence as it stood, rather than a
    re-fetch of a different week.
    """
    conn = _connect()
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


# --- The census -------------------------------------------------------------


def write_census(row: Dict[str, Any]) -> None:
    """Store one country's census for one run.

    Args:
        row: ``country_iso2``, ``as_of`` and the four JSONB blocks
            (``indicators``, ``articles``, ``versions``, ``coverage_components``)
            plus ``payload_fingerprint`` and ``evidence_coverage``.
    """
    conn = _connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO payload_census
                  (country_iso2, as_of, payload_fingerprint, evidence_coverage,
                   coverage_components, indicators, articles, versions)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (country_iso2, as_of) DO UPDATE SET
                    payload_fingerprint = EXCLUDED.payload_fingerprint,
                    evidence_coverage   = EXCLUDED.evidence_coverage,
                    coverage_components = EXCLUDED.coverage_components,
                    indicators          = EXCLUDED.indicators,
                    articles            = EXCLUDED.articles,
                    versions            = EXCLUDED.versions,
                    created_at          = now()
                """,
                (
                    row["country_iso2"],
                    row["as_of"],
                    row.get("payload_fingerprint"),
                    row.get("evidence_coverage"),
                    extras.Json(row.get("coverage_components") or {}),
                    extras.Json(row.get("indicators") or {}),
                    extras.Json(row.get("articles") or {}),
                    extras.Json(row.get("versions") or {}),
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_census(country_iso2: str, as_of: Any) -> Optional[Dict[str, Any]]:
    """Read one census row back.

    The consumer side of :func:`write_census`. A scoring run asserts against
    this rather than against the write returning cleanly, because a census that
    is written and never read is exactly the failure it exists to prevent.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT country_iso2, as_of, payload_fingerprint, evidence_coverage,
                       coverage_components, indicators, articles, versions
                  FROM payload_census
                 WHERE country_iso2 = %s AND as_of = %s
                """,
                (country_iso2, as_of),
            )
            r = cur.fetchone()
            if not r:
                return None
            return {
                "country_iso2": r[0], "as_of": r[1], "payload_fingerprint": r[2],
                "evidence_coverage": r[3], "coverage_components": r[4],
                "indicators": r[5], "articles": r[6], "versions": r[7],
            }
    finally:
        conn.close()


def read_recent_resolution(
    country_iso2: str, *, before: Any = None, limit: int = 5
) -> List[Dict[str, int]]:
    """Return per-ledger resolved counts from a country's own recent runs.

    What the alarm compares against. The baseline is the country's own history,
    not an absolute floor: Taiwan resolving zero is expected because Taiwan
    always resolved zero, while Portugal going from twenty to twelve is a source
    break. An absolute threshold cannot tell those apart.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT indicators -> 'resolved_by_ledger'
                  FROM payload_census
                 WHERE country_iso2 = %s
                   AND (%s::date IS NULL OR as_of < %s::date)
                 ORDER BY as_of DESC
                 LIMIT %s
                """,
                (country_iso2, before, before, limit),
            )
            return [r[0] for r in cur.fetchall() if r[0]]
    finally:
        conn.close()
