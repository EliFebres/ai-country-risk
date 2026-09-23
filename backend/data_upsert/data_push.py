"""
The dashboard-facing feeds, and the roster seed.

What is here has a short life: prices are overwritten every tick, alerts and the
calendar are rewritten every run, and nobody will ever reconstruct one of them.
The durable record — evidence, cached model output, the macro series, the score
and the ledger — lives in `store`, where the manifest requirement and the
immutable-stamp guard are enforced.

No `CREATE TABLE` lives here any more. Every table comes from `schema.py` and is
built by `main.py bootstrap`. Five DDL constants used to be scattered through
this file while four more existed only as prose in `backend/README.md`, which is
how a fresh database reached the first upsert and died on
`relation "indicator" does not exist`.
"""

from __future__ import annotations

import datetime
import hashlib
from typing import Any, Dict, List, Optional, Tuple

import psycopg2.extras as extras

from backend.util import db, env

env.load()


def _to_date_from_iso(s: str) -> datetime.date:
    """Accepts 'YYYY-MM-DD' or ISO 'YYYY-MM-DDTHH:MMZ' and returns a date."""
    if not s:
        raise ValueError("Empty generated_at timestamp")
    try:
        return datetime.date.fromisoformat(s[:10])
    except Exception:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).date()


def _to_ts_or_none(s: Optional[str]) -> Optional[datetime.datetime]:
    """Best-effort ISO8601 parser returning an aware UTC timestamp when possible."""
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _image_url_or_none(img: Any) -> Optional[str]:
    """Alerts sometimes carry a list of candidate images; take the first usable."""
    if isinstance(img, str):
        return img.strip() or None
    if isinstance(img, (list, tuple)):
        for candidate in img:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


# --- Identity ---------------------------------------------------------------

_COUNTRY_COLUMNS = (
    "iso2", "iso3", "name", "query_name", "tier", "region", "income_group",
    "monetary_regime", "monetary_sovereignty", "lat", "lng",
)


def upsert_countries(roster: List[Dict[str, Any]]) -> int:
    """Seed `country` from an enriched roster. Returns the row count written.

    The caller composes the roster — display name, query name, region, income
    group, monetary regime — because those come from three different places and
    this module should not have to know about any of them.

    Everything but the key is refreshed on conflict: a roster edit is meant to
    propagate, and a stale display name outliving its roster entry is the kind
    of drift that goes unnoticed for months.
    """
    rows: List[Tuple] = [
        tuple(c.get(col) for col in _COUNTRY_COLUMNS)
        for c in roster
        if c.get("iso2") and c.get("name")
    ]
    if not rows:
        return 0

    assignments = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in _COUNTRY_COLUMNS if c != "iso2"
    )
    conn = db.connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            extras.execute_values(
                cur,
                f"""
                INSERT INTO country ({", ".join(_COUNTRY_COLUMNS)})
                VALUES %s
                ON CONFLICT (iso2) DO UPDATE SET {assignments}
                """,
                rows,
            )
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_countries() -> Dict[str, Dict[str, Any]]:
    """Read the seeded roster back, keyed by ISO-2.

    The consumer side of `upsert_countries`: the run asserts that what the
    roster promised actually arrived rather than trusting that the write
    returned without raising.
    """
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_COUNTRY_COLUMNS)} FROM country")
            return {r[0]: dict(zip(_COUNTRY_COLUMNS, r)) for r in cur.fetchall()}
    finally:
        conn.close()


def read_latest_snapshot_date() -> Optional[datetime.date]:
    """The newest `risk_snapshot.run_date`, or None if there are no scores.

    The supervisor asks this instead of consulting a marker file, so an empty
    database is populated immediately and a restart after a successful run does
    nothing.
    """
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.risk_snapshot')")
            if cur.fetchone()[0] is None:
                return None
            cur.execute("SELECT max(run_date) FROM risk_snapshot")
            return cur.fetchone()[0]
    finally:
        conn.close()


# --- Economic calendar ------------------------------------------------------


