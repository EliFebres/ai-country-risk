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

`main.py` schedules three jobs and records each success in the `run_ledger` table, so the
schedule survives a restart or redeploy instead of living in memory:

| Job | Cadence | What it does |
| -------- | ----------------- | ------------ |
| `prices` | every tick (30 m) | Live FMP quotes for whichever markets are open — a no-op outside session hours |
| `etl` | first tick of a new ISO week | Roster, econ calendar, IMF indicators, ledger sources, all 48 risk snapshots, global alerts |
| `panels` | every 30 days | `backfill_missing_panels(force=True)` — rebuilds every `wb_panel_wide` partition so World Bank revisions and new years land |

A job that has never run is always due, so a fresh database bootstraps itself on first
boot. A job is stamped **only when it succeeds**, so a failure retries on the next tick
rather than waiting out its whole interval. To force one, delete its `run_ledger` row.

*Running the full ETL across the 48-country roster (`constants.COUNTRY_ROSTER`) can take several minutes due to polite pacing of feed resolution and per-article fetches. It runs on the same thread as the prices tick, so prices do not refresh while it works.*

---

## Key modules

* `backend/main.py` — the scheduler loop and the job cadences; the weekly job orchestrates data payload → news → LLM scoring → DB upsert.
* `backend/util/prices.py` — one prices poll cycle (`PricesDaemon.tick`), called by `main.py`.
* `backend/news_fetching/simple_scraper.py` — single-request extractor for summary, full text, and thumbnail.
* `backend/news_fetching/advanced_scraper.py` — Crawlbase-powered metadata, used only for **Top-3** articles still missing an image.
* `backend/news_fetching/url_resolver.py` — resolves `news.google.com` wrappers to publisher URLs.
* `backend/data_fetching/country_data_fetch.py` — World Bank panel ingestion.
* `backend/data_fetching/imf_macro_fetch.py` — IMF SDMX 2.1 fetch of the freshest monthly/quarterly indicators (e.g. inflation) → `indicator_series`.
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
snapshot — use `backend/util/tools/live_country_check.py` when you want the write.

Two cells exist purely to show the division of labor between the models: one
prints a single article end to end through the mini digest model (text in, JSON
out), the other prints the exact `ARTICLE_DIGESTS_JSON` and `FULL_TEXT` blocks
the scoring model receives.

It writes no snapshot — the last step builds the payload
`data_push.upsert_snapshot` would take and prints it instead. The one thing it
does write is the stage-1 digest cache (`llm_artifact`), which nothing else
reads. Use `backend/util/tools/live_country_check.py` when you want the snapshot write,
verification, and cleanup. It hits the network, and costs one cheap digest call
per uncached article plus one scoring call per run.

```bash
pip install ipykernel
```

---

## Database schema

Ten tables, defined in exactly one place — `backend/data_upsert/schema.py` — and
created idempotently. Every statement is `IF NOT EXISTS`, so `create_all` on a
live database is a no-op and a half-finished bootstrap resumes rather than
restarts. There is no migration tool.

Read the DDL there; it carries a comment per table explaining what the column is
for. The overview, and what absorbed what in the twenty-to-ten rebuild, is in
[`../docs/pipeline.md`](../docs/pipeline.md).

| Table | Holds |
|---|---|
| `country` | ISO-2, name, map coordinates, and the structural facts masking cannot replace |
| `article` | Every article from every source; `source_system` separates google-news / guardian / gdelt / nyt |
| `llm_artifact` | Content-addressed model output — digests and mask rewrites, keyed on a hash of the text |
| `indicator_series` | Every macro observation at any frequency, one key, one vintage rule |
| `risk_snapshot` | The product: score, summary, ledgers, flags, Top-3, lint, input manifest |
| `snapshot_diagnostic` | Probes and diagnostic arms — everything measuring the instrument, not the country |
| `run_ledger` | One row per unit of work: scheduler jobs, harvest windows, scored anchors |
| `market_price` | Live prices plus their quarter/year-start reference closes |
| `news_alert` | The globally ranked alerts feed, replaced whole each run |
| `economic_calendar_event` | Upcoming events with an AI importance score |

`python backend/main.py bootstrap --check` reports what is actually present,
along with the server version, collation and installed extensions.

---

## IMF & economic-calendar refresh (inside the weekly `etl` job)

Unlike the prices tick, which runs every 30 minutes, these two refreshes run **once a
week as phases of the `etl` job**:

* **IMF higher-frequency macro.** `data_fetching/imf_macro_fetch.py` pulls the
  freshest sub-annual prints (e.g. monthly/quarterly inflation) from the IMF SDMX 2.1
  API and upserts them into `indicator_series`, alongside every other observation.
  Resolution is freshest-period-wins over that one store, so a country in a
  fast-moving inflation regime is scored on a current figure rather than a
  year-old annual average. The tracked set lives in `constants.IMF_RECENT_INDICATORS`.
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
memory and rehydrates it from `market_price`'s reference columns in `load_state`, so a restart never pays
for a same-day refetch.

---

## License

MIT — see `LICENSE` at repo root.