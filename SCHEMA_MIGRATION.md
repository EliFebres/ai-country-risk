# Schema migration: 20 tables → 10

What the merge does, what it breaks in the frontend, and how to rewrite
`risk-server.ts` from this document without re-deriving anything.

The database has **20 tables and 292,071 rows**. Note that is 20, not the 18 the
brief assumed and not the 22 the code implies: `live_tv_channel` is queried by
the frontend but **has never existed** in the database, and there is no `article`
or `snapshot_diagnostic` table yet.

---

## The map

| Target | Absorbs | Rows in | Rows out |
|---|---|---:|---:|
| `country` | `country` + structural facts (YAML → column) | 48 | 48 |
| `article` | `historical_article` (rename only) | 103,161 | 103,161 |
| `llm_artifact` | `history_digest_cache` + `history_rewrite_cache` | 2,473 | 2,473 |
| `indicator_series` | `indicator_series` + `yearly_value` + `recent_indicator` + `indicator` + the parquet panel | 182,723 + panel | ~194,000 |
| `risk_snapshot` | `risk_snapshot` + `risk_snapshot_article` + `risk_lint` | 409 | 102 |
| `snapshot_diagnostic` | `probe_result` + the 12 diagnostic-arm rows of `history_run_ledger` | 70 | 70 |
| `run_ledger` | `job_run` + `harvest_checkpoint` + the 52 masked rows of `history_run_ledger` | 714 | 714 |
| `market_price` | `market_price` + `price_reference` | 22 | 14 |
| `article_digest` | **unchanged — declined merge, see below** | 2,186 | 2,186 |
| `news_alert` | **unchanged — declined merge, see below** | 30 | 30 |
| `economic_calendar_event` | **unchanged — declined merge, see below** | 235 | 235 |

Eleven objects if both declines stand; **nine** if both are overruled. The brief
set nine as the floor and asked for any compromise to be reported rather than
forced. Both declines are below, with the evidence.

---

## Declined merge 1: `article_digest` into `llm_artifact`

**The brief's reasoning:** all three digest/rewrite tables are
(content hash, version, mode) → model output.

**Why it does not hold.** Two of them are. `article_digest` is not: its primary
key is `(country_iso2, as_of, url)` and `content_sha256` is a *validation*
column, not the key.

Re-keying it by content hash is **lossy, and provably so**:

```
2,186 rows, 0 with a null hash, 1 model
2,070 distinct (content_sha256, model_id)  →  61 colliding keys
       of those 61, 32 hold DIFFERENT digests
```

The stage-1 model is not deterministic in practice, despite `temperature=0` and
a fixed seed. Content-addressing would pick one of two different digests for 32
articles, arbitrarily. `input_manifest` hashes what the model read and
`rebuild_snapshot` compares against it, so this is not cosmetic: it changes what
a rebuild is comparing.

**Recommendation:** `llm_artifact` absorbs the two genuinely content-addressed
caches. `article_digest` keeps its own key and its own table.

**If overruled:** migrate with `DISTINCT ON (content_sha256, model_id) … ORDER BY
as_of DESC` so the newest digest wins deterministically, and accept that 116 rows
collapse and 32 snapshots may see a digest they were not scored with.

## Declined merge 2: `economic_calendar_event` into `news_alert`

**The brief's reasoning:** both are dated frontend-facing events.

**Why it is worth declining.** They share the theme and nothing else. Different
keys — `(as_of, global_rank)` against `(event_time, country_code, event)` —
different lifecycles (alerts are `DELETE`d and rewritten whole each run, calendar
rows are pruned by date), and of ~20 columns only about half overlap. Merged
under a `kind` discriminator, each row leaves the other kind's columns null.

The decisive argument is the failure mode. The frontend's alerts query is
`SELECT … FROM news_alert WHERE as_of = …` with no `kind` filter, so a merge that
keeps the name **silently renders calendar events in the AI-alerts pane**. That
is worse than the 500 you asked for: wrong output, no error.

**Recommendation:** leave both tables alone.

**If overruled:** merge into a table named `feed_event`, not `news_alert`, so the
frontend query fails against a missing table and degrades to empty rather than
rendering the wrong rows.

---

## Target DDL, in brief

Only the parts that change are given.

