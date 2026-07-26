"""The backend's entire Postgres write layer.

Every table the dashboard reads is written here, and the frontend queries
those tables directly — so this module's SQL *is* the contract between the two
halves of the project. Change a column name here and the Next.js server
breaks; there is no API layer in between to absorb it.

The project has no migration tool, so each writer owns its table's DDL and
issues ``CREATE TABLE IF NOT EXISTS`` before writing. That keeps a fresh
database self-provisioning at the cost of a cheap no-op statement per call.

All writes go through ``_transaction()``, which owns connect/commit/rollback/
close. Upserts use ``COALESCE`` where a transient null (a symbol missing from
one quote batch, an unranked event) must not blank a previously-good value.
"""

import os
import datetime
from contextlib import contextmanager
from typing import Dict, Any, Iterator, NamedTuple, Optional, List, Tuple

import psycopg2
import psycopg2.extras as extras


def _require_db_url() -> str:
    """Read DATABASE_URL at call time (never cached at import)."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL is not set in the environment")
    return db_url


@contextmanager
def _transaction() -> Iterator["psycopg2.extensions.cursor"]:
    """One connection, one transaction.

    Yields a cursor; commits when the block finishes, rolls back and re-raises
    on any exception, and always closes the connection. Every write in this
    module goes through here so commit/rollback semantics live in one place.
    """
    conn = psycopg2.connect(_require_db_url())
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _image_url_or_none(img: Any) -> Optional[str]:
    """Normalize an image value (str or list) to a single http(s) URL, or None."""
    if isinstance(img, str):
        u = img.strip()
        return u if u.startswith(("http://", "https://")) else None
    if isinstance(img, list):
        for v in img:
            if isinstance(v, str) and v.strip().startswith(("http://", "https://")):
                return v.strip()
    return None


def _to_date_from_iso(s: str) -> datetime.date:
    """Parse the payload's ``generated_at`` into the snapshot's ``as_of`` date.

    Accepts ``'YYYY-MM-DD'`` or a full ISO timestamp (trailing ``Z`` allowed).

    Raises:
        ValueError: if ``s`` is empty or not an ISO date/datetime. This one is
            strict on purpose — ``as_of`` is half of the snapshot's primary
            key, so a bad value would silently write to the wrong day.
    """
    if not s:
        raise ValueError("Empty generated_at timestamp")
    try:
        # fast path YYYY-MM-DD
        return datetime.date.fromisoformat(s[:10])
    except Exception:
        # last resort
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.date()


def _to_ts_or_none(s: Optional[str]) -> Optional[datetime.datetime]:
    """Parse an article's publish time, or None if absent/unparseable.

    Lenient by design (unlike ``_to_date_from_iso``): publishers emit all kinds
    of date formats, and a missing timestamp on one article is not worth
    failing a snapshot over — the column is nullable.
    """
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# The `country` table is operator-provisioned (see backend/README.md), but the
# map-position columns were added later, so bring an existing database up to
# date without a migration tool.
_COUNTRY_GEO_DDL = """
ALTER TABLE country ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION;
ALTER TABLE country ADD COLUMN IF NOT EXISTS lng DOUBLE PRECISION;
"""


def upsert_countries(roster: List[Dict[str, Any]]) -> None:
    """Seed the `country` table from the roster: names and map positions.

    This is reference data, not per-run output, so it is written once at the
    start of a run rather than as a side effect of scoring. That matters for
    two reasons: the front-end reads countries, display names and map marker
    positions straight from this table (it holds no country list of its own),
    and a country therefore appears on the map as soon as it is added to
    ``constants.COUNTRY_ROSTER`` — before it has any risk snapshot.

    Uses DO UPDATE rather than DO NOTHING so a renamed country or a corrected
    coordinate actually propagates on the next run.

    Args:
        roster: ``constants.COUNTRY_ROSTER`` entries; each needs ``iso2``,
            ``name``, ``lat`` and ``lng``.

    Raises:
        ValueError: if an entry is missing one of those fields — a silent skip
            here would strand a country off the map with no obvious cause.
    """
    _require_db_url()  # fail fast even when there is nothing to write
    if not roster:
        return

    rows: List[Tuple] = []
    for entry in roster:
        missing = [k for k in ("iso2", "name", "lat", "lng") if entry.get(k) is None]
        if missing:
            raise ValueError(
                f"roster entry {entry.get('iso2') or entry!r} is missing {missing}; "
                "every country needs iso2/name/lat/lng to render on the map"
            )
        rows.append((entry["iso2"], entry["name"], float(entry["lat"]), float(entry["lng"])))

    with _transaction() as cur:
        cur.execute(_COUNTRY_GEO_DDL)
        extras.execute_values(
            cur,
            """
            INSERT INTO country (iso2, name, lat, lng)
            VALUES %s
            ON CONFLICT (iso2)
            DO UPDATE SET
              name = EXCLUDED.name,
              lat  = EXCLUDED.lat,
              lng  = EXCLUDED.lng
            """,
            rows,
            page_size=100,
        )


class _SnapshotData(NamedTuple):
    """Validated fields pulled out of an upsert_snapshot payload.

    Everything from ``score_3m`` down arrived with the perception/policy split
    and defaults to None, so a payload produced before it still upserts — those
    columns simply stay NULL for that row.
    """
    country: str
    as_of: datetime.date
    units: Dict[str, str]
    indicators: Dict[str, Any]
    llm_out: Dict[str, Any]
    top_articles: List[Dict[str, Any]]
    # Both horizons, before and after policy. `llm_out["score"]` remains the
    # gated 12-month score, unchanged in name and meaning.
    score_3m: Optional[float] = None
    raw_score_12m: Optional[float] = None
    raw_score_3m: Optional[float] = None
    # JSONB columns: the model's perception and what policy did with it.
    subscores: Optional[Dict[str, Any]] = None
    raw_subscores: Optional[Dict[str, Any]] = None
    subscore_evidence: Optional[Dict[str, Any]] = None
    condition_flags: Optional[Dict[str, Any]] = None
    article_scores: Optional[List[Dict[str, Any]]] = None
    applied_rules: Optional[List[str]] = None
    evidence_coverage: Optional[float] = None
    # Provenance: which model, prompt and policy produced this row.
    model_id: Optional[str] = None
    prompt_version: Optional[str] = None
    policy_version: Optional[str] = None


class _ArticleRow(NamedTuple):
    """One risk_snapshot_article row; field order matches the INSERT columns."""
    country_iso2: str
    as_of: datetime.date
    rank: int
    url: str
    title: Optional[str]
    source: Optional[str]
    published_at: Optional[datetime.datetime]
    impact: Optional[float]
    summary: Optional[str]
    image_url: Optional[str]


def payload_as_of(payload: Dict[str, Any]) -> datetime.date:
    """The ``as_of`` date this payload's snapshot will be keyed on.

    Public so the pipeline can key other same-run rows (article digests) on
    exactly the date ``upsert_snapshot`` will use — both read
    ``payload["_meta"]["generated_at"]``, so the two keys can never disagree.

    Raises:
        ValueError: if ``payload['_meta']['generated_at']`` is missing, not a
            string, or not an ISO date/datetime.
    """
    meta = payload.get("_meta") or {}
    gen_at = meta.get("generated_at")
    if not gen_at or not isinstance(gen_at, str):
        raise ValueError("payload['_meta']['generated_at'] must be a string ISO timestamp")
    return _to_date_from_iso(gen_at)


def _parse_snapshot_payload(payload: Dict[str, Any]) -> _SnapshotData:
    """Validate an upsert_snapshot payload and extract the fields it writes.

    Raises:
        TypeError/ValueError: describing the missing or malformed field.
    """
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")

    country = payload.get("country")
    if not country or not isinstance(country, str):
        raise ValueError("payload['country'] must be an ISO-2 string")

    as_of = payload_as_of(payload)
    units = (payload.get("_meta") or {}).get("units") or {}
    if not isinstance(units, dict):
        raise ValueError("payload['_meta']['units'] must be a dict of indicator -> unit")

    indicators = payload.get("indicators") or {}
    if not isinstance(indicators, dict) or not indicators:
        raise ValueError("payload['indicators'] must be a non-empty dict")

    llm_out = payload.get("llm_output") or {}
    if not (isinstance(llm_out, dict) and {"score", "bullet_summary"} <= set(llm_out.keys())):
        raise ValueError("payload['llm_output'] must include 'score' and 'bullet_summary'")

    top_articles = payload.get("top_articles") or []
    if not isinstance(top_articles, list):
        top_articles = []

    return _SnapshotData(
        country=country,
        as_of=as_of,
        units=units,
        indicators=indicators,
        llm_out=llm_out,
        top_articles=top_articles,
        score_3m=llm_out.get("score_3m"),
        raw_score_12m=llm_out.get("raw_score_12m"),
        raw_score_3m=llm_out.get("raw_score_3m"),
        subscores=llm_out.get("subscores"),
        raw_subscores=llm_out.get("raw_subscores"),
        subscore_evidence=llm_out.get("subscore_evidence"),
        condition_flags=llm_out.get("condition_flags"),
        # Every article's impact and topic group, not just the displayed
        # top-3: the full cross-section is the point of storing it.
        article_scores=llm_out.get("news_article_scores"),
        applied_rules=llm_out.get("applied_rules"),
        evidence_coverage=llm_out.get("evidence_coverage"),
        model_id=llm_out.get("model_id"),
        prompt_version=llm_out.get("prompt_version"),
        policy_version=llm_out.get("policy_version"),
    )


# `risk_snapshot` is operator-provisioned (see backend/README.md), but the
# perception/policy split added columns to it: both horizons raw and gated, the
# model's sub-factor detail, and the provenance stamps. Additive and idempotent
# so an existing database comes up to date without a migration tool.
_RISK_SNAPSHOT_DDL = """
ALTER TABLE risk_snapshot
  ADD COLUMN IF NOT EXISTS raw_score_12m     DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS raw_score_3m      DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS score_3m          DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS subscores         JSONB,
  ADD COLUMN IF NOT EXISTS raw_subscores     JSONB,
  ADD COLUMN IF NOT EXISTS subscore_evidence JSONB,
  ADD COLUMN IF NOT EXISTS condition_flags   JSONB,
  ADD COLUMN IF NOT EXISTS article_scores    JSONB,
  ADD COLUMN IF NOT EXISTS applied_rules     JSONB,
  ADD COLUMN IF NOT EXISTS evidence_coverage DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS model_id          TEXT,
  ADD COLUMN IF NOT EXISTS prompt_version    TEXT,
  ADD COLUMN IF NOT EXISTS policy_version    TEXT;
