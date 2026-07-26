# AI Country Risk Dashboard

🔗 **[View the live dashboard](https://www.elifebres.com/work/ai-country-risk-dashboard/live)**

![AI Risk Dashboard](/assets/ai-dashboard-snapshot.png)

## Overview

The **AI Country Risk Dashboard** is an open‑source web application that quantifies and visualizes geopolitical investment risk using artificial intelligence. It combines macro‑economic indicators from the World Bank with recent news and produces a **0–1 risk score** and an explanatory bullet summary for each country. Scores and summaries are stored in a PostgreSQL database and rendered on an interactive world map.

### Features

* **Data ingestion** – Downloads and formats World‑Bank macro‑economic indicators such as inflation, unemployment, political stability and other factors and stores them in per‑country panel datasets. The coverage universe currently includes 57 countries (see `COUNTRY_ROSTER` in `backend/utils/constants.py`). Sub‑annual prints (e.g. monthly/quarterly inflation) are refreshed from the **IMF** (SDMX 2.1) so fast‑moving economies aren't stuck on a year‑old annual figure, and the V‑Dem political‑corruption index is pulled from **Our World in Data (OWID)**.
* **Risk scoring** – Uses a large‑language model (OpenAI `gpt-4o-2024-08-06` via LangChain) to combine macro data and recent headlines into a single 0–1 risk score and a bullet‑point explanation. The AI prompt enforces hard rules around war, political stability and macro floors to ensure consistent scoring, and a YAML‑driven **sanctions / investability gate** pins un‑investable jurisdictions (e.g. Russia, Iran, North Korea, Cuba, occupied Ukrainian oblasts) to maximum risk.
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

## My Ai Prompt
```bash
You are a senior geopolitical risk analyst. Rate investor risk for {country} over the next 12 months using ONLY the evidence provided.

EVIDENCE_JSON
{evidence_json}

ARTICLES_JSON
# exactly these items only
# [{{"id":"a1","source":"...","published_at":"YYYY-MM-DD","title":"...","summary":"..."}}]
{articles_json}

Scoring bands (guidance; use full 0-1 range):
  • 0.05-0.20 = Low   • 0.20-0.40 = Low-Moderate   • 0.40-0.70 = Moderate
  • 0.70-0.90 = High  • 0.90-0.98 = Extreme (active war / nationwide shutdowns)

Sub-factors to score (diagnostic only):
  conflict_war, political_stability, governance_corruption, macroeconomic_volatility, regulatory_uncertainty.

# --- Localization & Materiality ---
Do NOT raise risk due to indirect foreign tensions or rhetoric. Elevate risk ONLY for {country} when evidence shows kinetic activity on its territory, imminent hostilities, or economically binding policy affecting {country}. Indirect disputes, UN votes, or rhetoric without domestic transmission = low impact.

# --- Hard Rules the model must apply (no post-processing will alter your score) ---
• War Reality: If a sustained interstate war or regular long-range strikes hit {country}'s cities/critical infrastructure → set conflict_war ≥ 0.90 AND overall score ≥ 0.90.
• Internal Conflict:
   - Level A (Severe): recurring mass-casualty attacks (≥20 killed) or mass kidnappings in the last 90 days across ≥3 regions → conflict_war ≥ 0.80 AND overall score ≥ 0.70.
   - Level B (Very severe): Level A + repeated attacks on critical infrastructure (pipelines/power grid) or major-city attacks → conflict_war ≥ 0.88 AND overall score ≥ 0.80.
   - Level C (Extreme): Level B + nationwide emergency effects (large displacement, prolonged curfews, export shut-ins) → overall score ≥ 0.90.
• Parliamentary Guardrail: Cabinet resignations, caretaker phases, coalition talks, or scheduled/snap elections remain **moderate** unless there is unconstitutional dissolution, emergency/martial law, week-long widespread violent unrest disrupting essential services, bank runs, capital controls, or sovereign default. Otherwise **political_stability should not exceed 0.45**.
• Macro floors (numeric): If CPI inflation ≥ 25% → macroeconomic_volatility ≥ 0.70 AND overall score ≥ 0.55. If ≥ 40% → ≥ 0.80 AND overall ≥ 0.65. If ≥ 80% → overall ≥ 0.80.

# --- Per-article impact labels (for diagnostics; caller won't re-score) ---
Impact ∈ [0,1]:
  • 0.85-1.00 Severe - kinetic activity in/against {country}, mass kidnappings, binding economic measures, or major infrastructure sabotage.
  • 0.60-0.75 Moderate - credible mobilization/preparations, high-probability sanctions.
  • 0.40-0.55 Mixed/unclear - indirect third-country events with uncertain transmission.
  • 0.05-0.25 Low/benign - rhetoric/symbolic acts.

Return ONLY valid JSON (no prose) exactly:

{{
  "subscores": {{
    "conflict_war": <float 0..1 or null>,
    "political_stability": <float 0..1 or null>,
    "governance_corruption": <float 0..1 or null>,
    "macroeconomic_volatility": <float 0..1 or null>,
    "regulatory_uncertainty": <float 0..1 or null>
  }},
  "news_article_scores": [
    {{"id": "<id from ARTICLES_JSON>", "impact": <float 0..1>}}
  ],
  "score": <float 0..1>,  # your single calibrated investor-risk score AFTER applying the hard rules above
  "bullet_summary": "<<=120 words explaining primary drivers and meaningful mitigants>"
}}
```

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
| `backend` | `.env` | `DATABASE_URL` – PostgreSQL connection string; `OPENAI_API_KEY` – OpenAI API key; `FMP_API_KEY` – Financial Modeling Prep key (economic calendar + prices daemon); optional `CRAWLBASE_JS_TOKEN` / `CRAWLBASE_TOKEN` for Reuters/Bloomberg enrichment |
| `frontend` | `.env` | `DATABASE_URL` – Postgres URL with `sslmode=require` |

The `backend/.env` file is read by the ETL pipeline and the database upsert routines. The `frontend/.env` is read by the Next.js server‑side API routes that serve the dashboard data.

### Backend setup

1. **Activate a virtual environment and install dependencies:**

 ```bash
 python -m venv venv && source venv/bin/activate
 pip install -r backend/requirements.txt
 ```

2. **Seed macro data:** No separate step is needed — the first run of the ETL automatically downloads World Bank panels for every country that does not already have one, and skips the rest.

3. **Run the end‑to‑end ETL:** This computes risk scores and persists them to the database.

 ```bash
 python backend/main.py
 ```

 The script loops over each country, builds the macro payload, fetches relevant news, calls the LLM to generate a risk score and bullet summary, and upserts the results into Postgres. Running the ETL for the 56‑country roster can take several minutes because the news fetcher throttles requests to stay under Google’s anonymous quota.

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

### Live prices feed (optional)

The bottom‑bar **Prices** pane is fed by a standalone, long‑running daemon — `backend/prices_daemon.py` — that is **separate from the daily `main.py` ETL**. Point a process supervisor (or boot‑time Task Scheduler entry) at it so the feed stays fresh:

 ```bash
python backend/prices_daemon.py        # continuous loop (Ctrl‑C to stop)
python backend/prices_daemon.py --once # one‑shot tick for verification
 ```

It reuses `FMP_API_KEY` + `DATABASE_URL`. See `backend/README.md` for details.

### Directory Structure
```bash
AI-Country-Risk-Dashboard/
├── backend/                    # Python ETL, LLM scoring and DB interface
│   ├── main.py                 # Entry point for the end‑to‑end daily ETL
│   ├── prices_daemon.py        # Standalone live‑prices poller (separate process)
│   ├── utils/
│   │   ├── ai/                 # LangChain LLM wrapper, prompt constants, alert/calendar rankers, legal_restrictions.yaml (sanctions gate)
│   │   ├── data_fetching/      # World Bank, IMF, OWID (political corruption), FMP (calendar/prices) fetchers
│   │   ├── news_fetching/      # Google News RSS, URL resolver, simple/advanced scrapers
│   │   ├── data_upsert/        # Transactional upserts into PostgreSQL (data_push.py)
│   │   ├── data_retrieval.py   # Reads panels and builds the LLM payload
│   │   ├── market_hours.py     # Market‑open gating for the prices daemon
│   │   ├── http.py             # Shared retry policy, User‑Agents, FMP GET wrapper
│   │   ├── dates.py            # Datetime formats shared across modules
│   │   └── constants.py        # Indicator definitions, asset universe, LLM prompt
│   ├── tests/                  # Characterization tests (no network, no DB)
│   └── README.md               # Detailed backend instructions
├── frontend/                   # Next.js (App Router) dashboard
│   ├── app/
│   │   ├── api/                # Cached DB‑backed API routes
│   │   ├── components/         # Map, sidebar, rail, bottom‑bar panes, charts
│   │   └── lib/                # Server queries, cached fetchers, client caches
│   └── README.md               # Detailed frontend instructions
├── assets/                     # Screenshots / demo media
├── LICENSE                     # MIT license
└── README.md                   # (You are here)
```

### Database Schema
The schema is created idempotently by the ETL (and the prices daemon) — there is no separate migration tool. See `backend/README.md` for the full DDL.

| Table | Description |
|-------|-------------|
| `country` | Country ISO‑2 code and canonical name |
| `indicator` | Indicator definitions and units |
| `yearly_value` | Annual World Bank macro values per country |
| `recent_indicator` | Freshest sub‑annual (IMF) values, preferred over the annual ones |
| `risk_snapshot` | 0–1 risk score and LLM bullet summary for a given date |
| `risk_snapshot_article` | Top‑3 news articles tied to a snapshot |
| `news_alert` | Globally AI‑ranked alerts feed |
| `economic_calendar_event` | Upcoming economic events with AI importance |
| `market_price` | Live prices (stocks/bonds/crypto/commodities) |
| `price_reference` | Quarter‑/year‑start closes for the 1Q/YTD calc |
| `live_tv_channel` | DB‑backed Live TV channel list (seed fallback if empty) |

### Contributing
Contributions, bug reports and feature requests are welcome! Please open an issue or submit a pull request on GitHub. When adding new data sources or indicators, update the `constants.py` mappings and ensure your changes are reflected in both the backend and the frontend.

### License
This project is licensed under the MIT License. See the `LICENSE` file for details.

### Acknowledgements
The dashboard relies on open data from the World Bank and the IMF for macro‑economic indicators, Our World in Data (V‑Dem) for the political‑corruption index, Financial Modeling Prep (FMP) for live market prices and the economic calendar, Google News for headline scraping, and OpenAI’s models for risk scoring. Thanks to the maintainers of LangChain, Next.js, MapLibre, Recharts and the open‑source community for their tools and libraries.