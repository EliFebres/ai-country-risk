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
from backend.data_upsert import data_push
from backend.utils.history import config
from backend.news_fetching import core

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
        How many distinct rows were written — fewer than ``len(rows)`` when the
        batch itself held two copies of one story. No-op returning 0 on empty.
    """
    data_push._require_db_url()   # fail fast even when there is nothing to write
    if not rows:
        return 0

    # Postgres refuses an ON CONFLICT that would touch one row twice in a single
    # command, so the same body-wins rule has to be applied inside the batch as
    # well as against the table. Doing it here rather than asking every caller to
    # de-duplicate first: a batch that raises does so half way through a harvest,
    # hours in, and the adapters are the wrong place to remember a storage rule.
    best: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        incumbent = best.get(row["url"])
        if incumbent is None or (row.get("body") and not incumbent.get("body")):
            best[row["url"]] = row

    values = [tuple(r[c] for c in _ROW_COLUMNS) for r in best.values()]
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
# The run ledger — what has been scored, at what cost, and where it landed
# ---------------------------------------------------------------------------
# `harvest_checkpoint` above tracks collection; this tracks scoring. They stay
# separate tables because they answer different questions and fail differently:
# a harvest window is re-runnable for free, a scored snapshot costs money and
# must never be paid for twice.

_RUN_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS history_run_ledger (
  as_of        DATE NOT NULL,
  country_iso2 TEXT NOT NULL,
  mode         TEXT NOT NULL,
  status       TEXT NOT NULL,
  spend_usd    DOUBLE PRECISION,
  manifest     JSONB,
  result       JSONB,
  updated_at   TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (as_of, country_iso2, mode)
);
"""


def write_run(
    as_of: datetime.date,
    country_iso2: str,
    mode: str,
    *,
    status: str,
    spend_usd: float = 0.0,
    manifest: Optional[Dict[str, Any]] = None,
    result: Optional[Dict[str, Any]] = None,
) -> None:
    """Record the outcome of scoring one country on one date. Idempotent.

    Args:
        mode: ``'masked'`` or ``'named'`` — see :data:`config.SCORING_MODES`.
        status: ``'complete'`` | ``'failed'`` | ``'skipped'``. Only
            ``'complete'`` makes a re-run skip the date.
        manifest: the provenance manifest — ``scoring_mode``, ``mask_map_version``,
            and per-article ``source_system`` + ``body_vintage`` — so a row can
            be rebuilt, or found to be unrebuildable.
        result: the model output. Populated for ``'named'`` runs, whose scores
            have nowhere else to live; ``None`` for ``'masked'`` runs, which are
            in ``risk_snapshot`` where the front end can read them.

    Raises:
        ValueError: on a mode outside :data:`config.SCORING_MODES` — a typo here
            would quietly split a series in two.
    """
    if mode not in config.SCORING_MODES:
        raise ValueError(f"mode must be one of {config.SCORING_MODES}, got {mode!r}")

    with _transaction() as cur:
        cur.execute(_RUN_LEDGER_DDL)
        cur.execute(
            """
            INSERT INTO history_run_ledger
              (as_of, country_iso2, mode, status, spend_usd, manifest, result, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (as_of, country_iso2, mode) DO UPDATE SET
              status     = EXCLUDED.status,
              spend_usd  = EXCLUDED.spend_usd,
              manifest   = COALESCE(EXCLUDED.manifest, history_run_ledger.manifest),
              result     = COALESCE(EXCLUDED.result, history_run_ledger.result),
              updated_at = EXCLUDED.updated_at
            """,
            (as_of, country_iso2, mode, status, spend_usd,
             data_push._json_or_none(manifest), data_push._json_or_none(result)),
        )


def completed_runs(mode: str, country_iso2: Optional[str] = None) -> set:
    """Anchor dates already scored in this mode. The pilot's resume point.

    Only ``status = 'complete'`` counts, so a run that died half way through a
    country is retried rather than silently skipped — the same rule
    :func:`completed_windows` uses for harvests.
    """
    sql = ["SELECT as_of FROM history_run_ledger WHERE mode = %s AND status = 'complete'"]
    params: List[Any] = [mode]
    if country_iso2:
        sql.append("AND country_iso2 = %s")
        params.append(country_iso2)
    with _transaction() as cur:
        cur.execute(_RUN_LEDGER_DDL)
        cur.execute(" ".join(sql), tuple(params))
        return {row[0] for row in cur.fetchall()}


