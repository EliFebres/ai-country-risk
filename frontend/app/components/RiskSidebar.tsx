// /components/Sidebar/RiskSidebar.tsx
'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import RiskReadingSection from './RiskSidebar/RiskReadingSection';
import EconomicGaugeSection from './RiskSidebar/EconomicGaugeSection';
import AiSummary from './RiskSidebar/AiSummary';
import NewsArticleSection from './RiskSidebar/NewsArticleSection';
import { loadDashboard, getArticlesFor, getIndicatorsFor } from '../lib/dashboard-client';
import { calendarDaysAgo } from '../lib/format';

type Props = {
  open: boolean;
  onClose: () => void;
  country: { name: string; risk: number; prevRisk?: number; iso2?: string;
             nonInvestable?: boolean } | null;
  /** When the current risk data was generated/pulled. Accepts Date, ISO string, or epoch ms. */
  dataTimestamp?: Date | string | number | null;
  durationMs?: number;
  easing?: string;
};

/** Sliding left panel with a country's risk reading, economic gauges, AI summary, and news. */
export default function RiskSidebar({
  open,
  onClose,
  country,
  dataTimestamp,
  durationMs = 500,
  easing = 'ease',
}: Props) {
  // Close on ESC
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const fadeMs = Math.max(120, Math.round(durationMs * 0.6));
  const flagSrc = country?.iso2 ? `/flags/${country.iso2.toUpperCase()}.svg` : null;

  // --- Newest underlying data date for the selected country ---
  // Reflects how stale our data is: the freshest date across this country's
  // news (article published_at / as_of) and economic indicators (year-end),
  // whichever is most recent. Falls back to the global pipeline timestamp.
  const [dataEndDate, setDataEndDate] = useState<Date | null>(null);
  useEffect(() => {
    if (!open || !country?.iso2) {
      setDataEndDate(null);
      return;
    }
    const iso = country.iso2.toUpperCase();
    let cancelled = false;

    (async () => {
      const times: number[] = [];

      // Both news + indicators come from the one combined dashboard payload,
      // loaded once per session (no per-open fetches).
      try {
        const data = await loadDashboard();
        if (cancelled) return;

        // News: latest article date (+ snapshot as_of)
        const news = getArticlesFor(data, iso);
        if (news) {
          for (const a of news.articles ?? []) {
            const t = new Date(a.published_at ?? '').getTime();
            if (!isNaN(t)) times.push(t);
          }
          const asOf = new Date(news.as_of ?? '').getTime();
          if (!isNaN(asOf)) times.push(asOf);
        }

        // Indicators: prefer a fresh observation's precise end-of-period date;
        // otherwise treat the annual value as that calendar year's end.
        const ind = getIndicatorsFor(data, iso);
        if (ind?.values) {
          for (const v of Object.values(ind.values)) {
            if (v?.period) {
              const t = new Date(v.period).getTime();
              if (!isNaN(t)) times.push(t);
            } else if (typeof v?.year === 'number') {
              times.push(new Date(v.year, 11, 31).getTime());
            }
          }
        }
      } catch {
        /* ignore */
      }

      if (!cancelled) {
        setDataEndDate(times.length ? new Date(Math.max(...times)) : null);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [open, country?.iso2]);

  // --- Data age (calendar days) ---
  const { daysOld, lastUpdatedLocal } = useMemo(() => {
    const dt =
      dataEndDate ??
      (dataTimestamp == null
        ? null
        : dataTimestamp instanceof Date
        ? dataTimestamp
        : new Date(dataTimestamp));
    if (!dt || isNaN(dt.getTime())) {
      return { daysOld: null as number | null, lastUpdatedLocal: null as string | null };
    }
    const local = dt.toLocaleDateString(undefined, { dateStyle: 'medium' });
    return { daysOld: calendarDaysAgo(dt), lastUpdatedLocal: local };
  }, [dataEndDate, dataTimestamp]);

  const ageTitle =
    typeof daysOld === 'number'
      ? `Most recent data: ${lastUpdatedLocal ?? 'unknown'}`
      : undefined;

  // --- Content-swap animation ---
  // Replay an entrance animation whenever the country changes while the panel is
  // already open (e.g. the idle auto-tour advancing to another country). Skipped
  // on the initial open — the panel's clip-path reveal already covers that.
  const iso = country?.iso2 ?? null;
  const prevIsoRef = useRef<string | null>(null);
  const wasOpenRef = useRef(false);
  const isSwitch = open && wasOpenRef.current && iso != null && iso !== prevIsoRef.current;
  useEffect(() => {
    prevIsoRef.current = iso;
    wasOpenRef.current = open;
  });
  const swapKey = iso ?? 'none';

  // --- Swipe-to-close for mobile (right → left) ---
  const [dragX, setDragX] = useState(0);
  const touchStartRef = useRef<{ x: number; y: number; t: number } | null>(null);
  const draggingRef = useRef(false);

  const resetDrag = () => setDragX(0);

  const handleTouchStart: React.TouchEventHandler<HTMLElement> = (e) => {
    if (!open) return;
    const t = e.touches[0];
    touchStartRef.current = { x: t.clientX, y: t.clientY, t: performance.now() };
    draggingRef.current = true;
  };

  const handleTouchMove: React.TouchEventHandler<HTMLElement> = (e) => {
    if (!open || !draggingRef.current || !touchStartRef.current) return;
    const t = e.touches[0];
    const dx = t.clientX - touchStartRef.current.x;
    const dy = t.clientY - touchStartRef.current.y;
    if (Math.abs(dx) > Math.abs(dy)) {
      setDragX(Math.max(-100, Math.min(0, dx)));
    }
  };

  const handleTouchEnd: React.TouchEventHandler<HTMLElement> = () => {
    if (!open || !draggingRef.current || !touchStartRef.current) {
      resetDrag();
      return;
    }
    draggingRef.current = false;
    touchStartRef.current = null;
    if (dragX <= -60) onClose();
    resetDrag();
  };

  return (
    <>
      {/* Click anywhere off the panel to close */}
      <div className={`backdrop ${open ? 'open' : ''}`} onClick={onClose} aria-hidden={!open} />

      <aside
        role="dialog"
        aria-modal="true"
        aria-hidden={!open}
        aria-label="Country risk details"
        className={`sidebar-risk ${open ? 'open' : 'closed'}`}
        style={
          {
            ['--revealMs' as string]: `${durationMs}ms`,
            ['--fadeMs' as string]: `${fadeMs}ms`,
            ['--easing' as string]: easing,
            ['--dragX' as string]: `${dragX}px`,
          } as React.CSSProperties
        }
        onTouchStart={handleTouchStart}
        onTouchMove={handleTouchMove}
        onTouchEnd={handleTouchEnd}
        onTouchCancel={() => {
          draggingRef.current = false;
          resetDrag();
        }}
      >
        <header className="bar">
          <button className="risk-close" aria-label="Close details" title="Close" onClick={onClose}>
            ✕
          </button>
          <div className={`titleRow ${isSwitch ? 'swap-anim' : ''}`} key={`title-${swapKey}`}>
            {flagSrc && (
              <span className="flagBox" aria-hidden="true">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  className="flag"
                  src={flagSrc}
                  alt=""
                  loading="eager"
                  // A country with no bundled flag hides the image rather than
                  // showing a broken-image icon, so adding one to the backend
                  // roster never breaks this view.
                  onError={(e) => { e.currentTarget.style.visibility = 'hidden'; }}
                />
              </span>
            )}
            <strong className="countryName">{country?.name ?? '—'}</strong>

            {country?.nonInvestable && (
              // Legal status, not a risk reading: a sanctions regime makes
              // securities exposure unlawful for US persons. The score beside it
              // is the analyst assessment and is deliberately not adjusted for
              // this — see backend policy_version p2.0.
              <span
                className="restrictedBadge"
                title="Sanctions make securities exposure unlawful for US persons. The risk score is not adjusted for this."
              >
                Restricted
              </span>
            )}

            {typeof daysOld === 'number' && (
              <span className="dataTracker" title={ageTitle} aria-label={ageTitle}>
                <span className="ageBox">Updated {daysOld}d ago</span>
              </span>
            )}
          </div>
        </header>

        <div className="header-divider" role="separator" aria-hidden="true" />

        <div className="content">
          {!country ? (
            <p className="muted">Click a country marker to see details.</p>
          ) : (
            <div className={`content-swap ${isSwitch ? 'swap-anim' : ''}`} key={swapKey}>
              <section className="card">
                <RiskReadingSection
                  countryName={country?.name}
                  iso2={country?.iso2}
                  currentRisk={country?.risk}
                  prevRisk={country?.prevRisk}
                  active={open}
                />

                <div className="economicSection">
                  <div className="econTitle">Economic Indicators</div>
                  <EconomicGaugeSection
                    countryName={country?.name}
                    iso2={country?.iso2}
                    active={open}
                  />
                </div>
              </section>

              <section className="card aiCard">
                <h3>AI Summary</h3>
                <AiSummary iso2={country?.iso2} active={open} />
              </section>

              <section className="card">
                <h3>News</h3>
                <NewsArticleSection iso2={country?.iso2} active={open} />
              </section>
            </div>
          )}
        </div>
      </aside>

      <style jsx>{`
        :global(*) {
          box-sizing: border-box;
        }

        .backdrop {
          position: absolute;
          top: var(--top-h);
          left: 0;
          right: 0;
          bottom: 0;
          background: rgba(0, 0, 0, 0);
          opacity: 0;
          transition: opacity var(--fadeMs, 220ms) var(--easing, ease);
          pointer-events: none;
          z-index: 8;
        }
        .backdrop.open {
          background: rgba(0, 0, 0, 0);
          opacity: 1;
          pointer-events: auto;
        }

        .sidebar-risk {
          position: absolute;
          top: var(--top-h);
          bottom: var(--bottom-h);
          left: 0;
          width: var(--w-risk);
          background: var(--term-bg);
          color: #e7e3d6;
          border-right: 1px solid var(--amber-border);
          box-shadow: 12px 0 30px rgba(0, 0, 0, 0.5);
          z-index: 10;
          display: flex;
          flex-direction: column;
          font-family: var(--term-font);
          will-change: clip-path, opacity, transform;
          transition: clip-path var(--revealMs, 360ms) var(--easing, ease),
            opacity var(--fadeMs, 220ms) var(--easing, ease), transform 140ms ease-out;
          container-type: inline-size;
          touch-action: pan-y;
          overscroll-behavior-x: contain;
          transform: translateX(var(--dragX, 0));
        }
        .sidebar-risk.closed {
          clip-path: inset(0 100% 0 0);
          opacity: 0;
          pointer-events: none;
        }
        .sidebar-risk.open {
          clip-path: inset(0 0 0 0);
          opacity: 1;
          pointer-events: auto;
        }

        @media (max-width: 768px) {
          .sidebar-risk {
            width: 100vw;
          }
        }

        .risk-close {
          position: absolute;
          top: 50%;
          transform: translateY(-50%);
          right: 12px;
          width: 28px;
          height: 28px;
          border-radius: 6px;
          border: 1px solid rgba(255, 180, 60, 0.3);
          background: rgba(255, 180, 60, 0.08);
          color: var(--amber);
          cursor: pointer;
          font-size: 14px;
          line-height: 1;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          z-index: 3;
          transition: background 120ms var(--easing, ease);
        }
        .risk-close:hover {
          background: rgba(255, 180, 60, 0.16);
        }

        .bar {
          position: relative;
          display: flex;
          align-items: center;
          padding: 12px 44px 10px 16px;
          flex: 0 0 auto;
        }
        .titleRow {
          display: flex;
          align-items: center;
          gap: 10px;
          min-width: 0;
          width: 100%;
          font-size: clamp(18px, 2.1cqw, 28px);
          line-height: 1;
          padding: 0.4em 0;
        }
        .countryName {
          font-size: 1.15em;
          line-height: 1.05;
          font-weight: 800;
          letter-spacing: 0.04em;
          text-transform: uppercase;
          color: var(--amber);
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
          flex: 1 1 auto;
          min-width: 0;
        }
        .flagBox {
          width: 2.1em;
          height: 1.25em;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          border-radius: 2px;
          overflow: hidden;
          background: rgba(255, 180, 60, 0.06);
          box-shadow: inset 0 0 0 1px var(--rule);
          flex: 0 0 auto;
          margin-inline-end: 0.3em;
        }
        .flag {
          width: 100%;
          height: 100%;
          object-fit: cover;
          display: block;
        }
        .restrictedBadge {
          display: inline-flex;
          align-items: center;
          flex: 0 0 auto;
          margin-inline-start: 0.6em;
          padding: 0.3em 0.55em;
          border-radius: 3px;
          background: var(--crit);
          color: #fff;
          font-size: 0.62em;
          font-weight: 800;
          line-height: 1;
          letter-spacing: 0.07em;
          text-transform: uppercase;
          white-space: nowrap;
        }

        .dataTracker {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          font-size: 0.62em;
          flex: 0 0 auto;
          margin-inline-start: auto;
        }
        .ageBox {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          padding: 0.3em 0.55em;
          border-radius: 3px;
          background: rgba(255, 180, 60, 0.08);
          border: 1px solid var(--rule);
          color: var(--amber-dim);
          font-weight: 600;
          line-height: 1;
          letter-spacing: 0.08em;
          text-transform: uppercase;
          white-space: nowrap;
        }

        .header-divider {
          background: var(--rule);
          height: 1px;
          width: 100%;
          margin: 0;
        }

        .content {
          padding: 0;
          flex: 1 1 auto;
          min-height: 0;
          display: flex;
          flex-direction: column;
          overflow-y: auto;
          scrollbar-width: none;
          -ms-overflow-style: none;
        }
        .content::-webkit-scrollbar {
          width: 0;
          height: 0;
          display: none;
        }
        .muted {
          color: var(--amber-dim);
        }

        /* Wraps the section stack so a country switch can replay an entrance
           animation. Mirrors .content's column layout so the AI card still
           fills the leftover vertical space. */
        .content-swap {
          flex: 1 1 auto;
          min-height: 0;
          display: flex;
          flex-direction: column;
        }
        .swap-anim {
          animation: swapIn var(--swapMs, 320ms) var(--easing, ease) both;
        }
        @keyframes swapIn {
          from {
            opacity: 0;
            transform: translateY(8px);
          }
          to {
            opacity: 1;
            transform: translateY(0);
          }
        }
        @media (prefers-reduced-motion: reduce) {
          .swap-anim {
            animation: none;
          }
        }

        .card {
          margin: 0;
          padding: 13px 14px;
          border-bottom: 1px solid var(--rule);
          flex: 0 0 auto;
        }
        /* AI Summary fills the leftover vertical space; its text auto-scales
           to fit so the content reaches the panel's bottom edge. */
        .aiCard {
          flex: 1 1 auto;
          /* Floor the card so it can't be squeezed to nothing on short panels:
             below this, the panel scrolls (via .content) instead of the AI
             summary collapsing. On tall panels flex-grow still fills past it. */
          min-height: 168px;
          display: flex;
          flex-direction: column;
        }
        .card :global(h3) {
          margin: 0 0 10px;
          font-size: 11px;
          font-weight: 700;
          letter-spacing: 0.14em;
          text-transform: uppercase;
          color: var(--amber);
        }
        .card :global(p) {
          margin: 0;
          font-size: 12px;
          line-height: 1.55;
          color: #cfcabb;
          text-wrap: pretty;
        }
        .card :global(.muted) {
          color: var(--amber-dim);
        }

        .economicSection {
          margin-top: 0.7em;
          padding: 0;
        }
        .econTitle {
          font-size: 11px;
          letter-spacing: 0.14em;
          text-transform: uppercase;
          font-weight: 700;
          color: var(--amber);
          margin: 0 0 4px;
        }
      `}</style>
    </>
  );
}
