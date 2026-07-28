// app/lib/cache-ttl.ts
//
// Per-topic cache lifetimes (in seconds) for the DB-backed API routes. Each TTL
// is chosen to match how often the underlying data is inserted, so we serve from
// an in-memory/CDN cache between inserts instead of hitting Neon on every request.
// (This is NOT a disk cache — nothing is written to the filesystem.)
//
// Tune a single number here when a topic's ingestion cadence changes.

const HOUR = 60 * 60;

export const CACHE_TTL = {
  // Country risk scores + summaries are recomputed by the weekly ETL (Mondays),
  // so a 12h window picks up a fresh run within hours while staying cheap.
  RISK: 12 * HOUR,
  RISK_SUMMARY: 12 * HOUR,

  // World Bank indicators are annual values refreshed by the same weekly run;
  // a day is plenty fresh.
  INDICATORS: 24 * HOUR,

  // News articles are tied to the latest snapshot today but conceptually update
  // more often; keep this shorter.
  ARTICLES: 6 * HOUR,

  // Economic-calendar events are AI-ranked each ETL run; a 6h window keeps the
  // "next 7 days" feed fresh (and lets just-passed events linger briefly).
  ECON_CALENDAR: 6 * HOUR,

  // AI news alerts are AI-ranked once per daily ETL run; a 12h window picks up a
  // fresh run within hours while keeping Neon hits cheap (matches RISK).
  AI_ALERTS: 12 * HOUR,     // daily generation

  // Live market snapshot written once per scheduler tick (~30 min); the Prices
  // pane polls /api/prices on the same cadence, so a 30-min TTL serves cached
  // rows between writes and hits Neon at most once per window.
  PRICES: 30 * 60,

  // Live TV channel list (the `live_tv_channel` table). Edited by hand via SQL
  // when a stream dies, so a short 10-min TTL lets a fix propagate quickly while
  // keeping Neon hits cheap for this tiny, rarely-changed table.
  CHANNELS: 10 * 60,
} as const;
