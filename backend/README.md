# AI Country Risk Dashboard — Backend

## Overview

This directory contains the **data-engineering and inference pipeline** that powers the Country-Risk dashboard. It performs five core jobs:

1. **Data ingestion** — downloads World Bank macro-economic indicators as tidy, per-country panel datasets, and refreshes the freshest sub-annual prints (e.g. monthly/quarterly inflation) from the **IMF** so the dashboard isn't stuck on stale annual values.
2. **Headline collection** — gathers recent articles via Google News RSS and resolves publisher URLs.
3. **Economic calendar** — pulls the upcoming ~14-day economic calendar from **FMP** and AI-ranks the events by investor importance.
4. **Risk scoring** — calls an LLM (via LangChain) to transform macro data + recent headlines into a single 0-1 risk score and an explanatory bullet summary, and to rank a global "AI Alerts" feed from every country's Top-3 articles.
5. **Persistence** — upserts the scores and all underlying data (indicators, articles, alerts, calendar) into a Neon-hosted PostgreSQL database for the frontend to consume.

### How headline scraping works (fast first, then targeted enrichment)

- All links are first processed with the **simple scraper** (`backend/news_fetching/simple_scraper.py`) which:
  - fetches each article **once**,
  - extracts a clean **summary**, **full text** (truncated for storage), and a **thumbnail** (OG/Twitter/JSON-LD with fallbacks).
- The LLM ranks articles by impact.
- **Only the Top-3** are optionally enriched with the **advanced scraper** (`backend/news_fetching/advanced_scraper.py`), and only when an article is still missing an image after the simple scraper and a Crawlbase token is available. This uses Crawlbase (JS rendering) to recover metadata while respecting `robots.txt`.

---

## Requirements

- **Python 3.10+** (tested on 3.11)
- PostgreSQL 15+ (Neon Serverless used in prod)
- `pip install -r backend/requirements.txt` (LangChain, pandas, psycopg2-binary, requests, beautifulsoup4, tldextract, python-dotenv, …)

---

## Environment variables (`.env` in `backend/`)

| Variable              | Purpose                                                                 |
| --------------------- | ----------------------------------------------------------------------- |
| `DATABASE_URL`        | Postgres connection string (Neon or local)                              |
| `OPENAI_API_KEY`      | OpenAI key used by `langchain_openai`                                   |
| `FMP_API_KEY`         | Financial Modeling Prep key — economic calendar and the live prices tick |
| `CRAWLBASE_JS_TOKEN`  | *(optional)* Crawlbase JS token for advanced Reuters/Bloomberg enrichment |
| `CRAWLBASE_TOKEN`     | *(optional)* Crawlbase standard token (used if JS token not provided)   |

> If neither Crawlbase token is set, the pipeline still runs; only the Top-3 Reuters/Bloomberg enrichment step is skipped.

---

## Quick start

```bash
# Create venv & install deps
python -m venv venv && source venv/bin/activate
pip install -r backend/requirements.txt

# Add .env in backend/ with DATABASE_URL, OPENAI_API_KEY, and optional Crawlbase token(s)

# Run the scheduler — the only process. Ticks every 30 min, runs what is overdue.
python backend/main.py

# Or a single pass over every due job, then exit (verification)
python backend/main.py --once
```

`main.py` schedules three jobs and records each success in the `job_run` table, so the
schedule survives a restart or redeploy instead of living in memory:

| Job | Cadence | What it does |
| -------- | ----------------- | ------------ |
| `prices` | every tick (30 m) | Live FMP quotes for whichever markets are open — a no-op outside session hours |
| `etl` | first tick of a new ISO week | Roster, econ calendar, IMF indicators, ledger sources, all 48 risk snapshots, global alerts |
| `panels` | every 30 days | `backfill_missing_panels(force=True)` — rebuilds every `wb_panel_wide` partition so World Bank revisions and new years land |

A job that has never run is always due, so a fresh database bootstraps itself on first
boot. A job is stamped **only when it succeeds**, so a failure retries on the next tick
rather than waiting out its whole interval. To force one, delete its `job_run` row.

