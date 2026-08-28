# AI Country Risk Dashboard — Frontend

A Next.js (App Router) terminal-style dashboard for country risk. It renders a MapLibre world map with per-country risk markers, a slide-in detail sidebar (risk reading + trend chart, economic gauges, AI summary, news), a global "World Risk Index" rail, a masthead status bar, and a bottom ticker bar.

All data comes from a Neon Postgres database, served through cached API routes that query the DB directly. There is **no** filesystem JSON file and **no** weekly refresh job — the previous `public/api/risk.json` / `POST /api/refresh-risk` flow has been removed.

## Tech Stack

- **Next.js 15** (App Router, server components) + **React 19** + **TypeScript 5**
- **Tailwind CSS 4** plus CSS design tokens in `app/globals.css` (`--amber-border`, `--risk-high/elev/low`, etc.)
- **MapLibre GL 5** with OpenFreeMap dark tiles for the map
- **Recharts 3** for the shared `RiskTrendChart` area chart
- **pg 8** connecting to **Neon Postgres** for all data

## Requirements

- Node 18+ and npm
- A Neon Postgres database
- Env var: `DATABASE_URL` (must include SSL, e.g. with `sslmode=require`)

## Quick Start

1. From the repo root: `cd frontend`
2. Install deps: `npm i`
3. Create `.env` with:
   ```
   DATABASE_URL=postgres://USER:PASSWORD@HOST/DB?sslmode=require
   ```
4. Run the dev server: `npm run dev`
5. Open http://localhost:3000 — the map loads risk markers from `/api/risk`; clicking a country opens the sidebar, which loads its details from `/api/dashboard`.

## Commands

- `npm run dev` — Start Next.js in development (Turbopack)
- `npm run build` — Production build
- `npm run start` — Run the production build
- `npm run lint` — Lint

## Architecture

A single route (`app/page.tsx`) renders `TerminalDashboard`, the client orchestrator that owns selection state and lays out the panels:

- **Map** (`app/components/Map.tsx`, via `MapClient.tsx`) — MapLibre map with SVG ring markers per country; loads `/api/risk` on mount.
- **RiskSidebar** (`app/components/RiskSidebar.tsx`) — slide-in detail panel with Risk Reading + trend chart, Economic Gauges, AI Summary, and News sections; loads `/api/dashboard`.
- **WorldRiskIndexRail** (`app/components/WorldRiskIndexRail.tsx`) — always-visible rail showing the global average trend, risk distribution, and top movers. The trend chart has a **metric dropdown selector** (avg risk plus the cross-country indicator averages) rendered through a **body-portaled overlay menu** so it floats above every panel, and it draws each series in average-baseline fill mode.
- **Masthead** (`app/components/Masthead.tsx`) — top status bar (coverage count, live clock, idle-tour toggle).
- **BottomBar** (`app/components/bottombar/`) — ticker bar with Live TV, World Markets, Prices, Econ Calendar, and AI Alerts panes.

`RiskTrendChart` (`app/components/RiskTrendChart.tsx`) is a shared Recharts area chart used by the sidebar's Risk Reading section and the rail. It supports two fill modes via its `baseline` prop: `'zero'` (default — fills from the line down to zero) and `'average'` (draws a dashed reference line at the series mean and fills the band between the line and that mean with a symmetric gradient).

## Data & API Routes

All routes run on the Node.js runtime, respond to `GET`, and are wrapped by `jsonRoute` (`app/lib/api.ts`). They share the same `unstable_cache`-wrapped fetchers in `app/lib/cached-fetchers.ts`.

| Route | Returns | Used by |
| --- | --- | --- |
| `/api/risk` | Latest risk score + history per country, with resolved map coordinates | Map (fast first paint) |
| `/api/dashboard` | Indicators + articles + summaries, plus cross-country indicator average trends, AI alerts, and econ-calendar events, composed in parallel (one request) | Sidebar, rail, AI Alerts, Econ Calendar |
| `/api/indicators` | Latest year/value for the target indicators per country | (individual topic) |
| `/api/articles` | Top-3 articles per country's latest snapshot | (individual topic) |
| `/api/risk-summary` | Latest AI bullet summary per country | (individual topic) |
| `/api/prices` | Live market prices (stocks / bonds / crypto / commodities) from the prices daemon | Prices pane (polls every 30 min) |
| `/api/econ-calendar` | Upcoming economic-calendar events | (individual topic) |

On the client, `RISK_CACHE` (`app/lib/risk-client.ts`) and `DASHBOARD_CACHE` (`app/lib/dashboard-client.ts`) dedupe their fetches across components for the session. The Prices pane instead polls through `app/lib/prices-client.ts` on a 30-minute cadence (no session memo — the server-side `unstable_cache` TTL protects Neon), so it keeps ticking with fresh data.

## Caching

Each topic is a single `unstable_cache` instance shared by every route that needs it, so Neon is queried at most once per TTL per topic. TTLs live in `app/lib/cache-ttl.ts`:

- **Risk** — 12h
- **Risk summaries** — 12h
- **Indicators** — 24h
- **Articles** — 6h
- **Econ calendar** — 6h
- **AI alerts** — 12h
- **Prices** — 30 min (matches the scheduler tick that writes them)

This is an in-memory/CDN cache — nothing is written to the filesystem.

## Database

Queries live in `app/lib/risk-server.ts` (`server-only`), which uses a process-wide pooled `pg` connection created from `DATABASE_URL`. The backend defines ten tables in `backend/data_upsert/schema.py`; the ones this app reads are:

- `country` — ISO2 code, name, and map coordinates
- `risk_snapshot` — per-country scores over time, plus `bullet_summary`, `non_investable`, and the `top_articles` JSONB array the News section renders
- `indicator_series` — every macro observation at any frequency, from every source, resolved freshest-period-wins
- `news_alert` — globally AI-ranked alerts feeding the AI Alerts pane
- `economic_calendar_event` — upcoming events (with AI importance) feeding the Econ Calendar pane
- `market_price` — live prices plus the 1Q/YTD reference closes feeding the Prices pane

> **Three of these queries are broken.** They still read tables the backend's
> twenty-to-ten table rebuild dissolved: `/api/indicators` and `/api/articles`
> return 500, and `fetchIndicatorAverageTrends` and `fetchChannels` fail quietly
> and return empty. Nothing under `frontend/` was updated, on purpose — the
> replacement SQL for each is written out in
> [`../docs/deferred.md`](../docs/deferred.md).

Map positions come from the database (`country.lat` / `country.lng`), seeded from the backend roster in `backend/util/constants.py`. The frontend holds no country list of its own — adding a country to that roster makes it appear here with no frontend change. A country whose row has no coordinates is skipped on the map.

## Placeholder / Seed Features

Most bottom-bar panes are now live-backed:

- **Prices** — live from `/api/prices` (the prices daemon's `market_price` rows).
- **AI Alerts** and **Econ Calendar** — live from the `/api/dashboard` payload (`news_alert` and `economic_calendar_event`).

Only **World Markets** and **Live TV** still render seed data (`app/lib/terminal-seed.ts`) — they are wired into the UI but not yet connected to the database.