"""

# ALTER TABLE takes an ACCESS EXCLUSIVE lock even when every column already
# exists, and the country loop calls upsert_snapshot once per country. Run it
# once per process instead. Set only after the transaction commits, so a
# rolled-back run re-issues it.
_risk_snapshot_ddl_done = False


def _json_or_none(value: Any) -> Optional[extras.Json]:
    """Wrap a value for a JSONB column, leaving None as SQL NULL."""
    return None if value is None else extras.Json(value)


def _article_rows(country: str, as_of: datetime.date,
                  top_articles: List[Dict[str, Any]]) -> List[_ArticleRow]:
    """Build risk_snapshot_article rows, dropping malformed entries."""
    rows: List[_ArticleRow] = []
    for a in top_articles:
        if not isinstance(a, dict):
            continue
        rank = a.get("rank")
        url = (a.get("url") or "").strip()
        if not url or rank not in (1, 2, 3):
            continue
        rows.append(_ArticleRow(
            country_iso2=country,
            as_of=as_of,
            rank=int(rank),
            url=url,
            title=a.get("title"),
            source=a.get("source"),
            published_at=_to_ts_or_none(a.get("published_at")),
            impact=(float(a["impact"]) if (a.get("impact") is not None) else None),
            summary=a.get("summary"),
            image_url=_image_url_or_none(a.get("image")),
        ))
    return rows


def upsert_snapshot(payload: Dict[str, Any], country_name: str) -> None:
    """
    Atomically insert or update a country-level snapshot.

    Writes to:
      • country                 (ensures parent row for FK)
      • indicator               (upsert by name, keeps unit updated)
      • yearly_value            (upsert by (country_iso2, indicator_id, yr))
      • risk_snapshot           (upsert by (country_iso2, as_of))
      • risk_snapshot_article   (top-3 links for this snapshot; optional; includes image_url)

    Expects in `payload`:
      - country (str ISO-2)
      - _meta.generated_at (ISO datetime string)
      - _meta.units (dict: indicator_name -> unit)
      - indicators (dict: indicator_name -> {"series": {year: value or None}})
      - llm_output.score, llm_output.bullet_summary

    Optional:
      - top_articles: list of dicts with
          {rank, url, title, source, published_at (ISO), impact, summary, image?}
      - the rest of ``llm_output`` (score_3m, raw_score_*, raw_subscores,
        subscore_evidence, condition_flags, news_article_scores, applied_rules,
        evidence_coverage, model_id, prompt_version, policy_version). Absent
        keys write NULL, so a payload from before the perception/policy split
        still upserts.
    """
    global _risk_snapshot_ddl_done

    _require_db_url()  # fail fast before any parsing work
    data = _parse_snapshot_payload(payload)
    rows_art = _article_rows(data.country, data.as_of, data.top_articles)

    with _transaction() as cur:
        if not _risk_snapshot_ddl_done:
            cur.execute(_RISK_SNAPSHOT_DDL)

        # 0) Ensure the parent 'country' row exists for the FK
        cur.execute(
            """
            INSERT INTO country (iso2, name)
            VALUES (%s, %s)
            ON CONFLICT (iso2) DO NOTHING
            """,
            (data.country, country_name),
        )

        # 1) Indicators + yearly series
        for ind_name, ind_data in data.indicators.items():
            unit = data.units[ind_name]  # rely on your existing contract; raises if missing

            # 1a) Upsert indicator row, capture its id
            cur.execute(
                """
                INSERT INTO indicator (name, unit)
                VALUES (%s, %s)
                ON CONFLICT (name)
                DO UPDATE SET unit = EXCLUDED.unit
                RETURNING id;
                """,
                (ind_name, unit),
            )
            ind_id = cur.fetchone()[0]

            # 1b) Prepare yearly rows (skip nulls)
            series = (ind_data or {}).get("series", {}) or {}
            rows_yv: List[Tuple[str, int, int, float]] = []
            for year, val in series.items():
                if val is None:
                    continue
                try:
                    yr_int = int(year)
                    val_f = float(val)
                except Exception:
                    continue
                rows_yv.append((data.country, ind_id, yr_int, val_f))

            if rows_yv:
                extras.execute_values(
                    cur,
                    """
                    INSERT INTO yearly_value (country_iso2, indicator_id, yr, value)
                    VALUES %s
                    ON CONFLICT (country_iso2, indicator_id, yr)
                    DO UPDATE SET value = EXCLUDED.value
                    """,
                    rows_yv,
                    page_size=1000,
                )

        # 2) Risk snapshot (latest AI score for the run date). `score` stays
        #    the gated 12-month score on the 0-1 scale — the front-end reads
        #    it and its meaning has not changed. Everything else is additive:
        #    the raw scores the model gave before policy, the sub-factor detail
        #    behind them, and which model/prompt/policy produced the row.
        cur.execute(
            """
            INSERT INTO risk_snapshot (
              country_iso2, as_of, score, bullet_summary,
              score_3m, raw_score_12m, raw_score_3m,
              subscores, raw_subscores, subscore_evidence, condition_flags,
              article_scores, applied_rules, evidence_coverage,
              model_id, prompt_version, policy_version
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (country_iso2, as_of)
            DO UPDATE SET
              score             = EXCLUDED.score,
              bullet_summary    = EXCLUDED.bullet_summary,
              score_3m          = EXCLUDED.score_3m,
              raw_score_12m     = EXCLUDED.raw_score_12m,
              raw_score_3m      = EXCLUDED.raw_score_3m,
              subscores         = EXCLUDED.subscores,
              raw_subscores     = EXCLUDED.raw_subscores,
              subscore_evidence = EXCLUDED.subscore_evidence,
              condition_flags   = EXCLUDED.condition_flags,
              article_scores    = EXCLUDED.article_scores,
              applied_rules     = EXCLUDED.applied_rules,
              evidence_coverage = EXCLUDED.evidence_coverage,
              model_id          = EXCLUDED.model_id,
              prompt_version    = EXCLUDED.prompt_version,
              policy_version    = EXCLUDED.policy_version
            """,
            (
                data.country, data.as_of,
                data.llm_out["score"], data.llm_out["bullet_summary"],
                data.score_3m, data.raw_score_12m, data.raw_score_3m,
                _json_or_none(data.subscores),
                _json_or_none(data.raw_subscores),
                _json_or_none(data.subscore_evidence),
                _json_or_none(data.condition_flags),
                _json_or_none(data.article_scores),
                _json_or_none(data.applied_rules),
                data.evidence_coverage,
                data.model_id, data.prompt_version, data.policy_version,
            ),
        )

        # 3) Optional: write the top-3 links for this snapshot (includes image_url)
        if rows_art:
            extras.execute_values(
                cur,
                """
                INSERT INTO risk_snapshot_article
                  (country_iso2, as_of, rank, url, title, source, published_at, impact, summary, image_url)
                VALUES %s
                ON CONFLICT (country_iso2, as_of, rank)
                DO UPDATE SET
                  url          = EXCLUDED.url,
                  title        = EXCLUDED.title,
                  source       = EXCLUDED.source,
                  published_at = EXCLUDED.published_at,
                  impact       = EXCLUDED.impact,
                  summary      = EXCLUDED.summary,
                  image_url    = EXCLUDED.image_url,
                  updated_at   = now()
                """,
                rows_art,
                page_size=10,
            )

    # Committed — the columns are there for the rest of this process.
    _risk_snapshot_ddl_done = True


_RECENT_INDICATOR_DDL = """
CREATE TABLE IF NOT EXISTS recent_indicator (
    country_iso2 CHAR(2)  NOT NULL,
    indicator    TEXT     NOT NULL,        -- display name, matches indicator.name
    period       DATE     NOT NULL,        -- end-of-period date of the observation
    freq         CHAR(1)  NOT NULL CHECK (freq IN ('M','Q','A')),
    value        DOUBLE PRECISION NOT NULL,
    unit         TEXT,
    source       TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (country_iso2, indicator)
);
"""


def upsert_recent_indicators(country_iso2: str, indicators: Dict[str, Dict[str, Any]]) -> None:
    """Upsert the freshest sub-annual observation per (country, indicator).

    Self-contained: ensures the ``recent_indicator`` table exists (the project has
    no migration tool; this adds one table without touching the pre-created risk
    schema), then upserts one row per (country, indicator) keyed by the indicator's
    display name. The front-end prefers these values over the World Bank annual
    ``yearly_value`` and falls back to the annual one when a country has no fresh
    row for an indicator.

    Args:
        country_iso2: ISO-2 country code (the DB country key, e.g. ``'AR'``).
        indicators: ``{indicator_name: {value, period (date), freq, unit?, source?}}``
            as produced by ``imf_macro_fetch.fetch_recent_indicators`` (keyed back
            from ISO-3 to ISO-2 by the caller). Rows missing value/period/freq are
            skipped.

    No-op if ``country_iso2`` is blank or ``indicators`` is empty.
    """
    _require_db_url()  # fail fast even when there is nothing to write
    if not country_iso2 or not indicators:
        return

    rows: List[Tuple] = []
    for name, d in indicators.items():
        if not isinstance(d, dict):
            continue
        value = d.get("value")
        period = d.get("period")
        freq = d.get("freq")
        if value is None or period is None or freq not in ("M", "Q", "A"):
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        rows.append((country_iso2, name, period, freq, value, d.get("unit"), d.get("source")))

    if not rows:
        return

    with _transaction() as cur:
        cur.execute(_RECENT_INDICATOR_DDL)
        extras.execute_values(
            cur,
            """
            INSERT INTO recent_indicator
              (country_iso2, indicator, period, freq, value, unit, source)
            VALUES %s
            ON CONFLICT (country_iso2, indicator)
            DO UPDATE SET
              period     = EXCLUDED.period,
              freq       = EXCLUDED.freq,
              value      = EXCLUDED.value,
              unit       = EXCLUDED.unit,
              source     = EXCLUDED.source,
              updated_at = now()
            """,
            rows,
            page_size=100,
        )


_ECON_EVENT_DDL = """
CREATE TABLE IF NOT EXISTS economic_calendar_event (
    id           BIGSERIAL PRIMARY KEY,
    event_time   TIMESTAMPTZ NOT NULL,
    country_code TEXT NOT NULL,
    country_name TEXT NOT NULL,
    event        TEXT NOT NULL,
    importance   TEXT NOT NULL CHECK (importance IN ('h','m','l')),
    currency     TEXT,
    previous     DOUBLE PRECISION,
    estimate     DOUBLE PRECISION,
    actual       DOUBLE PRECISION,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_time, country_code, event)
);
-- AI importance ranking (US-tilted). Added idempotently so an already-created
-- table picks up the columns without a migration tool.
ALTER TABLE economic_calendar_event ADD COLUMN IF NOT EXISTS ai_importance DOUBLE PRECISION;
ALTER TABLE economic_calendar_event ADD COLUMN IF NOT EXISTS ai_rationale  TEXT;
ALTER TABLE economic_calendar_event ADD COLUMN IF NOT EXISTS ai_scored_at  TIMESTAMPTZ;
"""


def upsert_economic_events(events: List[Dict[str, Any]]) -> None:
    """Upsert upcoming economic-calendar events for the front-end Econ Calendar pane.

    Self-contained: ensures the ``economic_calendar_event`` table exists (the
    project has no migration tool; this adds one table without touching the
    pre-created risk schema), bulk-upserts the rolling window, and prunes rows
    older than a day so the table stays a forward-looking feed.

    Each event dict (as produced by ``fmp_calendar_fetch.fetch_economic_calendar``):
      - event_time   (aware UTC datetime)  — release date & time
      - country_code (str, FMP 2-letter)   — e.g. 'US', 'EU'
      - country_name (str)                 — display name
      - event        (str)                 — release/decision name
      - importance   (str: 'h'|'m'|'l')    — criticality
      - currency, previous, estimate, actual (optional)
      - ai_importance (float 0..1), ai_rationale (str), ai_scored_at (datetime)
        — optional AI ranking (only the next-14-day subset; null otherwise).
        Nulls never overwrite an existing score (preserved via COALESCE).

    No-op if ``events`` is empty.
    """
    _require_db_url()  # fail fast even when there is nothing to write
    if not events:
        return

    rows: List[Tuple] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        event_time = e.get("event_time")
        code = (e.get("country_code") or "").strip()
        event = (e.get("event") or "").strip()
        importance = (e.get("importance") or "").strip()
        if not event_time or not code or not event or importance not in ("h", "m", "l"):
            continue
        ai_importance = e.get("ai_importance")
        try:
            ai_importance = float(ai_importance) if ai_importance is not None else None
        except (TypeError, ValueError):
            ai_importance = None

        rows.append(
            (
                event_time,
                code,
                e.get("country_name") or code,
                event,
                importance,
                e.get("currency"),
                e.get("previous"),
                e.get("estimate"),
                e.get("actual"),
                ai_importance,
                e.get("ai_rationale"),
                e.get("ai_scored_at"),
            )
        )

    if not rows:
        return

    with _transaction() as cur:
        cur.execute(_ECON_EVENT_DDL)

        extras.execute_values(
            cur,
            """
            INSERT INTO economic_calendar_event
              (event_time, country_code, country_name, event, importance,
               currency, previous, estimate, actual,
               ai_importance, ai_rationale, ai_scored_at)
            VALUES %s
            ON CONFLICT (event_time, country_code, event)
            DO UPDATE SET
              country_name  = EXCLUDED.country_name,
              importance    = EXCLUDED.importance,
              currency      = EXCLUDED.currency,
              previous      = EXCLUDED.previous,
              estimate      = EXCLUDED.estimate,
              actual        = EXCLUDED.actual,
              ai_importance = COALESCE(EXCLUDED.ai_importance, economic_calendar_event.ai_importance),
              ai_rationale  = COALESCE(EXCLUDED.ai_rationale,  economic_calendar_event.ai_rationale),
              ai_scored_at  = COALESCE(EXCLUDED.ai_scored_at,  economic_calendar_event.ai_scored_at),
              updated_at    = now()
            """,
            rows,
            page_size=500,
        )

        # Keep the table a rolling forward window.
        cur.execute(
            "DELETE FROM economic_calendar_event WHERE event_time < now() - interval '1 day'"
        )


_MARKET_PRICE_DDL = """
CREATE TABLE IF NOT EXISTS market_price (
    symbol        TEXT PRIMARY KEY,
    label         TEXT NOT NULL,
    asset_class   TEXT NOT NULL CHECK (asset_class IN ('stocks','bonds','crypto','commodities')),
    source_symbol TEXT,
    is_yield      BOOLEAN NOT NULL DEFAULT FALSE,
    px            DOUBLE PRECISION,
    chg           DOUBLE PRECISION,   -- 1D  (% for prices, points for yields)
    q             DOUBLE PRECISION,   -- 1Q
    ytd           DOUBLE PRECISION,   -- YTD
    sort_order    INTEGER NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_PRICE_REFERENCE_DDL = """
CREATE TABLE IF NOT EXISTS price_reference (
    symbol                 TEXT PRIMARY KEY,
    ref_q                  DOUBLE PRECISION,
    ref_q_date             DATE,
    ref_ytd                DOUBLE PRECISION,
    ref_ytd_date           DATE,
    reference_refreshed_on DATE,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def upsert_market_prices(rows: List[Dict[str, Any]]) -> None:
    """Upsert the latest snapshot of the Prices pane (one row per symbol).

    Self-contained: ensures the ``market_price`` table exists, then upserts each
    asset by its stable ``symbol`` primary key. The metric columns
    (``px``/``chg``/``q``/``ytd``) are written with COALESCE so a transient null
    (e.g. a missing 1Q/YTD reference, or a symbol absent from one quote batch)
    never blanks a previously-populated cell — the daemon simply omits whole
    rows for markets it didn't poll this tick, leaving their last values intact.

    Each row dict (as built by ``prices_daemon``):
      - symbol, label, asset_class, source_symbol, is_yield, sort_order  (metadata)
      - px, chg, q, ytd                                                  (metrics; may be None)

    No-op if ``rows`` is empty.
    """
    _require_db_url()  # fail fast even when there is nothing to write
    if not rows:
        return

    tuples: List[Tuple] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        symbol = (r.get("symbol") or "").strip()
        label = (r.get("label") or "").strip()
        asset_class = (r.get("asset_class") or "").strip()
        if not symbol or not label or asset_class not in ("stocks", "bonds", "crypto", "commodities"):
            continue
        tuples.append(
            (
                symbol,
                label,
                asset_class,
                r.get("source_symbol"),
                bool(r.get("is_yield")),
                r.get("px"),
                r.get("chg"),
                r.get("q"),
                r.get("ytd"),
                int(r.get("sort_order") or 0),
            )
        )

    if not tuples:
        return

    with _transaction() as cur:
        cur.execute(_MARKET_PRICE_DDL)

        extras.execute_values(
            cur,
            """
            INSERT INTO market_price
              (symbol, label, asset_class, source_symbol, is_yield,
               px, chg, q, ytd, sort_order)
            VALUES %s
            ON CONFLICT (symbol)
            DO UPDATE SET
              label         = EXCLUDED.label,
              asset_class   = EXCLUDED.asset_class,
              source_symbol = EXCLUDED.source_symbol,
              is_yield      = EXCLUDED.is_yield,
              px            = COALESCE(EXCLUDED.px,  market_price.px),
              chg           = COALESCE(EXCLUDED.chg, market_price.chg),
              q             = COALESCE(EXCLUDED.q,   market_price.q),
              ytd           = COALESCE(EXCLUDED.ytd, market_price.ytd),
              sort_order    = EXCLUDED.sort_order,
              updated_at    = now()
            """,
            tuples,
            page_size=100,
        )


def read_price_references() -> Dict[str, Dict[str, Any]]:
    """Return stored 1Q/YTD reference closes, keyed by symbol.

    Ensures the ``price_reference`` table exists first so the daemon can call this
    on startup before any write. Each value is
    ``{ref_q, ref_q_date, ref_ytd, ref_ytd_date, reference_refreshed_on}``.
    """
    with _transaction() as cur:
        cur.execute(_PRICE_REFERENCE_DDL)
        cur.execute(
            """
            SELECT symbol, ref_q, ref_q_date, ref_ytd, ref_ytd_date, reference_refreshed_on
              FROM price_reference
            """
        )
        out: Dict[str, Dict[str, Any]] = {}
        for sym, ref_q, ref_q_date, ref_ytd, ref_ytd_date, refreshed_on in cur.fetchall():
            out[sym] = {
                "ref_q": ref_q,
                "ref_q_date": ref_q_date,
                "ref_ytd": ref_ytd,
                "ref_ytd_date": ref_ytd_date,
                "reference_refreshed_on": refreshed_on,
            }
    return out


def upsert_price_references(refs: Dict[str, Dict[str, Any]], refreshed_on: datetime.date) -> None:
    """Persist the day's 1Q/YTD reference closes, stamping ``refreshed_on``.

    ``refs`` maps ``symbol -> {ref_q, ref_q_date, ref_ytd, ref_ytd_date}`` (as
    produced by ``fmp_prices_fetch.fetch_reference_closes`` keyed back to the
    internal symbol). Lets a restarted daemon skip the historical fetch when it
    already ran today. No-op if ``refs`` is empty.
    """
    _require_db_url()  # fail fast even when there is nothing to write
    if not refs:
        return

    rows: List[Tuple] = [
        (
            symbol,
            r.get("ref_q"),
            r.get("ref_q_date"),
            r.get("ref_ytd"),
            r.get("ref_ytd_date"),
            refreshed_on,
        )
        for symbol, r in refs.items()
        if isinstance(r, dict)
    ]
    if not rows:
        return

    with _transaction() as cur:
        cur.execute(_PRICE_REFERENCE_DDL)
        extras.execute_values(
            cur,
            """
            INSERT INTO price_reference
              (symbol, ref_q, ref_q_date, ref_ytd, ref_ytd_date, reference_refreshed_on)
            VALUES %s
            ON CONFLICT (symbol)
            DO UPDATE SET
              ref_q                  = EXCLUDED.ref_q,
              ref_q_date             = EXCLUDED.ref_q_date,
              ref_ytd                = EXCLUDED.ref_ytd,
              ref_ytd_date           = EXCLUDED.ref_ytd_date,
              reference_refreshed_on = EXCLUDED.reference_refreshed_on,
              updated_at             = now()
            """,
            rows,
            page_size=100,
        )


_NEWS_ALERT_DDL = """
CREATE TABLE IF NOT EXISTS news_alert (
    id           BIGSERIAL PRIMARY KEY,
    as_of        DATE        NOT NULL,
    global_rank  SMALLINT    NOT NULL,
    country_iso2 CHAR(2)     NOT NULL,
    country_name TEXT,
    url          TEXT        NOT NULL,
    title        TEXT,
    source       TEXT,
    published_at TIMESTAMPTZ,
    summary      TEXT,
    image_url    TEXT,
    topic        TEXT        NOT NULL,
    severity     TEXT        NOT NULL CHECK (severity IN ('Critical','Caution','Watch')),
    importance   DOUBLE PRECISION,
    rationale    TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (as_of, global_rank)
);
CREATE INDEX IF NOT EXISTS idx_news_alert_as_of ON news_alert (as_of);
"""


class _NewsAlertRow(NamedTuple):
    """One news_alert row; field order matches the INSERT columns."""
    as_of: datetime.date
    global_rank: int
    country_iso2: str
    country_name: Optional[str]
    url: str
    title: Optional[str]
    source: Optional[str]
    published_at: Optional[datetime.datetime]
    summary: Optional[str]
    image_url: Optional[str]
    topic: str
    severity: str
    importance: Optional[float]
    rationale: Optional[str]


def upsert_news_alerts(alerts: List[Dict[str, Any]], as_of: datetime.date) -> None:
    """Replace the global news alerts for ``as_of`` with this run's ranked set.

    Self-contained: ensures the ``news_alert`` table exists (the project has no
    migration tool; this adds one table without touching the pre-created risk
    schema), then uses replace-today semantics — all rows for ``as_of`` are
    deleted and re-inserted so a re-run can never leave stale ranks. History for
    prior ``as_of`` dates is preserved (matching ``risk_snapshot``).

    Each alert dict (as produced by ``alerts_ranker.rank_global_alerts``):
      - global_rank  (int 1..N)            — global importance rank
      - country_iso2 (str ISO-2)           — originating country
      - country_name (str)                 — display name
      - url          (str)                 — article link
      - title, source, summary             (optional)
      - published_at (ISO str)             — article publish time
      - image        (str or list)         — thumbnail URL(s)
      - topic        (str)                 — one of constants.ALERT_TOPICS
      - severity     (str)                 — Critical | Caution | Watch
      - importance   (float 0..1)          — global importance score
      - rationale    (str)                 — one-line ranking rationale

    No-op if ``alerts`` is empty.
    """
    _require_db_url()  # fail fast even when there is nothing to write
    if not alerts:
        return

    rows: List[_NewsAlertRow] = []
    for a in alerts:
        if not isinstance(a, dict):
            continue
        rank = a.get("global_rank")
        url = (a.get("url") or "").strip()
        country = (a.get("country_iso2") or "").strip()
        topic = (a.get("topic") or "").strip()
        severity = (a.get("severity") or "").strip()
        if not url or not country or not topic or severity not in ("Critical", "Caution", "Watch"):
            continue
        try:
            rank = int(rank)
        except (TypeError, ValueError):
            continue

        try:
            importance = float(a["importance"]) if a.get("importance") is not None else None
        except (TypeError, ValueError):
            importance = None

        rows.append(_NewsAlertRow(
            as_of=as_of,
            global_rank=rank,
            country_iso2=country,
            country_name=a.get("country_name"),
            url=url,
            title=a.get("title"),
            source=a.get("source"),
            published_at=_to_ts_or_none(a.get("published_at")),
            summary=a.get("summary"),
            image_url=_image_url_or_none(a.get("image")),
            topic=topic,
            severity=severity,
            importance=importance,
            rationale=a.get("rationale"),
        ))

    if not rows:
        return

    with _transaction() as cur:
        cur.execute(_NEWS_ALERT_DDL)

        # Replace-today semantics: clear this run date, then insert the ranked set.
        cur.execute("DELETE FROM news_alert WHERE as_of = %s", (as_of,))

        extras.execute_values(
            cur,
            """
            INSERT INTO news_alert
              (as_of, global_rank, country_iso2, country_name, url, title, source,
               published_at, summary, image_url, topic, severity, importance, rationale)
            VALUES %s
            """,
            rows,
            page_size=100,
        )


_ARTICLE_DIGEST_DDL = """
CREATE TABLE IF NOT EXISTS article_digest (
  country_iso2    TEXT        NOT NULL,
  as_of           DATE        NOT NULL,
  url             TEXT        NOT NULL,
  published_at    TIMESTAMPTZ,
  content_sha256  TEXT,
  digest          JSONB       NOT NULL,
  stage1_severity DOUBLE PRECISION,
  model_id        TEXT,
  PRIMARY KEY (country_iso2, as_of, url)
);
"""


def upsert_article_digests(digests: List[Dict[str, Any]]) -> None:
    """Upsert one country/day's stage-1 article digests (the digest cache).

    Self-contained: ensures the ``article_digest`` table exists, then upserts
    by ``(country_iso2, as_of, url)``. ``content_sha256`` is the hash of the
    article text the digest was computed from, so a same-day re-run with
    unchanged text can reuse the row instead of re-calling the model.

    Each digest dict (as built by ``ai.digest_engine``):
      - country_iso2, as_of (datetime.date), url   (the cache key; required)
      - digest                                     (the model's extraction dict; required)
      - published_at                               (ISO string or None)
      - content_sha256, stage1_severity, model_id  (may be None)

    No-op if ``digests`` is empty. Rows missing a key field are skipped.
    """
    _require_db_url()  # fail fast even when there is nothing to write
    if not digests:
        return

    rows: List[Tuple] = []
    for d in digests:
        if not isinstance(d, dict):
            continue
        iso2 = (d.get("country_iso2") or "").strip()
        as_of = d.get("as_of")
        url = (d.get("url") or "").strip()
        digest = d.get("digest")
        if not iso2 or not isinstance(as_of, datetime.date) or not url or not isinstance(digest, dict):
            continue
        try:
            severity = float(d["stage1_severity"]) if d.get("stage1_severity") is not None else None
        except (TypeError, ValueError):
            severity = None
        rows.append(
            (
                iso2,
                as_of,
                url,
                _to_ts_or_none(d.get("published_at")),
                d.get("content_sha256"),
                extras.Json(digest),
                severity,
                d.get("model_id"),
            )
        )

    if not rows:
        return

    with _transaction() as cur:
        cur.execute(_ARTICLE_DIGEST_DDL)
        extras.execute_values(
            cur,
            """
            INSERT INTO article_digest
              (country_iso2, as_of, url, published_at,
               content_sha256, digest, stage1_severity, model_id)
            VALUES %s
            ON CONFLICT (country_iso2, as_of, url)
            DO UPDATE SET
              published_at    = COALESCE(EXCLUDED.published_at, article_digest.published_at),
              content_sha256  = EXCLUDED.content_sha256,
              digest          = EXCLUDED.digest,
              stage1_severity = EXCLUDED.stage1_severity,
              model_id        = EXCLUDED.model_id
            """,
            rows,
            page_size=20,
        )


def read_article_digests(country_iso2: str, as_of: datetime.date) -> Dict[str, Dict[str, Any]]:
    """Return one country/day's cached digests, keyed by article url.

    Ensures the ``article_digest`` table exists first so the pipeline can call
    this before the first write ever happens. Each value is
    ``{content_sha256, digest, stage1_severity}``.
    """
    with _transaction() as cur:
        cur.execute(_ARTICLE_DIGEST_DDL)
        cur.execute(
            """
            SELECT url, content_sha256, digest, stage1_severity
              FROM article_digest
             WHERE country_iso2 = %s AND as_of = %s
            """,
            (country_iso2, as_of),
        )
        out: Dict[str, Dict[str, Any]] = {}
        for url, sha, digest, severity in cur.fetchall():
            out[url] = {
                "content_sha256": sha,
                "digest": digest,
                "stage1_severity": severity,
            }
    return out
