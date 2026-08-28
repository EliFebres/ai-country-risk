# AI Country Risk Dashboard

🔗 **[View the live dashboard](https://www.elifebres.com/work/ai-country-risk-dashboard/live)**

![AI Risk Dashboard](/assets/ai-dashboard-snapshot.png)

## Overview

The **AI Country Risk Dashboard** is an open‑source web application that quantifies and visualizes geopolitical investment risk using artificial intelligence. It combines macro‑economic indicators from the World Bank with recent news and produces a **0–1 risk score** and an explanatory bullet summary for each country. Scores and summaries are stored in a PostgreSQL database and rendered on an interactive world map.

### Features

* **Data ingestion** – Downloads World‑Bank macro‑economic indicators such as inflation, unemployment and political stability, alongside IMF, BIS and per‑edition IMF WEO series, and stores every observation at every frequency in one table (`indicator_series`) under one freshest‑wins rule. The coverage universe is the MSCI Developed and Emerging Markets indices plus Russia — 48 countries, listed in `backend/util/constants.py` (`COUNTRY_ROSTER`), which is the single source of truth. Sub‑annual prints (e.g. monthly/quarterly inflation) are refreshed from the **IMF** (SDMX 2.1) so fast‑moving economies aren't stuck on a year‑old annual figure, and the V‑Dem political‑corruption index is pulled from **Our World in Data (OWID)**.
* **Risk scoring** – Two model calls per country. A cheap model digests every fetched article into strict JSON; a frontier model (OpenAI `gpt-4o-2024-08-06` via LangChain) reads every digest plus the three highest‑severity bodies in full, and returns four ledger scores, two horizon scores and a bullet summary. It scores with the country's identity **masked** — names, cities, people, parties, currencies and institutions replaced by the roles they play, every number left exactly as written. Nothing downstream edits the number it returns: a YAML‑driven sanctions lookup adds a `RESTRICTED` **badge** beside the score rather than forcing it, and contradictions are recorded as advisory lint rather than corrected.
* **Live market & event feeds** – A standalone prices daemon polls **Financial Modeling Prep (FMP)** for live equity, bond‑yield, crypto and commodity quotes, a global **AI Alerts** feed re‑ranks every country's top headlines by importance to the world economy, and an AI‑ranked **economic calendar** surfaces the next ~14 days of major releases.
* **Persistence** – Persists macro series, risk snapshots, alerts, calendar events and live prices into a Neon‑hosted PostgreSQL database using a transactional upsert strategy.
* **Interactive frontend** – A Next.js (App Router) dashboard renders an interactive world map with clickable risk markers, a slide‑in country sidebar, a global "World Risk Index" rail, and a bottom ticker bar (Prices, World Markets, AI Alerts, Econ Calendar and DB‑backed Live TV streams). It also ships a hands‑off **"World Tour" idle auto‑tour** that cycles through countries after inactivity and a fullscreen‑map toggle. All data is served live from Postgres through cached API routes — there is no static JSON file and no weekly refresh job.
* **Extensible architecture** – The backend is pure Python and uses modular utilities for metric fetching, news scraping and LLM calls. The frontend uses modern React/Next.js and is ready to deploy to Vercel or your own server.

## Tech Stack

| Layer | Technologies |
|-------|--------------|
| **Frontend** | Next.js 15.5.6 (App Router), React 19.1.0, Tailwind CSS 4, MapLibre GL 5.6.2, Recharts 3.3.0, `pg` 8.16.3, TypeScript 5 |
| **Backend** | Python 3.10+, LangChain + OpenAI `gpt-4o-2024-08-06` |
| **Database** | Neon‑hosted PostgreSQL (schema created idempotently from the ETL) |
| **Data sources** | World Bank, IMF (SDMX 2.1), Our World in Data (V‑Dem), Financial Modeling Prep (FMP), Google News RSS, optional Crawlbase |

## How the score is made

A single structured-output call per country, under a versioned prompt
(`backend/llm/constants.py`, currently `v4.0-masked-production`). There is no
trained model and no weighted formula — the deterministic arithmetic in the
pipeline is *input* to the model's judgement, never applied on top of it.

The prompt asks for four ledger scores rather than a list of risk factors:

| Ledger | The question it asks | A high number means |
|---|---|---|
| **Friction** | what the state extracts, times how much of it fails to convert into capability | a worse wedge |
| **Order-uncertainty** | are the load-bearing rules — contracts, currency, statistics, succession — legible? | less underwritable |
| **Information capacity** | can the country's own instruments be trusted to measure it? | **weaker** instruments |
| **Edge vitality** | is the system still learning — firms forming, failing, inventing? | more vitality, and this one may never *raise* a risk score |

Two rules run through the whole thing:

* **Nothing downstream edits the model's score.** There were floors, a cap and a
  sanctions gate that forced a 1.0. All of it is deleted. A sanctioned country
  keeps the score its evidence earned and gains a `RESTRICTED` badge beside it;
  contradictions between the model's flags and its numbers are recorded by
  `util/lint.py` and corrected by nobody.
* **The country is not named.** Scoring runs with the identity masked — names,
  cities, people, parties, currencies and institutions replaced by the roles they
  play, every number left exactly as written. That is what makes a 2016 backfill
  and tomorrow's live run the same instrument.

The full walkthrough is in [`docs/pipeline.md`](docs/pipeline.md); the masking
layers, the exclusions and the measurement are in
[`docs/historical-ratings.md`](docs/historical-ratings.md).

## Getting Started

### Prerequisites

To run the dashboard you will need the following components:

| Component | Version/Requirement |
|-------------|---------------------------------------|
| Python | 3.10 or newer |
| Node & npm | Node 18+ |
| PostgreSQL | 15+ (Neon Serverless recommended) |
| OpenAI key | For LLM risk scoring |

### Clone the repository

```bash
git clone https://github.com/EliFebres/AI-Country-Risk-Dashboard.git
cd AI-Country-Risk-Dashboard
```

### Configure environment variables

Create the following `.env` files before running:

| Location | File | Keys & purpose |
|------------|--------------|---------------------------------------------------------------------|
| `backend` | `.env` | `DATABASE_URL` – PostgreSQL connection string; `OPENAI_API_KEY` – OpenAI API key; `FMP_API_KEY` – Financial Modeling Prep key (economic calendar + prices); optional `CRAWLBASE_JS_TOKEN` / `CRAWLBASE_TOKEN` for Reuters/Bloomberg enrichment |
| `frontend` | `.env` | `DATABASE_URL` – Postgres URL with `sslmode=require` |

The `backend/.env` file is read by the ETL pipeline and the database upsert routines. The `frontend/.env` is read by the Next.js server‑side API routes that serve the dashboard data.

### Backend setup

1. **Activate a virtual environment and install dependencies:**

 ```bash
 python -m venv venv && source venv/bin/activate
 pip install -r backend/requirements.txt
 ```

2. **Seed macro data:** No separate step is needed — the first run of the ETL automatically downloads World Bank panels for every country that does not already have one, and skips the rest.

3. **Run the backend:** `main.py` is the only process. It runs forever, ticking every 30 minutes and running whatever is overdue.

 ```bash
 python backend/main.py          # run forever
 python backend/main.py --once   # one pass over every due job, then exit
 ```

 | Job | Cadence | What it does |
 |----------|-------------------|--------------|
 | `prices` | every tick (30 m) | Live FMP quotes for whichever markets are open; a no‑op outside session hours |
 | `etl` | first tick of a new ISO week | Roster, econ calendar, IMF indicators, ledger sources, then a risk score for all 48 countries and the global alerts |
 | `panels` | every 30 days | Rebuilds every `wb_panel_wide` partition so World Bank revisions land |

 "When did this last run" lives in the `job_run` table, not in memory, so a restart or redeploy picks up where it left off — a box that was down for ten days comes back and immediately catches up on the week it missed. A job is stamped only when it succeeds, so a failure retries next tick. The weekly run takes several minutes because the news fetcher throttles requests to stay under Google's anonymous quota; prices do not refresh while it runs.

### Frontend setup

1. **Install dependencies:**
 ```bash
cd frontend
npm install
 ```

2. **Run the development server:**
 ```bash
npm run dev
 ```

The app reads live from Postgres through cached API routes — the map loads risk markers from `/api/risk`, and clicking a country opens the sidebar, which loads its details from `/api/dashboard`. There is no static seed file to populate. See `frontend/README.md` for the full route, caching and component breakdown.

### Deployment

There is one process to deploy. On Railway (or any host), point the build at `pip install -r backend/requirements.txt` and the start command at `python backend/main.py`, then set `DATABASE_URL`, `OPENAI_API_KEY` and `FMP_API_KEY`. Nothing else needs a scheduler — `main.py` is the scheduler, and `SIGTERM` shuts it down cleanly between ticks.

### Directory Structure
```bash
AI-Country-Risk-Dashboard/
├── backend/                    # Python ETL, LLM scoring and DB interface
│   ├── main.py                 # The one executable: scheduler loop, plus subcommands
│   ├── test.py                 # The one test executable
│   ├── data_fetching/          # Any non-article data, from any source
│   │   └── vintage/            # Per-edition IMF WEO, publication-lag dating
│   ├── news_fetching/          # Any article, live or historical
│   │   └── adapters/           # Guardian, GDELT, NYT harvesters
│   ├── data_upsert/            # Everything that reads or writes Postgres
│   ├── llm/                    # Prompts, schemas, model clients, masking, digests
│   ├── util/                   # Orchestration, and helpers belonging to no one folder
│   ├── testing/                # One test file per folder, plus the invariants
│   ├── notebooks/              # Jupyter walkthroughs
│   └── README.md               # Detailed backend instructions
├── frontend/                   # Next.js (App Router) dashboard
│   ├── app/
│   │   ├── api/                # Cached DB‑backed API routes
│   │   ├── components/         # Map, sidebar, rail, bottom‑bar panes, charts
│   │   └── lib/                # Server queries, cached fetchers, client caches
│   └── README.md               # Detailed frontend instructions
├── docs/                       # How the pipeline works, end to end
│   ├── pipeline.md             # Country data -> risk score
│   ├── historical-ratings.md   # The History Machine: masking, exclusions, meters
│   └── deferred.md             # Deliberate non-actions, with the reasoning
├── assets/                     # Screenshots / demo media
├── LICENSE                     # MIT license
└── README.md                   # (You are here)
```

### Database Schema
Ten tables, defined in one place — `backend/data_upsert/schema.py` — and created
idempotently. There is no separate migration tool. What each holds, and what
absorbed what in the twenty-to-ten rebuild, is in
[`docs/pipeline.md`](docs/pipeline.md).

| Table | Description |
|-------|-------------|
| `country` | ISO-2 code, name, map coordinates, and the structural facts masking cannot replace |
| `article` | Every article from every source, live and historical |
| `llm_artifact` | Content-addressed model output — digests and mask rewrites |
| `indicator_series` | Every macro observation at any frequency, from every source |
| `risk_snapshot` | The product: 0–1 score, bullet summary, ledgers, flags, Top-3, provenance |
| `snapshot_diagnostic` | Everything measuring the instrument rather than the country |
| `run_ledger` | One row per unit of work — scheduler jobs, harvest windows, scored anchors |
| `market_price` | Live prices plus their quarter/year-start reference closes |
| `news_alert` | The globally AI-ranked alerts feed |
| `economic_calendar_event` | Upcoming economic events with an AI importance score |

### Contributing
Contributions, bug reports and feature requests are welcome! Please open an issue or submit a pull request on GitHub. When adding new data sources or indicators, update the `constants.py` mappings and ensure your changes are reflected in both the backend and the frontend.

### License
This project is licensed under the MIT License. See the `LICENSE` file for details.

### Acknowledgements
The dashboard relies on open data from the World Bank and the IMF for macro‑economic indicators, Our World in Data (V‑Dem) for the political‑corruption index, Financial Modeling Prep (FMP) for live market prices and the economic calendar, Google News for headline scraping, and OpenAI’s models for risk scoring. Thanks to the maintainers of LangChain, Next.js, MapLibre, Recharts and the open‑source community for their tools and libraries.