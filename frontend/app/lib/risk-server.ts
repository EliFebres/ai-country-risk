// app/lib/risk-server.ts
import "server-only";
import type { Pool } from "pg";
import type { Channel } from "./terminal-seed";

// ----------------------------- Types --------------------------------------

export type JoinedLatestRisk = {
  iso2: string;
  name: string;
  // Map marker position, seeded from the backend roster
  // (backend/utils/constants.py -> country.lat/lng). The frontend keeps no
  // country list or coordinate table of its own.
  lat: number | null;
  lng: number | null;
  as_of: string;
  score: number;
  bullet_summary: string;
  // True when a sanctions regime makes securities exposure unlawful. Drives the
  // RESTRICTED badge. It does NOT affect `score`: as of backend policy_version
  // p2.0 the score is the model's own judgement and no code adjusts it.
  non_investable?: boolean | null;
  prev_as_of?: string | null;
  prev_score?: number | null;
  // arrays of all prior observations (excluding the latest), newest→oldest
  prev_scores?: number[] | null;
  prev_asofs?: string[] | null;
} & { prev_as_ofs?: string[] | null }; // keep backward compat with earlier shape

export type SummaryEntry = { country_iso2: string; bullet_summary: string };

export const INDICATOR_TARGET_NAMES = [
  "Rule of law (z-score)",
  "Inflation (% y/y)",
  "Interest payments (% revenue)",
  "GDP per-capita growth (% y/y)",
] as const;

export type IndicatorTargetName = (typeof INDICATOR_TARGET_NAMES)[number];

/**
 * One indicator reading for a country. `value` is the freshest available: a
 * sub-annual IMF observation from `recent_indicator` when present (carrying a
 * precise `period`/`freq`/`source`), otherwise the latest World Bank annual
 * value. `year` is always set (the period's year for fresh rows) for back-compat.
 */
export type IndicatorValue = {
  value: number;
  unit?: string;
  year: number;            // calendar year of the reading (period year for fresh rows)
  period?: string;         // ISO date 'YYYY-MM-DD' end-of-period (sub-annual rows only)
  freq?: "M" | "Q" | "A";  // observation frequency ('A' = annual WB fallback)
  source?: string;         // e.g. 'IMF' for fresh rows; undefined for WB annual
};

export type CountryIndicatorLatest = {
  iso2: string;
  name: string;
  values: Partial<Record<IndicatorTargetName, IndicatorValue>>;
};

/**
 * Indicators (by `indicator.name`) offered as cross-country average TRENDS in
 * the rail's metric dropdown. Sourced from annual `yearly_value`, averaged
 * across countries per year.
 */
export const AVG_TREND_INDICATOR_NAMES = [
  "Inflation (% y/y)",
  "Rule of law (z-score)",
  "GDP per-capita growth (% y/y)",
  "Unemployment (% labour force)",
  "Interest payments (% revenue)",
  "Political corruption index (0–1, higher = more corrupt)",
] as const;

/** One year's cross-country average for an indicator. */
export type IndicatorAvgPoint = { year: number; avg: number };

/** Map of `indicator.name` → its average-per-year series (oldest→newest). */
export type IndicatorAverageTrends = Record<string, IndicatorAvgPoint[]>;

export type CountryArticles = {
  iso2: string;
  name: string;
  as_of: string; // latest snapshot date for this country
  articles: {
    url: string;
    title?: string | null;
    source?: string | null;
    published_at?: string | null; // ISO string
    img_url?: string | null;      // image URL per article
  }[];
};

/** One upcoming economic-calendar release for the global Econ Calendar pane. */
export type EconCalendarEvent = {
  event_time: string;          // ISO 8601 UTC string
  country: string;             // display country name
  event: string;               // release / decision name
  importance: "h" | "m" | "l"; // FMP impact tier
};

