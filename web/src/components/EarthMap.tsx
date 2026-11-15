import type { CSSProperties, KeyboardEvent } from 'react';
import type { ContinentId } from '../data/analogs';
import { CONTINENTS, VB, WORLD_VIEW, continentView, graticulePath, landPath, project, spherePath } from '../lib/geo';
import type { Ranked } from '../lib/ranking';

interface Props {
  ranked: Ranked[];
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
  const view = continent ? continentView(continent) : WORLD_VIEW;
  const top3 = new Set(ranked.slice(0, 3).map((r) => r.id));
  const inContinent = continent ? ranked.filter((r) => r.continent === continent) : [];
  const rankIn = new Map(inContinent.map((r, i) => [r.id, i + 1]));
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
    <svg className="map earth-map" viewBox={`0 0 ${VB.w} ${VB.h}`} preserveAspectRatio="xMidYMid meet" role="group" aria-label="World map of Earth analog sites">
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

        {/* Continent view: numbered pins ranked within the continent */}
        {continent &&
          inContinent.map((r) => {
            const sel = r.id === selectedId;
            return (
              <g key={r.id} className={`pin pin-top${sel ? ' pin-selected' : ''}`} style={pinStyle(r.lon, r.lat)} role="button" tabIndex={0} aria-label={`Rank ${rankIn.get(r.id)} in ${continent}: ${r.name}`} aria-pressed={sel} onClick={() => onPick(r.id)} onKeyDown={onKey(() => onPick(r.id))}>
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
