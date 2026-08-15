# Schema: 20 tables → 10, built from scratch

There is no migration. The old database was deleted and rebuilt, so this
records **what the schema is**, what merged into what, the one merge declined,
and what a later session needs in order to rewrite the frontend.

## The tables

| Table | Absorbed | Why |
|---|---|---|
| `country` | `country` + structural facts (was YAML) | roster identity, plus the facts masking removes and cannot replace |
| `article` | `historical_article` | one article store; `source_system` already separates google-news / guardian / gdelt / nyt |
| `llm_artifact` | `article_digest` + `history_digest_cache` + `history_rewrite_cache` | one content-addressed store, `kind` ∈ digest / rewrite |
| `indicator_series` | `indicator_series` + `yearly_value` + `recent_indicator` + `indicator` + the Parquet panel | every macro observation, one key, one vintage rule |
| `risk_snapshot` | `risk_snapshot` + `risk_snapshot_article` + `risk_lint` | per-article impacts and lint findings are per-snapshot detail, as JSONB on the row that owns them |
| `snapshot_diagnostic` | `probe_result` + the arms' results | everything measuring the instrument rather than the country |
| `run_ledger` | `job_run` + `harvest_checkpoint` + `history_run_ledger` | one row per unit of work, one `job_type` |
| `market_price` | `market_price` + `price_reference` | same subject, different cadence |
| `news_alert` | unchanged | |
| `economic_calendar_event` | **unchanged — declined, see below** | |

Ten, against a target of nine. `SCHEMA.py` is the single definition; it used to
live in twenty places, five of which existed only as a fenced SQL block in a
README with an executable copy inside a test harness — which is why a fresh
clone could not build itself.

## The declined merge

`economic_calendar_event` into `news_alert`. They share the theme "dated thing
the frontend shows" and nothing else: different keys — `(as_of, global_rank)`
against `(event_time, country_code, event)` — different lifecycles (alerts are
deleted and rewritten whole each run, calendar rows are pruned by date), and
half the columns of each would be null in the other.

The decisive argument is the failure mode. The frontend's alerts query is
`SELECT … FROM news_alert WHERE as_of = …` with no `kind` filter, so a merge
that kept the name would **silently render calendar events in the AI-alerts
pane**: wrong output, no error. If it is ever merged, name the result
`feed_event` so the query fails against a missing table instead.

A second merge was declined and then reinstated. `article_digest` into
`llm_artifact` was lossy *as a migration* — 61 colliding `(sha, model)` keys in
the old data, 32 of them holding different digests for byte-identical text,
because the stage-1 model is not deterministic in practice despite
`temperature=0` and a fixed seed. Rebuilding from scratch removed the thing
being lost, and content-addressing is now strictly better than what it
replaced: two snapshots can no longer disagree about the digest of identical
text.

## What changed beyond renaming

Redesigned rather than carried over, because a rebuild had no migration
constraints to respect:

- **`recent_indicator` is gone.** `refresh_imf_indicators` fetched the same IMF
  series twice — once as a latest print, once as a full history — because the
  latest-print table could not carry the volatility windows. Same data, two
  tables. `payload._resolve` already picks the freshest observation.
- **The Parquet panel is gone.** World Bank annuals are `indicator_series` rows
  written by `country_data_fetch.panel_rows`, stamped 31 December of their own
  year and **capped at today** — the uncapped version stamped the current
  year's value four months in the future, which the payload reported as
  negative staleness.
- **`build_evidence_payload` lost `panel` and `recent`.** All three inputs held
  overlapping copies of the same observations; freshest-wins is one resolution
  over one store now.
- **`risk_snapshot.raw_subscores`** was created and never written. Dropped.
  `subscores` became `ledger_scores`, which is what it holds.
- **`run_ledger` uses sentinel defaults** (`country_iso2 = ''`,
  `as_of = '1970-01-01'`, `variant = ''`) so a scheduler job with no country
  shares a primary key with a harvest window and a scored anchor. Not elegant;
  the alternative was a nullable primary key or keeping three tables.
- **The diagnostic arms stay in `run_ledger`** with every other run — an arm is
  work that completed, and splitting it out would fork resume and spend
  accounting. Only the arm's *result* goes to `snapshot_diagnostic`.

