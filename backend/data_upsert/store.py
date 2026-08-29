"""The article substrate, the run ledger, and the model-output cache.

Three harvesters fill `article` from three different archives, and a fourth
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
``content_sha256`` therefore hashes the unmasked body, so `llm_artifact` can key
on ``(content_sha256, kind, version, mode)`` and hold the named and masked
variants of one article side by side.

The DDL lives in :mod:`backend.data_upsert.schema`, not here. Every writer used
to issue its own ``CREATE TABLE IF NOT EXISTS`` before writing, which meant the
schema was defined in twenty places and a fresh clone could not build itself.
"""

import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg2.extras as extras

from backend.util import provenance
from backend.data_upsert import data_push
from backend.data_upsert.schema import (  # noqa: F401  (re-exported)
    BODY_STATUSES, TERMINAL_BODY_STATUSES)
from backend.util import config
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
_STATUS_RANK = ("array_position(ARRAY['"
                + "','".join(BODY_STATUSES) + "'], %s)")

# What `read_pending` offers back, split by how soon. These drive the query
# rather than describing it: a state added to `BODY_STATUSES` and to neither of
# these is one `read_pending` will never return, which is the one-way door the
# retryable states exist to close — and `TestTheQueueHasNoOneWayDoors` fails on
# exactly that.
RETRY_IMMEDIATELY: Tuple[str, ...] = ("pending", "transient")
BACKED_OFF: str = "no-capture"
RETRYABLE_BODY_STATUSES: Tuple[str, ...] = RETRY_IMMEDIATELY + (BACKED_OFF,)


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
    """Map one canonical article item onto an ``article`` row.

    Pure — no database, no clock unless you leave ``harvested_at`` to default —
    so an adapter's payload-to-row path is testable against a fixture.

    Args:
        item: a ``core.normalize_item`` dict. Themes are read from ``_theme``
            and topped up by ``core.classify_themes``, so a row always carries
            every theme its text supports, not only the query that found it.
        country_iso2: the country this harvest was for.
        source_system: ``'guardian'`` | ``'gdelt'`` | ``'nyt'`` | ``'google-news'``.
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
        extras.execute_values(
            cur,
            f"""
            INSERT INTO article ({", ".join(_ROW_COLUMNS)})
            VALUES %s
            ON CONFLICT (url) DO UPDATE SET
              -- A body always beats a stub, whichever arrived second.
              body           = COALESCE(EXCLUDED.body, article.body),
              body_vintage   = CASE WHEN EXCLUDED.body IS NOT NULL
                                    THEN EXCLUDED.body_vintage
                                    ELSE article.body_vintage END,
              content_sha256 = CASE WHEN EXCLUDED.body IS NOT NULL
                                    THEN EXCLUDED.content_sha256
                                    ELSE article.content_sha256 END,
              -- A harvester may raise a status but never lower one.
              body_status    = CASE WHEN {_STATUS_RANK % "EXCLUDED.body_status"}
                                       > {_STATUS_RANK % "article.body_status"}
                                    THEN EXCLUDED.body_status
                                    ELSE article.body_status END,
              -- Whoever holds the body owns the provenance that goes with it.
              source_system  = CASE WHEN EXCLUDED.body IS NOT NULL
                                    THEN EXCLUDED.source_system
                                    ELSE article.source_system END,
              title          = COALESCE(EXCLUDED.title, article.title),
              abstract       = COALESCE(EXCLUDED.abstract, article.abstract),
              publisher_link = COALESCE(EXCLUDED.publisher_link, article.publisher_link),
              wayback_url    = COALESCE(EXCLUDED.wayback_url, article.wayback_url),
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
        cur.execute(
            """
            UPDATE article
               SET body           = %s,
                   body_status    = %s,
                   body_vintage   = %s,
                   wayback_url    = COALESCE(%s, wayback_url),
                   content_sha256 = %s,
                   -- Counted here rather than by the caller because this is the
                   -- only writer, and a count the drain maintained would reset
                   -- every time the process restarted — which, on a six-hourly
                   -- cron, is the normal case rather than the exception.
                   body_attempts  = article.body_attempts + 1,
                   body_last_attempt_at = now()
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

    **Three states qualify, not one.** ``pending`` has never been tried and
    ``transient`` failed for a reason that may not recur, so both come back
    immediately. ``no-capture`` comes back only after
    :data:`config.WAYBACK_RECHECK_DAYS`, because the archive may gain a capture
    later but will not gain one within six hours — and returning it every run
    would spend an unattended drain re-walking the same uncapturable set forever
    while the log showed steady activity.

    This is the whole reason the retryable states exist. The queue used to be
    ``body_status = 'pending'`` alone, against a recovery stage that wrote
    ``failed`` for anything it could not capture: a one-way door, executed four
    times a day with nobody watching.
    """
    immediate = "', '".join(RETRY_IMMEDIATELY)
    where = [f"(body_status IN ('{immediate}')"
             f"  OR (body_status IN ('{BACKED_OFF}')"
             f"      AND (body_last_attempt_at IS NULL"
             f"           OR body_last_attempt_at < now() - %s::interval)))"]
    params: List[Any] = [f"{config.WAYBACK_RECHECK_DAYS} days"]
    if source_system:
        where.append("source_system = %s")
        params.append(source_system)
    sql = (f"SELECT url, country_iso2, source_system, published_at, title, "
           f"       body_status, body_attempts "
           f"FROM article WHERE {' AND '.join(where)} "
           f"ORDER BY published_at, url")
    if limit:
        sql += " LIMIT %s"
        params.append(limit)

    with _transaction() as cur:
        cur.execute(sql, tuple(params))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# The run ledger — every unit of work, whatever kind
# ---------------------------------------------------------------------------
# Harvest windows, scored anchors and the scheduler's own jobs all answer the
# same question — what finished, when, with what status — and used to answer it
# in three tables with three key shapes. One table, one `job_type`, and the
# sentinel defaults on `country_iso2` / `as_of` / `variant` are what let a
# scheduler job with no country share a primary key with a scored anchor that
# has a country, a date and a mode.
#
# They still fail differently, and that difference lives in the callers rather
# than in the storage: a harvest window is re-runnable for free, a scored
# snapshot costs money and must never be paid for twice.

def completed_windows(source_system: str, country_iso2: str) -> set:
    """Window start dates already harvested for this source and country.

    What makes every harvester resumable: a re-run skips these and picks up
    where it stopped, which matters because a Guardian harvest spans days of
    quota and a Wayback drain spans hours of a service that will rate-limit it.
    """
    with _transaction() as cur:
        cur.execute(
            """
            SELECT as_of FROM run_ledger
             WHERE job_type = 'harvest' AND variant = %s
               AND country_iso2 = %s AND status = 'done'
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
    seconds: Optional[float] = None,
    calls: Optional[int] = None,
) -> None:
    """Stamp one (source, country, window) as finished. Idempotent.

    Args:
        seconds: how long the window took, measured by the harvester.
        calls: upstream requests it cost.

    Both are recorded rather than inferred. They were briefly derived from the
    gap between consecutive ``completed_at`` stamps, which is exact only while
    windows run strictly in sequence in one uninterrupted process — and the
    Guardian harvest is neither: it stops on a daily quota and resumes eight
    hours later, so the first window of day two would have read as an
    eight-hour window. A second source running alongside breaks it the same
    way. The harvester holds a clock already; asking it costs nothing and is
    right under interruption, which is the normal case here rather than the
    exception.
    """
    with _transaction() as cur:
        cur.execute(
            """
            INSERT INTO run_ledger
              (job_type, country_iso2, as_of, variant, status, completed_at, detail)
            VALUES ('harvest', %s, %s, %s, %s, now(), %s)
            ON CONFLICT (job_type, country_iso2, as_of, variant) DO UPDATE SET
              status       = EXCLUDED.status,
              completed_at = EXCLUDED.completed_at,
              detail       = EXCLUDED.detail
            """,
            (country_iso2, window_start, source_system, status,
             data_push._json_or_none({"window_end": window_end.isoformat(),
                                      "items_written": items_written,
                                      "note": note,
                                      "seconds": (round(seconds, 1)
                                                  if seconds is not None else None),
                                      "calls": calls})),
        )


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
        mode: one of :data:`config.SCORING_MODES`.
        status: ``'complete'`` | ``'failed'`` | ``'skipped'``. Only
            ``'complete'`` makes a re-run skip the date.
        manifest: the provenance manifest — ``scoring_mode``, ``mask_map_version``,
            and per-article ``source_system`` + ``body_vintage`` — so a row can
            be rebuilt, or found to be unrebuildable.
        result: the model output. Populated for the diagnostic arms, whose
            scores have nowhere else to live; ``None`` for ``'masked'`` runs,
            which are in ``risk_snapshot`` where the front end reads them.

    The run itself goes to ``run_ledger`` whatever the mode — an arm is still
    work that completed, and splitting it out would fork resume and spend
    accounting into two code paths for one concept. A *result* goes to
    ``snapshot_diagnostic``, which is where everything measuring the instrument
    rather than the country lives.

    Raises:
        ValueError: on a mode outside :data:`config.SCORING_MODES` — a typo here
            would quietly split a series in two.
    """
    if mode not in config.SCORING_MODES:
        raise ValueError(f"mode must be one of {config.SCORING_MODES}, got {mode!r}")

    with _transaction() as cur:
        cur.execute(
            """
            INSERT INTO run_ledger
              (job_type, country_iso2, as_of, variant, status, completed_at,
               spend_usd, detail)
            VALUES ('snapshot', %s, %s, %s, %s, now(), %s, %s)
            ON CONFLICT (job_type, country_iso2, as_of, variant) DO UPDATE SET
              status       = EXCLUDED.status,
              spend_usd    = EXCLUDED.spend_usd,
              completed_at = EXCLUDED.completed_at,
              detail       = COALESCE(EXCLUDED.detail, run_ledger.detail)
            """,
            (country_iso2, as_of, mode, status, spend_usd,
             data_push._json_or_none({"manifest": manifest} if manifest else None)),
        )
        if result is not None:
            cur.execute(
                """
                INSERT INTO snapshot_diagnostic
                  (country_iso2, as_of, kind, variant, detail)
                VALUES (%s, %s, 'arm', %s, %s)
                ON CONFLICT (country_iso2, as_of, kind, variant) DO UPDATE SET
                  detail = EXCLUDED.detail
                """,
                (country_iso2, as_of, mode, data_push._json_or_none(result)),
            )


def completed_runs(mode: str, country_iso2: Optional[str] = None) -> set:
    """Anchor dates already scored in this mode. The pilot's resume point.

    Only ``status = 'complete'`` counts, so a run that died half way through a
    country is retried rather than silently skipped — the same rule
    :func:`completed_windows` uses for harvests.
    """
    sql = ["SELECT as_of FROM run_ledger "
           "WHERE job_type = 'snapshot' AND variant = %s AND status = 'complete'"]
    params: List[Any] = [mode]
    if country_iso2:
        sql.append("AND country_iso2 = %s")
        params.append(country_iso2)
    with _transaction() as cur:
        cur.execute(" ".join(sql), tuple(params))
        return {row[0] for row in cur.fetchall()}


def read_frozen_versions() -> Optional[Dict[str, str]]:
    """The version set pinned at pilot start, or None before the first run.

    One sentinel row — ``job_type = 'pilot-freeze'`` with the empty country,
    epoch anchor and empty variant `run_ledger` already uses for work that has
    no country and no date. The ledger is the right home for it because it is
    the same thing the ledger already holds for spend: state that must survive
    a restart, since a governor held in memory is no governor at all.
    """
    with _transaction() as cur:
        cur.execute("SELECT detail FROM run_ledger WHERE job_type = 'pilot-freeze'")
        row = cur.fetchone()
    return ((row or [None])[0] or {}).get("versions") or None


def write_frozen_versions(versions: Dict[str, str]) -> None:
    """Pin ``versions`` as the set this pilot is running under. Idempotent.

    Overwrites, because the only two callers are the first start (nothing to
    overwrite) and an acknowledged drift override (where recording the new set
    is the point — an override that left the old row would refuse again on the
    next resume and teach the operator to pass the flag by reflex).
    """
    with _transaction() as cur:
        cur.execute(
            """
            INSERT INTO run_ledger (job_type, status, detail)
            VALUES ('pilot-freeze', 'complete', %s)
            ON CONFLICT (job_type, country_iso2, as_of, variant)
            DO UPDATE SET detail = EXCLUDED.detail, completed_at = now()
            """,
            (data_push._json_or_none({"versions": versions}),),
        )


def total_spend_usd() -> float:
    """Every dollar the pilot has metered so far, across runs and processes.

    The budget governor's memory. Held in the ledger rather than in the runner
    so that stopping and resuming a multi-hour pilot cannot reset the budget to
    zero and quietly spend it twice.
    """
    with _transaction() as cur:
        cur.execute("SELECT COALESCE(SUM(spend_usd), 0) FROM run_ledger")
        return float(cur.fetchone()[0])


def read_runs(mode: Optional[str] = None) -> List[Dict[str, Any]]:
    """Ledger rows, oldest first — what ``reports.py`` renders its meters from.

    The arm's ``result`` is joined back from ``snapshot_diagnostic`` rather than
    duplicated into the ledger, so there is one copy of a measured score and it
    lives with the other measurements.
    """
    where = "AND r.variant = %s" if mode else ""
    with _transaction() as cur:
        cur.execute(f"""
            SELECT r.as_of, r.country_iso2, r.variant AS mode, r.status,
                   r.spend_usd, r.detail -> 'manifest' AS manifest,
                   d.detail AS result
              FROM run_ledger r
              LEFT JOIN snapshot_diagnostic d
                     ON d.country_iso2 = r.country_iso2
                    AND d.as_of        = r.as_of
                    AND d.variant      = r.variant
                    AND d.kind         = 'arm'
             WHERE r.job_type = 'snapshot' {where}
             ORDER BY r.country_iso2, r.as_of, r.variant
        """, (mode,) if mode else ())
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# The model-output cache — the one that makes a weekly cadence affordable
# ---------------------------------------------------------------------------
# Weekly anchors with a 30-day window overlap about four times, so without a
# cache the pilot would pay to digest each article four times over. Keyed on
# content because the same article appears in four different snapshots and its
# digest is identical in all of them.
#
# `mode` is in the key because the masked and named digests of one article are
# genuinely different texts, and must never be served for each other. `kind`
# separates a digest from a full-text rewrite, which are the same shape of fact
# — this model, this version, this text, this output — and used to be two
# tables saying it twice.

def read_digest_cache(hashes: Sequence[str], digest_model: str,
                      mode: str) -> Dict[str, Dict[str, Any]]:
    """Cached digests for these content hashes, keyed by hash.

    A miss is an absent key, never a null row: the caller re-digests whatever is
    missing, which is also what happens the first time a mask map changes and
    every masked hash is new.
    """
    if not hashes:
        return {}
    with _transaction() as cur:
        cur.execute(
            """
            SELECT content_sha256, payload, stage1_severity
              FROM llm_artifact
             WHERE kind = 'digest' AND content_sha256 = ANY(%s)
               AND version = %s AND mode = %s
            """,
            (list(hashes), digest_model, mode),
        )
        return {r[0]: {"digest": r[1], "stage1_severity": r[2]} for r in cur.fetchall()}


def write_digest_cache(rows: Sequence[Dict[str, Any]], digest_model: str,
                       mode: str) -> int:
    """Cache digests by content hash.

    Args:
        rows: dicts with ``content_sha256``, ``digest`` and ``stage1_severity``.
            Rows without a hash or without a digest are dropped — a failed
            digest must be retried next time, not cached as a failure.

    Returns:
        How many rows were written.
    """
    values = [
        (r["content_sha256"], "digest", digest_model, mode,
         data_push._json_or_none(r["digest"]), r.get("stage1_severity"))
        for r in rows
        if r.get("content_sha256") and isinstance(r.get("digest"), dict)
    ]
    if not values:
        return 0
    with _transaction() as cur:
        extras.execute_values(
            cur,
            """
            INSERT INTO llm_artifact
              (content_sha256, kind, version, mode, payload, stage1_severity)
            VALUES %s
            ON CONFLICT (content_sha256, kind, version, mode) DO NOTHING
            """,
            values,
            page_size=200,
        )
    return len(values)


def read_context_cache(keys: Sequence[str], context_version: str,
                       mode: str) -> Dict[str, str]:
    """Cached trailing-context paragraphs for these keys, keyed by key.

    Modelled on the rewrite pair rather than the digest pair because a context
    paragraph is one string, not a structured digest — but it reads a *set* of
    keys, because an anchor needs four quarters at once and four round trips to
    fetch four short strings is three too many.

    A miss is an absent key, and an empty paragraph is never a hit: an empty
    context is how generation reports failure, and caching that would silence
    the block for every future anchor in the quarter rather than letting the
    next run try again.
    """
    if not keys:
        return {}
    with _transaction() as cur:
        cur.execute(
            """
            SELECT content_sha256, payload ->> 'text'
              FROM llm_artifact
             WHERE kind = 'context' AND content_sha256 = ANY(%s)
               AND version = %s AND mode = %s
            """,
            (list(keys), context_version, mode),
        )
        return {row[0]: row[1] for row in cur.fetchall() if row[1]}


def write_context_cache(rows: Sequence[Dict[str, Any]], context_version: str,
                        mode: str) -> int:
    """Cache trailing-context paragraphs.

    Args:
        rows: dicts with ``key`` and ``text``. The key is not a hash of one
            article's body — it hashes the *set of articles the quarter
            selected*, so a re-harvest or a recovered body changes it and the
            paragraph is rebuilt. Keying on ``(country, quarter)`` alone would
            serve a paragraph written before half the evidence arrived, and
            would do it silently, because a quarter that has gained articles
            looks exactly like one that has not.
    """
    values = [(r["key"], "context", context_version, mode,
               data_push._json_or_none({"text": r["text"]}))
              for r in rows
              if r.get("key") and (r.get("text") or "").strip()]
    if not values:
        return 0
    with _transaction() as cur:
        extras.execute_values(
            cur,
            """
            INSERT INTO llm_artifact
              (content_sha256, kind, version, mode, payload)
            VALUES %s
            ON CONFLICT (content_sha256, kind, version, mode) DO NOTHING
            """,
            values,
            page_size=200,
        )
    return len(values)


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
        cur.execute(
            """
            SELECT content_sha256, payload ->> 'rewritten'
              FROM llm_artifact
             WHERE kind = 'rewrite' AND content_sha256 = ANY(%s)
               AND version = %s AND mode = %s
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
    values = [(r["content_sha256"], "rewrite", rewrite_version, mode,
               data_push._json_or_none({"rewritten": r["rewritten"]}))
              for r in rows
              if r.get("content_sha256") and (r.get("rewritten") or "").strip()]
    if not values:
        return 0
    with _transaction() as cur:
        extras.execute_values(
            cur,
            """
            INSERT INTO llm_artifact
              (content_sha256, kind, version, mode, payload)
            VALUES %s
            ON CONFLICT (content_sha256, kind, version, mode) DO NOTHING
            """,
            values,
            page_size=200,
        )
    return len(values)


# ---------------------------------------------------------------------------
# Reports — the deliverables of steps 2, 3 and 4
# ---------------------------------------------------------------------------

def _rows(sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    """Run one read against the substrate."""
    with _transaction() as cur:
        cur.execute(sql, tuple(params))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def counts_by_year() -> List[Dict[str, Any]]:
    """Article counts per source x country x year."""
    return _rows("""
        SELECT source_system, country_iso2,
               EXTRACT(YEAR FROM published_at)::int AS year, COUNT(*)::int AS n
          FROM article
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
          FROM article {where}
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
          FROM article
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
          FROM article
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
        "SELECT url FROM article WHERE url = ANY(%s)", (list(urls),))}