/** One globally-ranked AI news alert for the bottom-bar "AI Alerts" pane. */
export type NewsAlert = {
  global_rank: number;          // 1..N global importance rank
  country_iso2: string;         // originating country (ISO-2)
  country_name: string | null;  // display name
  url: string;                  // article link
  title: string | null;         // headline
  source: string | null;        // publisher
  published_at: string | null;  // ISO 8601 string
  topic: string;                // one of ALERT_TOPICS
  severity: "Critical" | "Caution" | "Watch";
  importance: number | null;    // global-economy importance (0..1)
  rationale: string | null;     // one-line ranking rationale
  image_url: string | null;     // thumbnail URL
};

/** One asset row for the bottom-bar "Prices" pane (maintained by the prices daemon). */
export type MarketPrice = {
  symbol: string;       // stable internal id (DB primary key)
  label: string;        // display label
  asset_class: "stocks" | "bonds" | "crypto" | "commodities";
  is_yield: boolean;    // bonds: chg/q/ytd are POINT diffs, not % moves
  px: number | null;    // last price / yield
  chg: number | null;   // 1D  (% for prices, points for yields)
  q: number | null;     // 1Q
  ytd: number | null;   // YTD
  sort_order: number;   // curated display order
};

// ----------------------------- DB (Neon / pg) -----------------------------

declare global {
  // Reused across hot reloads / serverless invocations in the same process.
  // eslint-disable-next-line no-var
  var __NEON_POOL__: Pool | undefined;
}

/**
 * Data-access layer for the Neon (Postgres) risk database.
 *
 * Wraps a process-wide `pg` connection pool — created lazily and cached on a
 * global so hot reloads and concurrent requests share a single pool — and
 * exposes one method per query. SQL strings are identical to the prior
 * function-based module; only the organization changed.
 */
export class RiskRepository {
  /**
   * Resolve the shared Neon pool, creating it on first use.
   *
   * @returns The singleton `pg` {@link Pool}.
   * @throws If called in a browser context or `DATABASE_URL` is unset.
   */
  private async pool(): Promise<Pool> {
    if (typeof window !== "undefined") {
      throw new Error("DB functions must run on the server.");
    }
    const { Pool } = await import("pg");
    if (!process.env.DATABASE_URL) {
      throw new Error("Missing DATABASE_URL env var for Neon.");
    }
    if (!global.__NEON_POOL__) {
      global.__NEON_POOL__ = new Pool({
        connectionString: process.env.DATABASE_URL,
        ssl: { rejectUnauthorized: false },
        // max: 3,
      });
    }
    return global.__NEON_POOL__;
  }

  /**
   * Latest and previous risk snapshot per country, plus arrays of all prior
   * scores/dates (excluding the latest), ordered newest→oldest.
   */
  async fetchJoinedLatestRisks(): Promise<JoinedLatestRisk[]> {
    const pool = await this.pool();
    const { rows } = await pool.query<JoinedLatestRisk>(
      `
    WITH ranked AS (
      SELECT
        rs.country_iso2,
        rs.as_of,
        rs.score,
        rs.bullet_summary,
        rs.non_investable,
        ROW_NUMBER() OVER (
          PARTITION BY rs.country_iso2
          ORDER BY rs.as_of DESC
        ) AS rn
      FROM risk_snapshot rs
    ),
    latest AS (
      SELECT country_iso2, as_of, score, bullet_summary, non_investable
      FROM ranked
      WHERE rn = 1
    ),
    prev_single AS (
      SELECT country_iso2, as_of AS prev_as_of, score AS prev_score
      FROM ranked
      WHERE rn = 2
    ),
    prev_list AS (
      SELECT
        country_iso2,
        ARRAY_AGG(score::float8 ORDER BY as_of DESC) FILTER (WHERE rn >= 2) AS prev_scores,
        ARRAY_AGG(as_of ORDER BY as_of DESC)          FILTER (WHERE rn >= 2) AS prev_as_ofs
      FROM ranked
      GROUP BY country_iso2
    )
    SELECT
      c.iso2,
      c.name,
      c.lat,
      c.lng,
      l.as_of,
      l.score,
      l.bullet_summary,
      l.non_investable,
      ps.prev_as_of,
      ps.prev_score,
      pl.prev_scores,
      pl.prev_as_ofs
    FROM latest l
    LEFT JOIN prev_single ps ON ps.country_iso2 = l.country_iso2
    LEFT JOIN prev_list   pl ON pl.country_iso2 = l.country_iso2
    JOIN country c           ON c.iso2 = l.country_iso2
    ORDER BY c.name ASC;
    `
    );
    return rows;
  }