## Environment assumptions, stated rather than inherited

Verified against the live database (PostgreSQL 18.4):

- **No extensions.** A working database has only `plpgsql`, which Postgres
  installs itself. Nothing here needs uuid generation, pgcrypto or trigram
  indexes.
- **Collation is the database's own**, `C.UTF-8` here. It orders punctuation
  differently from an ICU locale, which shows up in `indicator_code` sorts
  (`GOV.DEBT` vs `GOV_WGI`). Nothing depends on it for correctness;
  `schema.verify()` reports it so a surprise is visible.
- **`timestamptz` everywhere, never `timestamp`**, so no value re-dates itself
  by the deploy region's offset.

---

## Frontend impact

**No compatibility views, and nothing under `frontend/` was touched.** This is
the map for rewriting `risk-server.ts` later. Every query lives in that one
file.

### Survives untouched

| Route | Method | Why |
|---|---|---|
| `/api/risk` | `fetchJoinedLatestRisks` | `risk_snapshot` + `country` keep their names and every column it selects |
| `/api/risk-summary` | `fetchLatestSummaries` | `risk_snapshot.bullet_summary` only |
| `/api/prices` | `fetchMarketPrices` | `market_price` keeps its name and gains columns |
| `/api/econ-calendar` | `fetchEconCalendarEvents` | the declined merge — table unchanged |

### Breaks loudly — 500s

| Route | Method | Old → new |
|---|---|---|
| `/api/indicators` | `fetchLatestIndicatorValues` (`:265`) | `indicator` + `yearly_value` + `recent_indicator` → `indicator_series` |
| `/api/articles` | `fetchLatestArticlesForLatestSnapshots` (`:440`) | `risk_snapshot_article` → `risk_snapshot.top_articles` JSONB |
| `/api/dashboard` | composes both | inherits them |

`fetchLatestIndicatorValues` **looks** defensive and is not: its `catch` falls
back to `annualOnlySql`, which reads the same two dead tables, so the fallback
throws uncaught. Do not read the `try` as protection.

### Breaks quietly — returns empty

| Route | Method | Effect |
|---|---|---|
| `/api/dashboard` | `fetchIndicatorAverageTrends` (`:409`) | `catch → {}`; the trend rail renders no lines |
| `/api/dashboard` | `fetchChannels` (`:429`) | already empty — `live_tv_channel` **has never existed** in the database |

### The rewrites, concretely

**`fetchLatestIndicatorValues`** — the join by indicator *name* is gone.
`indicator_series` is keyed by code, and label/unit live in
`backend/util/constants.py::INDICATOR_REGISTRY`. Either hard-code the code→label
pairs in the frontend or expose them from the backend; there is no table to join.

```sql
SELECT DISTINCT ON (country_iso2, indicator_code)
       country_iso2, indicator_code, period, freq, value, source, as_of
  FROM indicator_series
 WHERE indicator_code = ANY($1::text[])
 ORDER BY country_iso2, indicator_code, period DESC, as_of DESC;
```

Shape change: the old query returned `annual_value` **and** `recent_value` side
by side and the client picked. The new one returns the winner already resolved,
so `useRecent` goes away and `year` comes from `period`.

**`fetchLatestArticlesForLatestSnapshots`** — no longer a join.

```sql
SELECT s.country_iso2, c.name, s.as_of, s.top_articles
  FROM risk_snapshot s JOIN country c ON c.iso2 = s.country_iso2
 WHERE (s.country_iso2, s.as_of) IN (
   SELECT country_iso2, max(as_of) FROM risk_snapshot GROUP BY 1)
   AND s.top_articles IS NOT NULL;
```

Shape change: three rows per country become one row with a three-element JSONB
array, ordered by rank. The `rank BETWEEN 1 AND 3` CHECK is gone, so
`article_ranking.ensure_top_three` is the only guard —
`testing/test_news_fetching.py::TestEnsureTopThree` covers it.

**`fetchIndicatorAverageTrends`** — same table swap, grouped by `period`,
filtered to `freq = 'A'`.

**`fetchChannels`** — delete it, or create the table. It has never worked.