*Running the full ETL across the 48-country roster (`constants.COUNTRY_ROSTER`) can take several minutes due to polite pacing of feed resolution and per-article fetches. It runs on the same thread as the prices tick, so prices do not refresh while it works.*

---

## Key modules

* `backend/main.py` — the scheduler loop and the job cadences; the weekly job orchestrates data payload → news → LLM scoring → DB upsert.
* `backend/util/prices.py` — one prices poll cycle (`PricesDaemon.tick`), called by `main.py`.
* `backend/news_fetching/simple_scraper.py` — single-request extractor for summary, full text, and thumbnail.
* `backend/news_fetching/advanced_scraper.py` — Crawlbase-powered metadata, used only for **Top-3** articles still missing an image.
* `backend/news_fetching/url_resolver.py` — resolves `news.google.com` wrappers to publisher URLs.
* `backend/data_fetching/country_data_fetch.py` — World Bank panel ingestion.
* `backend/data_fetching/imf_macro_fetch.py` — IMF SDMX 2.1 fetch of the freshest monthly/quarterly indicators (e.g. inflation) → `recent_indicator`.
* `backend/data_fetching/fmp_calendar_fetch.py` — FMP ~14-day economic-calendar pull.
* `backend/llm/langchain_llm.py` — LLM call for risk scoring.
* `backend/llm/alerts_ranker.py` — LLM global ranking of pooled Top-3 articles into the `news_alert` feed.
* `backend/llm/calendar_ranker.py` — LLM ranking of calendar events by investor importance.
* `backend/data_upsert/data_push.py` — transactional upserts for every table below.
* `backend/util/http.py` — shared retry policy, User-Agent strings, and FMP GET wrapper.
* `backend/llm/client.py` — the scoring model name and its deterministic settings, in one place.
* `backend/util/dates.py` — the two datetime formats shared across modules.

---

## Tests

Characterization tests covering the pure logic (market-hours gating, article
relevance scoring, Top-3 selection, external-payload parsing, the sanctions
gate, and the price math). They touch no network and no database.

```bash
pip install pytest
python backend/test.py
```

---

## Notebook

`backend/notebooks/country_rating_walkthrough.ipynb` rates one country from
scratch and shows its work — what evidence went in, how stale it is, what the
news added, what the model decided and why. It runs the same code paths
`pipeline._process_country` runs, in five steps: the evidence, the news, the
score, the guardrails, and the Top-3 that reach the dashboard. Charts are inline
SVG so the notebook adds no plotting dependency.

Pick the country by editing `ISO2` in the *Pick a country* cell, then Run All.
It makes real network calls and spends OpenAI credits, and it writes no
snapshot — use `tests/live_country_check.py` when you want the write.

Two cells exist purely to show the division of labor between the models: one
prints a single article end to end through the mini digest model (text in, JSON
out), the other prints the exact `ARTICLE_DIGESTS_JSON` and `FULL_TEXT` blocks
the scoring model receives.

It writes no snapshot — the last step builds the payload
`data_push.upsert_snapshot` would take and prints it instead. The one thing it
does write is the stage-1 digest cache (`article_digest`), which nothing else
reads. Use `tests/live_country_check.py` when you want the snapshot write,
verification, and cleanup. It hits the network, and costs one cheap digest call
per uncached article plus one scoring call per run.

```bash
pip install ipykernel
```

---

## Database schema (simplified)

