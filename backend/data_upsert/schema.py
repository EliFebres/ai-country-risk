"""Every table, in one place, created from nothing.

The schema used to live in twenty places: ten `CREATE TABLE` blocks inside
`data_push`, five inside `store`, and five more that existed *only* as a fenced
SQL block in the README with an executable copy in a test harness. Those last
five were "operator-provisioned", which meant a fresh clone could not build
itself and nobody found out until they tried.

Ten tables, down from twenty. What merged, and the one merge that was declined,
is in `docs/pipeline.md`.

**Assumptions this module makes explicit rather than inheriting.**

* **No extensions.** Nothing here needs one — no uuid generation, no pgcrypto,
  no trigram indexes. `BIGSERIAL` and `JSONB` are core. The only extension in a
  working database is `plpgsql`, which Postgres installs itself.
* **Collation is the database's own.** Every text comparison uses it, and
  `C.UTF-8` orders punctuation differently from an ICU locale — which shows up
  in `indicator_code` sorts, where `GOV.DEBT` and `GOV_WGI` swap places.
  Nothing depends on that ordering for correctness, but `verify()` reports the
  collation so a surprise is visible rather than silent.
* **`timestamptz` everywhere, never `timestamp`.** An absolute instant does not
  depend on the server's timezone; a naive one does, and a backfill that
  re-dates itself by the deploy region's offset is the kind of error that reads
  as data.

Every statement is `IF NOT EXISTS`, so `create_all` on a live database is a
no-op and a half-finished bootstrap resumes rather than restarts.
"""

from typing import Dict, List, Tuple

# The body-status ladder, worst to best. Lives here because the CHECK below is
# its definition; `store` imports it so the Python and the constraint cannot
# disagree about what a valid status is.
BODY_STATUSES: Tuple[str, ...] = (
    "pending", "failed", "degraded-title-only", "recovered")

_STATUSES_SQL = "'" + "','".join(BODY_STATUSES) + "'"


# --- The roster ------------------------------------------------------------

COUNTRY = """
CREATE TABLE IF NOT EXISTS country (
    iso2        CHAR(2) PRIMARY KEY,
    name        TEXT NOT NULL,
    lat         DOUBLE PRECISION,
    lng         DOUBLE PRECISION,
    -- Structural facts the masked model is shown in place of the identity it
    -- cannot see: region, income group, monetary sovereignty, reserve-currency
    -- status. Cited values only; seeded from code, never hand-edited in place.
    structural  JSONB
)
"""


# --- Articles --------------------------------------------------------------

