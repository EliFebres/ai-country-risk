// app/api/outlook/report/route.ts
import { getForecastReport } from "@/app/lib/cached-fetchers";
import { jsonRoute } from "@/app/lib/api";

export const runtime = "nodejs";

/**
 * `GET /api/outlook/report`: the latest `forecast-report` aggregate the
 * backend recorded (coverage, the 4w and 12w tables, arms, the watch table and
 * the systematic-errors block), for a methodology or status page. `null`
 * until the first report has run.
 */
export const GET = jsonRoute(getForecastReport);
