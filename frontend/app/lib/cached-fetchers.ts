// app/lib/cached-fetchers.ts
//
// Shared `unstable_cache`-wrapped fetchers for the DB-backed API routes. Every
// route (and the combined /api/dashboard route) imports these SAME cache
// instances, so each topic hits Neon at most once per its own TTL no matter how
// many routes invoke it. Keep the cache keys/tags here in sync with the per-topic
// revalidate tags used elsewhere.
import "server-only";
import { unstable_cache } from "next/cache";
import { riskRepository } from "@/app/lib/risk-server";
import { CACHE_TTL } from "@/app/lib/cache-ttl";
import type { CountryRisk } from "@/app/lib/risk-client";

/**
 * Normalize a snapshot date to `'YYYY-MM-DD'`. `pg` returns DATE columns as JS
 * `Date`s at *local* midnight, so we read the local Y-M-D (not `toISOString`,
 * which can shift to the previous UTC day). Plain strings are passed through.
 */
function asIsoDate(v: unknown): string {
  if (!v) return "";
  if (v instanceof Date) {
    const y = v.getFullYear();
    const m = String(v.getMonth() + 1).padStart(2, "0");
    const d = String(v.getDate()).padStart(2, "0");
    return `${y}-${m}-${d}`;
  }
  const s = String(v);
  const m = /^(\d{4}-\d{2}-\d{2})/.exec(s);
  return m ? m[1] : s;
}

/** Latest risk score + history per country, with map coordinates resolved. */
export const getRisks = unstable_cache(
  async (): Promise<CountryRisk[]> => {
    const joined = await riskRepository.fetchJoinedLatestRisks();
    const out: CountryRisk[] = [];

    for (const row of joined) {
      // Map position comes from the DB (seeded by the backend roster), so a
      // country added to the backend appears here with no frontend change.
      const lng = Number(row.lng);
      const lat = Number(row.lat);
      if (!Number.isFinite(lng) || !Number.isFinite(lat)) continue; // unseeded; skip
      const lngLat: [number, number] = [lng, lat];

      // Zip prior scores with their snapshot dates so the finite-filter keeps the
      // two arrays parallel (a dropped non-finite score drops its date too).
      const rawScores = Array.isArray(row.prev_scores) ? row.prev_scores : [];
      const rawDates = row.prev_as_ofs ?? row.prev_asofs ?? [];
      const list: number[] = [];
      const dates: string[] = [];
      rawScores.forEach((s, i) => {
        const n = Number(s);
        if (Number.isFinite(n)) {
          list.push(n);
          dates.push(asIsoDate(rawDates[i]));
        }
      });
      // (defensive) if list missing but prev_score exists, include it
      if (list.length === 0 && row.prev_score != null) {
        const n = Number(row.prev_score);
        if (Number.isFinite(n)) {
          list.push(n);
          dates.push(asIsoDate(row.prev_as_of));
        }
      }

      const entry: CountryRisk = {
        name: row.name,
        lngLat,
        risk: Number(row.score),
        iso2: row.iso2,
      };
      if (row.as_of) entry.asOf = asIsoDate(row.as_of);
      if (list.length > 0) {
        entry.prevRiskSeries = list; // newest→oldest, excluding current
        entry.prevRisk = list[0];
        entry.prevAsOfs = dates; // parallel to prevRiskSeries
      }
      out.push(entry);
    }

    return out;
  },
  ["risk-data"],
  { revalidate: CACHE_TTL.RISK, tags: ["risk"] }
);

/** Latest year/value for the four target indicators, per country. */
export const getIndicators = unstable_cache(
  async () => riskRepository.fetchLatestIndicatorValues(),
  ["indicators-latest"],
  { revalidate: CACHE_TTL.INDICATORS, tags: ["indicators"] }
);

/** Cross-country average-per-year trend for each selectable rail indicator. */
export const getIndicatorAverages = unstable_cache(
  async () => riskRepository.fetchIndicatorAverageTrends(),
  ["indicator-averages"],
  { revalidate: CACHE_TTL.INDICATORS, tags: ["indicators"] }
);

/** Live TV channel list for the bottom-bar pane (DB-backed, SQL-editable). */
export const getChannels = unstable_cache(
  async () => riskRepository.fetchChannels(),
  ["live-tv-channels"],
  { revalidate: CACHE_TTL.CHANNELS, tags: ["channels"] }
);

/** Top-3 articles per country's latest snapshot. */
export const getArticles = unstable_cache(
  async () => riskRepository.fetchLatestArticlesForLatestSnapshots(),
  ["articles-latest"],
  { revalidate: CACHE_TTL.ARTICLES, tags: ["articles"] }
);

/** Latest non-empty AI bullet summary per country. */
export const getSummaries = unstable_cache(
  async () => riskRepository.fetchLatestSummaries(),
  ["risk-summaries"],
  { revalidate: CACHE_TTL.RISK_SUMMARY, tags: ["risk-summary"] }
);

/** Up to 12 upcoming (next 7 days) economic-calendar events, closest first. */
export const getEconCalendar = unstable_cache(
  async () => riskRepository.fetchEconCalendarEvents(),
  ["econ-calendar-upcoming"],
  { revalidate: CACHE_TTL.ECON_CALENDAR, tags: ["econ-calendar"] }
);

/** Latest run's globally-ranked AI news alerts, ordered by global rank. */
export const getNewsAlerts = unstable_cache(
  async () => riskRepository.fetchLatestNewsAlerts(),
  ["ai-alerts-latest"],
  { revalidate: CACHE_TTL.AI_ALERTS, tags: ["ai-alerts"] }
);

/** Latest market-price snapshot for the Prices pane, in curated display order. */
export const getMarketPrices = unstable_cache(
  async () => riskRepository.fetchMarketPrices(),
  ["market-prices"],
  { revalidate: CACHE_TTL.PRICES, tags: ["prices"] }
);