ARTICLE = f"""
CREATE TABLE IF NOT EXISTS article (
    url             TEXT PRIMARY KEY,
    publisher_link  TEXT,
    country_iso2    TEXT NOT NULL,
    -- google-news | guardian | gdelt | nyt. The live run and the backfill are
    -- the same path with a different source, so they share this table.
    source_system   TEXT NOT NULL,
    published_at    TIMESTAMPTZ NOT NULL,
    title           TEXT,
    abstract        TEXT,
    body            TEXT,
    -- api-native | wayback-YYYYMMDD | live-refetch. How old the *body* is, which
    -- is not how old the article is: a 2018 piece captured in 2020 carries two
    -- years of hindsight and must not reach a 2018 snapshot.
    body_vintage    TEXT,
    body_status     TEXT NOT NULL CHECK (body_status IN ({_STATUSES_SQL})),
    wayback_url     TEXT,
    content_sha256  TEXT,
    themes          TEXT[],
    tier            TEXT NOT NULL DEFAULT 'full',
    harvested_at    TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


# --- Model output, content-addressed ---------------------------------------

LLM_ARTIFACT = """
CREATE TABLE IF NOT EXISTS llm_artifact (
    content_sha256  TEXT NOT NULL,
    -- digest | rewrite. One table because both answer the same question:
    -- "what did the model produce for this exact text, under these versions".
    kind            TEXT NOT NULL CHECK (kind IN ('digest', 'rewrite')),
    -- the digest model, or the rewrite prompt version
    version         TEXT NOT NULL,
    -- masked | named. Has to be in the key: the same text digested under the
    -- two regimes gives two different answers, and serving a named digest to a
    -- masked run puts a president's name in the prompt with every gate clean.
    mode            TEXT NOT NULL CHECK (mode IN ('masked', 'named')),
    payload         JSONB NOT NULL,
    stage1_severity DOUBLE PRECISION,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (content_sha256, kind, version, mode)
)
"""


# --- Macro -----------------------------------------------------------------

INDICATOR_SERIES = """
CREATE TABLE IF NOT EXISTS indicator_series (
    country_iso2    TEXT NOT NULL,
    -- A key of constants.INDICATOR_REGISTRY, which also owns the label, unit
    -- and ledger. There is no `indicator` table: a code that is not in the
    -- registry is a code nothing reads, and that was a real defect for a year.
    indicator_code  TEXT NOT NULL,
    freq            TEXT NOT NULL CHECK (freq IN ('M', 'Q', 'A')),
    -- '2026-06' | '2026Q2' | '2025', matching the frequency. Never a date:
    -- `payload._period_to_date` returns None for '2025-12-31' at freq A and the
    -- row vanishes from every payload, silently.
    period          TEXT NOT NULL,
    value           DOUBLE PRECISION,
    -- When this observation became public, NOT when it was fetched. The whole
    -- no-future rule for macro rests on this column being honest.
    as_of           DATE NOT NULL,
    source          TEXT NOT NULL,
    vintage_scheme  TEXT NOT NULL DEFAULT 'as-published-latest',
    PRIMARY KEY (country_iso2, indicator_code, freq, period, as_of)
)
"""


# --- Scores ----------------------------------------------------------------

RISK_SNAPSHOT = """
CREATE TABLE IF NOT EXISTS risk_snapshot (
    country_iso2       CHAR(2) NOT NULL REFERENCES country(iso2) ON DELETE CASCADE,
    as_of              DATE NOT NULL,

    -- The frontend's contract. Only these five are read by risk-server.ts.
    score              DOUBLE PRECISION,
    bullet_summary     TEXT,
    non_investable     BOOLEAN,

    score_3m           DOUBLE PRECISION,
    raw_score_12m      DOUBLE PRECISION,
    raw_score_3m       DOUBLE PRECISION,

    -- The four friction-framework ledgers, as the model returned them.
    ledger_scores      JSONB,
    subscore_evidence  JSONB,
    condition_flags    JSONB,
    article_scores     JSONB,
    evidence_coverage  DOUBLE PRECISION,

    friction_score          DOUBLE PRECISION,
    order_uncertainty_score DOUBLE PRECISION,
    information_score       DOUBLE PRECISION,
    edge_vitality           DOUBLE PRECISION,

    applied_rules      JSONB,
    legal_gate         JSONB,

    -- The three articles the sidebar shows, in rank order. Was its own table
    -- keyed (country, as_of, rank) with a CHECK; an ordered array says the same
    -- thing and `article_ranking.ensure_top_three` is now the only guard.
    top_articles       JSONB,
    -- Advisory tripwires for this row. Was its own table keyed by rule.
    lint               JSONB,

    -- What produced the row, and enough to rebuild it.
    model_id           TEXT,
    prompt_version     TEXT,
    policy_version     TEXT,
    scoring_mode       TEXT NOT NULL DEFAULT 'masked'
                       CHECK (scoring_mode IN ('masked', 'named')),
    input_manifest     JSONB,

    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (country_iso2, as_of)
)
"""


SNAPSHOT_DIAGNOSTIC = """
CREATE TABLE IF NOT EXISTS snapshot_diagnostic (
    country_iso2 TEXT NOT NULL,
    as_of        DATE NOT NULL,
    -- probe | arm. Everything measuring the instrument rather than the country.
    kind         TEXT NOT NULL CHECK (kind IN ('probe', 'arm')),
    -- probe: "{mask_map}:{sweep}:{probe_model}:{probe_version}", because one
    --   country-day can be probed under several version tuples and each is its
    --   own measurement.
    -- arm:   'named' | 'masked_nostructural'. These never touch risk_snapshot —
    --   they share (country, as_of) with their masked twin and would overwrite
    --   the production series on its own primary key.
    variant      TEXT NOT NULL,
    detail       JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (country_iso2, as_of, kind, variant)
)
"""


# --- Work done -------------------------------------------------------------

RUN_LEDGER = """
CREATE TABLE IF NOT EXISTS run_ledger (
    -- etl | panels | prices | harvest | snapshot. One row per unit of work.
    job_type     TEXT NOT NULL,
    -- The sentinels are deliberate. A scheduler job has no country and no
    -- anchor, a harvest window has both plus a source, and a scored snapshot
    -- has a mode; a nullable primary key cannot express that, and three tables
    -- to say "what finished, when, with what status" was the duplication this
    -- merge removes. Every existing query is a prefix of this key.
    country_iso2 TEXT NOT NULL DEFAULT '',
    as_of        DATE NOT NULL DEFAULT '1970-01-01',
    variant      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    spend_usd    DOUBLE PRECISION,
    detail       JSONB,
    PRIMARY KEY (job_type, country_iso2, as_of, variant)
)
"""


# --- Frontend feeds --------------------------------------------------------

MARKET_PRICE = """
CREATE TABLE IF NOT EXISTS market_price (
    symbol        TEXT PRIMARY KEY,
    label         TEXT NOT NULL,
    asset_class   TEXT NOT NULL
                  CHECK (asset_class IN ('stocks', 'bonds', 'crypto', 'commodities')),
    source_symbol TEXT,
    is_yield      BOOLEAN NOT NULL DEFAULT FALSE,
    px            DOUBLE PRECISION,
    chg           DOUBLE PRECISION,
    q             DOUBLE PRECISION,
    ytd           DOUBLE PRECISION,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    -- The quarter and year-start reference closes, refreshed on their own
    -- cadence. Was `price_reference`, same key, different tick rate.
    ref_q                  DOUBLE PRECISION,
    ref_q_date             DATE,
    ref_ytd                DOUBLE PRECISION,
    ref_ytd_date           DATE,
    reference_refreshed_on DATE,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


NEWS_ALERT = """
CREATE TABLE IF NOT EXISTS news_alert (
    id            BIGSERIAL PRIMARY KEY,
    as_of         DATE NOT NULL,
    global_rank   SMALLINT NOT NULL,
    country_iso2  CHAR(2) NOT NULL,
    country_name  TEXT,
    url           TEXT NOT NULL,
    title         TEXT,
    source        TEXT,
    published_at  TIMESTAMPTZ,
    summary       TEXT,
    image_url     TEXT,
    topic         TEXT NOT NULL,
    severity      TEXT NOT NULL CHECK (severity IN ('Critical', 'Caution', 'Watch')),
    importance    DOUBLE PRECISION,
    rationale     TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (as_of, global_rank)
)
"""


# Kept separate from news_alert on purpose. They share the theme "dated thing
# the frontend shows" and nothing else: different keys, different lifecycles
# (alerts are deleted and rewritten whole each run, calendar rows are pruned by
# date), and half the columns of each would be null in the other. Merging them
# under a `kind` column would also make the alerts query silently return
# calendar events, which is worse than an error. See `docs/deferred.md`.
ECONOMIC_CALENDAR_EVENT = """
CREATE TABLE IF NOT EXISTS economic_calendar_event (
    id            BIGSERIAL PRIMARY KEY,
    event_time    TIMESTAMPTZ NOT NULL,
    country_code  TEXT NOT NULL,
    country_name  TEXT NOT NULL,
    event         TEXT NOT NULL,
    importance    TEXT NOT NULL CHECK (importance IN ('h', 'm', 'l')),
    currency      TEXT,
    previous      DOUBLE PRECISION,
    estimate      DOUBLE PRECISION,
    actual        DOUBLE PRECISION,
    ai_importance DOUBLE PRECISION,
    ai_rationale  TEXT,
    ai_scored_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_time, country_code, event)
)
"""


# Creation order: `risk_snapshot` references `country`, nothing else has an FK.
TABLES: Tuple[Tuple[str, str], ...] = (
    ("country", COUNTRY),
    ("article", ARTICLE),
    ("llm_artifact", LLM_ARTIFACT),
    ("indicator_series", INDICATOR_SERIES),
    ("risk_snapshot", RISK_SNAPSHOT),
    ("snapshot_diagnostic", SNAPSHOT_DIAGNOSTIC),
    ("run_ledger", RUN_LEDGER),
    ("market_price", MARKET_PRICE),
    ("news_alert", NEWS_ALERT),
    ("economic_calendar_event", ECONOMIC_CALENDAR_EVENT),
)


INDEXES: Tuple[str, ...] = (
    # The snapshot window: every historical score reads one country over 30 days.
    "CREATE INDEX IF NOT EXISTS article_country_published_idx "
    "  ON article (country_iso2, published_at DESC)",
    # Body recovery walks everything still owed a body.
    "CREATE INDEX IF NOT EXISTS article_pending_idx "
    "  ON article (body_status) WHERE body_status = 'pending'",
    # The digest cache's second chance is a hash lookup across snapshots.
    "CREATE INDEX IF NOT EXISTS llm_artifact_kind_idx "
    "  ON llm_artifact (kind, mode)",
    # The payload builder reads one country's whole macro history at once.
    "CREATE INDEX IF NOT EXISTS indicator_series_country_code_idx "
    "  ON indicator_series (country_iso2, indicator_code)",
    # The frontend asks for the newest snapshot per country.
    "CREATE INDEX IF NOT EXISTS risk_snapshot_as_of_idx "
    "  ON risk_snapshot (as_of DESC)",
    # Resume reads completed anchors for one mode.
    "CREATE INDEX IF NOT EXISTS run_ledger_type_status_idx "
    "  ON run_ledger (job_type, status)",
    "CREATE INDEX IF NOT EXISTS news_alert_as_of_idx "
    "  ON news_alert (as_of DESC, global_rank)",
    "CREATE INDEX IF NOT EXISTS econ_event_time_idx "
    "  ON economic_calendar_event (event_time)",
)


def table_names() -> List[str]:
    """The ten, in creation order."""
    return [name for name, _ in TABLES]


def create_all(cur) -> List[str]:
    """Create every table and index. Idempotent; returns what was created."""
    created = []
    for name, ddl in TABLES:
        cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
        existed = cur.fetchone()[0] is not None
        cur.execute(ddl)
        if not existed:
            created.append(name)
    for statement in INDEXES:
        cur.execute(statement)
    return created


def verify(cur) -> Dict[str, object]:
    """What actually exists, and the environment assumptions worth reporting."""
    cur.execute("""
        SELECT table_name FROM information_schema.tables
         WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
      ORDER BY table_name
    """)
    present = [r[0] for r in cur.fetchall()]

    counts = {}
    for name in present:
        cur.execute(f'SELECT count(*) FROM "{name}"')
        counts[name] = cur.fetchone()[0]

    cur.execute("SELECT extname FROM pg_extension ORDER BY 1")
    extensions = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT datcollate FROM pg_database WHERE datname = current_database()")
    collation = cur.fetchone()[0]
    cur.execute("SHOW server_version")
    version = cur.fetchone()[0]

    expected = set(table_names())
    return {
        "present": present,
        "counts": counts,
        "missing": sorted(expected - set(present)),
        "unexpected": sorted(set(present) - expected),
        "extensions": extensions,
        "collation": collation,
        "server_version": version,
    }
