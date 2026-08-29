"""The backend's entire Postgres write layer.

Every table the dashboard reads is written here, and the frontend queries
those tables directly — so this module's SQL *is* the contract between the two
halves of the project. Change a column name here and the Next.js server
breaks; there is no API layer in between to absorb it.

The DDL lives in :mod:`backend.data_upsert.schema`, which builds all ten tables
from nothing. Each writer used to own its own ``CREATE TABLE IF NOT EXISTS``,
which meant the schema was defined in twenty places and five of the tables
existed only as a fenced SQL block in a README.

All writes go through ``_transaction()``, which owns connect/commit/rollback/
close. Upserts use ``COALESCE`` where a transient null (a symbol missing from
one quote batch, an unranked event) must not blank a previously-good value.
"""

import os
import datetime
import logging
import socket
import urllib.parse
from contextlib import contextmanager
from typing import Dict, Any, Iterator, NamedTuple, Optional, List, Tuple

import psycopg2
import psycopg2.extras as extras

# `lags` imports nothing from this package -- it is a table of publication
# delays and two date functions -- so this does not close the data_fetching ->
# data_upsert loop that already exists.
from backend.data_fetching.vintage import lags

logger = logging.getLogger(__name__)


def write_origin() -> Dict[str, str]:
    """Which machine and which database this write came from.

    Nothing recorded either, and the cost of that was a session spent proving
    by inspection which of two Neon projects a table belonged to -- the answer
    being unavailable from the rows themselves, because a row written by the
    six-hourly cron on the harvest box and one written from a laptop are
    byte-identical.

    Host and database name only. The DSN carries a password and this value goes
    into a JSONB column that gets printed, exported and pasted into write-ups,
    so it is assembled from parts rather than by redacting a string -- there is
    no version of "strip the credentials" that stays correct when the URL
    format changes.
    """
    try:
        parts = urllib.parse.urlsplit(_require_db_url())
        database = f"{parts.hostname or '?'}{parts.path or ''}"
    except Exception:  # noqa: BLE001 - provenance must never cost a write
        database = "?"
    return {"host": socket.gethostname(), "database": database}


# The two databases, and the variable that chooses between them. There is
# deliberately no default: `DATABASE_URL` was a bare name that every tool
# reached for without deciding anything, and the thing it reached was
# production. A session was spent proving which project held which half of this
# project because that choice had never been made explicitly anywhere.
_TARGETS = {"prod": "PROD_DATABASE_URL", "dev": "DEV_DATABASE_URL"}
_TARGET_VAR = "RISK_DB_TARGET"
_resolved_once: Dict[str, str] = {}


def resolve_target() -> str:
    """Which database this process writes to. Explicit or it raises."""
    target = (os.getenv(_TARGET_VAR) or "").strip().lower()
    if target not in _TARGETS:
        raise RuntimeError(
            f"{_TARGET_VAR} must be one of {sorted(_TARGETS)}; got {target or '(unset)'}. "
            f"There is no default on purpose -- the old bare DATABASE_URL was one, "
            f"and it pointed at production.")
    return target


def _require_db_url() -> str:
    """The DSN for the chosen target, read at call time (never cached at import).

    Logged the first time each target resolves, so a run's output says which
    database it wrote to rather than leaving it to be inferred afterwards from
    the rows -- which, before `written_by`, was not possible at all.
    """
    target = resolve_target()
    var = _TARGETS[target]
    db_url = os.getenv(var)
    if not db_url:
        raise RuntimeError(f"{_TARGET_VAR}={target} but {var} is not set.")
    if target not in _resolved_once:
        _resolved_once[target] = var
        logger.info("database: %s -> %s", target, write_origin()["database"])
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