def total_spend_usd() -> float:
    """Every dollar the pilot has metered so far, across runs and processes.

    The budget governor's memory. Held in the ledger rather than in the runner
    so that stopping and resuming a multi-hour pilot cannot reset the budget to
    zero and quietly spend it twice.
    """
    with _transaction() as cur:
        cur.execute(_RUN_LEDGER_DDL)
        cur.execute("SELECT COALESCE(SUM(spend_usd), 0) FROM history_run_ledger")
        return float(cur.fetchone()[0])


def read_runs(mode: Optional[str] = None) -> List[Dict[str, Any]]:
    """Ledger rows, oldest first — what ``reports.py`` renders its meters from."""
    where = "WHERE mode = %s" if mode else ""
    with _transaction() as cur:
        cur.execute(_RUN_LEDGER_DDL)
        cur.execute(f"""
            SELECT as_of, country_iso2, mode, status, spend_usd, manifest, result
              FROM history_run_ledger {where}
             ORDER BY country_iso2, as_of, mode
        """, (mode,) if mode else ())
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# The digest cache — the one that makes a weekly cadence affordable
# ---------------------------------------------------------------------------
# Weekly anchors with a 30-day window overlap about four times, so without a
# cache the pilot would pay to digest each article four times over. Keyed on
# content rather than on (country, as_of) like the daily run's `article_digest`,
# because the same article appears in four different snapshots and its digest is
# identical in all of them.
#
# `mode` is in the key because the masked and named digests of one article are
# genuinely different texts, and must never be served for each other.

_DIGEST_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS history_digest_cache (
  content_sha256  TEXT NOT NULL,
  digest_model    TEXT NOT NULL,
  mode            TEXT NOT NULL,
  digest          JSONB NOT NULL,
  stage1_severity DOUBLE PRECISION,
  created_at      TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (content_sha256, digest_model, mode)
);
"""


def read_digest_cache(hashes: Sequence[str], digest_model: str, mode: str) -> Dict[str, Dict[str, Any]]:
    """Cached digests for these content hashes, keyed by hash.

    A miss is an absent key, never a null row: the caller re-digests whatever is
    missing, which is also what happens the first time a mask map changes and
    every masked hash is new.
    """
    if not hashes:
        return {}
    with _transaction() as cur:
        cur.execute(_DIGEST_CACHE_DDL)
        cur.execute(
            """
            SELECT content_sha256, digest, stage1_severity
              FROM history_digest_cache
             WHERE content_sha256 = ANY(%s) AND digest_model = %s AND mode = %s
            """,
            (list(hashes), digest_model, mode),
        )
        return {r[0]: {"digest": r[1], "stage1_severity": r[2]} for r in cur.fetchall()}


def write_digest_cache(rows: Sequence[Dict[str, Any]], digest_model: str, mode: str) -> int:
    """Cache digests by content hash.

    Args:
        rows: dicts with ``content_sha256``, ``digest`` and ``stage1_severity``.
            Rows without a hash or without a digest are dropped — a failed
            digest must be retried next time, not cached as a failure.

    Returns:
        How many rows were written.
    """
    values = [
        (r["content_sha256"], digest_model, mode,
         data_push._json_or_none(r["digest"]), r.get("stage1_severity"))
        for r in rows
        if r.get("content_sha256") and isinstance(r.get("digest"), dict)
    ]
    if not values:
        return 0
    with _transaction() as cur:
        cur.execute(_DIGEST_CACHE_DDL)
        extras.execute_values(
            cur,
            """
            INSERT INTO history_digest_cache
              (content_sha256, digest_model, mode, digest, stage1_severity, created_at)
            VALUES %s
            ON CONFLICT (content_sha256, digest_model, mode) DO NOTHING
            """,
            [v + (datetime.datetime.now(datetime.timezone.utc),) for v in values],
            page_size=200,
        )
    return len(values)


# ---------------------------------------------------------------------------
# The full-text rewrite cache — what makes a snapshot reproducible at all
# ---------------------------------------------------------------------------
# The two or three bodies the scorer reads end to end are rewritten by a model,
# and until this table existed that output was kept nowhere. Two consequences,
# and the second is the one that mattered.
#
# It was paid for repeatedly: weekly anchors over a 30-day window put the same
# article in about four consecutive snapshots, and a top-severity article stays
# top-severity across all four, so the same body was rewritten four times.
#
# And the snapshot could not be rebuilt. `input_manifest` hashes the bytes the
# model read, and for these articles those bytes were generated prose that no
# longer existed anywhere. Re-running produced a different sentence and a
# different hash, so the manifest's promise — this row can be reproduced —
# failed on precisely the three articles the scorer weighted most heavily.
#
# Keyed like the digest cache: content hash, prompt version, mode. The content
# hash is over the *masked* text, so a gazetteer change lands in the key without
# the gazetteer version needing to be part of it.

_REWRITE_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS history_rewrite_cache (
  content_sha256  TEXT NOT NULL,
  rewrite_version TEXT NOT NULL,
  mode            TEXT NOT NULL,
  rewritten       TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (content_sha256, rewrite_version, mode)
);
"""