```sql
-- Seeded from constants.COUNTRY_ROSTER at the start of every run
-- (data_push.upsert_countries). The front-end reads the country list, display
-- names AND map marker positions from here, so it holds no country data of its
-- own: adding a country to the roster is the only edit needed.
CREATE TABLE country (
    iso2  CHAR(2) PRIMARY KEY,
    name  TEXT      NOT NULL,  -- canonical English name
    lat   DOUBLE PRECISION,    -- map marker latitude
    lng   DOUBLE PRECISION     -- map marker longitude
);

CREATE TABLE indicator (
    id   SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    unit TEXT         NOT NULL
);

CREATE TABLE yearly_value (
    country_iso2 CHAR(2) REFERENCES country(iso2),
    indicator_id INT     REFERENCES indicator(id),
    yr           INT,
    value        DOUBLE PRECISION,
    PRIMARY KEY (country_iso2, indicator_id, yr)
);

-- Freshest sub-annual (monthly/quarterly) observation per (country, indicator),
-- sourced from the IMF. The front-end prefers these over the annual yearly_value
-- and falls back to the annual one when a country has no fresh row. One row per
-- (country, indicator), upserted in place.
CREATE TABLE recent_indicator (
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

-- Only the first four columns need provisioning: `data_push.upsert_snapshot`
-- issues `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for the rest once per
-- process, so an existing database comes up to date on the next run. `score` is
-- the gated 12-month score (0-1) the front-end reads; everything below it is
-- additive research detail, all nullable.
CREATE TABLE risk_snapshot (
    country_iso2   CHAR(2) REFERENCES country(iso2),
    as_of          DATE,
    score          DOUBLE PRECISION,
    bullet_summary TEXT,

    -- Added by the perception/policy split: both horizons, raw and gated.
    score_3m          DOUBLE PRECISION,
    raw_score_12m     DOUBLE PRECISION,  -- the model's own score, pre-policy
    raw_score_3m      DOUBLE PRECISION,
    subscores         JSONB,             -- gated sub-factor scores
    raw_subscores     JSONB,
    subscore_evidence JSONB,
    condition_flags   JSONB,             -- war / conflict / emergency / stress
    article_scores    JSONB,             -- EVERY article's impact + topic_group
    applied_rules     JSONB,             -- which floors/caps/gates fired
    evidence_coverage DOUBLE PRECISION,
    legal_gate        JSONB,             -- the sanctions rule that forced a 1.0

    -- Provenance: what produced this row, and what it saw.
    model_id       TEXT,
    prompt_version TEXT,
    policy_version TEXT,
    input_manifest JSONB,                -- per-article hashes + macro vintage

    PRIMARY KEY (country_iso2, as_of)
);

CREATE TABLE risk_snapshot_article (
    id            BIGSERIAL PRIMARY KEY,
    country_iso2  CHAR(2)      NOT NULL REFERENCES country(iso2),
    as_of         DATE         NOT NULL,
    rank          SMALLINT     NOT NULL CHECK (rank BETWEEN 1 AND 3),

    url           TEXT         NOT NULL,
    title         TEXT,
    source        TEXT,
    published_at  TIMESTAMPTZ,
    impact        DOUBLE PRECISION,
    summary       TEXT,
    image_url     TEXT,

    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),

    UNIQUE (country_iso2, as_of, rank),
    FOREIGN KEY (country_iso2, as_of)
        REFERENCES risk_snapshot (country_iso2, as_of)
        ON DELETE CASCADE
);

CREATE INDEX idx_risk_snapshot_article_country_date
    ON risk_snapshot_article (country_iso2, as_of);

-- Global "AI Alerts": each run pools every country's Top-3 articles, the LLM
-- ranks them by importance to the global economy, tags one topic + severity,
-- and only the top-N (ALERTS_TOP_N) are stored. Replace-per-day semantics.
CREATE TABLE news_alert (
    id           BIGSERIAL PRIMARY KEY,
    as_of        DATE         NOT NULL,
    global_rank  SMALLINT     NOT NULL,
    country_iso2 CHAR(2)      NOT NULL,
    country_name TEXT,

    url          TEXT         NOT NULL,
    title        TEXT,
    source       TEXT,
    published_at TIMESTAMPTZ,
    summary      TEXT,
    image_url    TEXT,

    topic        TEXT         NOT NULL,  -- Conflict|Sanctions|Macro|Politics|Trade|Energy|Security|Markets
    severity     TEXT         NOT NULL CHECK (severity IN ('Critical','Caution','Watch')),
    importance   DOUBLE PRECISION,       -- global-economy importance (0-1)
    rationale    TEXT,

    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),

    UNIQUE (as_of, global_rank)
);