**`country`** gains `structural JSONB` — region, income group, monetary
sovereignty, reserve-currency status. Currently `backend/data/curated/
structural_facts.yaml`, five countries filled; becomes committed seed data in
phase 4.

**`article`** is `historical_article` renamed. No column changes. `source_system`
already distinguishes google-news / guardian / gdelt / nyt. The daily run still
does not write here — see `DEFERRED.md`.

**`llm_artifact`**
```
content_sha256 TEXT, kind TEXT CHECK (kind IN ('digest','rewrite')),
version TEXT,          -- digest_model, or rewrite_version
mode TEXT,             -- 'masked' | 'named'
payload JSONB,         -- the digest, or {"rewritten": "…"}
stage1_severity FLOAT8, created_at TIMESTAMPTZ
PRIMARY KEY (content_sha256, kind, version, mode)
```

**`indicator_series`** keeps its five-column key. Absorbs:
- `yearly_value` → `as_of` = 31 Dec of `yr`, `freq='A'`, `source='World Bank'`
- `recent_indicator` → `period` from the date, `freq`/`source` as stored
- `indicator` → dies; label and unit come from `constants.INDICATOR_REGISTRY`
- the parquet panel → same shape as `yearly_value`

All nine `indicator.name` values map to registry codes with none left over, and
the only code that already has annual rows is `CPI.YOY` — those are WEO editions
carrying edition `as_of` dates, so World Bank rows at year-end `as_of` coexist
rather than collide.

**`risk_snapshot`** keeps `(country_iso2, as_of)` and its 25 columns, gaining:
- `top_articles JSONB` — the 306 `risk_snapshot_article` rows, exactly 3 per
  snapshot for all 102, as an ordered array of
  `{rank, url, title, source, published_at, impact, summary, image_url}`
- `lint JSONB` — array of `{rule, detail, created_at}`

**`snapshot_diagnostic`** — everything measuring the instrument rather than the
country.
```
country_iso2 TEXT, as_of DATE, kind TEXT,   -- 'probe' | 'arm'
variant TEXT,        -- probe: "{mask_map}:{sweep}:{model}:{probe_version}"
                     -- arm:   'named' | 'masked_nostructural'
detail JSONB, created_at TIMESTAMPTZ
PRIMARY KEY (country_iso2, as_of, kind, variant)
```
58 probe rows over 35 country-days (multiple version tuples each, which is why
`variant` is in the key) plus 12 arm rows.

**`run_ledger`** — one row per unit of work, with sentinel defaults so a single
key covers all three sources.
```
job_type TEXT NOT NULL,                            -- 'etl'|'panels'|'harvest'|'snapshot'
country_iso2 TEXT NOT NULL DEFAULT '',
as_of DATE NOT NULL DEFAULT '1970-01-01',          -- anchor, or harvest window start
variant TEXT NOT NULL DEFAULT '',                  -- source_system, or scoring mode
status TEXT NOT NULL, completed_at TIMESTAMPTZ NOT NULL,
spend_usd FLOAT8, detail JSONB
PRIMARY KEY (job_type, country_iso2, as_of, variant)
```
The sentinels are not elegant. They are what lets `job_run`'s single-column key
and `harvest_checkpoint`'s three-column key share one table without a nullable
primary key, and every existing query maps onto a prefix of this one.

**`market_price`** gains `ref_q, ref_q_date, ref_ytd, ref_ytd_date,
reference_refreshed_on` from `price_reference`. Same `symbol` key, 8 of 14
symbols carry references.

---

## Frontend impact

**No compatibility views, and no edits to anything under `frontend/`.** This
section is the map for rewriting `risk-server.ts` in a later session.

Every query lives in `frontend/app/lib/risk-server.ts`.

### Survives untouched

| Route | Method | Why |
|---|---|---|
| `/api/risk` | `fetchJoinedLatestRisks` | reads `risk_snapshot` + `country`, both keep their names and every column it selects |
| `/api/risk-summary` | `fetchLatestSummaries` | `risk_snapshot.bullet_summary` only |
| `/api/prices` | `fetchMarketPrices` | `market_price` keeps its name and gains columns |

### Breaks loudly — 500s