def read_rewrite_cache(hashes: Sequence[str], rewrite_version: str,
                       mode: str) -> Dict[str, str]:
    """Cached rewritten bodies for these content hashes, keyed by hash.

    A miss is an absent key. Never returns the empty string as a hit: an empty
    rewrite is how :func:`rewrite.rewrite_body` reports failure, and caching a
    failure would degrade the article to title-only forever.
    """
    if not hashes:
        return {}
    with _transaction() as cur:
        cur.execute(_REWRITE_CACHE_DDL)
        cur.execute(
            """
            SELECT content_sha256, rewritten
              FROM history_rewrite_cache
             WHERE content_sha256 = ANY(%s) AND rewrite_version = %s AND mode = %s
            """,
            (list(hashes), rewrite_version, mode),
        )
        return {row[0]: row[1] for row in cur.fetchall() if row[1]}


def write_rewrite_cache(rows: Sequence[Dict[str, Any]], rewrite_version: str,
                        mode: str) -> int:
    """Cache rewritten bodies by content hash.

    Args:
        rows: dicts with ``content_sha256`` and ``rewritten``. Rows with an empty
            rewrite are dropped — that is the fail-closed signal, and a cached
            failure would degrade the article on every future snapshot rather
            than letting the next run try again.
    """
    values = [(r["content_sha256"], rewrite_version, mode, r["rewritten"])
              for r in rows
              if r.get("content_sha256") and (r.get("rewritten") or "").strip()]
    if not values:
        return 0
    with _transaction() as cur:
        cur.execute(_REWRITE_CACHE_DDL)
        extras.execute_values(
            cur,
            """
            INSERT INTO history_rewrite_cache
              (content_sha256, rewrite_version, mode, rewritten, created_at)
            VALUES %s
            ON CONFLICT (content_sha256, rewrite_version, mode) DO NOTHING
            """,
            [v + (datetime.datetime.now(datetime.timezone.utc),) for v in values],
            page_size=200,
        )
    return len(values)


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
               body_status, body_vintage, tier, COUNT(*)::int AS n
          FROM historical_article
         GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 2, 3, 4, 5
    """)


def read_window(iso2: str, start: datetime.datetime,
                end: datetime.datetime) -> List[Dict[str, Any]]:
    """Every article for one country in ``[start, end)``.

    The upper bound is **strict**, and that is the whole point: ``end`` is the
    snapshot's anchor, and an article published on the anchor is same-day news
    the live run's own cutoff would not reliably have had either.

    Ordered deterministically — newest first, ties broken on URL — so two
    assemblies of the same snapshot rank identical articles identically. The
    relevance sort downstream is stable, so this order is what decides ties.
    """
    return _rows("""
        SELECT url, publisher_link, title, abstract, body, body_status,
               body_vintage, source_system, published_at, themes, tier
          FROM historical_article
         WHERE country_iso2 = %s AND published_at >= %s AND published_at < %s
         ORDER BY published_at DESC, url ASC
    """, (iso2, start, end))


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
