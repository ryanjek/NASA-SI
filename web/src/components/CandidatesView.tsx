import { useMemo, useState } from 'react';
import { VB, WORLD_VIEW, graticulePath, landPath, project, spherePath } from '../lib/geo';
import data from '../data/candidates.json';
import { SectionTitle } from './ui';
import { seedLabel } from '../lib/pipeline';

/** Stage-2 output: fine (0.8 km) cells ranked by similarity to each Phase 1 seed, with their measured features. */
interface Candidate {
  seed: string;
  rank: number;
  fineCellId: number;
  patchId: number;
  lat: number;
  lon: number;
  distance: number | null;
  similarity: number | null;
  featuresMatched: number;
  matched: string[];
  features: Record<string, number | null>;
  opticalError: boolean;
}
const CANDIDATES = data.candidates as Candidate[];

// Every seed in the ranking file, in file order, with a name from lib/pipeline and a stable colour.
const PALETTE = ['#72c8ea', '#b79cf2', '#e5b13a', '#5bd6a3', '#f08a5a', '#e57ab5', '#9db4ff', '#c9d66b', '#6fd3c7', '#f2c14e', '#a2aabc', '#ff9e7a', '#8fd18f', '#d7a6ff', '#7fb3ff'];
const SEEDS: { id: string; label: string; color: string }[] = [...new Set((data.candidates as { seed: string }[]).map((c) => c.seed))].map((id, i) => ({ id, label: seedLabel(id), color: PALETTE[i % PALETTE.length] }));
const colorOf = (seed: string) => SEEDS.find((s) => s.id === seed)?.color ?? '#999';

export const FEATURES: { id: string; label: string; unit: string; digits: number }[] = [
  { id: 'lst_summer_max_c', label: 'Summer max surface temp', unit: '°C', digits: 1 },
  { id: 'lst_winter_min_c', label: 'Winter min surface temp', unit: '°C', digits: 1 },
  { id: 'air_temp_days_above_0c', label: 'Days above 0 °C', unit: 'days/yr', digits: 0 },
  { id: 'precip_mm_yr', label: 'Precipitation', unit: 'mm/yr', digits: 0 },
  { id: 'humidity_mean_pct', label: 'Relative humidity', unit: '%', digits: 0 },
  { id: 'snow_persistence', label: 'Snow persistence', unit: '', digits: 2 },
  { id: 'elev_m', label: 'Elevation', unit: 'm', digits: 0 },
  { id: 'slope_median_deg', label: 'Median slope', unit: '°', digits: 1 },
  { id: 'slope_p95_deg', label: '95th-percentile slope', unit: '°', digits: 1 },
  { id: 'relief_1km_m', label: 'Relief (1 km)', unit: 'm', digits: 0 },
  { id: 'tri_100m', label: 'Ruggedness (100 m)', unit: 'm', digits: 1 },
  { id: 'ndvi_summer_median', label: 'Summer NDVI', unit: '', digits: 2 },
  { id: 'bare_fraction', label: 'Bare-ground fraction', unit: '', digits: 2 },
  { id: 'impact_distance_km', label: 'Distance to impact crater', unit: 'km', digits: 0 },
];

const key = (c: Candidate) => `${c.seed}:${c.rank}`;