CREATE INDEX idx_news_alert_as_of ON news_alert (as_of);

-- Upcoming economic-calendar events (next ~14 days) from FMP, for the bottom-bar
-- "Econ Calendar" pane. Each ETL run upserts events and the LLM tags an
-- investor-importance score (US-tilted) so the pane can sort by what matters.
CREATE TABLE economic_calendar_event (
    id           BIGSERIAL PRIMARY KEY,
    event_time   TIMESTAMPTZ NOT NULL,
    country_code TEXT NOT NULL,
    country_name TEXT NOT NULL,
    event        TEXT NOT NULL,
    importance   TEXT NOT NULL CHECK (importance IN ('h','m','l')),  -- FMP impact
    currency     TEXT,
    previous     DOUBLE PRECISION,
    estimate     DOUBLE PRECISION,
    actual       DOUBLE PRECISION,

    ai_importance DOUBLE PRECISION,    -- LLM investor-importance score (0-1)
    ai_rationale  TEXT,
    ai_scored_at  TIMESTAMPTZ,

    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_time, country_code, event)
);

-- Live "Prices" pane. Maintained by main.py's prices tick (util/prices.py): one
-- row per tracked asset, upserted in place every 30 minutes. Stocks/crypto/
-- commodities come from FMP batch-quote; US Treasury yields from FMP treasury-
-- rates. is_yield rows carry POINT changes (shown as %); others carry % moves.
CREATE TABLE market_price (
    symbol        TEXT PRIMARY KEY,      -- stable internal id, e.g. 'SP500','US10Y'
    label         TEXT    NOT NULL,      -- display label (MSCI rows relabeled to ETF)
    asset_class   TEXT    NOT NULL CHECK (asset_class IN ('stocks','bonds','crypto','commodities')),
    source_symbol TEXT,                  -- FMP quote symbol / treasury-rates tenor
    is_yield      BOOLEAN NOT NULL DEFAULT FALSE,

    px            DOUBLE PRECISION,      -- last price / yield
    chg           DOUBLE PRECISION,      -- 1D  (% for prices, points for yields)
    q             DOUBLE PRECISION,      -- 1Q
    ytd           DOUBLE PRECISION,      -- YTD
    sort_order    INTEGER NOT NULL DEFAULT 0,

    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Quarter-/year-start reference closes for the 1Q/YTD calc. Refreshed at most
-- once per day so each live tick computes q/ytd in-process with zero extra calls.
CREATE TABLE price_reference (
    symbol                 TEXT PRIMARY KEY,
    ref_q                  DOUBLE PRECISION,
    ref_q_date             DATE,
    ref_ytd                DOUBLE PRECISION,
    ref_ytd_date           DATE,
    reference_refreshed_on DATE,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Stage-1 article digests (ai/digest_engine.py): a cheap model's factual
-- extraction of every fetched article, cached per (country, day, url) with a
-- hash of the digested text so a same-day re-run makes ~zero stage-1 calls.
-- Created by code (data_push.upsert_article_digests); not read by the frontend.
CREATE TABLE article_digest (
    country_iso2    TEXT        NOT NULL,
    as_of           DATE        NOT NULL,
    url             TEXT        NOT NULL,
    published_at    TIMESTAMPTZ,
    content_sha256  TEXT,                  -- sha256 of the digested article text
    digest          JSONB       NOT NULL,  -- DIGEST_SCHEMA output
    stage1_severity DOUBLE PRECISION,      -- 0-100; picks the full-text articles
    model_id        TEXT,                  -- pinned digest model release
    PRIMARY KEY (country_iso2, as_of, url)
);

-- Scheduler bookkeeping (data_push.read_job_runs / mark_job_run). One row per
-- job in main.py's JOBS table, stamped only on success, so main.py can tell on
-- every tick what is overdue and catch up after downtime. Not read by the
-- frontend; `DELETE FROM job_run WHERE job = '...'` forces a re-run.
CREATE TABLE job_run (
    job      TEXT PRIMARY KEY,      -- 'etl' | 'panels'
    last_run TIMESTAMPTZ NOT NULL
);
```

---

## IMF & economic-calendar refresh (inside the weekly `etl` job)

Unlike the prices tick, which runs every 30 minutes, these two refreshes run **once a
week as phases of the `etl` job**:

* **IMF higher-frequency macro.** `data_fetching/imf_macro_fetch.py` pulls the
  freshest sub-annual prints (e.g. monthly/quarterly inflation) from the IMF SDMX 2.1
  API and upserts them into `recent_indicator`. The front-end prefers these over the
  annual World Bank `yearly_value`, so a country in a fast-moving inflation regime shows
  a current figure instead of a year-old one. The tracked set lives in `constants.IMF_RECENT_INDICATORS`.
* **Economic calendar.** `data_fetching/fmp_calendar_fetch.py` pulls the upcoming
  ~14-day calendar from FMP; `llm/calendar_ranker.py` then scores each event's
  investor importance (`ai_importance` / `ai_rationale`) before the rows are upserted into
  `economic_calendar_event`.

## Curated inputs (`backend/data/curated.csv`)

Some of what the three ledgers need has no free, stable, no-auth API. Those values are
typed by hand into one CSV, which `data_fetching/curated_loader.py` reads during
step 0d of the ETL and upserts into `indicator_series`.

```csv
country_iso2,indicator_code,period,value,as_of
PT,RSF.PRESS.SCORE,2025,75.6,2026-07-28
SA,RESERVES.USD,2026-05,410000000000,2026-07-15
```

| Column | Meaning |
|---|---|
| `country_iso2` | ISO-3166-1 alpha-2. Must be in `constants.COUNTRY_ROSTER`; off-roster rows are skipped with a logged count, not an error. |
| `indicator_code` | A key in `constants.INDICATOR_REGISTRY`, which supplies `freq` and `source`. An unknown code raises. |
| `period` | `YYYY` (annual), `YYYYQn` (quarterly) or `YYYY-MM` (monthly). Must match the indicator's registered frequency. |
| `value` | A number. **Blank means "reported as unavailable"** and is stored as NULL — a different fact from the row being absent. |
| `as_of` | `YYYY-MM-DD`: when this value became known to us, not the period it describes. Per row, because these sources refresh on different cadences. |

**The file ships with a header row and nothing else.** That is deliberate: a template with
plausible-looking sample rows loads silently, reaches the model as evidence, and produces
a confident score built on invented numbers. An empty file loads to nothing and the
payload honestly says the indicator is absent.

The loader is loud about malformed rows and silent about an absent file — a row that is
present is a row someone meant to be used, so a typo must not degrade into missing
evidence that looks identical to "never filled". It raises behind a `try` in the
pipeline, so a bad row is surfaced in the log without costing the run its scores.

### Sources, ranked by impact

Fill them in this order. The first two unlock the most.

| # | `indicator_code` | What it unlocks | Where to get it |
|---|---|---|---|
| 1 | `RESERVES.USD` | With `constants.FX_REGIMES`, turns on `suppressed_vol_flag` — the only input telling the model that measured calm may be manufactured. Monthly, total reserve assets in USD. | IMF IRFCL, table I.A line 1. Manual because IRFCL publishes ~800 series per country and the reserve-assets line can't be identified from the codes; picking the wrong one would silently produce a plausible but wrong trend. |
| 2 | `STAT.TAX.TOP.RATE` | `rome_gap` — the gap between what the statute claims and what the state collects. Annual, top combined statutory corporate rate in percent. | OECD Corporate Tax Statistics, table II.1. After filling, compute `constants.ROME_REFERENCE_RATIO` once. |
| 3 | `RSF.PRESS.SCORE` | Half of `instrument_quality`'s required core pair (`IQ.SPI.OVRL` arrives automatically). Annual score 0–100, higher = freer. | RSF World Press Freedom Index — the score column, not the rank. Published each May. |
| 4 | `BIS.POLICY.RATE` | `real_policy_rate`. Only needed as a fallback: `bis_bulk_fetch.py` covers 41 of the 48 roster countries automatically. Use this for the seven euro-area members with no national series. | BIS `WS_CBPOL`, monthly, percent. |
| 5 | `INFORMAL.PCT.GDP` | Context for `rome_gap`: a large collection gap means something different when a third of the economy is informal. Annual, percent of GDP. | IMF WP/18/17 (Medina & Schneider) or the World Bank Informal Economy Database. Irregular. |
| 6 | `WUI.INDEX` | Order-uncertainty evidence that is measured rather than inferred from articles. Quarterly. | worlduncertaintyindex.com, per-country panel. |
| 7 | `UNWPP.DPND.OL.PROJ` | The projection half of `dependency_trajectory` (the current level arrives from the World Bank). `period` is the year being projected *to*. | UN World Population Prospects, medium variant. Revised every two years. |
| 8 | `OECD.PISA.MEAN` | The edge ledger's learning-outcome line, and the one the prompt reads before education spending. `period` is the round year (`2022`), `value` the mean of the three domain means. | OECD Data Explorer → Education → PISA. Triennial. 44 of 48 roster countries sat 2022; China, Egypt and Kuwait did not — leave them out rather than substituting another assessment. |
| 9 | `OBS.SCORE` | Optional supplement to `instrument_quality`; sharpens it, cannot substitute for the core pair. Biennial, 0–100. | International Budget Partnership, Open Budget Survey. |
| 10 | `UN.EGDI` | Optional supplement to `instrument_quality`. Biennial. **Rescale to 0–100** (the UN publishes 0–1) so it shares a scale with the other components. | UN E-Government Survey. |
| 11 | `OECD.TAX.WEDGE` | Supplementary friction evidence: the wedge on labour specifically. Annual, percent of labour cost, single average worker. OECD members only — absent for most of the EM roster by construction. | OECD Taxing Wages, table 0.1. |

Three curated inputs are **not** series and live in `util/constants.py` instead:
`FX_REGIMES`, `ELECTIONS` and `ROME_REFERENCE_RATIO`. Each carries its source and its
update cadence in a comment there.

### Metrics with no source, and why

Two metrics have no free source worth the plumbing, so they degrade honestly rather than
getting a row nobody will fill:

* `wage_productivity_gap` needs real wage growth and output-per-worker growth. Returns
  `None`; supplementary by design.
* `precommitted_share` needs social protection as a percent of revenue for its second
  half. It returns the interest-only figure marked `partial: true`, which is the behaviour
  the function was written for. It never imputes the missing half.

If you later find a usable source for either, add one `INDICATOR_REGISTRY` entry and the
rows go in `curated.csv` with no loader change.

## Live prices feed (`util/prices.py`)

`PricesDaemon.tick` keeps the bottom-bar "Prices" pane fresh. `main.py` calls it once
per scheduler tick (`PRICES_POLL_SECONDS`, default 1800) and it upserts the latest
snapshot into `market_price`.

* **Sources.** Equity indices, the MSCI ETF proxies (ACWI/ACWX/EEM, relabeled), crypto,
  and commodities come from **FMP batch-quote** in one call per tick. US Treasury yields
  (2Y/10Y/30Y) come from **FMP treasury-rates** (FMP has no non-US yield feed, so the
  Bonds pane tracks US tenors only). The tracked universe + symbol map lives in
  `constants.PRICE_ASSETS`.
* **Cost control.** FMP quote classes are fetched only while their market is open
  (`util/market_hours.py`: crypto 24/7, US equities on the NYSE session, commodities on
  the Globex window). The yields and the 1Q/YTD reference closes refresh at most once per
  ET day.

Reuses `FMP_API_KEY` + `DATABASE_URL` — no other secret needed. `python backend/main.py
--once` runs a single tick for verification. The daemon holds its per-day state in
memory and rehydrates it from `price_reference` in `load_state`, so a restart never pays
for a same-day refetch.

---

## License

MIT — see `LICENSE` at repo root.