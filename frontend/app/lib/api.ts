// app/lib/api.ts
import { NextResponse, type NextRequest } from "next/server";

/**
 * Build a `GET` route handler that serializes a fetcher's result as JSON.
 *
 * Centralizes the identical try/catch the DB-backed routes used to repeat:
 * success → `200` with the payload, thrown error → `500` with
 * `{ error: <message> }` (message resolved exactly as before, no `any`).
 *
 * @typeParam T - The fetcher's resolved payload type.
 * @param fetcher - Async producer of the response body.
 * @returns A Next.js route handler suitable for `export const GET = ...`.
 */
export function jsonRoute<T>(fetcher: () => Promise<T>): () => Promise<NextResponse> {
  return async () => {
    try {
      const data = await fetcher();
      return NextResponse.json(data, { status: 200 });
    } catch (err) {
      const e = err as { message?: unknown } | null | undefined;
      return NextResponse.json({ error: String(e?.message ?? err) }, { status: 500 });
    }
  };
}

/**
 * An error a route handler throws to answer with a specific status (a 400 for
 * a bad query parameter, a 404 for a country with nothing to show) instead of
 * the generic 500.
 */
export class HttpError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
  }
}

/**
 * Like {@link jsonRoute}, for a handler that reads the request (query params).
 * An {@link HttpError} becomes its own status; anything else is a 500.
 */
export function jsonRouteWithRequest<T>(
  fetcher: (req: NextRequest) => Promise<T>
): (req: NextRequest) => Promise<NextResponse> {
  return async (req: NextRequest) => {
    try {
      const data = await fetcher(req);
      return NextResponse.json(data, { status: 200 });
    } catch (err) {
      if (err instanceof HttpError) {
        return NextResponse.json({ error: err.message }, { status: err.status });
      }
      const e = err as { message?: unknown } | null | undefined;
      return NextResponse.json({ error: String(e?.message ?? err) }, { status: 500 });
    }
  };
}
