import { useEffect, useMemo, useState, type KeyboardEvent } from 'react';
import type { ContinentId } from '../data/analogs';
import { markerLabel, type MarsSite } from '../data/marsSites';
import { CONTINENTS, VB, WORLD_VIEW, continentOf, continentView, graticulePath, landPath, project, spherePath } from '../lib/geo';
import { loadSurvivors, type Results, type Stage1Group, type Stage2Cell, type Stage2Group } from '../lib/pipeline';
import { plural } from '../lib/format';
import { FEATURES } from './CandidatesView';
import { SectionTitle } from './ui';

const SEED_COLORS = ['#72c8ea', '#e5b13a', '#b79cf2', '#5bd6a3'];
type Counts = Partial<Record<ContinentId | 'other', number>>;

const onKey = (fn: () => void) => (e: KeyboardEvent) => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    fn();
  }
};
const countBy = (pts: [number, number][]): Counts => {
  const out: Counts = {};
  for (const [lon, lat] of pts) {
    const c = continentOf(lon, lat) ?? 'other';
    out[c] = (out[c] ?? 0) + 1;
  }
  return out;
};
const coord = (lat: number, lon: number) => `${Math.abs(lat).toFixed(2)}°${lat < 0 ? 'S' : 'N'}, ${Math.abs(lon).toFixed(2)}°${lon < 0 ? 'W' : 'E'}`;

interface Props {
  site: MarsSite;
  results: Exclude<Results, { tier: 'demo' }>;
  onEditSite: () => void;
}

