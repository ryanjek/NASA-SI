import { useMemo, type KeyboardEvent } from 'react';
import type { MarsSite } from '../data/marsSites';

const VBW = 1440;
const VBH = 836;

function rng(seed: number) {
  let s = seed;
  return () => (s = (s * 16807) % 2147483647) / 2147483647;
}

interface Props {
  sites: readonly MarsSite[];
  selectedId: string | null;
  scoreOf: (s: MarsSite) => number;
  onSelect: (id: string) => void;
}

/** Stylised Arcadia Planitia terrain with the screened candidate sites. Purely vector, so it works offline. */
export function MarsMap({ sites, selectedId, scoreOf, onSelect }: Props) {
  const art = useMemo(() => {
    const r = rng(7);
    const tones = ['#7A3A22', '#5A2A1B', '#8C4A2B', '#3C1D13', '#6B3220', '#A15B36'];
    const blobs = Array.from({ length: 16 }, (_, i) => {
      const w = 260 + r() * 420;
      const h = 160 + r() * 300;
      return { cx: r() * VBW, cy: r() * VBH, rx: w / 2, ry: h / 2, fill: tones[i % tones.length], op: 0.22 + r() * 0.3 };
    });
    const contours = ([[620, 420, 5], [1100, 250, 3], [300, 650, 3]] as const).flatMap(([cx, cy, n]) =>
      Array.from({ length: n }, (_, k) => ({ cx, cy, rx: (120 * (k + 1) + 40) / 2, ry: (80 * (k + 1) + 30) / 2 })),
    );
    const craters = Array.from({ length: 22 }, () => ({ cx: r() * VBW, cy: r() * VBH, r: (10 + r() * 34) / 2 }));
    const pts = Array.from({ length: 90 }, (_, i) => {
      const a = (i / 90) * Math.PI * 2;
      const k = 1 + 0.16 * Math.sin(3 * a + 1) + 0.09 * Math.sin(5 * a + 2) + 0.05 * Math.sin(9 * a);
      return [640 + 400 * k * Math.cos(a), 346 + 290 * k * Math.sin(a)] as const;
    });
    const region = 'M' + pts.map(([x, y]) => `${x.toFixed(1)} ${y.toFixed(1)}`).join('L') + 'Z';
    return { blobs, contours, craters, region };
  }, []);

  const key = (id: string) => (e: KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onSelect(id);
    }
  };

  return (
    <svg className="map mars-map" viewBox="160 90 1000 640" preserveAspectRatio="xMidYMid slice" role="group" aria-label="Arcadia Planitia candidate landing sites">
      <defs>
        <radialGradient id="marsBase" cx="0.45" cy="0.5" r="0.85">
          <stop offset="0" stopColor="#5B2B1C" />
          <stop offset="0.6" stopColor="#2E1710" />
          <stop offset="1" stopColor="#170C08" />
        </radialGradient>
        <radialGradient id="suit" cx="0.5" cy="0.5" r="0.5">
          <stop offset="0" stopColor="#5BD6A3" stopOpacity="0.34" />
          <stop offset="1" stopColor="#5BD6A3" stopOpacity="0" />
        </radialGradient>
        <filter id="terrainBlur" filterUnits="userSpaceOnUse" x="-300" y="-300" width="2040" height="1436">
          <feGaussianBlur stdDeviation="55" />
        </filter>
      </defs>
      {/* Everything pans left together when the popup opens, so the sites stay in the free area. */}
      <g className={`mars-world${selectedId ? ' is-shifted' : ''}`}>
      <rect width={VBW} height={VBH} fill="url(#marsBase)" />
      <g filter="url(#terrainBlur)" aria-hidden="true">
        {art.blobs.map((b, i) => (
          <ellipse key={i} cx={b.cx} cy={b.cy} rx={b.rx} ry={b.ry} fill={b.fill} opacity={b.op} />
        ))}
      </g>
      <g aria-hidden="true" fill="none">
        {art.contours.map((c, i) => (
          <ellipse key={i} cx={c.cx} cy={c.cy} rx={c.rx} ry={c.ry} stroke="#ECEFF5" strokeOpacity="0.06" />
        ))}
        {art.craters.map((c, i) => (
          <circle key={i} cx={c.cx} cy={c.cy} r={c.r} stroke="#ECEFF5" strokeOpacity="0.14" strokeWidth="1.2" />
        ))}
        <path d={art.region} fill="#72C8EA" fillOpacity="0.07" stroke="#72C8EA" strokeOpacity="0.85" strokeWidth="1.6" strokeDasharray="7 6" />
      </g>
      <text x="560" y="172" className="map-region-label">
        ARCADIA PLANITIA
      </text>

      {sites.map((s) => (
        <ellipse key={s.id + 'g'} cx={s.pos.x} cy={s.pos.y} rx="95" ry="72" fill="url(#suit)" pointerEvents="none" />
      ))}

      {sites.map((s) => {
        const sel = s.id === selectedId;
        const num = s.id.slice(-1);
        return (
          <g
            key={s.id}
            className={`site${sel ? ' site-selected' : ''}`}
            transform={`translate(${s.pos.x} ${s.pos.y})`}
            role="button"
            tabIndex={0}
            aria-label={`${s.name}, score ${Math.round(scoreOf(s))}${sel ? ', selected' : ''}`}
            aria-pressed={sel}
            onClick={() => onSelect(s.id)}
            onKeyDown={key(s.id)}
          >
            <circle r="30" fill="transparent" />
            {sel && <circle className="site-pulse" r="24" fill="none" />}
            <circle className="site-dot" r={sel ? 20 : 15} />
            <text className="site-num" textAnchor="middle" dy="0.35em">
              {num}
            </text>
            <g transform={`translate(${sel ? 28 : 23} -10)`}>
              <rect className="site-tag-bg" width="78" height="22" rx="8" />
              <text className="site-tag" x="39" y="11" textAnchor="middle" dy="0.35em">
                {s.id} · {Math.round(scoreOf(s))}
              </text>
            </g>
          </g>
        );
      })}
      </g>
    </svg>
  );
}

/** Small global-Mars inset with the Arcadia box. */
export function MiniMars() {
  return (
    <figure className="minimap">
      <svg viewBox="0 0 208 128" aria-hidden="true">
        <defs>
          <linearGradient id="miniBase" x1="0" y1="1" x2="1" y2="0">
            <stop offset="0" stopColor="#2A140E" />
            <stop offset="1" stopColor="#6B3220" />
          </linearGradient>
          <filter id="miniBlur">
            <feGaussianBlur stdDeviation="9" />
          </filter>
        </defs>
        <rect width="208" height="128" fill="url(#miniBase)" />
        <g filter="url(#miniBlur)">
          <ellipse cx="150" cy="40" rx="42" ry="22" fill="#A15B36" opacity=".45" />
          <ellipse cx="50" cy="90" rx="46" ry="24" fill="#3C1D13" opacity=".6" />
          <ellipse cx="120" cy="100" rx="40" ry="20" fill="#A15B36" opacity=".35" />
        </g>
        <rect x="40" y="26" width="46" height="30" fill="#72C8EA" fillOpacity=".2" stroke="#72C8EA" strokeWidth="1.5" />
      </svg>
      <figcaption>MARS · GLOBAL</figcaption>
    </figure>
  );
}
