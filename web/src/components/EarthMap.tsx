import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent as RPointerEvent } from 'react';
import type { ContinentId } from '../data/analogs';
import { CONTINENTS, VB, WORLD_VIEW, continentView, graticulePath, landPath, project, spherePath, type View } from '../lib/geo';

const MAX_K = 40;
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/** Frame a continent's results tightly so their spread is visible; falls back to the whole continent. */
function fitView(continent: ContinentId, items: readonly MapItem[]): View {
  const base = continentView(continent);
  if (!items.length) return base;
  const pts = items.map((r) => project(r.lon, r.lat));
  const xs = pts.map((p) => p[0]);
  const ys = pts.map((p) => p[1]);
  const w = Math.max(...xs) - Math.min(...xs);
  const h = Math.max(...ys) - Math.min(...ys);
  const k = clamp(Math.min(560 / Math.max(w, 1), 400 / Math.max(h, 1)), base.k, 14);
  const cx = (Math.max(...xs) + Math.min(...xs)) / 2;
  const cy = (Math.max(...ys) + Math.min(...ys)) / 2;
  return { k, tx: 540 - k * cx, ty: 440 - k * cy };
}

/** What the map needs per result. Sample analogs (Ranked) and stage 2 cells both fit. `similarity` is 0..100. */
export interface MapItem {
  id: string;
  name: string;
  region: string;
  lon: number;
  lat: number;
  similarity: number;
  continent: ContinentId | null;
}

interface Props {
  ranked: readonly MapItem[];
  continent: ContinentId | null;
  selectedId: string | null;
  onContinent: (id: ContinentId | null) => void;
  onPick: (id: string) => void;
}

/** Ring around a pin: the filled arc is the similarity (0-100%), so rank and strength are both visible. */
function SimilarityRing({ r, value }: { r: number; value: number }) {
  const pct = Math.max(0, Math.min(100, value));
  return (
    <>
      <circle r={r} className="pin-ring-bg" />
      <circle r={r} className="pin-ring" pathLength={100} strokeDasharray={`${pct} 100`} transform="rotate(-90)" />
    </>
  );
}

const onKey = (fn: () => void) => (e: KeyboardEvent) => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    fn();
  }
};