  /** Latest non-empty bullet summary per country (one row each). */
  async fetchLatestSummaries(): Promise<SummaryEntry[]> {
    const pool = await this.pool();
    const { rows } = await pool.query<SummaryEntry>(
      `SELECT DISTINCT ON (country_iso2)
            country_iso2, bullet_summary
       FROM risk_snapshot
      WHERE bullet_summary IS NOT NULL
        AND TRIM(bullet_summary) <> ''
   ORDER BY country_iso2, as_of DESC;`
    );
    return rows.map((r) => ({
      country_iso2: (r.country_iso2 || "").toUpperCase(),
      bullet_summary: r.bullet_summary,
    }));
  }

  /**
   * Each country's freshest value for the four target indicators.
   *
   * For every (country, indicator) the latest World Bank annual value is taken
   * (DISTINCT ON … yr DESC), then a sub-annual IMF observation from
   * `recent_indicator` — refreshed far more often than the WB annual series — is
   * preferred when present. The annual value remains the fallback, so a country
   * the IMF doesn't cover still shows its WB figure (labeled as annual).
   *
   * Defensive: `recent_indicator` is maintained by the backend run and may not
   * exist yet in a fresh environment, so a join failure falls back to the
   * annual-only query rather than failing the whole /api/dashboard payload.
   */
  async fetchLatestIndicatorValues(): Promise<CountryIndicatorLatest[]> {
    const pool = await this.pool();

    type Row = {
      iso2: string;
      name: string;
      indicator_name: IndicatorTargetName;
      unit: string | null;
      yr: number;
      annual_value: number;
      recent_value: number | null;
      recent_period: string | null;
      recent_freq: "M" | "Q" | "A" | null;
      recent_source: string | null;
    };

    // latest WB annual value per (country, indicator), with the freshest IMF
    // observation LEFT JOINed by indicator name.
    const joinedSql = `
    WITH targets AS (
      SELECT id, name, unit
        FROM indicator
       WHERE name = ANY($1::text[])
    ),
    latest AS (
      SELECT DISTINCT ON (y.country_iso2, y.indicator_id)
             y.country_iso2, y.indicator_id, y.yr, y.value
        FROM yearly_value y
        JOIN targets t ON t.id = y.indicator_id
    ORDER BY y.country_iso2, y.indicator_id, y.yr DESC
    )
    SELECT c.iso2,
           c.name,
           t.name        AS indicator_name,
           t.unit,
           l.yr,
           l.value       AS annual_value,
           ri.value      AS recent_value,
           ri.period::text AS recent_period,
           ri.freq       AS recent_freq,
           ri.source     AS recent_source
      FROM latest l
      JOIN targets t  ON t.id = l.indicator_id
      JOIN country c  ON c.iso2 = l.country_iso2
 LEFT JOIN recent_indicator ri
        ON ri.country_iso2 = c.iso2
       AND ri.indicator    = t.name
    ORDER BY c.name, t.name;
    `;

    // Annual-only fallback (identical shape, recent_* nulled) for environments
    // where the recent_indicator table doesn't exist yet.
    const annualOnlySql = `
    WITH targets AS (
      SELECT id, name, unit FROM indicator WHERE name = ANY($1::text[])
    ),
    latest AS (
      SELECT DISTINCT ON (y.country_iso2, y.indicator_id)
             y.country_iso2, y.indicator_id, y.yr, y.value
        FROM yearly_value y
        JOIN targets t ON t.id = y.indicator_id
    ORDER BY y.country_iso2, y.indicator_id, y.yr DESC
    )
    SELECT c.iso2, c.name, t.name AS indicator_name, t.unit, l.yr,
           l.value AS annual_value,
           NULL::float8 AS recent_value, NULL::text AS recent_period,
           NULL::text AS recent_freq, NULL::text AS recent_source
      FROM latest l
      JOIN targets t ON t.id = l.indicator_id
      JOIN country c ON c.iso2 = l.country_iso2
    ORDER BY c.name, t.name;
    `;

    let rows: Row[];
    try {
      ({ rows } = await pool.query<Row>(joinedSql, [INDICATOR_TARGET_NAMES]));
    } catch (err) {
      console.warn("fetchLatestIndicatorValues: recent_indicator join failed, using annual-only:", err);
      ({ rows } = await pool.query<Row>(annualOnlySql, [INDICATOR_TARGET_NAMES]));
    }

    const byIso2 = new Map<string, CountryIndicatorLatest>();
    for (const r of rows) {
      const key = (r.iso2 || "").toUpperCase();
      let entry = byIso2.get(key);
      if (!entry) {
        entry = { iso2: key, name: r.name, values: {} };
        byIso2.set(key, entry);
      }

      const useRecent = r.recent_value != null && !!r.recent_period;
      entry.values[r.indicator_name] = useRecent
        ? {
            value: Number(r.recent_value),
            unit: r.unit ?? undefined,
            year: new Date(r.recent_period as string).getUTCFullYear(),
            period: r.recent_period as string,
            freq: (r.recent_freq ?? "M") as "M" | "Q" | "A",
            source: r.recent_source ?? undefined,
          }
        : {
            value: Number(r.annual_value),
            unit: r.unit ?? undefined,
            year: Number(r.yr),
            freq: "A",
          };
    }
    return Array.from(byIso2.values());
  }