def upsert_structural_facts(facts: Dict[str, Dict[str, Any]]) -> None:
    """Attach each country's structural block to its roster row.

    The facts masking removed and cannot replace: whether a country issues a
    reserve currency, whether it can devalue, which income group it is in.
    Hand-researched and cited, shipped as data in the repo, and written here so
    a bootstrapped database has them before the first score rather than after
    somebody notices the block is missing.

    Countries absent from ``facts`` keep whatever they have — five of
    forty-eight are filled and the rest are legitimately blank, so this must not
    blank the filled ones on a partial load.
    """
    _require_db_url()
    if not facts:
        return
    rows = [(iso2, extras.Json(block)) for iso2, block in facts.items()
            if isinstance(block, dict) and block]
    if not rows:
        return
    with _transaction() as cur:
        extras.execute_values(
            cur,
            """
            INSERT INTO country (iso2, name, structural)
            VALUES %s
            ON CONFLICT (iso2) DO UPDATE SET structural = EXCLUDED.structural
            """,
            [(iso2, iso2, block) for iso2, block in rows],
            template="(%s, %s, %s)",
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
    # JSONB columns: the model's judgement and the observations beside it.
    subscores: Optional[Dict[str, Any]] = None
    subscore_evidence: Optional[Dict[str, Any]] = None
    condition_flags: Optional[Dict[str, Any]] = None
    article_scores: Optional[List[Dict[str, Any]]] = None
    applied_rules: Optional[List[str]] = None
    evidence_coverage: Optional[float] = None
    # The four ledger scores, broken out of `subscores` into their own columns
    # so the front-end and any analysis can read one without parsing JSONB.
    friction_score: Optional[float] = None
    order_uncertainty_score: Optional[float] = None
    information_score: Optional[float] = None
    edge_vitality: Optional[float] = None
    # Legal investability. `non_investable` drives the RESTRICTED badge;
    # `legal_gate` is the rule behind it, with citations. Neither touches `score`.
    non_investable: Optional[bool] = None
    legal_gate: Optional[Dict[str, Any]] = None
    # Provenance: which model, prompt and policy produced this row.
    model_id: Optional[str] = None
    prompt_version: Optional[str] = None
    policy_version: Optional[str] = None
    # What the model saw: per-article hashes and the macro panel's vintage
    # (``provenance.build_input_manifest``). Payload-level, not part of
    # ``llm_output`` — it describes the inputs, not the model's answer.
    input_manifest: Optional[Dict[str, Any]] = None
    # Which scoring regime produced the row. Defaults to 'named' because that is
    # what every caller who does not say otherwise is doing — the daily run's
    # articles name their country. Never None: an unstamped row in a series that
    # spans a regime change is indistinguishable from a wrong one.
    scoring_mode: str = "named"


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


def _ledger(llm_out: Dict[str, Any], name: str) -> Optional[float]:
    """Read one ledger score out of the model output, or None.

    Already on the 0-1 scale — ``langchain_llm._from_100`` converts every model
    score the moment the call returns, so these match ``score`` and
    ``evidence_coverage`` without further arithmetic.
    """
    scores = llm_out.get("ledger_scores") or llm_out.get("subscores") or {}
    value = scores.get(name) if isinstance(scores, dict) else None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


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

    # Optional and non-fatal by design: the pipeline passes None when manifest
    # assembly failed, and a payload from before provenance existed has no key
    # at all. Either way the snapshot still writes.
    input_manifest = payload.get("input_manifest")
    if not isinstance(input_manifest, dict):
        input_manifest = None

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
        # The ledger scores and the evidence ids behind them, stored together —
        # a score without its citations is not auditable.
        subscores={
            "ledger_scores": llm_out.get("subscores") or {},
            "subscore_evidence": llm_out.get("subscore_evidence") or {},
        } if llm_out.get("subscores") else None,
        subscore_evidence=llm_out.get("subscore_evidence"),
        condition_flags=llm_out.get("condition_flags"),
        friction_score=_ledger(llm_out, "friction"),
        order_uncertainty_score=_ledger(llm_out, "order_uncertainty"),
        information_score=_ledger(llm_out, "information_capacity"),
        edge_vitality=_ledger(llm_out, "edge_vitality"),
        non_investable=llm_out.get("non_investable"),
        # Every article's impact and topic group, not just the displayed
        # top-3: the full cross-section is the point of storing it.
        article_scores=llm_out.get("news_article_scores"),
        applied_rules=llm_out.get("applied_rules"),
        evidence_coverage=llm_out.get("evidence_coverage"),
        legal_gate=llm_out.get("legal_gate"),
        model_id=llm_out.get("model_id"),
        prompt_version=llm_out.get("prompt_version"),
        policy_version=llm_out.get("policy_version"),
        input_manifest=input_manifest,
        scoring_mode=payload.get("scoring_mode") or "named",
    )


# `risk_snapshot` is operator-provisioned (see backend/README.md), but the
# perception/policy split added columns to it: both horizons raw and gated, the
# model's sub-factor detail, and the provenance stamps. Additive and idempotent
# so an existing database comes up to date without a migration tool.
# ALTER TABLE takes an ACCESS EXCLUSIVE lock even when every column already
# exists, and the country loop calls upsert_snapshot once per country. Run it
# once per process instead. Set only after the transaction commits, so a
# rolled-back run re-issues it.
def _json_or_none(value: Any) -> Optional[extras.Json]:
    """Wrap a value for a JSONB column, leaving None as SQL NULL."""
    return None if value is None else extras.Json(value)