/** Earth results backed by pipeline output: ranked stage 2 cells, or unranked stage 1 survivors. Never sample data. */
export function PipelineEarthView({ site, results, onEditSite }: Props) {
  const [continent, setContinent] = useState<ContinentId | null>(null);
  const view = continent ? continentView(continent) : WORLD_VIEW;
  const worldStyle = { transform: `translate(${view.tx}px, ${view.ty}px) scale(${view.k})` };
  const pinStyle = (lon: number, lat: number) => {
    const [x, y] = project(lon, lat);
    return { transform: `translate(${x}px, ${y}px) scale(${1 / view.k})` };
  };

  const stage2 = results.tier === 'stage2' ? results.groups : null;
  const stage1 = results.tier === 'stage1' ? results.groups : null;

  // ----- stage 2 state -----
  const [seed, setSeed] = useState<string>(results.groups[0].seed);
  const [selected, setSelected] = useState<number | null>(null);
  const group: Stage2Group | undefined = stage2?.find((g) => g.seed === seed) ?? stage2?.[0];
  const inView = (c: Stage2Cell) => !continent || continentOf(c.lon, c.lat) === continent;
  const shownCells = group ? group.cells.filter(inView) : [];
  const active = group?.cells.find((c) => c.rank === selected && inView(c)) ?? shownCells[0];

  // ----- stage 1 state -----
  const [points, setPoints] = useState<Record<string, number[]> | null>(null);
  const [off, setOff] = useState<Set<string>>(new Set());
  useEffect(() => {
    if (!stage1) return;
    let live = true;
    loadSurvivors().then((p) => live && setPoints(p));
    return () => {
      live = false;
    };
  }, [stage1]);
  const survivors = useMemo(() => {
    if (!stage1 || !points) return null;
    return stage1.map((g: Stage1Group, i) => {
      const flat = points[g.seed] ?? [];
      const pts: [number, number][] = [];
      for (let j = 0; j < flat.length; j += 2) pts.push([flat[j], flat[j + 1]]);
      return { ...g, color: SEED_COLORS[i % SEED_COLORS.length], pts, counts: countBy(pts) };
    });
  }, [stage1, points]);

  // Continent bar counts: stage 2 = active seed's cells; stage 1 = distinct patches across the seeds that are on.
  const counts: Counts = useMemo(() => {
    if (group) return countBy(group.cells.map((c) => [c.lon, c.lat]));
    if (!survivors) return {};
    const seen = new Map<string, [number, number]>();
    for (const s of survivors) if (!off.has(s.seed)) for (const p of s.pts) seen.set(`${p[0]},${p[1]}`, p);
    return countBy([...seen.values()]);
  }, [group, survivors, off]);

  const continentName = CONTINENTS.find((c) => c.id === continent)?.name;
  const seedColor = (s: string) => SEED_COLORS[Math.max(0, results.groups.findIndex((g) => g.seed === s)) % SEED_COLORS.length];

  return (
    <div className="stage earth-stage">
      <div className="earth-canvas">
        <svg className="map earth-map" viewBox={`0 0 ${VB.w} ${VB.h}`} preserveAspectRatio="xMidYMid meet" role="group" aria-label="World map of pipeline results">
          <defs>
            {CONTINENTS.map((c) => (
              <clipPath id={`pclip-${c.id}`} key={c.id}>
                <path d={c.path} />
              </clipPath>
            ))}
          </defs>
          <g className="world" style={worldStyle}>
            <path className="sphere" d={spherePath} />
            <path className="graticule" d={graticulePath} />
            <path className={`land${continent ? ' land-dim' : ''}`} d={landPath} />
            {continent && (
              <g clipPath={`url(#pclip-${continent})`}>
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
                onClick={() => setContinent(c.id === continent ? null : c.id)}
                onKeyDown={onKey(() => setContinent(c.id === continent ? null : c.id))}
              />
            ))}
            {CONTINENTS.map((c) => (
              <text key={c.id + 't'} className={`continent-label${c.id === continent ? ' is-on' : ''}`} style={{ transform: `translate(${c.labelAt[0]}px, ${c.labelAt[1]}px) scale(${1 / view.k})` }} textAnchor="middle">
                {c.name.toUpperCase()}
              </text>
            ))}

            {/* Stage 1: every surviving ~50 km patch centre as a dot, one path per seed. */}
            {survivors?.map(
              (s) =>
                !off.has(s.seed) && (
                  <path
                    key={s.seed}
                    className="survivor-dots"
                    d={s.pts.map(([lon, lat]) => {
                      const [x, y] = project(lon, lat);
                      return `M${x.toFixed(1)} ${y.toFixed(1)}h0`;
                    }).join('')}
                    stroke={s.color}
                    strokeWidth={3.2 / view.k}
                    aria-hidden="true"
                  />
                ),
            )}

            {/* Stage 2: other seeds as small dots, the active seed as numbered pins (rank within that seed). */}
            {stage2?.filter((g) => g.seed !== group?.seed).flatMap((g) =>
              g.cells.map((c) => (
                <g key={`${g.seed}:${c.rank}`} className="pin pin-small" style={pinStyle(c.lon, c.lat)} aria-hidden="true">
                  <circle r="4" fill={seedColor(g.seed)} stroke="rgba(0,0,0,.5)" />
                </g>
              )),
            )}
            {group &&
              [...shownCells].reverse().map((c) => {
                const sel = c === active;
                return (
                  <g
                    key={c.rank}
                    className={`pin pin-top${sel ? ' pin-selected' : ''}`}
                    style={pinStyle(c.lon, c.lat)}
                    role="button"
                    tabIndex={0}
                    aria-label={`${group.label} rank ${c.rank}`}
                    aria-pressed={sel}
                    onClick={() => setSelected(c.rank)}
                    onKeyDown={onKey(() => setSelected(c.rank))}
                  >
                    <circle r={sel ? 16 : 12} className="pin-dot" style={{ fill: seedColor(group.seed) }} />
                    <text textAnchor="middle" dy="0.35em" className="pin-num pin-num-sm">
                      {c.rank}
                    </text>
                  </g>
                );
              })}
          </g>
        </svg>

        <div className="overlay top-left">
          <section className="context-card context-compact" aria-labelledby="pctx-title">
            <SectionTitle id="pctx-title">Searching for analogs of</SectionTitle>
            <div className="context-site">
              <span className="site-chip" aria-hidden="true">
                {markerLabel(site)}
              </span>
              <div>
                <strong>{site.name}</strong>
                <span>{site.region}, Mars</span>
              </div>
            </div>
            <p className="faint context-seeds">Seeds: {results.groups.map((g) => g.label).join(', ')}</p>
            <button type="button" className="link-button context-edit" onClick={onEditSite}>
              Edit site
            </button>
          </section>
        </div>

        <div className="overlay top-center hide-compact">
          <p className="pill" role="status">
            <span className={`dot ${results.tier === 'stage2' ? 'dot-good' : 'dot-warn'}`} aria-hidden="true" />
            {results.tier === 'stage2' ? 'Stage 2 pipeline results' : 'Passes stage 1 gates · not ranked yet'}
            {continentName && <> · <strong className="accent-ice">{continentName}</strong></>}
          </p>
        </div>

        <div className="overlay bottom-right">
          <nav className="continent-bar" aria-label="Explore by continent">
            <span className="continent-bar-label">Explore by continent</span>
            {CONTINENTS.map((c) => {
              const n = counts[c.id] ?? 0;
              return (
                <button key={c.id} type="button" className={`chip chip-button${c.id === continent ? ' chip-mars' : ''}`} aria-pressed={c.id === continent} disabled={n === 0} title={n === 0 ? `No results in ${c.name}` : undefined} onClick={() => setContinent(c.id === continent ? null : c.id)}>
                  {c.name}
                  <span className="count">{n.toLocaleString('en-US')}</span>
                </button>
              );
            })}
          </nav>
        </div>
      </div>

      {stage2 && group ? (
        <aside className="panel" aria-label="Stage 2 pipeline results">
          <header className="panel-head">
            {continent && (
              <button type="button" className="link-button back-link" onClick={() => setContinent(null)}>
                ← All continents
              </button>
            )}
            <h2>Stage 2 pipeline results</h2>
            <p>
              0.8 km cells ranked by the pipeline for each Earth seed of {site.name}. Ranks are within a seed{continentName ? `; showing ${continentName} only` : ''}.
            </p>
          </header>
          <div className="seed-chips" role="tablist" aria-label="Earth seed">
            {stage2.map((g) => (
              <button key={g.seed} type="button" role="tab" aria-selected={g.seed === group.seed} className={`chip chip-button${g.seed === group.seed ? ' chip-mars' : ''}`} onClick={() => { setSeed(g.seed); setSelected(null); }}>
                <span className="swatch" style={{ background: seedColor(g.seed) }} aria-hidden="true" /> {g.label}
                <span className="count">{g.cells.length}</span>
              </button>
            ))}
          </div>
          {!active && <p className="empty">No {group.label} cells in {continentName}. Pick another continent or seed.</p>}
          {active && (
            <section aria-labelledby="cell-title">
              <h3 id="cell-title" className="cell-title">
                {group.label} · rank {active.rank}
              </h3>
              <p className="faint">
                {coord(active.lat, active.lon)} · patch {active.patchId} · cell {active.fineCellId}
              </p>
              <p className="cand-score">
                <strong>{active.similarity?.toFixed(3) ?? 'Unavailable'}</strong> pipeline similarity (not comparable across seeds) · distance {active.distance?.toFixed(2) ?? 'Unavailable'} · {active.featuresMatched} features compared
              </p>
              {active.opticalError && <p className="empty">No usable optical scene, so NDVI and bare-ground are missing for this cell.</p>}
              <table className="feature-table">
                <tbody>
                  {FEATURES.map((f) => {
                    const v = active.features[f.id];
                    return (
                      <tr key={f.id} className={v == null ? 'is-missing' : undefined}>
                        <th scope="row">{f.label}</th>
                        <td>{v == null ? 'Unavailable' : `${v.toFixed(f.digits)} ${f.unit}`.trim()}</td>
                        <td className="faint">{active.matched.includes(f.id) ? 'compared' : ''}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </section>
          )}
          <SectionTitle>
            {group.label} · {plural(shownCells.length, 'cell', 'cells')}
          </SectionTitle>
          <ol className="cand-list">
            {shownCells.map((c) => (
              <li key={c.rank}>
                <button type="button" className={`plain-button cand-row${c === active ? ' is-selected' : ''}`} onClick={() => setSelected(c.rank)}>
                  <span className="swatch" style={{ background: seedColor(group.seed) }} aria-hidden="true" />
                  <span>
                    #{c.rank} · {coord(c.lat, c.lon)}
                  </span>
                  <span className="faint">{c.similarity?.toFixed(3) ?? '–'}</span>
                </button>
              </li>
            ))}
          </ol>
        </aside>
      ) : (
        <aside className="panel" aria-label="Stage 1 survivors">
          <header className="panel-head">
            {continent && (
              <button type="button" className="link-button back-link" onClick={() => setContinent(null)}>
                ← All continents
              </button>
            )}
            <h2>Passes stage 1 gates · not ranked yet</h2>
            <p>
              ~50 km patches that passed the prefilter gates for each Earth seed of {site.name}. They have no similarity score; stage 2 has not ranked them yet.
            </p>
          </header>
          {!survivors && <p className="empty">Loading stage 1 survivors…</p>}
          {survivors?.map((s) => (
            <section key={s.seed} className="survivor-card" aria-label={`${s.label} survivors`}>
              <label className="survivor-head">
                <input type="checkbox" checked={!off.has(s.seed)} onChange={() => setOff((o) => { const n = new Set(o); if (n.has(s.seed)) n.delete(s.seed); else n.add(s.seed); return n; })} />
                <span className="swatch" style={{ background: s.color }} aria-hidden="true" />
                <strong>{s.label}</strong>
                <span className="faint">{s.survivors.toLocaleString('en-US')} patches</span>
              </label>
              <table className="feature-table">
                <tbody>
                  {CONTINENTS.map((c) => (
                    <tr key={c.id} className={c.id === continent ? 'is-on' : undefined}>
                      <th scope="row">{c.name}</th>
                      <td>{(s.counts[c.id] ?? 0).toLocaleString('en-US')}</td>
                    </tr>
                  ))}
                  {(s.counts.other ?? 0) > 0 && (
                    <tr>
                      <th scope="row">Outside the continent outlines (islands, coasts)</th>
                      <td>{(s.counts.other ?? 0).toLocaleString('en-US')}</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </section>
          ))}
        </aside>
      )}
    </div>
  );
}