export function CandidatesView() {
  const [seed, setSeed] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const shown = useMemo(() => CANDIDATES.filter((c) => !seed || c.seed === seed).sort((a, b) => a.seed.localeCompare(b.seed) || a.rank - b.rank), [seed]);
  const active = CANDIDATES.find((c) => key(c) === selected) ?? shown[0];
  const view = WORLD_VIEW;
  const countFor = (id: string) => CANDIDATES.filter((c) => c.seed === id).length;

  return (
    <div className="stage earth-stage">
      <div className="earth-canvas">
        <svg className="map earth-map" viewBox={`0 0 ${VB.w} ${VB.h}`} preserveAspectRatio="xMidYMid meet" role="group" aria-label="World map of stage 2 candidate cells">
          <g className="world" style={{ transform: `translate(${view.tx}px, ${view.ty}px) scale(${view.k})` }}>
            <path className="sphere" d={spherePath} />
            <path className="graticule" d={graticulePath} />
            <path className="land" d={landPath} />
            {shown.map((c) => {
              const [x, y] = project(c.lon, c.lat);
              const sel = active && key(c) === key(active);
              return (
                <g key={key(c)} className="pin" style={{ transform: `translate(${x}px, ${y}px) scale(${1 / view.k})` }} role="button" tabIndex={0} aria-label={`${c.seed} rank ${c.rank}`} onClick={() => setSelected(key(c))} onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && setSelected(key(c))}>
                  <circle r="14" fill="transparent" />
                  <circle r={sel ? 9 : 5.5} fill={colorOf(c.seed)} stroke={sel ? '#fff' : 'rgba(0,0,0,.55)'} strokeWidth={sel ? 2.5 : 1} />
                  <title>{`${SEEDS.find((s) => s.id === c.seed)?.label} · rank ${c.rank}`}</title>
                </g>
              );
            })}
          </g>
        </svg>
        <div className="overlay top-left">
          <section className="context-card" aria-labelledby="cand-title">
            <SectionTitle id="cand-title">Stage 2 candidates</SectionTitle>
            <p className="faint">{CANDIDATES.length} fine cells (0.8 km), ranked within each seed. Scores are not comparable across seeds.</p>
            <div className="seed-chips">
              {SEEDS.map((s) => (
                <button key={s.id} type="button" className={`chip chip-button${seed === s.id ? ' chip-mars' : ''}`} aria-pressed={seed === s.id} onClick={() => setSeed(seed === s.id ? null : s.id)}>
                  <span className="swatch" style={{ background: s.color }} aria-hidden="true" /> {s.label}
                  <span className="count">{countFor(s.id)}</span>
                </button>
              ))}
            </div>
          </section>
        </div>
      </div>

      <aside className="panel" aria-label="Stage 2 candidate details">
        {active && (
          <>
            <header className="panel-head">
              <h2>
                {SEEDS.find((s) => s.id === active.seed)?.label} · rank {active.rank}
              </h2>
              <p>
                {active.lat.toFixed(3)}°, {active.lon.toFixed(3)}° · patch {active.patchId} · cell {active.fineCellId}
              </p>
            </header>
            <p className="cand-score">
              <strong>{active.similarity?.toFixed(3) ?? '–'}</strong> similarity · distance {active.distance?.toFixed(2) ?? '–'} · {active.featuresMatched} features compared
            </p>
            {active.opticalError && <p className="empty">No usable optical scene, so NDVI and bare-ground are missing for this cell.</p>}
            <table className="feature-table">
              <tbody>
                {FEATURES.map((f) => {
                  const v = active.features[f.id];
                  return (
                    <tr key={f.id} className={v == null ? 'is-missing' : undefined}>
                      <th scope="row">{f.label}</th>
                      <td>{v == null ? 'missing' : `${v.toFixed(f.digits)} ${f.unit}`.trim()}</td>
                      <td className="faint">{active.matched.includes(f.id) ? 'compared' : ''}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </>
        )}
        <SectionTitle>Ranked cells</SectionTitle>
        <ol className="cand-list">
          {shown.map((c) => (
            <li key={key(c)}>
              <button type="button" className={`plain-button cand-row${active && key(c) === key(active) ? ' is-selected' : ''}`} onClick={() => setSelected(key(c))}>
                <span className="swatch" style={{ background: colorOf(c.seed) }} aria-hidden="true" />
                <span>
                  {SEEDS.find((s) => s.id === c.seed)?.label} #{c.rank}
                </span>
                <span className="faint">{c.similarity?.toFixed(3)}</span>
              </button>
            </li>
          ))}
        </ol>
      </aside>
    </div>
  );
}
