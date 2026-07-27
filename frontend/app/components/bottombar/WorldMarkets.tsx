'use client';

import { useEffect, useState } from 'react';
import { EXCHANGES, type Exchange } from '../../lib/terminal-seed';

function isMarketOpen(ex: Exchange): boolean {
  const d = new Date();
  const dec = d.getUTCHours() + d.getUTCMinutes() / 60;
  return ex.days.includes(d.getUTCDay()) && dec >= ex.o && dec < ex.c;
}

/** Decimal UTC hour → "HH:MM". */
function hhmm(dec: number): string {
  const h = Math.floor(dec);
  const m = Math.round((dec - h) * 60);
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

/** Trading session window in UTC, e.g. "13:30–20:00". */
function sessionUtc(ex: Exchange): string {
  return `${hhmm(ex.o)}–${hhmm(ex.c)}`;
}

/** Bottom-bar pane: live UTC clock and per-exchange open/closed status. */
export default function WorldMarkets() {
  // Re-evaluate open/closed against the live UTC clock every 30s.
  const [, setNow] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setNow((n) => n + 1), 30000);
    return () => clearInterval(id);
  }, []);

  const statuses = EXCHANGES.map((ex) => ({ ex, open: isMarketOpen(ex) }));
  const openCount = statuses.filter((s) => s.open).length;

  return (
    <div className="mini-table markets-col">
      <div className="mini-head">
        <span className="mh-title">
          <span className="swatch" style={{ background: 'var(--risk-low)' }} />
          World Markets
        </span>
        <span className="mh-sub">{openCount} OPEN</span>
      </div>
      <div className="mini-body">
        <table className="mini markets">
          <thead>
            <tr>
              <th className="mname">Exchange</th>
              <th className="mutc">UTC</th>
              <th className="mstat">Status</th>
            </tr>
          </thead>
          <tbody>
            {statuses.map(({ ex, open }) => (
              <tr key={ex.code}>
                <td className="mname" title={`${ex.name} · ${ex.country}`}>
                  <div className="mname-inner">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      className="mkt-flag"
                      src={`/flags/${ex.iso2}.svg`}
                      alt=""
                      onError={(e) => { e.currentTarget.style.visibility = 'hidden'; }}
                    />
                    <span className="name">{ex.code}</span>
                  </div>
                </td>
                <td className="mutc">{sessionUtc(ex)}</td>
                <td className="mstat">
                  <span className={`mkt-status ${open ? 'open' : 'closed'}`}>
                    {open ? 'OPEN' : 'CLOSED'}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