  /**
   * Cross-country AVERAGE per year for each selectable trend indicator.
   *
   * Averages `yearly_value.value` across all reporting countries per year, for
   * the indicators in {@link AVG_TREND_INDICATOR_NAMES}. Years with thin
   * coverage are dropped (`HAVING COUNT(*) >= 10`) so a partially-reported
   * latest World Bank year doesn't produce a jumpy final point, and the window
   * is trimmed to recent years (`yr >= 2010`). Returns one array per indicator
   * name, ordered oldest→newest. Defensive: returns `{}` on query failure so a
   * missing column/table never fails the whole /api/dashboard payload.
   */
  async fetchIndicatorAverageTrends(): Promise<IndicatorAverageTrends> {
    try {
      const pool = await this.pool();
      const { rows } = await pool.query<{ name: string; yr: number; avg: number }>(
        `
      SELECT i.name, y.yr, AVG(y.value) AS avg
        FROM yearly_value y
        JOIN indicator i ON i.id = y.indicator_id
       WHERE i.name = ANY($1::text[])
         AND y.value IS NOT NULL
         AND y.yr >= 2010
    GROUP BY i.name, y.yr
      HAVING COUNT(*) >= 10
    ORDER BY i.name, y.yr;
      `,
        [AVG_TREND_INDICATOR_NAMES]
      );

      const out: IndicatorAverageTrends = {};
      for (const r of rows) {
        (out[r.name] ??= []).push({ year: Number(r.yr), avg: Number(r.avg) });
      }
      return out;
    } catch (err) {
      console.warn("fetchIndicatorAverageTrends failed; returning {}:", err);
      return {};
    }
  }

  /**
   * Live TV channel list for the bottom-bar pane, from the `live_tv_channel`
   * table (key, label, YouTube `channel_id`, sort order). Kept in the DB — not
   * hardcoded — so a dead stream can be re-pointed with a single SQL UPDATE,
   * no code deploy. Maps `channel_id` → the client `Channel.id`. Defensive:
   * returns `[]` on query failure so the component falls back to its seed list.
   */
  async fetchChannels(): Promise<Channel[]> {
    try {
      const pool = await this.pool();
      const { rows } = await pool.query<{ key: string; label: string; channel_id: string }>(
        `SELECT key, label, channel_id FROM live_tv_channel ORDER BY sort, key`
      );
      return rows.map((r) => ({ key: r.key, label: r.label, id: r.channel_id }));
    } catch (err) {
      console.warn("fetchChannels failed; returning []:", err);
      return [];
    }
  }

