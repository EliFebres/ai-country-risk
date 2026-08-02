"""The article substrate: raw harvested text, and what we know about it.

Three harvesters fill this table from three different archives, and a fourth
stage goes back over it recovering the bodies the archives did not hand over.
They all write through here, which is what lets one rule live in one place —
notably the only rule that really matters at this layer:

    **A body always beats a stub.**

GDELT knows about ten times as many articles as the Guardian API but returns no
text at all. So the same story arrives twice, once with a body and once without,
in whichever order the harvest happened to run. Expressing "the body wins" in
the upsert's ``ON CONFLICT`` rather than in each adapter means no adapter can
get it wrong, and re-running a harvester is always safe.

**Bodies here are raw and unmasked, always.** Masking is a transform applied at
the scoring boundary, never at harvest: a masked body would make the mask map
unversionable, and the store useless the day the gazetteer improves.
``content_sha256`` therefore hashes the unmasked body, so the digest cache can
key on ``(content_sha256, digest_model, mode)`` and hold the named and masked
variants of one article side by side.

Following the rest of the backend, this writer owns its DDL and issues
``CREATE TABLE IF NOT EXISTS`` before writing — the project has no migration
tool, and a self-provisioning table costs one cheap no-op per call.
"""

import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg2.extras as extras

from backend.utils import provenance
from backend.utils.data_upsert import data_push
from backend.utils.news_fetching import core

# The project's one connect/commit/rollback/close helper. Private to data_push
# by name only: every write in the backend goes through it, and standing up a
# second transaction helper here is exactly the divergence this module exists
# to prevent.
_transaction = data_push._transaction

# How much a body_status is worth. A harvester upsert may raise an article's
# status but never lower it, so a GDELT re-run cannot push a recovered article
# back to 'pending' and buy it a second (billable) leakage scan. Only
# `mark_body` — the recovery stage, which is authoritative — may move a status
# downward.
BODY_STATUSES: Tuple[str, ...] = ("pending", "failed", "degraded-title-only", "recovered")
_STATUS_RANK = "array_position(ARRAY['pending','failed','degraded-title-only','recovered'], %s)"

_HISTORICAL_ARTICLE_DDL = """
CREATE TABLE IF NOT EXISTS historical_article (
  url             TEXT PRIMARY KEY,
  publisher_link  TEXT,
  country_iso2    TEXT NOT NULL,
  source_system   TEXT NOT NULL,
  published_at    TIMESTAMPTZ NOT NULL,
  title           TEXT,
  abstract        TEXT,
  body            TEXT,
  body_vintage    TEXT,
  body_status     TEXT NOT NULL,
  wayback_url     TEXT,
  content_sha256  TEXT,
  themes          TEXT[],
  tier            TEXT NOT NULL DEFAULT 'full',
  harvested_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_histart_country_date
  ON historical_article (country_iso2, published_at);
"""

_HARVEST_CHECKPOINT_DDL = """
CREATE TABLE IF NOT EXISTS harvest_checkpoint (
  source_system TEXT, country_iso2 TEXT, window_start DATE, window_end DATE,
  status TEXT, items_written INT, note TEXT, updated_at TIMESTAMPTZ,
  PRIMARY KEY (source_system, country_iso2, window_start)
);
"""


# ---------------------------------------------------------------------------
# Item -> row: the one place the two vocabularies meet
# ---------------------------------------------------------------------------
# A canonical article item speaks the live pipeline's language (`link`, `text`,
# `published`); the table speaks the store's (`url`, `body`, `published_at`).
# Translating in exactly one function means an adapter never has to know both.