def _event_id(event_time: Any, country_code: str, event: str) -> str:
    """A stable id for one calendar event.

    Derived from the three fields that identify it, so the same release keeps
    the same id across runs. A serial id would make idempotency a lookup.
    """
    raw = f"{event_time}|{country_code}|{event}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def upsert_economic_events(events: List[Dict[str, Any]]) -> None:
    """Upsert the rolling calendar window and prune what has passed.

    A null AI score never overwrites an existing one: only the next-14-day
    subset is ranked, and the rest of the window arrives unscored every run.
    """
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
        try:
            ai_importance = (
                float(e["ai_importance"]) if e.get("ai_importance") is not None else None
            )
        except (TypeError, ValueError):
            ai_importance = None

        rows.append((
            _event_id(event_time, code, event), event_time, code,
            e.get("country_name"), event, importance, e.get("currency"),
            e.get("previous"), e.get("estimate"), e.get("actual"),
            ai_importance, e.get("ai_rationale"), e.get("ai_scored_at"),
        ))

    if not rows:
        return

    conn = db.connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            extras.execute_values(
                cur,
                """
                INSERT INTO economic_calendar_event
                  (event_id, event_time, country_code, country_name, event,
                   importance, currency, previous, estimate, actual,
                   ai_importance, ai_rationale, ai_scored_at)
                VALUES %s
                ON CONFLICT (event_id) DO UPDATE SET
                  country_name  = EXCLUDED.country_name,
                  importance    = EXCLUDED.importance,
                  currency      = EXCLUDED.currency,
                  previous      = EXCLUDED.previous,
                  estimate      = EXCLUDED.estimate,
                  actual        = EXCLUDED.actual,
                  ai_importance = COALESCE(EXCLUDED.ai_importance, economic_calendar_event.ai_importance),
                  ai_rationale  = COALESCE(EXCLUDED.ai_rationale, economic_calendar_event.ai_rationale),
                  ai_scored_at  = COALESCE(EXCLUDED.ai_scored_at, economic_calendar_event.ai_scored_at)
                """,
                rows,
            )
            cur.execute(
                "DELETE FROM economic_calendar_event "
                "WHERE event_time < now() - interval '1 day'"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- Prices -----------------------------------------------------------------
#
# `market_price` is keyed (symbol, ts) and carries a `role`. A live quote and
# the quarter-start and year-start reference closes are all observations of the
# same symbol at different times; they used to live in two tables, one of which
# could hold only a single row per symbol.


def upsert_market_prices(rows: List[Dict[str, Any]]) -> None:
    """Write this tick's quotes as observations.

    The daemon omits whole rows for markets it did not poll, so a symbol gains
    no observation this tick rather than having its last values blanked.
    """
    if not rows:
        return

    ts = datetime.datetime.now(datetime.timezone.utc)
    tuples: List[Tuple] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        symbol = (r.get("symbol") or "").strip()
        label = (r.get("label") or "").strip()
        asset_class = (r.get("asset_class") or "").strip()
        if not symbol or not label or asset_class not in (
            "stocks", "bonds", "crypto", "commodities"
        ):
            continue
        tuples.append((
            symbol, ts, "quote", label, asset_class, r.get("source_symbol"),
            bool(r.get("is_yield")), r.get("px"), r.get("chg"), r.get("q"),
            r.get("ytd"), int(r.get("sort_order") or 0),
        ))

    if not tuples:
        return

    conn = db.connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            extras.execute_values(
                cur,
                """
                INSERT INTO market_price
                  (symbol, ts, role, label, asset_class, source_symbol,
                   is_yield, px, chg, q, ytd, sort_order)
                VALUES %s
                ON CONFLICT (symbol, ts) DO UPDATE SET
                  px  = COALESCE(EXCLUDED.px,  market_price.px),
                  chg = COALESCE(EXCLUDED.chg, market_price.chg),
                  q   = COALESCE(EXCLUDED.q,   market_price.q),
                  ytd = COALESCE(EXCLUDED.ytd, market_price.ytd)
                """,
                tuples,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_price_references(
    refs: Dict[str, Dict[str, Any]], refreshed_on: datetime.date
) -> None:
    """Store the quarter-start and year-start closes as dated observations."""
    if not refs:
        return

    tuples: List[Tuple] = []
    for symbol, r in refs.items():
        if not isinstance(r, dict):
            continue
        for role, value_key, date_key in (
            ("reference-quarter", "ref_q", "ref_q_date"),
            ("reference-ytd", "ref_ytd", "ref_ytd_date"),
        ):
            value, when = r.get(value_key), r.get(date_key)
            if value is None or when is None:
                continue
            if isinstance(when, datetime.datetime):
                ts = when
            else:
                ts = datetime.datetime.combine(
                    when, datetime.time(0, 0), tzinfo=datetime.timezone.utc
                )
            tuples.append((symbol, ts, role, value, refreshed_on))

    if not tuples:
        return

    conn = db.connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            extras.execute_values(
                cur,
                """
                INSERT INTO market_price (symbol, ts, role, px, created_at)
                VALUES %s
                ON CONFLICT (symbol, ts) DO UPDATE SET
                  role = EXCLUDED.role,
                  px = EXCLUDED.px,
                  created_at = EXCLUDED.created_at
                """,
                tuples,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_price_references() -> Dict[str, Dict[str, Any]]:
    """The stored 1Q/YTD reference closes, keyed by symbol."""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (symbol, role) symbol, role, px, ts, created_at
                  FROM market_price
                 WHERE role <> 'quote'
                 ORDER BY symbol, role, ts DESC
                """
            )
            out: Dict[str, Dict[str, Any]] = {}
            for symbol, role, px, ts, created_at in cur.fetchall():
                slot = out.setdefault(symbol, {})
                if role == "reference-quarter":
                    slot["ref_q"] = px
                    slot["ref_q_date"] = ts.date() if ts else None
                else:
                    slot["ref_ytd"] = px
                    slot["ref_ytd_date"] = ts.date() if ts else None
                slot["reference_refreshed_on"] = created_at.date() if created_at else None
            return out
    finally:
        conn.close()


def read_reference_refreshed_on() -> Optional[datetime.date]:
    """When the reference closes were last refreshed, or None.

    The prices loop asks this rather than consulting a process-local flag, so a
    restart inherits the day's work instead of redoing it.
    """
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.market_price')")
            if cur.fetchone()[0] is None:
                return None
            cur.execute(
                "SELECT max(created_at)::date FROM market_price WHERE role <> 'quote'"
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def read_yields_updated_at() -> Optional[datetime.datetime]:
    """The newest bond-yield observation, or None."""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.market_price')")
            if cur.fetchone()[0] is None:
                return None
            cur.execute(
                "SELECT max(ts) FROM market_price WHERE role = 'quote' AND is_yield"
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


# --- Alerts -----------------------------------------------------------------


def upsert_news_alerts(alerts: List[Dict[str, Any]], run_date: datetime.date) -> None:
    """Replace the alerts for `run_date` with this run's ranked set.

    Replace-today semantics: every row for `run_date` is deleted and re-inserted,
    so a re-run cannot leave a stale rank behind. Earlier dates are preserved.
    """
    if not alerts:
        return

    rows: List[Tuple] = []
    for a in alerts:
        if not isinstance(a, dict):
            continue
        url = (a.get("url") or "").strip()
        country = (a.get("country_iso2") or "").strip()
        topic = (a.get("topic") or "").strip()
        severity = (a.get("severity") or "").strip()
        if not url or not country or not topic or severity not in (
            "Critical", "Caution", "Watch"
        ):
            continue
        try:
            rank = int(a.get("global_rank"))
        except (TypeError, ValueError):
            continue
        try:
            importance = (
                float(a["importance"]) if a.get("importance") is not None else None
            )
        except (TypeError, ValueError):
            importance = None

        rows.append((
            country, run_date, rank, a.get("country_name"), url, a.get("title"),
            a.get("source"), _to_ts_or_none(a.get("published_at")),
            a.get("summary"), _image_url_or_none(a.get("image")), topic,
            severity, importance, a.get("rationale"),
        ))

    if not rows:
        return

    conn = db.connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("DELETE FROM news_alert WHERE run_date = %s", (run_date,))
            extras.execute_values(
                cur,
                """
                INSERT INTO news_alert
                  (country_iso2, run_date, rank, country_name, url, title,
                   source, published_at, summary, image_url, topic, severity,
                   importance, rationale)
                VALUES %s
                """,
                rows,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