def _article_rows(top_articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The top three, normalised, in rank order, ready for the JSONB column.

    Was its own table keyed ``(country, as_of, rank)`` with a
    ``rank BETWEEN 1 AND 3`` CHECK. An ordered array says the same thing on the
    row that owns it, which means the constraint is gone and
    ``article_ranking.ensure_top_three`` is the only guard left — covered by
    ``testing/test_news_fetching.py::TestEnsureTopThree``.

    Malformed entries are dropped rather than raising: a bad image URL must not
    cost a country its score.
    """
    rows: List[Dict[str, Any]] = []
    for a in top_articles:
        if not isinstance(a, dict):
            continue
        rank = a.get("rank")
        url = (a.get("url") or "").strip()
        if not url or rank not in (1, 2, 3):
            continue
        published_at = _to_ts_or_none(a.get("published_at"))
        rows.append({
            "rank": int(rank),
            "url": url,
            "title": a.get("title"),
            "source": a.get("source"),
            # JSONB, so a datetime has to become a string here rather than
            # relying on the adapter to do it.
            "published_at": published_at.isoformat() if published_at else None,
            "impact": (float(a["impact"]) if a.get("impact") is not None else None),
            "summary": a.get("summary"),
            "image_url": _image_url_or_none(a.get("image")),
        })
    return sorted(rows, key=lambda r: r["rank"])


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
      - input_manifest: ``provenance.build_input_manifest`` output — what the
        model saw. None (or absent) writes NULL without touching a manifest a
        previous run of the same day already stored.
      - the rest of ``llm_output`` (score_3m, raw_score_*, subscores,
        subscore_evidence, condition_flags, news_article_scores, applied_rules,
        evidence_coverage, legal_gate, non_investable, model_id,
        prompt_version, policy_version). Absent keys write NULL, so a payload
        from an earlier version of the model output still upserts.

    **What ``score`` means changed.** Up to ``policy_version`` p1.0 it was the
    model's 12-month score after floors, caps and a sanctions override. From
    p2.0 it is the model's ``score_12m`` and nothing else — no code path in this
    project writes any other value into it. Sanctioned countries are marked with
    ``non_investable`` instead of being forced to 1.0. Read ``policy_version``
    to know which convention a stored row follows; the two are not comparable
    for a sanctioned country.
    """
    _require_db_url()  # fail fast before any parsing work
    data = _parse_snapshot_payload(payload)
    rows_art = _article_rows(data.top_articles)

    with _transaction() as cur:
        # 0) Ensure the parent 'country' row exists for the FK
        cur.execute(
            """
            INSERT INTO country (iso2, name)
            VALUES (%s, %s)
            ON CONFLICT (iso2) DO NOTHING
            """,
            (data.country, country_name),
        )

        # 1) Risk snapshot (latest AI score for the run date). `score` is the
        #    model's own 12-month judgement on the 0-1 scale — same column, same
        #    scale, same front-end reader, but as of policy_version p2.0 no code
        #    adjusts it. Everything else is additive: the four ledger scores and
        #    the evidence ids behind them, the condition flags as observations,
        #    the sanctions badge, which model/prompt/policy produced the row, and
        #    what the model saw when it did (`input_manifest`).
        #
        #    `score` and `bullet_summary` overwrite plainly — the newest run's
        #    assessment is the current one, deliberately. Every detail column
        #    COALESCEs instead: a re-run that lost a field (LLM failure,
        #    provenance bug) must not blank the good value a previous run of the
        #    same day already stored.
        cur.execute(
            """
            INSERT INTO risk_snapshot (
              country_iso2, as_of, score, bullet_summary,
              score_3m, raw_score_12m, raw_score_3m,
              ledger_scores, subscore_evidence, condition_flags,
              article_scores, applied_rules, evidence_coverage, legal_gate,
              model_id, prompt_version, policy_version, input_manifest,
              friction_score, order_uncertainty_score, information_score,
              edge_vitality, non_investable, scoring_mode, top_articles
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (country_iso2, as_of)
            DO UPDATE SET
              score             = EXCLUDED.score,
              bullet_summary    = EXCLUDED.bullet_summary,
              score_3m          = COALESCE(EXCLUDED.score_3m,          risk_snapshot.score_3m),
              raw_score_12m     = COALESCE(EXCLUDED.raw_score_12m,     risk_snapshot.raw_score_12m),
              raw_score_3m      = COALESCE(EXCLUDED.raw_score_3m,      risk_snapshot.raw_score_3m),
              ledger_scores     = COALESCE(EXCLUDED.ledger_scores,     risk_snapshot.ledger_scores),
              subscore_evidence = COALESCE(EXCLUDED.subscore_evidence, risk_snapshot.subscore_evidence),
              condition_flags   = COALESCE(EXCLUDED.condition_flags,   risk_snapshot.condition_flags),
              friction_score          = COALESCE(EXCLUDED.friction_score,          risk_snapshot.friction_score),
              order_uncertainty_score = COALESCE(EXCLUDED.order_uncertainty_score, risk_snapshot.order_uncertainty_score),
              information_score       = COALESCE(EXCLUDED.information_score,       risk_snapshot.information_score),
              edge_vitality           = COALESCE(EXCLUDED.edge_vitality,           risk_snapshot.edge_vitality),
              non_investable          = COALESCE(EXCLUDED.non_investable,          risk_snapshot.non_investable),
              article_scores    = COALESCE(EXCLUDED.article_scores,    risk_snapshot.article_scores),
              applied_rules     = COALESCE(EXCLUDED.applied_rules,     risk_snapshot.applied_rules),
              evidence_coverage = COALESCE(EXCLUDED.evidence_coverage, risk_snapshot.evidence_coverage),
              legal_gate        = COALESCE(EXCLUDED.legal_gate,        risk_snapshot.legal_gate),
              model_id          = COALESCE(EXCLUDED.model_id,          risk_snapshot.model_id),
              prompt_version    = COALESCE(EXCLUDED.prompt_version,    risk_snapshot.prompt_version),
              policy_version    = COALESCE(EXCLUDED.policy_version,    risk_snapshot.policy_version),
              input_manifest    = COALESCE(EXCLUDED.input_manifest,    risk_snapshot.input_manifest),
              top_articles      = COALESCE(EXCLUDED.top_articles,      risk_snapshot.top_articles),
              -- Not COALESCEd: the mode of the run that just wrote is the mode
              -- of the row, and a re-score in the other regime must say so.
              scoring_mode      = EXCLUDED.scoring_mode,
              updated_at        = now()
            """,
            (
                data.country, data.as_of,
                data.llm_out["score"], data.llm_out["bullet_summary"],
                data.score_3m, data.raw_score_12m, data.raw_score_3m,
                _json_or_none(data.subscores),
                _json_or_none(data.subscore_evidence),
                _json_or_none(data.condition_flags),
                _json_or_none(data.article_scores),
                _json_or_none(data.applied_rules),
                data.evidence_coverage,
                _json_or_none(data.legal_gate),
                data.model_id, data.prompt_version, data.policy_version,
                _json_or_none(data.input_manifest),
                data.friction_score, data.order_uncertainty_score,
                data.information_score, data.edge_vitality, data.non_investable,
                data.scoring_mode,
                _json_or_none(rows_art or None),
            ),
        )


# The generic series store: one row per (country, indicator, freq, period), with
# the date we learned the value alongside the period it describes. Every source
# added by the friction framework — the new World Bank codes, the IMF monthly
# series, the curated drop folder — writes here.
#
# It is now the only macro store. `recent_indicator` held one latest print per
# (country, indicator) and was written from the same IMF call that already wrote
# the full history here, so it was the same data twice; the parquet panel held
# the World Bank annuals and is gone the same way. Freshest-wins is resolved by
# `payload._resolve` across one table rather than merged across three.
#
# `as_of` is when the value became known to us, which is not the period it
# describes: a 2024 annual figure first published in mid-2025 is stale by a year
# on the day we fetch it, and the payload can only say so if both dates are
# stored. `vintage_scheme` records which revision policy produced the value,
# reserving room for a first-release series later without a column rename.
# Widening an existing table's primary key. Separate from the CREATE because
# `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so
# every deployment that ran before this would have kept the narrow key and
# quietly kept collapsing vintages.
_SERIES_FREQS = ("M", "Q", "A")


# The scheme a row carries when its writer said nothing about where the date
# came from — which is also, in practice, the mark of a date read off the clock.
_UNDECLARED_VINTAGE = "as-published-latest"


def _corrected_stamp(code: str, freq: str, period: str,
                     as_of: datetime.date) -> Optional[datetime.date]:
    """A publication date for a row whose ``as_of`` is implausibly late, else None.

    The chokepoint guard. Three writers stamped `as_of = date.today()` on
    observations describing past periods, and `payload._resolve`'s vintage bound
    then dropped every one of them at every historical anchor -- two whole
    ledgers resolved nothing for the entire pilot. Each writer was a one-line
    fix and the next writer would have been the fourth, because this function is
    where they all meet and it accepted any date at all.

    The test is deliberately "too late", not "not equal to what lags would say":

    * A stamp more than `MAX_LAG_DAYS` past its own period end cannot be a
      publication date. No publisher is two years late, so it is a clock read.
    * A stamp *earlier* than its period end is the opposite defect and is left
      alone. `country_data_fetch.panel_rows` writes 31-December-of-the-year
      deliberately, and its current-year row is capped at today by design; both
      are pinned by tests. Re-dating those here would be a second, unrequested
      change to the one annual path that always worked.
    * A row that declares a real scheme is never touched: a WEO edition date is
      a fact, and a curated row's date was typed by someone who knew it.
    """
    if not lags.within_bounds(as_of, period, freq):
        end = lags.period_end(period, freq)
        if end is not None and as_of > end + datetime.timedelta(days=lags.MAX_LAG_DAYS):
            return lags.published_on(period, freq, code)
    return None


def upsert_indicator_series(rows: List[Dict[str, Any]]) -> None:
    """Upsert observations into the generic ``indicator_series`` store.

    Args:
        rows: dicts with ``country_iso2``, ``indicator_code``, ``freq``
            (``'M'``/``'Q'``/``'A'``), ``period``, ``as_of`` (a ``date``), and
            ``source``. ``value`` may be None — an explicitly-published null is
            worth storing, since it distinguishes "reported as unavailable" from
            "we never asked". ``vintage_scheme`` defaults to
            ``'as-published-latest'``.

            Rows missing any required field, or carrying an unknown ``freq``, are
            skipped with a count logged rather than failing the batch: one
            malformed row from one source must not cost a whole run its data.

    No-op on an empty list.
    """
    _require_db_url()  # fail fast even when there is nothing to write
    if not rows:
        return

    prepared: List[Tuple] = []
    skipped = 0
    restamped = 0
    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue
        iso2 = row.get("country_iso2")
        code = row.get("indicator_code")
        freq = row.get("freq")
        period = row.get("period")
        as_of = row.get("as_of")
        source = row.get("source")
        if not (iso2 and code and period and source) or freq not in _SERIES_FREQS:
            skipped += 1
            continue
        if not isinstance(as_of, datetime.date):
            skipped += 1
            continue

        value = row.get("value")
        if value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                skipped += 1
                continue

        scheme = str(row.get("vintage_scheme") or _UNDECLARED_VINTAGE)
        if scheme == _UNDECLARED_VINTAGE:
            corrected = _corrected_stamp(str(code), freq, str(period), as_of)
            if corrected is not None:
                restamped += 1
                as_of, scheme = corrected, lags.SCHEME

        prepared.append((
            str(iso2), str(code), freq, str(period), value, as_of, str(source),
            scheme,
        ))

    if skipped:
        logger.warning("indicator_series: skipped %d malformed row(s) of %d", skipped, len(rows))
    if restamped:
        # Loud rather than silent: a writer landing here is one that has not been
        # taught to date its own rows, and the count is how you find it.
        logger.warning("indicator_series: re-dated %d clock-stamped row(s) of %d "
                       "-- the writer stamped a fetch date on a past period",
                       restamped, len(rows))
    if not prepared:
        return

    with _transaction() as cur:
        extras.execute_values(
            cur,
            """
            INSERT INTO indicator_series
              (country_iso2, indicator_code, freq, period, value, as_of, source, vintage_scheme)
            VALUES %s
            ON CONFLICT (country_iso2, indicator_code, freq, period, as_of)
            DO UPDATE SET
              value          = EXCLUDED.value,
              source         = EXCLUDED.source,
              vintage_scheme = EXCLUDED.vintage_scheme
            """,
            prepared,
            page_size=500,
        )


def delete_series_rows(keys: List[Tuple[str, str, str, str, datetime.date]]) -> int:
    """Delete ``indicator_series`` rows by full primary key.

    Exists because ``as_of`` is *in* that key. Re-dating an observation is
    therefore not an upsert — it inserts a second row and leaves the first one
    standing, which is how a migration that reports "re-dated 36,654 rows" can
    leave every one of them still readable at its old date. The move is insert
    the new, then delete the old, in that order: a failure between the two
    duplicates a row, and the alternative order loses it.

    Returns the number of rows actually removed.
    """
    if not keys:
        return 0
    removed = 0
    with _transaction() as cur:
        # Batched by hand rather than by `execute_values`' own `page_size`,
        # because `cur.rowcount` reports only the last statement it ran — a
        # 11,698-row delete in pages of 500 reports 198, which reads as a
        # migration that mostly did not happen.
        for start in range(0, len(keys), 500):
            extras.execute_values(
                cur,
                """
                DELETE FROM indicator_series s
                USING (VALUES %s) AS v(country_iso2, indicator_code, freq, period, as_of)
                WHERE s.country_iso2   = v.country_iso2
                  AND s.indicator_code = v.indicator_code
                  AND s.freq           = v.freq
                  AND s.period         = v.period
                  AND s.as_of          = v.as_of::date
                """,
                [(a, b, c, d, e.isoformat())
                 for a, b, c, d, e in keys[start:start + 500]],
                page_size=500,
            )
            removed += cur.rowcount
    return removed


def has_annual_series(country_iso2: str,
                      codes: Optional[List[str]] = None,
                      *, source: Optional[str] = None) -> bool:
    """Whether this country already has annual rows for these indicator codes.

    What makes the World Bank backfill incremental. Was
    ``has_country_partition``, which asked the filesystem whether a Parquet
    directory held a file; the same question, asked of the store that now holds
    the answer.

    Filter by something. "Any annual row at all" is the wrong question now that
    several sources write them: the WEO editions land 160k before the World
    Bank fetch runs, so an unfiltered check reports every country as done and
    the panel step completes in seconds having written nothing. A producer that
    runs, logs OK and does no work is the exact failure this project keeps
    finding.

    ``codes`` is not enough on its own either — CPI.YOY is both a panel column
    and a WEO subject, so a code filter answers the same way for the same
    reason. ``source`` is what actually separates them.
    """
    sql = ["SELECT EXISTS (SELECT 1 FROM indicator_series",
           "WHERE country_iso2 = %s AND freq = 'A'"]
    params: List[Any] = [country_iso2]
    if codes:
        sql.append("AND indicator_code = ANY(%s)")
        params.append(list(codes))
    if source:
        sql.append("AND source = %s")
        params.append(source)
    sql.append(")")
    with _transaction() as cur:
        cur.execute(" ".join(sql), tuple(params))
        return bool(cur.fetchone()[0])


def read_indicator_series(country_iso2: str) -> Dict[str, List[Dict[str, Any]]]:
    """Return one country's stored series, grouped by indicator code.

    Args:
        country_iso2: ISO-2 country code.

    Returns:
        ``{indicator_code: [{period, freq, value, as_of, source, vintage_scheme},
        ...]}`` with each list in ascending period order, so the last entry is the
        newest and a rolling window is a plain tail slice. Empty when the country
        has nothing stored yet.

        Ordering is lexicographic on ``period``, which is correct for all three
        zero-padded formats this table stores (``'2026-06'``, ``'2026Q1'``,
        ``'2025'``) as long as a single indicator sticks to one of them — which
        the ``freq`` half of the primary key enforces in practice.
    """
    _require_db_url()
    if not country_iso2:
        return {}

    out: Dict[str, List[Dict[str, Any]]] = {}
    with _transaction() as cur:
        cur.execute(
            """
            SELECT indicator_code, freq, period, value, as_of, source, vintage_scheme
            FROM indicator_series
            WHERE country_iso2 = %s
            ORDER BY indicator_code, period
            """,
            (country_iso2,),
        )
        for code, freq, period, value, as_of, source, scheme in cur.fetchall():
            out.setdefault(code, []).append({
                "period": period,
                "freq": freq,
                "value": float(value) if value is not None else None,
                "as_of": as_of,
                "source": source,
                "vintage_scheme": scheme,
            })
    return out


# Lint findings: contradictions between what the model flagged and what it
# scored. Advisory — nothing reads this table to change a score, and the
# pipeline does not block on a finding. It exists so the disagreements the old
# enforcement layer used to silently overwrite are visible instead.
def upsert_lint_findings(findings: List[Dict[str, Any]]) -> None:
    """Record one run's lint findings.

    Keyed (country, as_of, rule), so re-running a day replaces that day's
    findings for the same rule rather than accumulating duplicates. ``detail``
    overwrites plainly — unlike the snapshot columns, a fresher finding is
    always the better one, and there is no partial-failure case where an older
    detail is worth keeping.

    Args:
        findings: ``{country_iso2, as_of, rule, detail}`` dicts from
            ``util.lint.check``. Malformed entries are skipped.

    No-op on an empty list.
    """
    _require_db_url()  # fail fast even when there is nothing to write
    if not findings:
        return

    # Deduplicated by rule within each snapshot before the write. Two findings
    # sharing a rule used to make Postgres reject the whole INSERT ("cannot
    # affect row a second time"), and lint is supposed to be the non-blocking
    # part of the run — a duplicate rule must not raise where a contradiction
    # was the whole point. Last one wins.
    by_snapshot: Dict[Tuple[str, datetime.date], Dict[str, Dict[str, Any]]] = {}
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        iso2 = finding.get("country_iso2")
        as_of = finding.get("as_of")
        rule = finding.get("rule")
        if not iso2 or not rule or not isinstance(as_of, datetime.date):
            continue
        by_snapshot.setdefault((str(iso2), as_of), {})[str(rule)] = {
            "rule": str(rule), "detail": finding.get("detail"),
        }

    if not by_snapshot:
        return

    # An INSERT rather than an UPDATE, because lint runs *before* the snapshot
    # is written: phase 5 lints what phase 7 stores. Writing the key alone
    # leaves a stub whose score is NULL, and `upsert_snapshot` fills it in
    # without touching `lint` — so the two are order-independent, which an
    # UPDATE would not be.
    rows = [(iso2, as_of, _json_or_none(sorted(rules.values(), key=lambda f: f["rule"])))
            for (iso2, as_of), rules in by_snapshot.items()]
    with _transaction() as cur:
        extras.execute_values(
            cur,
            """
            INSERT INTO risk_snapshot (country_iso2, as_of, lint)
            VALUES %s
            ON CONFLICT (country_iso2, as_of)
            DO UPDATE SET lint = EXCLUDED.lint, updated_at = now()
            """,
            rows,
            page_size=100,
        )


def read_lint_findings(as_of: Optional[datetime.date] = None,
                       country_iso2: Optional[str] = None,
                       since: Optional[datetime.date] = None) -> List[Dict[str, Any]]:
    """Lint findings, newest first — the half of this tripwire that was missing.

    ``lint.check`` runs for every country on every run and `upsert_lint_findings`
    has been writing this table since the enforcement layer was deleted. Nothing
    read it back. The observe-only decision was justified on the argument that a
    contradiction would be *written down next to the score and looked at*, and
    for as long as nothing surfaced them the second half of that sentence was not
    true — the table was a place findings went, not a place anyone saw them.

    Args:
        as_of: one run's findings.
        country_iso2: one country's.
        since: everything from this date forward, for a trend rather than a
            snapshot — a rule that fires constantly is a prompt problem, and
            that only shows up across days.
    """
    where, params = [], []
    if as_of:
        where.append("as_of = %s")
        params.append(as_of)
    if country_iso2:
        where.append("country_iso2 = %s")
        params.append(country_iso2)
    if since:
        where.append("as_of >= %s")
        params.append(since)
    where.append("lint IS NOT NULL")
    clause = f"WHERE {' AND '.join(where)}"
    with _transaction() as cur:
        # One row per finding, unnested back out of the array on the snapshot
        # that owns it, so callers keep the flat shape they had when this was
        # its own table.
        cur.execute(f"""
            SELECT s.country_iso2, s.as_of,
                   f ->> 'rule'  AS rule,
                   f -> 'detail' AS detail,
                   s.updated_at  AS created_at
              FROM risk_snapshot s
              CROSS JOIN LATERAL jsonb_array_elements(s.lint) AS f
             {clause}
             ORDER BY s.as_of DESC, s.country_iso2, rule
        """, tuple(params))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def read_stage1_degradation(as_of: Optional[datetime.date] = None,
                            since: Optional[datetime.date] = None
                            ) -> List[Dict[str, Any]]:
    """Snapshots whose scorer read truncated bodies instead of digests.

    Read straight out of `risk_snapshot.input_manifest`, where
    ``provenance.stage1_health`` has been recording it since `28a8889`. That
    commit's stated purpose was that "the scorer read digests" and "the scorer
    read truncated bodies" would stop being indistinguishable after the fact.
    They stayed indistinguishable, because the block had no reader — the same
    shape as the WEO loader whose rows nothing resolved and the probe whose
    results lived in a commit message.

    Only rows with at least one degraded article come back: a clean run should
    print nothing rather than forty-eight zeroes.
    """
    where, params = [
        "(COALESCE((input_manifest -> 'stage1' ->> 'degraded')::int, 0)"
        " + COALESCE((input_manifest -> 'stage1' ->> 'truncated')::int, 0)) > 0"
    ], []
    if as_of:
        where.append("as_of = %s")
        params.append(as_of)
    if since:
        where.append("as_of >= %s")
        params.append(since)
    with _transaction() as cur:
        cur.execute(f"""
            SELECT country_iso2, as_of,
                   (input_manifest -> 'stage1' ->> 'articles')::int AS articles,
                   (input_manifest -> 'stage1' ->> 'digested')::int AS digested,
                   (input_manifest -> 'stage1' ->> 'degraded')::int AS degraded,
                   COALESCE((input_manifest -> 'stage1' ->> 'truncated')::int, 0)
                       AS truncated,
                   input_manifest -> 'stage1' -> 'degraded_ids'     AS degraded_ids,
                   input_manifest -> 'stage1' -> 'truncated_ids'    AS truncated_ids
              FROM risk_snapshot
             WHERE input_manifest IS NOT NULL AND {' AND '.join(where)}
             ORDER BY as_of DESC, country_iso2
        """, tuple(params))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


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


def upsert_market_prices(rows: List[Dict[str, Any]]) -> None:
    """Upsert the latest snapshot of the Prices pane (one row per symbol).

    Self-contained: ensures the ``market_price`` table exists, then upserts each
    asset by its stable ``symbol`` primary key. The metric columns
    (``px``/``chg``/``q``/``ytd``) are written with COALESCE so a transient null
    (e.g. a missing 1Q/YTD reference, or a symbol absent from one quote batch)
    never blanks a previously-populated cell — the daemon simply omits whole
    rows for markets it didn't poll this tick, leaving their last values intact.

    Each row dict (as built by ``util.prices``):
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

    On the same row as the live quote now. They were two tables because they
    tick at different rates — a quote every half hour, a reference once a day —
    but that is a cadence, not a key: both are facts about one symbol.

    Each value is
    ``{ref_q, ref_q_date, ref_ytd, ref_ytd_date, reference_refreshed_on}``.
    """
    with _transaction() as cur:
        cur.execute(
            """
            SELECT symbol, ref_q, ref_q_date, ref_ytd, ref_ytd_date, reference_refreshed_on
              FROM market_price
             WHERE reference_refreshed_on IS NOT NULL
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

    An UPDATE rather than an upsert, because the row belongs to the quote: a
    symbol's ``label`` and ``asset_class`` come from the asset universe in
    ``constants`` and are written by :func:`upsert_market_prices`. A reference
    for a symbol that has never been quoted is skipped and logged rather than
    inventing a half-row to hang it on.
    """
    _require_db_url()  # fail fast even when there is nothing to write
    if not refs:
        return

    rows: List[Tuple] = [
        (
            r.get("ref_q"),
            r.get("ref_q_date"),
            r.get("ref_ytd"),
            r.get("ref_ytd_date"),
            refreshed_on,
            symbol,
        )
        for symbol, r in refs.items()
        if isinstance(r, dict)
    ]
    if not rows:
        return

    with _transaction() as cur:
        cur.executemany(
            """
            UPDATE market_price
               SET ref_q                  = %s,
                   ref_q_date             = %s,
                   ref_ytd                = %s,
                   ref_ytd_date           = %s,
                   reference_refreshed_on = %s,
                   updated_at             = now()
             WHERE symbol = %s
            """,
            rows,
        )
        cur.execute(
            "SELECT count(*) FROM market_price WHERE symbol = ANY(%s)",
            (list(refs),),
        )
        matched = cur.fetchone()[0]
    if matched < len(rows):
        logger.info("[prices] %d reference close(s) had no quote row yet; "
                    "they land on the next refresh", len(rows) - matched)


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


# --- Identifiability probe results -------------------------------------------
# What the probe guessed, kept rather than logged.
#
# The probe ran twenty bundles on 2026-08-03 and left six traces, all of them
# incidental — `article_digest` rows written as a side effect of digesting. The
# result itself, fifteen of twenty identified with the leaking evidence quoted,
# survives only in a commit message. So the sweep fix that followed cannot be
# measured against the thing it was meant to fix, and the next masking change
# will be in the same position.
#
# That is the same shape as the WEO loader writing rows nothing read and the
# digest cache the history path never called: the producer looked fine and
# nothing recorded whether it worked. A probe is a measurement, and a
# measurement nobody stores is an opinion.
#
# Keyed on both masking versions, not just the map. The whole finding behind
# `SWEEP_VERSION` is that `mask_map_version` does not identify a masking
# behaviour on its own — two sweeps shared g5 — so a key without it would let a
# re-probe silently overwrite the baseline it was supposed to be compared with.
#
# `alternatives` and `insufficient_information` were computed and dropped. The
# schema asks the model for its three most likely countries with probabilities
# and for a flag saying it is guessing; `probe()` validates both and returns
# them; this table stored neither. So a run cost real money to measure a
# distribution and kept only its argmax, and a stored `ZZ @ 0.0` could not be
# told apart from a failed call. The first 26 rows are that loss, and they
# cannot be recovered — the only fix is to stop repeating it.

def upsert_probe_result(
    country_iso2: str,
    as_of: datetime.date,
    guess: Dict[str, Any],
    *,
    mask_map_version: str,
    sweep_version: str,
    probe_model: str,
    probe_version: str,
    n_articles: Optional[int] = None,
) -> None:
    """Record what the probe guessed about one masked bundle.

    Args:
        country_iso2: the truth, so ``identified`` can be stored rather than
            recomputed by whoever reads this later against a roster that may
            have changed.
        as_of: the bundle's anchor.
        guess: a ``masking.probe.probe`` result — ``{country, confidence,
            evidence, alternatives, insufficient_information}``. The evidence
            string is kept in full: "which country is this" is answered by the
            guess, but "why did masking fail here" is only ever answered by the
            text it quoted back. The ranked alternatives are kept for the same
            reason in a different direction: a bundle the probe places second at
            0.45 is not masked, and the argmax alone says it is.
        mask_map_version, sweep_version: the masking behaviour being measured.
            Both, for the reason in this section's comment.
        probe_model: the model that guessed, since a cheaper or smarter probe
            measures a different thing.
        n_articles: bundle size, so a thin week is not read as good masking.

    Never raises on a write failure — the probe is a measurement attached to a
    snapshot that is otherwise fine, and losing the measurement must not cost
    the score. It logs instead.
    """
    if not isinstance(guess, dict):
        return
    try:
        with _transaction() as cur:
            cur.execute(
                """
                INSERT INTO snapshot_diagnostic
                  (country_iso2, as_of, kind, variant, detail)
                VALUES (%s, %s, 'probe', %s, %s)
                ON CONFLICT (country_iso2, as_of, kind, variant)
                DO UPDATE SET detail = EXCLUDED.detail, created_at = now()
                """,
                (country_iso2, as_of,
                 # The variant carries the whole version tuple, because that is
                 # what identifies a measurement: two sweeps shared mask map g5,
                 # so a key without the sweep would let a re-probe overwrite the
                 # baseline it was supposed to be compared against.
                 f"{mask_map_version}:{sweep_version}:{probe_model}:{probe_version}",
                 extras.Json({
                     "mask_map_version": mask_map_version,
                     "sweep_version": sweep_version,
                     "probe_model": probe_model,
                     "probe_version": probe_version,
                     "guess": str(guess.get("country") or ""),
                     "confidence": float(guess.get("confidence") or 0.0),
                     "evidence": str(guess.get("evidence") or ""),
                     "identified": (str(guess.get("country") or "").upper()
                                    == country_iso2.upper()),
                     "alternatives": guess.get("alternatives") or None,
                     "insufficient_information": bool(
                         guess.get("insufficient_information")),
                     "n_articles": n_articles,
                     "git_sha": os.environ.get("GIT_SHA") or None,
                 })),
            )
    except Exception:
        logger.exception("[%s %s] probe result not recorded; the snapshot stands",
                         country_iso2, as_of)


def read_probe_results(country_iso2: Optional[str] = None,
                       mask_map_version: Optional[str] = None,
                       sweep_version: Optional[str] = None) -> List[Dict[str, Any]]:
    """Stored probe results, newest bundle first.

    What makes a re-probe a diff against a table rather than against a commit
    message. Filters are optional and compose: no arguments returns everything,
    a version pair returns one masking behaviour's baseline.
    """
    where, params = ["kind = 'probe'"], []
    if country_iso2:
        where.append("country_iso2 = %s")
        params.append(country_iso2)
    # The versions live inside `detail` now, so they are filtered as JSONB
    # rather than as columns. Same selectivity at this scale — the whole table
    # is tens of rows — and it keeps the key one string instead of four.
    for field, value in (("mask_map_version", mask_map_version),
                         ("sweep_version", sweep_version)):
        if value:
            where.append(f"detail ->> '{field}' = %s")
            params.append(value)
    with _transaction() as cur:
        cur.execute(f"""
            SELECT country_iso2, as_of,
                   detail ->> 'mask_map_version'  AS mask_map_version,
                   detail ->> 'sweep_version'     AS sweep_version,
                   detail ->> 'probe_model'       AS probe_model,
                   detail ->> 'probe_version'     AS probe_version,
                   detail ->> 'guess'             AS guess,
                   (detail ->> 'confidence')::float8 AS confidence,
                   detail ->> 'evidence'          AS evidence,
                   (detail ->> 'identified')::boolean AS identified,
                   detail -> 'alternatives'       AS alternatives,
                   (detail ->> 'insufficient_information')::boolean
                       AS insufficient_information,
                   (detail ->> 'n_articles')::int AS n_articles,
                   detail ->> 'git_sha'           AS git_sha,
                   created_at                     AS probed_at
              FROM snapshot_diagnostic
             WHERE {' AND '.join(where)}
             ORDER BY as_of DESC, country_iso2
        """, tuple(params))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# --- Scheduler bookkeeping ---------------------------------------------------
# The one place the process records "this job finished". main.py reads it on
# every tick to decide what is overdue, so a restart or a week of downtime
# catches up instead of silently skipping a run.

def read_job_runs() -> Dict[str, datetime.datetime]:
    """Return ``{job: last_run}`` for every scheduled job that has ever run.

    Values are timezone-aware (Postgres ``TIMESTAMPTZ``), safe to compare
    against ``datetime.now(timezone.utc)``.

    A scheduler job is a row in ``run_ledger`` like any other completed work,
    using the sentinel defaults for the country, anchor and variant it does not
    have. It was its own two-column table for no reason other than being
    written first.
    """
    with _transaction() as cur:
        cur.execute("SELECT job_type, completed_at FROM run_ledger "
                    "WHERE country_iso2 = '' AND variant = ''")
        return {job: last_run for job, last_run in cur.fetchall()}


def mark_job_run(job: str) -> None:
    """Stamp ``job`` as having just finished successfully."""
    with _transaction() as cur:
        cur.execute(
            """
            INSERT INTO run_ledger (job_type, status, completed_at)
            VALUES (%s, 'complete', now())
            ON CONFLICT (job_type, country_iso2, as_of, variant)
            DO UPDATE SET status = 'complete', completed_at = now()
            """,
            (job,),
        )
