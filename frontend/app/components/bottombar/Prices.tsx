'use client';

import { useEffect, useMemo, useState } from 'react';
import {
  fetchMarketPrices,
  groupByCategory,
  type MarketPrice,
  type PriceCategory,
} from '../../lib/prices-client';

/** Refresh cadence — matches the scheduler's ~30-minute price write cadence. */
const POLL_MS = 30 * 60 * 1000;

function fmtPx(a: MarketPrice) {
  if (a.px == null) return '—';
  if (a.is_yield) return a.px.toFixed(2) + '%';
  if (a.px >= 1000) return a.px.toLocaleString(undefined, { maximumFractionDigits: 1 });
  if (a.px >= 1) return a.px.toFixed(2);
  return a.px.toFixed(4);
}
function fmtCv(v: number | null, isYield: boolean) {
  if (v == null) return '—';
  const s = v >= 0 ? '+' : '';
  return isYield ? `${s}${v.toFixed(2)}` : `${s}${v.toFixed(2)}%`;
}
const cls = (v: number | null) => (v == null ? '' : v >= 0 ? 'up' : 'down');

/**
 * Bottom-bar pane: live market prices backed by the `market_price` table. Polls
 * the dedicated /api/prices route every ~5 minutes (the daemon's write cadence)
 * and renders the latest snapshot in place. A failed poll keeps the last good
 * data rather than blanking the pane.
 */
export default function Prices() {
  // null = still loading; [] = loaded but empty.
  const [prices, setPrices] = useState<MarketPrice[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const load = () => {
      fetchMarketPrices(controller.signal)
        .then((rows) => {
          if (!cancelled) setPrices(rows);
        })
        .catch(() => {
          // Keep prior data on a failed poll; only fall back to [] on first load.
          if (!cancelled) setPrices((prev) => prev ?? []);
        });
    };
    load();
    const id = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      controller.abort();
      clearInterval(id);
    };
  }, []);

  const cats = useMemo<PriceCategory[]>(() => groupByCategory(prices ?? []), [prices]);

  const { up, total } = useMemo(() => {
    let u = 0;
    let t = 0;
    (prices ?? []).forEach((a) => {
      if (a.chg == null) return;
      t++;
      if (a.chg >= 0) u++;
    });
    return { up: u, total: t };
  }, [prices]);

  return (
    <div className="mini-table tracker-col">
      <div className="mini-head">
        <span className="mh-title">
          <span className="swatch" style={{ background: 'var(--amber)' }} />
          Prices
        </span>
        <span className="mh-sub">
          {up}/{total} UP
        </span>
      </div>
      <div className="mini-body">
        <table className="mini tracker">
          <thead>
            <tr>
              <th className="trk-sym">Asset</th>
              <th className="trk-px">Last</th>
              <th className="trk-c">1D</th>
              <th className="trk-c">1Q</th>
              <th className="trk-c">YTD</th>
            </tr>
          </thead>
          <tbody>
            {cats.map((cat) => (
              <FragmentRows key={cat.key} cat={cat} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function FragmentRows({ cat }: { cat: PriceCategory }) {
  return (
    <>
      <tr className="trk-group">
        <td colSpan={5}>{cat.label}</td>
      </tr>
      {cat.assets.map((a) => (
        <tr key={a.symbol}>
          <td className="trk-sym">{a.label}</td>
          <td className="trk-px">{fmtPx(a)}</td>
          <td className={`trk-c ${cls(a.chg)}`}>{fmtCv(a.chg, a.is_yield)}</td>
          <td className={`trk-c ${cls(a.q)}`}>{fmtCv(a.q, a.is_yield)}</td>
          <td className={`trk-c ${cls(a.ytd)}`}>{fmtCv(a.ytd, a.is_yield)}</td>
        </tr>
      ))}
    </>
  );
}