  /**
   * For each country, find its latest `risk_snapshot.as_of` and take up to 3
   * most recent (by `published_at DESC`, then `rank ASC`) articles for that
   * same `as_of`. Countries with 0 articles are still included (empty array).
   */
  async fetchLatestArticlesForLatestSnapshots(): Promise<CountryArticles[]> {
    const pool = await this.pool();

    const { rows } = await pool.query<{
      iso2: string;
      name: string;
      as_of: string;
      url: string | null;
      title: string | null;
      source: string | null;
      published_at: string | null;
      image_url: string | null;
      rn: number | null;
    }>(`
    WITH latest AS (
      SELECT DISTINCT ON (rs.country_iso2)
             rs.country_iso2, rs.as_of
        FROM risk_snapshot rs
    ORDER BY rs.country_iso2, rs.as_of DESC
    ),
    ranked AS (
      SELECT
        a.country_iso2,
        a.as_of,
        a.url,
        a.title,
        a.source,
        a.published_at,
        a.image_url,
        ROW_NUMBER() OVER (
          PARTITION BY a.country_iso2
          ORDER BY a.published_at DESC NULLS LAST, a.rank ASC, a.id ASC
        ) AS rn
      FROM risk_snapshot_article a
      JOIN latest l
        ON l.country_iso2 = a.country_iso2
       AND l.as_of = a.as_of
    ),
    top3 AS (
      SELECT *
      FROM ranked
      WHERE rn <= 3
    )
    SELECT
      c.iso2,
      c.name,
      l.as_of::text AS as_of,
      t.url,
      t.title,
      t.source,
      t.published_at::text AS published_at,
      t.image_url,
      t.rn
    FROM latest l
    JOIN country c ON c.iso2 = l.country_iso2
    LEFT JOIN top3 t ON t.country_iso2 = l.country_iso2
    ORDER BY c.name ASC, t.rn ASC NULLS LAST;
  `);

    // Group rows into one object per country.
    const byIso2 = new Map<string, CountryArticles>();
    for (const r of rows) {
      const key = (r.iso2 || "").toUpperCase();
      let entry = byIso2.get(key);
      if (!entry) {
        entry = { iso2: key, name: r.name, as_of: r.as_of, articles: [] };
        byIso2.set(key, entry);
      }
      if (r.url) {
        entry.articles.push({
          url: r.url,
          title: r.title ?? null,
          source: r.source ?? null,
          published_at: r.published_at ?? null,
          img_url: r.image_url ?? null, // map DB image_url -> JSON img_url
        });
      }
    }
    // Ensure every country from latest is present (even if 0 articles)
    return Array.from(byIso2.values());
  }

  /**
   * Up to 20 upcoming economic-calendar events for the global Econ Calendar pane.
   *
   * Window is hour-precise (`now()` … `now() + 7 days`, both TIMESTAMPTZ) so an
   * imminent release is never dropped by date rounding. Selection: take all
   * high-impact events first; if fewer than 20, backfill with medium-impact
   * events — in both tiers the most important by `ai_importance` win — then cap
   * at 20. Rows are returned ordered by `event_time` ascending (closest first).
   */
  async fetchEconCalendarEvents(): Promise<EconCalendarEvent[]> {
    const pool = await this.pool();

    const { rows } = await pool.query<{
      event_time: string | Date;
      country_name: string;
      event: string;
      importance: "h" | "m" | "l";
    }>(`
    WITH windowed AS (
      SELECT event_time, country_name, event, importance, ai_importance
        FROM economic_calendar_event
       WHERE event_time >= now()
         AND event_time <= now() + interval '7 days'
         AND importance IN ('h', 'm')
    ),
    prioritized AS (
      SELECT *,
             ROW_NUMBER() OVER (
               ORDER BY
                 CASE WHEN importance = 'h' THEN 0 ELSE 1 END ASC, -- all highs before mediums
                 ai_importance DESC NULLS LAST,                    -- most important first within tier
                 event_time ASC                                    -- stable tiebreak
             ) AS sel_rank
        FROM windowed
    )
    SELECT event_time, country_name, event, importance
      FROM prioritized
     WHERE sel_rank <= 20
  ORDER BY event_time ASC;
  `);

    return rows.map((r) => ({
      event_time: new Date(r.event_time).toISOString(),
      country: r.country_name,
      event: r.event,
      importance: r.importance,
    }));
  }

