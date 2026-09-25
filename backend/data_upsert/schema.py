"""
The whole schema, in one place, built in one call.

Ten tables. Not eleven, not nine: below ten means merging things whose keys and
lifecycles differ, above ten means something is stored twice. The count is not a
target, it is what falls out of keeping one key per kind of fact.

Before this module the schema lived in four places — prose in
`backend/README.md` for four tables, `CREATE TABLE` constants scattered through
`data_push.py` for five more, and two here. A fresh database reached the first
upsert and died on `relation "indicator" does not exist`, because prose does not
run. Now `bootstrap()` takes an empty database to the full schema and
`main.py bootstrap` calls it.

**What went away, and where it went.** `indicator`, `yearly_value` and
`recent_indicator` were three tables holding one fact — a macro value for a
country in a period — keyed three different ways, which the front-end joined by
display name. They are one table, `indicator_series`, keyed by the source's own
code and carrying the vintage and the scheme that produced it.
`risk_snapshot_article` was a three-row child table for something that is an
ordered list; it is `risk_snapshot.top_articles`. `payload_census` described the
same run as `risk_snapshot` and was joined to it by nobody; it is the manifest.

**What the manifest is for.** A score is comparable with another score only if
you can tell what produced both. Every `risk_snapshot` row carries the evidence
it was made from and the version of everything that could have changed the
answer, so a reader in three years can reconstruct the payload and decide
whether two weeks are the same instrument. A row without a manifest is refused
by the write path.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

__all__ = [
    "TABLES",
    "INDEXES",
    "STAMPED_FIELDS",
    "BODY_STATUSES",
    "ARTIFACT_KINDS",
    "bootstrap",
    "create_all",
    "table_names",
    "verify",
    "expected_columns",
    "drift",
    "SchemaDrift",
]


# Fields describing how a row was produced rather than what it says. Once
# written they are never rewritten: the write path refuses an upsert that would
# change one, and logs both values. The old branch lost its ability to detect a
# stale reference because a later commit quietly restamped a `git_sha`.
STAMPED_FIELDS: Tuple[str, ...] = (
    "prompt_version",
    "scoring_model",
    "digest_model",
    "gate_model",
    "git_sha",
    "payload_fingerprint",
)

#: How a run read an article. Recorded per article in the manifest, never on
#: the `article` row: it is a fact about the run.
BODY_STATUSES = ("full", "clipped", "digest-only", "title-only")

# `rewrite` has no writer yet — it is the masking layer's kind, present in the
# CHECK so that switching masking on later is not a schema change.
ARTIFACT_KINDS = ("relevance", "digest", "rewrite")
_KINDS_SQL = ", ".join(f"'{k}'" for k in ARTIFACT_KINDS)


# --- 1. Identity ------------------------------------------------------------

COUNTRY = """
CREATE TABLE IF NOT EXISTS country (
    iso2                 CHAR(2) PRIMARY KEY,
    iso3                 CHAR(3),
    name                 TEXT NOT NULL,
    -- What retrieval actually asks for. "Hong Kong SAR, China" is the World
    -- Bank's label and a phrase no headline contains; under it Hong Kong
    -- returned one article across six themes, against sixty under "Hong Kong".
    -- That is a stored fact about the country, not a constant in the fetcher.
    query_name           TEXT,
    tier                 TEXT,
    region               TEXT,
    income_group         TEXT,
    monetary_regime      TEXT,
    -- full | constrained | none.
    monetary_sovereignty TEXT,
    lat                  DOUBLE PRECISION,
    lng                  DOUBLE PRECISION,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


# --- 2. The evidence --------------------------------------------------------

ARTICLE = """
CREATE TABLE IF NOT EXISTS article (
    -- Only what is true of the article whatever run read it. Which country it
    -- was evidence for, how it was read and which query found it are facts
    -- about a run, and live in that run's manifest: stored here, one Greenland
    -- story "belonged" to Denmark when the US run read it, and a week that read
    -- a headline overwrote the status of a week that read the body.
    --
    -- The resolved publisher link. The Google News wrapper is not identity:
    -- one story arrives under several wrappers, and deduping on the wrapper let
    -- a single story take three of twenty slots.
    url                 TEXT NOT NULL,
    -- The hash of what was read, not of what was fetched: when a body is
    -- clipped this covers the clipped text, so a cache keyed on the full
    -- article cannot serve a digest of words the model never saw. In the key,
    -- so a re-fetched body that changed is a new row and the text last week's
    -- manifest points at survives. '' when no body was fetched.
    content_sha256      TEXT NOT NULL DEFAULT '',
    wrapper_url         TEXT,
    source_system       TEXT NOT NULL DEFAULT 'google-news',
    publisher           TEXT,
    -- The feed's date and the page's own date, kept apart because a
    -- republished piece is dated to the day it was re-listed and the
    -- disagreement is the signal.
    published_at        TIMESTAMPTZ,
    page_published_at   TIMESTAMPTZ,
    title               TEXT,
    abstract            TEXT,
    body                TEXT,
    body_chars_original INT,
    body_clipped        BOOLEAN NOT NULL DEFAULT FALSE,
    harvested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (url, content_sha256)
)
"""


# --- 3. Model output, content-addressed -------------------------------------

LLM_ARTIFACT = f"""
CREATE TABLE IF NOT EXISTS llm_artifact (
    content_sha256  TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ({_KINDS_SQL})),
    -- The hash of the prompt text. A version somebody has to remember to bump
    -- is a version that will eventually be wrong while looking right.
    version         TEXT NOT NULL,
    -- masked | named. In the key because the same text under the two regimes
    -- gives two different answers.
    mode            TEXT NOT NULL DEFAULT 'named'
                    CHECK (mode IN ('masked', 'named')),
    -- '' for kinds that are not per-country. A relevance verdict is about
    -- (this text, this country): a German election story is structural for
    -- Germany and irrelevant for Portugal. The country is already inside the
    -- relevance hash; as a column it makes "every verdict for PT" a query
    -- rather than a hash recomputation.
    country_iso2    TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL,
    payload         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (content_sha256, kind, version, mode, country_iso2)
)
"""


# --- 4. The macro record ----------------------------------------------------

INDICATOR_SERIES = """
CREATE TABLE IF NOT EXISTS indicator_series (
    country_iso2   TEXT NOT NULL,
    -- The source's own code. A friendly name is ours to change;
    -- FP.CPI.TOTL.ZG is the World Bank's and means the same in five years.
    indicator_code TEXT NOT NULL,
    period         INT NOT NULL,
    -- When the number became knowable, in the key — so a revision is a new row
    -- rather than an overwrite, and a score can be re-read against the data as
    -- it stood rather than as it was later corrected.
    as_of          DATE NOT NULL,
    -- source-published | publication-lag-estimate | first-seen. A measured
    -- date, a derived one and the day we first held the value are different
    -- facts and stay distinguishable. See util/vintage.py.
    as_of_scheme   TEXT NOT NULL,
    value          DOUBLE PRECISION NOT NULL,
    unit           TEXT,
    source         TEXT NOT NULL,
    ledger         TEXT NOT NULL,
    freq           CHAR(1) NOT NULL DEFAULT 'A',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (country_iso2, indicator_code, period, as_of)
)
"""


# --- 5. The product ---------------------------------------------------------

RISK_SNAPSHOT = """
CREATE TABLE IF NOT EXISTS risk_snapshot (
    country_iso2        CHAR(2) NOT NULL REFERENCES country(iso2),
    -- Stamped once at the top of a run and passed everywhere. Read per country,
    -- it split a two-hour run across two dates — 34 rows on one day and 14 on
    -- the next, with the census and the snapshot disagreeing by one country.
    run_date            DATE NOT NULL,

    -- Integers 0-100, as the model returned them. No bands, no rescale to 0-1:
    -- bands are a display decision and a float is a conversion the reader can
    -- do. Nothing is compressed at write.
    score_12m           INTEGER NOT NULL,
    score_3m            INTEGER NOT NULL,
    friction_score      INTEGER,
    order_score         INTEGER,
    information_score   INTEGER,
    edge_score          INTEGER,

    condition_flags     JSONB,
    bullet_summary      TEXT,
    subscore_evidence   JSONB,
    article_scores      JSONB,
    -- The ordered top articles, which used to be three rows in a child table
    -- for something that is a list.
    top_articles        JSONB,

    -- Computed from the census, never authored by the model. Columns rather
    -- than manifest fields so the dashboard reads them without parsing JSON.
    evidence_coverage   INTEGER,
    coverage_components JSONB,

    -- Stamps: immutable once written. See STAMPED_FIELDS.
    payload_fingerprint TEXT,
    prompt_version      TEXT,
    scoring_model       TEXT,
    digest_model        TEXT,
    gate_model          TEXT,
    seed                INTEGER,
    git_sha             TEXT,
    payload_tokens      INTEGER,

    restricted_badge    JSONB,
    -- Everything needed to reconstruct the payload: selected article ids with
    -- body statuses, rejected ids with labels, resolved indicator codes with
    -- periods and as_of, per-ledger counts, and the census. A row without one
    -- is refused by the writer.
    manifest            JSONB NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (country_iso2, run_date)
)
"""


# --- 6. Measuring the instrument, not the country ---------------------------

SNAPSHOT_DIAGNOSTIC = """
CREATE TABLE IF NOT EXISTS snapshot_diagnostic (
    country_iso2 TEXT NOT NULL,
    run_date     DATE NOT NULL,
    -- repeat | arm | probe. What is being measured.
    kind         TEXT NOT NULL,
    model        TEXT NOT NULL,
    variant      TEXT NOT NULL DEFAULT '',
    payload      JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (country_iso2, run_date, kind, model, variant)
)
"""


# --- 7. What ran, where, and what it cost -----------------------------------

RUN_LEDGER = """
CREATE TABLE IF NOT EXISTS run_ledger (
    -- etl | panels | calendar | imf-refresh | alerts | report | prices.
    job_type      TEXT NOT NULL,
    -- '' for work that is not per-country.
    country_iso2  TEXT NOT NULL DEFAULT '',
    run_date      DATE NOT NULL,
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    status        TEXT NOT NULL,
    -- Where it ran and what it wrote to. Two databases with confusable names
    -- cost a day once; this makes "which database produced this row" a query
    -- instead of a memory. Never credentials.
    host          TEXT,
    db_host       TEXT,
    db_name       TEXT,
    git_sha       TEXT,
    input_tokens  BIGINT,
    output_tokens BIGINT,
    spend_usd     DOUBLE PRECISION,
    detail        JSONB,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (job_type, country_iso2, run_date)
)
"""


# --- 8. Prices --------------------------------------------------------------

MARKET_PRICE = """
CREATE TABLE IF NOT EXISTS market_price (
    symbol        TEXT NOT NULL,
    -- The observation's own timestamp, in the key, so this is a series rather
    -- than one row overwritten in place.
    ts            TIMESTAMPTZ NOT NULL,
    -- quote | reference-quarter | reference-ytd. The once-daily reference
    -- closes are observations too. They used to live in their own table keyed
    -- by symbol alone, which made it impossible to hold more than one.
    role          TEXT NOT NULL DEFAULT 'quote'
                  CHECK (role IN ('quote', 'reference-quarter', 'reference-ytd')),
    label         TEXT,
    asset_class   TEXT,
    source_symbol TEXT,
    is_yield      BOOLEAN NOT NULL DEFAULT FALSE,
    px            DOUBLE PRECISION,
    chg           DOUBLE PRECISION,
    q             DOUBLE PRECISION,
    ytd           DOUBLE PRECISION,
    sort_order    INTEGER,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, ts)
)
"""


# --- 9. Alerts --------------------------------------------------------------

NEWS_ALERT = """
CREATE TABLE IF NOT EXISTS news_alert (
    country_iso2 CHAR(2) NOT NULL,
    run_date     DATE NOT NULL,
    -- The global rank across every country's pooled articles, which is unique
    -- within a country for a given run.
    rank         SMALLINT NOT NULL,
    country_name TEXT,
    url          TEXT NOT NULL,
    title        TEXT,
    source       TEXT,
    published_at TIMESTAMPTZ,
    summary      TEXT,
    image_url    TEXT,
    topic        TEXT NOT NULL,
    severity     TEXT CHECK (severity IN ('Critical', 'Caution', 'Watch')),
    importance   DOUBLE PRECISION,
    rationale    TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (country_iso2, run_date, rank)
)
"""


# --- 10. Calendar -----------------------------------------------------------

ECONOMIC_CALENDAR_EVENT = """
CREATE TABLE IF NOT EXISTS economic_calendar_event (
    -- Derived from (event_time, country_code, event), so the same event keeps
    -- the same id across runs. A serial id would make idempotency a lookup.
    event_id      TEXT PRIMARY KEY,
    event_time    TIMESTAMPTZ NOT NULL,
    country_code  TEXT NOT NULL,
    country_name  TEXT,
    event         TEXT NOT NULL,
    importance    TEXT CHECK (importance IN ('h', 'm', 'l')),
    currency      TEXT,
    previous      TEXT,
    estimate      TEXT,
    actual        TEXT,
    ai_importance DOUBLE PRECISION,
    ai_rationale  TEXT,
    ai_scored_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


# Order matters: `risk_snapshot` references `country`.
TABLES: Dict[str, str] = {
    "country": COUNTRY,
    "article": ARTICLE,
    "llm_artifact": LLM_ARTIFACT,
    "indicator_series": INDICATOR_SERIES,
    "risk_snapshot": RISK_SNAPSHOT,
    "snapshot_diagnostic": SNAPSHOT_DIAGNOSTIC,
    "run_ledger": RUN_LEDGER,
    "market_price": MARKET_PRICE,
    "news_alert": NEWS_ALERT,
    "economic_calendar_event": ECONOMIC_CALENDAR_EVENT,
}


INDEXES: Tuple[str, ...] = (
    # Reconstruction looks a manifest's articles up by the hash of what was read.
    "CREATE INDEX IF NOT EXISTS article_content_idx "
    "ON article (content_sha256)",
    "CREATE INDEX IF NOT EXISTS llm_artifact_kind_idx "
    "ON llm_artifact (kind, mode, country_iso2)",
    "CREATE INDEX IF NOT EXISTS indicator_series_country_code_idx "
    "ON indicator_series (country_iso2, indicator_code, period DESC)",
    # The quality report reads a country's own recent runs.
    "CREATE INDEX IF NOT EXISTS risk_snapshot_country_date_idx "
    "ON risk_snapshot (country_iso2, run_date DESC)",
    "CREATE INDEX IF NOT EXISTS run_ledger_type_date_idx "
    "ON run_ledger (job_type, run_date DESC)",
    "CREATE INDEX IF NOT EXISTS market_price_symbol_role_ts_idx "
    "ON market_price (symbol, role, ts DESC)",
    "CREATE INDEX IF NOT EXISTS news_alert_run_date_idx "
    "ON news_alert (run_date DESC, rank)",
    "CREATE INDEX IF NOT EXISTS econ_event_time_idx "
    "ON economic_calendar_event (event_time)",
)


class SchemaDrift(RuntimeError):
    """A table exists with a shape that is not the one in this module."""


_CONSTRAINT_WORDS = ("primary", "foreign", "unique", "check", "constraint", "exclude")


def expected_columns(ddl: str) -> List[str]:
    """Column names declared by one `CREATE TABLE` body, in order.

    Parsed from the DDL rather than kept in a second list beside it, because a
    second list is a thing that drifts from the first one and nobody notices
    until the database disagrees with both.
    """
    body = ddl[ddl.index("(") + 1: ddl.rindex(")")]
    names: List[str] = []
    depth = 0
    for raw in body.splitlines():
        line = raw.split("--", 1)[0].strip()
        if not line:
            continue
        if depth == 0:
            first = line.split()[0].strip(",").lower()
            if first and first not in _CONSTRAINT_WORDS and first.isidentifier():
                names.append(first)
        depth += line.count("(") - line.count(")")
    return names


def drift(cur) -> Dict[str, Dict[str, List[str]]]:
    """Compare every existing table against the shape declared here.

    Returns `{table: {"missing": [...], "unexpected": [...]}}` for tables that
    exist and disagree. Absent tables are not drift — they are about to be
    created.

    This exists because `CREATE TABLE IF NOT EXISTS` is silent about a table
    that is already there in the wrong shape. The polite failure is an index on
    a column that does not exist; the impolite one is a run that writes into the
    wrong shape and says nothing.
    """
    out: Dict[str, Dict[str, List[str]]] = {}
    for name, ddl in TABLES.items():
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s",
            (name,),
        )
        actual = {r[0] for r in cur.fetchall()}
        if not actual:
            continue
        wanted = set(expected_columns(ddl))
        missing = sorted(wanted - actual)
        unexpected = sorted(actual - wanted)
        if missing or unexpected:
            out[name] = {"missing": missing, "unexpected": unexpected}
    return out


def table_names() -> List[str]:
    """The ten, in creation order."""
    return list(TABLES)


def create_all(cur) -> List[str]:
    """Create every table and index if absent. Idempotent.

    Takes a cursor rather than opening a connection so the caller owns the
    transaction: provisioning that commits on its own leaves a half-built schema
    behind when something later fails.
    """
    for ddl in TABLES.values():
        cur.execute(ddl)
    for idx in INDEXES:
        cur.execute(idx)
    return table_names()


def verify(cur) -> Dict[str, bool]:
    """Which of the ten the database actually has.

    The consumer side of `create_all`. A statement that returned without raising
    is not evidence that a table exists.
    """
    out: Dict[str, bool] = {}
    for name in table_names():
        cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
        out[name] = cur.fetchone()[0] is not None
    return out


def bootstrap(conn) -> Dict[str, bool]:
    """Take an empty database to the full schema in one call.

    Raises:
        RuntimeError: if any table is still absent afterwards.
    """
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            drifted = drift(cur)
            if drifted:
                lines = [
                    f"  {name}: missing {d['missing']}, unexpected {d['unexpected']}"
                    for name, d in sorted(drifted.items())
                ]
                raise SchemaDrift(
                    "the database disagrees with schema.py and bootstrap "
                    "will not paper over it:" + chr(10)
                    + chr(10).join(lines)
                    + chr(10) * 2
                    + "Reconcile the table, or back it up and drop it so "
                    "bootstrap can rebuild it. CREATE TABLE IF NOT EXISTS is "
                    "silent about a table that already exists in the wrong "
                    "shape, and a run that writes into the wrong shape is "
                    "worse than one that refuses to start."
                )
            create_all(cur)
            present = verify(cur)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    missing = [name for name, ok in present.items() if not ok]
    if missing:
        raise RuntimeError(f"bootstrap did not create: {', '.join(missing)}")
    return present
