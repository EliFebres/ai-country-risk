import os
import datetime
from typing import Dict, Any, Optional, List, Tuple

import psycopg2
import psycopg2.extras as extras
from dotenv import load_dotenv

load_dotenv()

DB_URL = os.getenv("DATABASE_URL")


def _to_date_from_iso(s: str) -> datetime.date:
    """
    Accepts 'YYYY-MM-DD' or ISO 'YYYY-MM-DDTHH:MMZ' and returns date().
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
    """
    Best-effort ISO8601 parser that returns aware UTC timestamps when possible.
    """
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


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
    """
    if not DB_URL:
        raise RuntimeError("DATABASE_URL is not set in the environment")
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")

    # Required fields
    country = payload.get("country")
    if not country or not isinstance(country, str):
        raise ValueError("payload['country'] must be an ISO-2 string")

    meta = payload.get("_meta") or {}
    gen_at = meta.get("generated_at")
    if not gen_at or not isinstance(gen_at, str):
        raise ValueError("payload['_meta']['generated_at'] must be a string ISO timestamp")
    units = meta.get("units") or {}
    if not isinstance(units, dict):
        raise ValueError("payload['_meta']['units'] must be a dict of indicator -> unit")

    as_of: datetime.date = _to_date_from_iso(gen_at)

    indicators = payload.get("indicators") or {}
    if not isinstance(indicators, dict) or not indicators:
        raise ValueError("payload['indicators'] must be a non-empty dict")

    llm_out = payload.get("llm_output") or {}
    if not (isinstance(llm_out, dict) and {"score", "bullet_summary"} <= set(llm_out.keys())):
        raise ValueError("payload['llm_output'] must include 'score' and 'bullet_summary'")

    # Optional: new top-3 article rows
    top_articles = payload.get("top_articles") or []
    if not isinstance(top_articles, list):
        top_articles = []

    conn = psycopg2.connect(DB_URL)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            # 0) Ensure the parent 'country' row exists for the FK
            cur.execute(
                """
                INSERT INTO country (iso2, name)
                VALUES (%s, %s)
                ON CONFLICT (iso2) DO NOTHING
                """,
                (country, country_name),
            )

            # 1) Indicators + yearly series
            for ind_name, ind_data in indicators.items():
                unit = units[ind_name]  # rely on your existing contract; raises if missing

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
                    rows_yv.append((country, ind_id, yr_int, val_f))

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

            # 2) Risk snapshot (latest AI score for the run date)
            cur.execute(
                """
                INSERT INTO risk_snapshot (country_iso2, as_of, score, bullet_summary)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (country_iso2, as_of)
                DO UPDATE SET
                  score = EXCLUDED.score,
                  bullet_summary = EXCLUDED.bullet_summary
                """,
                (country, as_of, llm_out["score"], llm_out["bullet_summary"]),
            )

            # 3) Optional: write the top-3 links for this snapshot (now includes image_url)
            rows_art: List[Tuple] = []
            for a in top_articles:
                if not isinstance(a, dict):
                    continue
                rank = a.get("rank")
                url = (a.get("url") or "").strip()
                if not url or rank not in (1, 2, 3):
                    continue

                # Normalize image value to a single URL string (or None)
                img = a.get("image")
                image_url: Optional[str] = None
                if isinstance(img, str):
                    u = img.strip()
                    image_url = u if u.startswith(("http://", "https://")) else None
                elif isinstance(img, list):
                    for v in img:
                        if isinstance(v, str) and v.strip().startswith(("http://", "https://")):
                            image_url = v.strip()
                            break

                rows_art.append(
                    (
                        country,                                # country_iso2
                        as_of,                                  # as_of (DATE)
                        int(rank),                              # rank 1..3
                        url,                                    # url (TEXT NOT NULL)
                        a.get("title"),                         # title
                        a.get("source"),                        # source
                        _to_ts_or_none(a.get("published_at")),  # published_at TIMESTAMPTZ
                        (float(a["impact"]) if (a.get("impact") is not None) else None),
                        a.get("summary"),
                        image_url,                               # NEW: image_url
                    )
                )

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

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
    if not DB_URL:
        raise RuntimeError("DATABASE_URL is not set in the environment")
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

    conn = psycopg2.connect(DB_URL)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
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
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
    if not DB_URL:
        raise RuntimeError("DATABASE_URL is not set in the environment")
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

    conn = psycopg2.connect(DB_URL)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
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

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
    if not DB_URL:
        raise RuntimeError("DATABASE_URL is not set in the environment")
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

    conn = psycopg2.connect(DB_URL)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
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

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_price_references() -> Dict[str, Dict[str, Any]]:
    """Return stored 1Q/YTD reference closes, keyed by symbol.

    Ensures the ``price_reference`` table exists first so the daemon can call this
    on startup before any write. Each value is
    ``{ref_q, ref_q_date, ref_ytd, ref_ytd_date, reference_refreshed_on}``.
    """
    if not DB_URL:
        raise RuntimeError("DATABASE_URL is not set in the environment")

    conn = psycopg2.connect(DB_URL)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
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
        conn.commit()
        return out
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_price_references(refs: Dict[str, Dict[str, Any]], refreshed_on: datetime.date) -> None:
    """Persist the day's 1Q/YTD reference closes, stamping ``refreshed_on``.

    ``refs`` maps ``symbol -> {ref_q, ref_q_date, ref_ytd, ref_ytd_date}`` (as
    produced by ``fmp_prices_fetch.fetch_reference_closes`` keyed back to the
    internal symbol). Lets a restarted daemon skip the historical fetch when it
    already ran today. No-op if ``refs`` is empty.
    """
    if not DB_URL:
        raise RuntimeError("DATABASE_URL is not set in the environment")
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

    conn = psycopg2.connect(DB_URL)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
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
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
    if not DB_URL:
        raise RuntimeError("DATABASE_URL is not set in the environment")
    if not alerts:
        return

    rows: List[Tuple] = []
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

        rows.append(
            (
                as_of,                               # as_of (DATE)
                rank,                                # global_rank
                country,                             # country_iso2
                a.get("country_name"),               # country_name
                url,                                 # url (TEXT NOT NULL)
                a.get("title"),                      # title
                a.get("source"),                     # source
                _to_ts_or_none(a.get("published_at")),  # published_at TIMESTAMPTZ
                a.get("summary"),                    # summary
                _image_url_or_none(a.get("image")),  # image_url
                topic,                               # topic
                severity,                            # severity
                importance,                          # importance
                a.get("rationale"),                  # rationale
            )
        )

    if not rows:
        return

    conn = psycopg2.connect(DB_URL)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
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

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
