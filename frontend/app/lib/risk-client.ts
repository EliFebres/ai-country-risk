// app/lib/risk-client.ts
//
// Client-side risk types + the shared in-memory risk cache. The map fetches the
// risk dataset once and primes this cache; other client components read it
// synchronously instead of re-fetching (avoiding a stale-CDN divergence).

/** A single country's risk reading plus map position. */
export type CountryRisk = {
  name: string;
  lngLat: [number, number]; // [lng, lat]
  risk: number;             // 0..1 (current risk)
  prevRisk?: number;        // previous single value (convenience)
  prevRiskSeries?: number[]; // all prior scores, newest→oldest (excludes current)
  asOf?: string;            // latest snapshot date (ISO 'YYYY-MM-DD')
  prevAsOfs?: string[];     // prior snapshot dates, newest→oldest (parallel to prevRiskSeries)
  iso2?: string;            // populated by weekly refresh
  nonInvestable?: boolean;  // sanctions badge; independent of `risk`
};

let RISK_CACHE: CountryRisk[] | null = null;

/**
 * Store the freshly-loaded risk dataset for other components to read.
 * @param rows - The risk rows fetched by the map.
 */
export function primeRiskCache(rows: CountryRisk[]): void {
  RISK_CACHE = rows;
}

/**
 * Synchronous peek at the primed risk dataset.
 * @returns The cached rows, or `null` before the map has loaded them.
 */
export function getRiskCache(): CountryRisk[] | null {
  return RISK_CACHE;
}

/** A country's latest article bundle (top 0–3 for its latest snapshot). */
export type CountryArticles = {
  iso2: string;
  name: string;
  as_of: string;
  articles: {
    url: string;
    title?: string | null;
    source?: string | null;
    published_at?: string | null;
    img_url?: string | null;
  }[];
};