def article_row(
    item: Dict,
    *,
    country_iso2: str,
    source_system: str,
    body_status: str,
    body_vintage: Optional[str] = None,
    tier: str = "full",
    wayback_url: Optional[str] = None,
    harvested_at: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Map one canonical article item onto a ``historical_article`` row.

    Pure — no database, no clock unless you leave ``harvested_at`` to default —
    so an adapter's payload-to-row path is testable against a fixture.

    Args:
        item: a ``core.normalize_item`` dict. Themes are read from ``_theme``
            and topped up by ``core.classify_themes``, so a row always carries
            every theme its text supports, not only the query that found it.
        country_iso2: the country this harvest was for.
        source_system: ``'guardian'`` | ``'gdelt'`` | ``'nyt'``.
        body_status: one of :data:`BODY_STATUSES`.
        body_vintage: ``'api-native'`` | ``'wayback-YYYYMMDD'`` |
            ``'live-refetch'``, or None while the body is still pending.
        tier: ``'full'`` or ``'abstract-only'`` (the NYT degraded tier).
        wayback_url: the capture the body came from, when it came from one.
        harvested_at: defaults to now, UTC.

    Returns:
        A dict keyed by column name, with ``content_sha256`` computed over the
        raw unmasked body.

    Raises:
        ValueError: on an unknown ``body_status``, a missing URL, or an
            unparseable ``published`` — the column is NOT NULL because an
            article with no date cannot be placed in any snapshot window, and
            silently dropping it at write time would be a hole nothing reports.
    """
    if body_status not in BODY_STATUSES:
        raise ValueError(f"body_status must be one of {BODY_STATUSES}, got {body_status!r}")

    url = core.dedupe_key(item)
    if not url:
        raise ValueError(f"item has no URL to key on: {item!r}")

    published_at = data_push._to_ts_or_none(item.get("published"))
    if published_at is None:
        raise ValueError(f"{url} has no parseable publication date: {item.get('published')!r}")

    body = item.get("text") or None

    # The retrieving query's theme first (stronger evidence), then everything
    # else the text supports. Snapshot assembly reads these to fill the same
    # per-theme floor the live run uses, so a single tag would under-serve it.
    themes = list(dict.fromkeys(
        ([item["_theme"]] if item.get("_theme") else [])
        + core.classify_themes(item.get("title"), body)
    ))

    return {
        "url": url,
        "publisher_link": item.get("publisher_link") or None,
        "country_iso2": country_iso2,
        "source_system": source_system,
        "published_at": published_at,
        "title": item.get("title") or None,
        "abstract": item.get("snippet") or None,
        "body": body,
        "body_vintage": body_vintage,
        "body_status": body_status,
        "wayback_url": wayback_url,
        "content_sha256": provenance.text_sha256(body),
        "themes": themes,
        "tier": tier,
        "harvested_at": harvested_at or datetime.datetime.now(datetime.timezone.utc),
    }


_ROW_COLUMNS = (
    "url", "publisher_link", "country_iso2", "source_system", "published_at",
    "title", "abstract", "body", "body_vintage", "body_status", "wayback_url",
    "content_sha256", "themes", "tier", "harvested_at",
)


def upsert_articles(rows: Sequence[Dict[str, Any]]) -> int:
    """Write harvested articles, letting the better copy of each story win.

    Idempotent and order-independent: run the Guardian harvest before or after
    GDELT and the same article ends up with the same body either way.

    Args:
        rows: dicts from :func:`article_row`.

    Returns:
        How many rows were sent. No-op returning 0 on an empty sequence.
    """
    data_push._require_db_url()   # fail fast even when there is nothing to write
    if not rows:
        return 0

    values = [tuple(r[c] for c in _ROW_COLUMNS) for r in rows]
    with _transaction() as cur:
        cur.execute(_HISTORICAL_ARTICLE_DDL)
        extras.execute_values(
            cur,
            f"""
            INSERT INTO historical_article ({", ".join(_ROW_COLUMNS)})
            VALUES %s
            ON CONFLICT (url) DO UPDATE SET
              -- A body always beats a stub, whichever arrived second.
              body           = COALESCE(EXCLUDED.body, historical_article.body),
              body_vintage   = CASE WHEN EXCLUDED.body IS NOT NULL
                                    THEN EXCLUDED.body_vintage
                                    ELSE historical_article.body_vintage END,
              content_sha256 = CASE WHEN EXCLUDED.body IS NOT NULL
                                    THEN EXCLUDED.content_sha256
                                    ELSE historical_article.content_sha256 END,
              -- A harvester may raise a status but never lower one.
              body_status    = CASE WHEN {_STATUS_RANK % "EXCLUDED.body_status"}
                                       > {_STATUS_RANK % "historical_article.body_status"}
                                    THEN EXCLUDED.body_status
                                    ELSE historical_article.body_status END,
              -- Whoever holds the body owns the provenance that goes with it.
              source_system  = CASE WHEN EXCLUDED.body IS NOT NULL
                                    THEN EXCLUDED.source_system
                                    ELSE historical_article.source_system END,
              title          = COALESCE(EXCLUDED.title, historical_article.title),
              abstract       = COALESCE(EXCLUDED.abstract, historical_article.abstract),
              publisher_link = COALESCE(EXCLUDED.publisher_link, historical_article.publisher_link),
              wayback_url    = COALESCE(EXCLUDED.wayback_url, historical_article.wayback_url),
              themes         = EXCLUDED.themes,
              tier           = EXCLUDED.tier,
              harvested_at   = EXCLUDED.harvested_at
            """,
            values,
            page_size=200,
        )
    return len(values)


def mark_body(
    url: str,
    *,
    body: Optional[str],
    body_status: str,
    body_vintage: Optional[str] = None,
    wayback_url: Optional[str] = None,
) -> None:
    """Record the outcome of one recovery attempt.

    Unlike :func:`upsert_articles` this writes the status unconditionally, in
    both directions: the recovery stage is authoritative, and it is the only
    thing allowed to demote — a live-refetched body the leakage scan flags goes
    to ``degraded-title-only`` with the body discarded, and that demotion must
    stick.

    Raises:
        ValueError: on an unknown ``body_status``.
    """
    if body_status not in BODY_STATUSES:
        raise ValueError(f"body_status must be one of {BODY_STATUSES}, got {body_status!r}")

    with _transaction() as cur:
        cur.execute(_HISTORICAL_ARTICLE_DDL)
        cur.execute(
            """
            UPDATE historical_article
               SET body           = %s,
                   body_status    = %s,
                   body_vintage   = %s,
                   wayback_url    = COALESCE(%s, wayback_url),
                   content_sha256 = %s
             WHERE url = %s
            """,
            (body, body_status, body_vintage, wayback_url,
             provenance.text_sha256(body), url),
        )


def read_pending(limit: Optional[int] = None,
                 source_system: Optional[str] = None) -> List[Dict[str, Any]]:
    """The body-recovery queue, oldest article first.

    Ordered by publication so an interrupted drain resumes in the same place
    and the recovery curve fills in from the far end of history forward, which
    is where the interesting failures are.
    """
    where = ["body_status = 'pending'"]
    params: List[Any] = []
    if source_system:
        where.append("source_system = %s")
        params.append(source_system)
    sql = (f"SELECT url, country_iso2, source_system, published_at, title "
           f"FROM historical_article WHERE {' AND '.join(where)} "
           f"ORDER BY published_at, url")
    if limit:
        sql += " LIMIT %s"
        params.append(limit)

    with _transaction() as cur:
        cur.execute(_HISTORICAL_ARTICLE_DDL)
        cur.execute(sql, tuple(params))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------

def completed_windows(source_system: str, country_iso2: str) -> set:
    """Window start dates already harvested for this source and country.

    What makes every harvester resumable: a re-run skips these and picks up
    where it stopped, which matters because a Guardian harvest spans days of
    quota and a Wayback drain spans hours of a service that will rate-limit it.
    """
    with _transaction() as cur:
        cur.execute(_HARVEST_CHECKPOINT_DDL)
        cur.execute(
            """
            SELECT window_start FROM harvest_checkpoint
             WHERE source_system = %s AND country_iso2 = %s AND status = 'done'
            """,
            (source_system, country_iso2),
        )
        return {row[0] for row in cur.fetchall()}


def write_checkpoint(
    source_system: str,
    country_iso2: str,
    window_start: datetime.date,
    window_end: datetime.date,
    *,
    status: str = "done",
    items_written: int = 0,
    note: str = "",
) -> None:
    """Stamp one (source, country, window) as finished. Idempotent."""
    with _transaction() as cur:
        cur.execute(_HARVEST_CHECKPOINT_DDL)
        cur.execute(
            """
            INSERT INTO harvest_checkpoint
              (source_system, country_iso2, window_start, window_end,
               status, items_written, note, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (source_system, country_iso2, window_start) DO UPDATE SET
              window_end    = EXCLUDED.window_end,
              status        = EXCLUDED.status,
              items_written = EXCLUDED.items_written,
              note          = EXCLUDED.note,
              updated_at    = EXCLUDED.updated_at
            """,
            (source_system, country_iso2, window_start, window_end,
             status, items_written, note),
        )


# ---------------------------------------------------------------------------
# Reports — the deliverables of steps 2, 3 and 4
# ---------------------------------------------------------------------------

def _rows(sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    """Run one read against the substrate, ensuring the table exists first."""
    with _transaction() as cur:
        cur.execute(_HISTORICAL_ARTICLE_DDL)
        cur.execute(sql, tuple(params))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def counts_by_year() -> List[Dict[str, Any]]:
    """Article counts per source x country x year."""
    return _rows("""
        SELECT source_system, country_iso2,
               EXTRACT(YEAR FROM published_at)::int AS year, COUNT(*)::int AS n
          FROM historical_article
         GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
    """)


def counts_by_month(source_system: Optional[str] = None) -> List[Dict[str, Any]]:
    """Article counts per country x month — the evenness check.

    A month at zero is a hole that will produce an empty snapshot later. Finding
    it here, where a harvest can simply be re-run over that window, is worth far
    more than finding it in snapshot assembly.
    """
    where = "WHERE source_system = %s" if source_system else ""
    return _rows(f"""
        SELECT country_iso2, date_trunc('month', published_at)::date AS month,
               COUNT(*)::int AS n
          FROM historical_article {where}
         GROUP BY 1, 2 ORDER BY 1, 2
    """, (source_system,) if source_system else ())


def recovery_curve() -> List[Dict[str, Any]]:
    """Body outcomes per source x year — how far back the news blend stays honest.

    This curve is a deliverable in its own right: it is what decides where
    Tier 1 really ends, rather than where anyone hoped it would.
    """
    return _rows("""
        SELECT source_system, EXTRACT(YEAR FROM published_at)::int AS year,
               body_status, body_vintage, COUNT(*)::int AS n
          FROM historical_article
         GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4
    """)


def existing_urls(urls: Sequence[str]) -> set:
    """Which of these URLs the store already holds.

    Two jobs in one query. It is the pre-insert de-dupe the GDELT harvester
    needs, and it is the only way to report the Guardian overlap at all: the
    upsert collapses both copies of a story onto one row keyed by URL, so after
    the write there is nothing left to count.
    """
    if not urls:
        return set()
    return {r["url"] for r in _rows(
        "SELECT url FROM historical_article WHERE url = ANY(%s)", (list(urls),))}
