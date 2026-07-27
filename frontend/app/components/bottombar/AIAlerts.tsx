'use client';

import { useEffect, useState } from 'react';
import { loadDashboard, type NewsAlert } from '../../lib/dashboard-client';
import { daysAgoLabel } from '../../lib/format';

/**
 * Bottom-bar pane: globally-ranked AI news alerts. Data comes from the shared
 * /api/dashboard payload (fetched once per session via loadDashboard()); each row
 * links out to its source article. Mirrors the EconCalendar load pattern.
 */
export default function AIAlerts() {
  // null = still loading; [] = loaded but empty.
  const [alerts, setAlerts] = useState<NewsAlert[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    loadDashboard()
      .then((data) => {
        if (!cancelled) setAlerts(data.newsAlerts ?? []);
      })
      .catch(() => {
        if (!cancelled) setAlerts([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const rows = alerts ?? [];
  const criticalCount = rows.filter((a) => a.severity === 'Critical').length;

  return (
    <div className="mini-table alerts-col">
      <div className="mini-head">
        <span className="mh-title">
          <span className="swatch" style={{ background: 'var(--crit)' }} />
          AI Alerts
        </span>
        <span className="mh-sub">{criticalCount} CRITICAL</span>
      </div>
      <div className="mini-body">
        {alerts === null ? (
          <div className="alert-empty">Loading…</div>
        ) : rows.length === 0 ? (
          <div className="alert-empty">No alerts</div>
        ) : (
          rows.map((a) => {
            const when = daysAgoLabel(a.published_at);
            const meta = [a.source, when].filter(Boolean).join(' · ');
            return (
              <a
                key={`${a.global_rank}-${a.url}`}
                className="alert-row"
                href={a.url}
                target="_blank"
                rel="noopener noreferrer"
              >
                <div className="alert-top">
                  <span className={`alert-sev ${a.severity.toLowerCase()}`}>{a.severity}</span>
                  <span className="alert-cat">{a.topic}</span>
                  <span className="alert-iso">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      className="alert-flag"
                      src={`/flags/${a.country_iso2}.svg`}
                      alt=""
                      onError={(e) => { e.currentTarget.style.visibility = 'hidden'; }}
                    />
                    {a.country_iso2}
                  </span>
                  {meta && <span className="alert-src">{meta}</span>}
                </div>
                <div className="alert-text">{a.title}</div>
              </a>
            );
          })
        )}
      </div>

      <style jsx>{`
        .alert-empty {
          padding: 14px 12px;
          font-size: 10.5px;
          color: var(--amber-dim);
          letter-spacing: 0.04em;
          text-transform: uppercase;
        }
      `}</style>
    </div>
  );
}
