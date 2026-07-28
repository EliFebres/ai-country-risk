'use client';

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import Map, { type MapApi } from './Map';
import Masthead from './Masthead';
import MapFullscreenButton from './MapFullscreenButton';
import WorldRiskIndexRail from './WorldRiskIndexRail';
import BottomBar from './bottombar/BottomBar';
import RiskSidebar from './RiskSidebar';
import { useIdleTour } from '../lib/useIdleTour';
import type { CountryRisk } from '../lib/risk-client';

const LS_KEY = 'crd_bottom_min';
const LS_IDLE_KEY = 'crd_idle_tour';

export type SelectOpts = { pan?: boolean; panDuration?: number };

// Idle auto-tour pans slower than a manual click for a calmer, ambient feel.
const IDLE_PAN_DURATION = 3000;
// With the explicit toggle replacing the old 90s timer, the tour only needs a
// short settle after the last interaction before it (re)starts cycling.
const IDLE_START_DELAY = 5000;

/**
 * Root client dashboard: owns country-selection state and wires together the
 * map, risk sidebar, World Risk Index rail, and bottom bar, plus the idle
 * auto-tour that cycles countries after inactivity.
 */
export default function TerminalDashboard() {
  const mapRef = useRef<MapApi>(null);

  const [selected, setSelected] = useState<CountryRisk | null>(null);
  const [riskRows, setRiskRows] = useState<CountryRisk[] | null>(null);
  const [dataTimestamp, setDataTimestamp] = useState<Date | string | number | null>(null);
  const [bottomMinimized, setBottomMinimized] = useState(false);
  const [idleEnabled, setIdleEnabled] = useState(true);
  // Bottom-bar height fit: the rail reports the height that makes the bar's top
  // meet the end of its content (the "Improving" table). Null → CSS fallback.
  const [barBaseH, setBarBaseH] = useState<number | null>(null);
  const handleRailMeasure = useCallback((h: number | null) => setBarBaseH(h), []);

  // Single shared selection entry point for markers, rail movers, and alerts.
  const selectCountry = useCallback((dot: CountryRisk | null, opts?: SelectOpts) => {
    setSelected(dot);
    if (dot) {
      if (opts?.pan !== false) mapRef.current?.panTo(dot.lngLat, { duration: opts?.panDuration });
    } else {
      mapRef.current?.resetZoom();
    }
  }, []);

  const handleData = useCallback(
    (rows: CountryRisk[], timestamp: Date | string | number | null) => {
      setRiskRows(rows);
      setDataTimestamp(timestamp);
    },
    []
  );

  // Newest as_of across all countries = most recent risk_snapshot date. Drives
  // the masthead's "Data As Of" so it reflects when the data was generated
  // rather than when the client fetched it. ISO 'YYYY-MM-DD' sorts lexically.
  const latestAsOf = useMemo(
    () =>
      riskRows?.reduce<string | null>(
        (max, r) => (r.asOf && (!max || r.asOf > max) ? r.asOf : max),
        null
      ) ?? null,
    [riskRows]
  );

  // Idle auto-tour: when armed via the masthead toggle, cycle through random
  // countries every ~40s once interaction has settled. Any real interaction
  // pauses it and resets the short idle clock; flipping the toggle off stops it.
  useIdleTour({
    rows: riskRows,
    currentName: selected?.name,
    onPick: (c) => selectCountry(c, { pan: true, panDuration: IDLE_PAN_DURATION }),
    startDelayMs: IDLE_START_DELAY,
    intervalMs: 40000,
    enabled: idleEnabled,
  });

  // Restore the persisted full-screen + idle-tour state on mount (client-only).
  useEffect(() => {
    try {
      if (localStorage.getItem(LS_KEY) === '1') setBottomMinimized(true);
      if (localStorage.getItem(LS_IDLE_KEY) === '0') setIdleEnabled(false);
    } catch {
      /* ignore */
    }
  }, []);

  // Persist the idle-tour toggle.
  useEffect(() => {
    try {
      localStorage.setItem(LS_IDLE_KEY, idleEnabled ? '1' : '0');
    } catch {
      /* ignore */
    }
  }, [idleEnabled]);

  // Persist + resize the map after the chrome transition settles.
  const firstRun = useRef(true);
  useEffect(() => {
    try {
      localStorage.setItem(LS_KEY, bottomMinimized ? '1' : '0');
    } catch {
      /* ignore */
    }
    if (firstRun.current) {
      firstRun.current = false;
      return;
    }
    const t = setTimeout(() => mapRef.current?.resize(), 320);
    return () => clearTimeout(t);
  }, [bottomMinimized]);

  // When the rail reports a fit height, drive both the bar height (--bottom-h-base)
  // and the side panels' bottom anchor (--bottom-h) so they meet exactly. --bottom-h
  // can't just inherit from the :root `var(--bottom-h-base)` indirection — that
  // resolves against :root, not this inline override — so set it explicitly.
  const appStyle: CSSProperties | undefined =
    barBaseH != null
      ? ({
          '--bottom-h-base': `${barBaseH}px`,
          '--bottom-h': bottomMinimized ? '0px' : `${barBaseH}px`,
        } as CSSProperties)
      : undefined;

  return (
    <div className={`app ${bottomMinimized ? 'bottom-min' : ''}`} style={appStyle}>
      <Masthead
        coverage={riskRows?.length ?? null}
        dataTimestamp={latestAsOf}
        idleEnabled={idleEnabled}
        onToggleIdle={() => setIdleEnabled((v) => !v)}
      />

      <Map ref={mapRef} onSelectCountry={selectCountry} onData={handleData} />

      <MapFullscreenButton
        minimized={bottomMinimized}
        onToggle={() => setBottomMinimized((v) => !v)}
      />

      <WorldRiskIndexRail rows={riskRows} onSelectCountry={selectCountry} onMeasure={handleRailMeasure} />

      <BottomBar />

      <RiskSidebar
        open={!!selected}
        country={
          selected
            ? {
                name: selected.name,
                risk: selected.risk,
                prevRisk: selected.prevRisk,
                iso2: selected.iso2,
                nonInvestable: selected.nonInvestable,
              }
            : null
        }
        dataTimestamp={dataTimestamp}
        onClose={() => selectCountry(null)}
      />

      <style jsx>{`
        .app {
          position: relative;
          width: 100vw;
          height: 100dvh;
          overflow: hidden;
          background: var(--term-bg);
        }
        .app.bottom-min {
          --bottom-h: 0px;
        }
      `}</style>
    </div>
  );
}
