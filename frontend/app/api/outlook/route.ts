// app/api/outlook/route.ts
import type { NextRequest } from "next/server";
import { getOutlook } from "@/app/lib/cached-fetchers";
import { HttpError, jsonRouteWithRequest } from "@/app/lib/api";

export const runtime = "nodejs";

/**
 * `GET /api/outlook?iso2=PT`: the latest outlook for one country ("Where it
 * is headed"), its top-five open watch items and the forecast ledger's track
 * record. Scores are integers 0-100; divide by 100 to display 0.63.
 *
 * 400 when `iso2` is not a two-letter code, 404 when the country has no
 * outlook yet.
 */
export const GET = jsonRouteWithRequest(async (req: NextRequest) => {
  const iso2 = (req.nextUrl.searchParams.get("iso2") ?? "").trim().toUpperCase();
  if (!/^[A-Z]{2}$/.test(iso2)) {
    throw new HttpError(400, "iso2 must be a two-letter country code, e.g. ?iso2=PT");
  }
  const bundle = await getOutlook(iso2);
  if (!bundle) {
    throw new HttpError(404, `no outlook for ${iso2}`);
  }
  return bundle;
});