| Route | Method | Old → new |
|---|---|---|
| `/api/indicators` | `fetchLatestIndicatorValues` (`:265`) | `indicator` + `yearly_value` + `recent_indicator` → `indicator_series` |
| `/api/articles` | `fetchLatestArticlesForLatestSnapshots` (`:440`) | `risk_snapshot_article` → `risk_snapshot.top_articles` JSONB |
| `/api/dashboard` | composes the above | inherits both |

`fetchLatestIndicatorValues` **looks** defensive and is not. Its `catch` falls
back to `annualOnlySql`, which reads the same two dead tables, so the fallback
throws uncaught and the route 500s. Do not read the `try` as protection.

### Breaks quietly — returns empty

| Route | Method | Effect |
|---|---|---|
| `/api/dashboard` | `fetchIndicatorAverageTrends` (`:409`) | `catch → {}`; the trend rail renders no lines |
| `/api/dashboard` | `fetchChannels` (`:429`) | already empty — `live_tv_channel` does not exist |

### Rewrites, concretely

**`fetchLatestIndicatorValues`** — the join by indicator *name* disappears.
`indicator_series` is keyed by code, and label/unit now live in
`backend/util/constants.py::INDICATOR_REGISTRY`. Either hard-code the nine
code→label pairs in the frontend or expose them from the backend; there is no
longer a table to join.

```sql
-- was: indicator JOIN yearly_value LEFT JOIN recent_indicator, by name
-- now: one DISTINCT ON over indicator_series, freshest period per code
SELECT DISTINCT ON (country_iso2, indicator_code)
       country_iso2, indicator_code, period, freq, value, source, as_of
  FROM indicator_series
 WHERE indicator_code = ANY($1::text[])
 ORDER BY country_iso2, indicator_code, period DESC, as_of DESC;
```
Shape change: the old query returned one row per (country, indicator) with
`annual_value` **and** `recent_value` side by side, and the client picked. The
new one returns the winner already resolved, so `useRecent` in the mapping loop
goes away and `year` comes from `period`.

**`fetchLatestArticlesForLatestSnapshots`** — no longer a join.

```sql
SELECT s.country_iso2, c.name, s.as_of, s.top_articles
  FROM risk_snapshot s JOIN country c ON c.iso2 = s.country_iso2
 WHERE (s.country_iso2, s.as_of) IN (
   SELECT country_iso2, max(as_of) FROM risk_snapshot GROUP BY 1)
   AND s.top_articles IS NOT NULL;
```
Shape change: three rows per country become one row with a three-element JSONB
array. `rank` is the array index plus one; the `rank BETWEEN 1 AND 3` CHECK is
gone, so `article_ranking.ensure_top_three` is now the only guard —
`testing/test_news_fetching.py::TestEnsureTopThree` covers it.

**`fetchIndicatorAverageTrends`** — same table swap as the indicators route,
grouped by `period` instead of `yr`, filtered to `freq = 'A'`.

**`fetchChannels`** — delete it, or create the table. It has never worked.

---

## Migration

Run once, from `python backend/main.py migrate`, not as hand-run SQL.

**Order**, chosen so nothing reads a table that has already moved:
1. `article` — rename, instant, no copy
2. `llm_artifact` — create, copy the two caches, verify counts, drop
3. `indicator_series` — add the four sources, verify, drop `yearly_value`,
   `recent_indicator`, `indicator`
4. `risk_snapshot` — add JSONB columns, fold, verify, drop the two tables
5. `snapshot_diagnostic` — create, copy, verify, drop `probe_result`
6. `run_ledger` — create, copy three sources, verify, drop
7. `market_price` — add reference columns, fold, drop `price_reference`

**Rules**
- Every step is idempotent, so a half-finished run resumes.
- Nothing is dropped until its target has been counted and matches.
- The count of every source table is recorded before and asserted after.
- `--dry-run` prints the plan and every count without writing.

**Verification**
- Row counts before and after, per table, printed as a table.
- The 16-point behavioural fingerprint used to verify the folder move re-run
  against the new schema.
- **Byte-for-byte rebuild is not available.** No row currently in
  `risk_snapshot` can be rebuilt in this tree: the one masked row is stamped
  gazetteer `g3` against a `g5` tree with no sweep recorded, so all 20 of its
  digests miss the cache. `main.py rebuild` now refuses rather than spending.
  The pilot writes the first verifiable rows, so this check moves to after the
  pilot.