export function EarthMap({ ranked, continent, selectedId, onContinent, onPick }: Props) {
  const top3 = new Set(ranked.slice(0, 3).map((r) => r.id));
  const inContinent = continent ? ranked.filter((r) => r.continent === continent) : [];
  const base = continent ? fitView(continent, inContinent) : WORLD_VIEW;

  // User zoom on top of the base view: wheel/pinch zooms around the cursor, drag pans, double-click resets.
  const svgRef = useRef<SVGSVGElement>(null);
  const [user, setUser] = useState<View | null>(null);
  const userRef = useRef<View | null>(null);
  const baseRef = useRef(base);
  baseRef.current = base;
  const setZoom = (v: View | null) => {
    userRef.current = v;
    setUser(v);
  };
  const resetKey = `${continent}|${ranked.map((r) => r.id).join(',')}`;
  useEffect(() => setZoom(null), [resetKey]);
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (ev: WheelEvent) => {
      ev.preventDefault();
      const ctm = svg.getScreenCTM();
      if (!ctm) return;
      const p = new DOMPoint(ev.clientX, ev.clientY).matrixTransform(ctm.inverse());
      const cur = userRef.current ?? baseRef.current;
      const dy = clamp(ev.deltaY * (ev.deltaMode === 1 ? 16 : 1), -120, 120);
      const k = clamp(cur.k * Math.exp(-dy * (ev.ctrlKey ? 0.01 : 0.0025)), baseRef.current.k, MAX_K);
      if (k === cur.k) return;
      if (k <= baseRef.current.k + 1e-6) return setZoom(null);
      const s = k / cur.k;
      setZoom({ k, tx: p.x - (p.x - cur.tx) * s, ty: p.y - (p.y - cur.ty) * s });
    };
    svg.addEventListener('wheel', onWheel, { passive: false });
    return () => svg.removeEventListener('wheel', onWheel);
  }, []);
  const drag = useRef<{ x: number; y: number; v: View; moved: boolean } | null>(null);
  const suppress = useRef(false);
  const onDown = (ev: RPointerEvent<SVGSVGElement>) => {
    suppress.current = false;
    if (ev.button === 0 && userRef.current) drag.current = { x: ev.clientX, y: ev.clientY, v: userRef.current, moved: false };
  };
  const onMove = (ev: RPointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    if (!d) return;
    const ctm = ev.currentTarget.getScreenCTM();
    if (!ctm) return;
    const dx = (ev.clientX - d.x) / ctm.a;
    const dy = (ev.clientY - d.y) / ctm.d;
    if (!d.moved && Math.hypot(dx, dy) < 3) return;
    d.moved = true;
    setZoom({ ...d.v, tx: d.v.tx + dx, ty: d.v.ty + dy });
  };
  const onUp = () => {
    if (drag.current?.moved) suppress.current = true;
    drag.current = null;
  };
  const view = user ?? base;
  // Global rank, also in the continent view: no renumbering within a continent.
  const rankIn = new Map(ranked.map((r, i) => [r.id, i + 1]));
  const worldStyle = { transform: `translate(${view.tx}px, ${view.ty}px) scale(${view.k})` };

  // Keep the big top-3 pins from stacking on each other in the world view. Each is nudged apart,
  // and a leader line (below) points back to its true location.
  const nudged = new Map<string, [number, number]>();
  if (!continent) {
    const pts = ranked.slice(0, 3).map((r) => ({ id: r.id, p: project(r.lon, r.lat) as [number, number] }));
    const MIN = 60; // minimum centre distance, in screen units (pin 40 + ring 6 + gap)
    for (let pass = 0; pass < 8; pass++) {
      for (let i = 0; i < pts.length; i++) {
        for (let j = i + 1; j < pts.length; j++) {
          const dx = (pts[j].p[0] - pts[i].p[0]) * view.k;
          const dy = (pts[j].p[1] - pts[i].p[1]) * view.k;
          const d = Math.hypot(dx, dy);
          if (d < MIN) {
            const ux = d > 0 ? dx / d : 1;
            const uy = d > 0 ? dy / d : 0;
            const push = (MIN - d) / 2 / view.k;
            pts[i].p = [pts[i].p[0] - ux * push, pts[i].p[1] - uy * push];
            pts[j].p = [pts[j].p[0] + ux * push, pts[j].p[1] + uy * push];
          }
        }
      }
    }
    pts.forEach((o) => nudged.set(o.id, o.p));
  }

  const pinStyle = (lon: number, lat: number, id?: string) => {
    const [x, y] = (id && nudged.get(id)) || project(lon, lat);
    return { transform: `translate(${x}px, ${y}px) scale(${1 / view.k})` };
  };

  return (
    <svg
      ref={svgRef}
      className={`map earth-map${user ? ' is-user-zoom' : ''}`}
      viewBox={`0 0 ${VB.w} ${VB.h}`}
      preserveAspectRatio="xMidYMid meet"
      role="group"
      aria-label="World map of Earth analog sites"
      onPointerDown={onDown}
      onPointerMove={onMove}
      onPointerUp={onUp}
      onPointerLeave={onUp}
      onClickCapture={(ev) => {
        if (!suppress.current) return;
        suppress.current = false;
        ev.stopPropagation();
      }}
      onDoubleClick={() => setZoom(null)}
    >
      <defs>
        {CONTINENTS.map((c) => (
          <clipPath id={`clip-${c.id}`} key={c.id}>
            <path d={c.path} />
          </clipPath>
        ))}
      </defs>
      <g className="world" style={worldStyle}>
        <path className="sphere" d={spherePath} />
        <path className="graticule" d={graticulePath} />
        <path className={`land${continent ? ' land-dim' : ''}`} d={landPath} />
        {continent && (
          <g clipPath={`url(#clip-${continent})`}>
            <path className="land land-on" d={landPath} />
          </g>
        )}
        {CONTINENTS.map((c) => (
          <path
            key={c.id}
            d={c.path}
            className={`continent-hit${c.id === continent ? ' is-on' : ''}`}
            role="button"
            tabIndex={0}
            aria-label={`Explore ${c.name}`}
            aria-pressed={c.id === continent}
            onClick={() => onContinent(c.id === continent ? null : c.id)}
            onKeyDown={onKey(() => onContinent(c.id === continent ? null : c.id))}
          />
        ))}
        {CONTINENTS.map((c) => (
          <text key={c.id + 't'} className={`continent-label${c.id === continent ? ' is-on' : ''}`} style={{ transform: `translate(${c.labelAt[0]}px, ${c.labelAt[1]}px) scale(${1 / view.k})` }} textAnchor="middle">
            {c.name.toUpperCase()}
          </text>
        ))}

        {/* Global view: small dots for every candidate, big pins for the top 3 */}
        {!continent &&
          ranked
            .filter((r) => !top3.has(r.id))
            .map((r) => (
              <g key={r.id} className="pin pin-small" style={pinStyle(r.lon, r.lat)} role="button" tabIndex={0} aria-label={`${r.name}, ${r.region}`} onClick={() => onPick(r.id)} onKeyDown={onKey(() => onPick(r.id))}>
                <circle r="12" fill="transparent" />
                <circle r="4.5" className="pin-small-dot" />
                <title>{`${r.name} · ${Math.round(r.similarity)}%`}</title>
              </g>
            ))}
        {!continent &&
          ranked
            .slice(0, 3)
            .map((r, i) => ({ r, rank: i + 1 }))
            .reverse() /* rank 1 paints on top */
            .map(({ r, rank }) => {
              const [tx, ty] = project(r.lon, r.lat);
              const n = nudged.get(r.id);
              const moved = n ? Math.hypot((n[0] - tx) * view.k, (n[1] - ty) * view.k) > 2 : false;
              return (
                <g key={r.id}>
                  {moved && n && (
                    <>
                      <line className="pin-leader" x1={tx} y1={ty} x2={n[0]} y2={n[1]} />
                      <circle className="pin-origin" cx={tx} cy={ty} r={3 / view.k} />
                    </>
                  )}
                  <g className="pin pin-top" style={pinStyle(r.lon, r.lat, r.id)} role="button" tabIndex={0} aria-label={`Rank ${rank}: ${r.name}, ${r.region}`} onClick={() => onPick(r.id)} onKeyDown={onKey(() => onPick(r.id))}>
                    <g className="pin-inner" style={{ '--d': `${rank * 90}ms` } as CSSProperties}>
                      <SimilarityRing r={26} value={r.similarity} />
                      <circle r="20" className="pin-dot" />
                      <text textAnchor="middle" dy="0.35em" className="pin-num">
                        {rank}
                      </text>
                    </g>
                  </g>
                </g>
              );
            })}

        {/* Continent view: numbered pins showing each site's global rank */}
        {continent &&
          // Reverse so rank 1 is drawn last, on top of lower-ranked pins.
          [...inContinent].reverse().sort((a, b) => Number(a.id === selectedId) - Number(b.id === selectedId)).map((r) => {
            const sel = r.id === selectedId;
            return (
              <g key={r.id} className={`pin pin-top${sel ? ' pin-selected' : ''}`} style={pinStyle(r.lon, r.lat)} role="button" tabIndex={0} aria-label={`Global rank ${rankIn.get(r.id)}: ${r.name}`} aria-pressed={sel} onClick={() => onPick(r.id)} onKeyDown={onKey(() => onPick(r.id))}>
                <SimilarityRing r={(sel ? 24 : 17) + 6} value={r.similarity} />
                <circle r={sel ? 24 : 17} className="pin-dot" />
                <text textAnchor="middle" dy="0.35em" className="pin-num">
                  {rankIn.get(r.id)}
                </text>
              </g>
            );
          })}
      </g>
    </svg>
  );
}