  /**
   * The latest run's globally-ranked AI news alerts, ordered by `global_rank`.
   *
   * Self-contained and defensive: the `news_alert` table is newer than the rest
   * of the risk schema and may not exist in every environment yet, so a query
   * failure (missing table, etc.) is swallowed and returns `[]` rather than
   * failing the combined /api/dashboard payload.
   */
  async fetchLatestNewsAlerts(): Promise<NewsAlert[]> {
    try {
      const pool = await this.pool();
      const { rows } = await pool.query<{
        global_rank: number;
        country_iso2: string;
        country_name: string | null;
        url: string;
        title: string | null;
        source: string | null;
        published_at: string | Date | null;
        topic: string;
        severity: "Critical" | "Caution" | "Watch";
        importance: number | null;
        rationale: string | null;
        image_url: string | null;
      }>(`
        SELECT global_rank, country_iso2, country_name, url, title, source,
               published_at, topic, severity, importance, rationale, image_url
          FROM news_alert
         WHERE as_of = (SELECT MAX(as_of) FROM news_alert)
      ORDER BY global_rank ASC;
      `);

      return rows.map((r) => ({
        global_rank: r.global_rank,
        country_iso2: r.country_iso2,
        country_name: r.country_name,
        url: r.url,
        title: r.title,
        source: r.source,
        published_at: r.published_at ? new Date(r.published_at).toISOString() : null,
        topic: r.topic,
        severity: r.severity,
        importance: r.importance != null ? Number(r.importance) : null,
        rationale: r.rationale,
        image_url: r.image_url,
      }));
    } catch (err) {
      console.warn("fetchLatestNewsAlerts failed; returning []:", err);
      return [];
    }
  }

  /**
   * Latest market-price snapshot for the bottom-bar "Prices" pane, in curated
   * display order (`sort_order`).
   *
   * Self-contained and defensive: the `market_price` table is maintained by the
   * separate prices daemon and may not exist in every environment yet, so a query
   * failure (missing table, etc.) is swallowed and returns `[]` rather than
   * failing the /api/prices route.
   */
  async fetchMarketPrices(): Promise<MarketPrice[]> {
    try {
      const pool = await this.pool();
      const { rows } = await pool.query<{
        symbol: string;
        label: string;
        asset_class: "stocks" | "bonds" | "crypto" | "commodities";
        is_yield: boolean;
        px: number | null;
        chg: number | null;
        q: number | null;
        ytd: number | null;
        sort_order: number;
      }>(`
        SELECT symbol, label, asset_class, is_yield, px, chg, q, ytd, sort_order
          FROM market_price
      ORDER BY sort_order ASC;
      `);

      const num = (v: number | null) => (v != null ? Number(v) : null);
      return rows.map((r) => ({
        symbol: r.symbol,
        label: r.label,
        asset_class: r.asset_class,
        is_yield: Boolean(r.is_yield),
        px: num(r.px),
        chg: num(r.chg),
        q: num(r.q),
        ytd: num(r.ytd),
        sort_order: Number(r.sort_order),
      }));
    } catch (err) {
      console.warn("fetchMarketPrices failed; returning []:", err);
      return [];
    }
  }
}

/** Shared singleton repository — mirrors the pooled-connection lifetime. */
export const riskRepository = new RiskRepository();
