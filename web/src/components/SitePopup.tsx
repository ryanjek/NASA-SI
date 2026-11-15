import { useEffect } from 'react';
import { CATEGORIES, partialScore, siteScores, type Bounds, type Weights } from '../lib/scoring';
import type { MarsSite } from '../data/marsSites';
import { BoundsControls } from './BoundsControls';
import { Icon } from './Icon';
import { SectionTitle, scoreColor } from './ui';

interface Props {
  site: MarsSite;
  weights: Weights;
  bounds: Bounds;
  busy: boolean;
  onBounds: (b: Bounds) => void;
  onReset: () => void;
  onClose: () => void;
  onRun: () => void;
}

/** Mars coordinates as published: planetocentric latitude N/S, longitude 0–360°E. Fewer decimals when approximate. */
const fmtLatLon = (s: MarsSite) => {
  const d = s.isSample ? 1 : s.approxCoords ? 2 : 4;
  return `${Math.abs(s.lat).toFixed(d)}°${s.lat < 0 ? 'S' : 'N'} ${s.lon.toFixed(d)}°E`;
};

const RAW_FIELD = { flatness: 'flatFraction', ice: 'swimMedian', dust: 'dci', elevation: 'elevationM', coverage: 'coverage' } as const;

const fmtSigned = (v: number) => (v < 0 ? '−' : '+') + Math.abs(v).toFixed(2);

const fmt = <T,>(v: T | null, f: (v: T) => string) => (v == null ? 'Unavailable' : f(v));

function rawText(site: MarsSite, id: (typeof CATEGORIES)[number]['id']): string {
  const r = site.raw;
  switch (id) {
    case 'flatness':
      return fmt(r.flatFraction, (v) => `${Math.round(v * 100)}%`);
    case 'ice':
      return fmt(r.swimMedian, fmtSigned);
    case 'dust':
      return fmt(r.dci, (v) => v.toFixed(3));
    case 'elevation':
      return fmt(r.elevationM, (v) => `${(v / 1000).toFixed(1).replace('-', '−')} km`);
    case 'coverage':
      return fmt(r.coverage, (v) => `${Math.round(v * 100)}%`);
  }
}

export function SitePopup({ site, weights, bounds, busy, onBounds, onReset, onClose, onRun }: Props) {
  const scores = siteScores(site.raw);
  const score = partialScore(scores, weights);
  const missing = CATEGORIES.filter((c) => scores[c.id] == null);

  // Esc closes the popup (unless a modal dialog is open on top).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !document.querySelector('dialog[open]')) onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <aside className="popup" aria-label={`${site.name} details`}>
      <div className="popup-body">
        <header className="popup-head">
          <div className="popup-title">
            <h2>{site.name}</h2>
            <p className="muted">
              {site.region} · {fmtLatLon(site)}{site.isSample ? ' · sample location' : ''}
              {site.approxCoords && <span className="tag tag-demo tag-inline">Approximate coordinates</span>}
            </p>
          </div>
          <span className="chip chip-mars" title="Screening score from the values below, using the fixed rubric weights. Categories without a value are left out. Not a landing grade.">
            {score == null ? 'Score unavailable' : missing.length ? `Partial score ${Math.round(score)} · ${CATEGORIES.length - missing.length}/${CATEGORIES.length}` : `Site score ${Math.round(score)}`}
          </span>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close site details">
            <Icon name="close" />
          </button>
        </header>

        <p className="popup-desc">
          {site.isSample && <span className="tag tag-demo">Demo only</span>} {site.description}
        </p>

        <section aria-labelledby="cond-title">
          <div className="section-head">
            <SectionTitle id="cond-title">Conditions</SectionTitle>
            {site.isSample && <span className="tag tag-demo">Sample values</span>}
          </div>
          {!site.isSample && site.missing && (
            <p className="hint cond-method">Computed from MOLA and TES over a ~9 km cell around the site.</p>
          )}
          {site.valueNote && <p className="hint cond-warn">{site.valueNote}</p>}
          <ul className="cond-list">
            {CATEGORIES.map((c) => (
              <li className="cond" key={c.id}>
                <span className="cond-label">{c.label}</span>
                <span className="cond-value">{rawText(site, c.id)}</span>
                <span className="cond-src">
                  {scores[c.id] == null ? (
                    `${c.source} · ${site.missing?.[RAW_FIELD[c.id]] ?? 'not computed for this site'}`
                  ) : (
                    <>
                      {c.source} rubric{site.isSample ? ' · sample value' : ''} · scored <span style={{ color: scoreColor(scores[c.id]! * 100) }}>{Math.round(scores[c.id]! * 100)}</span>
                    </>
                  )}
                </span>
              </li>
            ))}
          </ul>
          {missing.length > 0 && (
            <p className="hint cond-note">
              {missing.length === CATEGORIES.length
                ? 'No condition values have been computed for this site yet, so it has no score.'
                : `Score leaves out ${missing.map((c) => c.label.toLowerCase()).join(', ')} (no value) and re-weights the rest.`}
            </p>
          )}
        </section>

        <section aria-labelledby="haz-title">
          <SectionTitle id="haz-title">Potential hazards</SectionTitle>
          {site.hazards.length === 0 && (
            <p className="hint">
              <span className="tag tag-demo">Unavailable</span> No imagery or terrain hazard review has been run for this site.
            </p>
          )}
          <ul className="hazards">
            {site.hazards.map((h) => (
              <li key={h.text}>
                <span className={`dot dot-${h.level}`} aria-hidden="true" />
                <span className="hazard-text">{h.text}</span>
                <span className="tag">{h.tag}</span>
              </li>
            ))}
          </ul>
        </section>

        <hr />
        <BoundsControls site={site} bounds={bounds} onChange={onBounds} />
      </div>
      <footer className="popup-foot">
        <button type="button" className="btn btn-ghost" onClick={onReset}>
          Reset
        </button>
        <button type="button" className="btn btn-primary" onClick={onRun} disabled={busy}>
          {busy ? 'Searching…' : 'Run analog search'}
          {!busy && <Icon name="arrow-right" />}
        </button>
      </footer>
    </aside>
  );
}
