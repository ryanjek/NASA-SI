import { useEffect } from 'react';
import { CATEGORIES, marsCategoryScores, weightedScore, type Weights } from '../lib/scoring';
import type { MarsSite } from '../data/marsSites';
import { WeightSliders } from './WeightSliders';
import { Icon } from './Icon';
import { SectionTitle, scoreColor } from './ui';

interface Props {
  site: MarsSite;
  weights: Weights;
  busy: boolean;
  onChange: (w: Weights) => void;
  onReset: () => void;
  onClose: () => void;
  onRun: () => void;
}

const fmtSigned = (v: number) => (v < 0 ? '−' : '+') + Math.abs(v).toFixed(2);

function rawText(site: MarsSite, id: (typeof CATEGORIES)[number]['id']): string {
  const r = site.raw;
  switch (id) {
    case 'flatness':
      return `${Math.round(r.flatFraction * 100)}%`;
    case 'ice':
      return fmtSigned(r.swimMedian);
    case 'dust':
      return r.dci.toFixed(3);
    case 'elevation':
      return `${(r.elevationM / 1000).toFixed(1).replace('-', '−')} km`;
    case 'coverage':
      return `${Math.round(r.coverage * 100)}%`;
  }
}

export function SitePopup({ site, weights, busy, onChange, onReset, onClose, onRun }: Props) {
  const scores = marsCategoryScores(site.raw);
  const total = Math.round(weightedScore(scores, weights));

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
              {site.region} · {site.lat.toFixed(1)}°N {site.lon.toFixed(1)}°E
            </p>
          </div>
          <span className="chip chip-mars" title="Weighted screening score under your current priorities. Not a landing grade.">
            Site score {total}
          </span>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close site details">
            <Icon name="close" />
          </button>
        </header>

        <p className="popup-desc">{site.description}</p>

        <section aria-labelledby="cond-title">
          <SectionTitle id="cond-title">Conditions</SectionTitle>
          <ul className="cond-list">
            {CATEGORIES.map((c) => (
              <li className="cond" key={c.id}>
                <span className="cond-label">{c.label}</span>
                <span className="cond-value">{rawText(site, c.id)}</span>
                <span className="cond-src">
                  {c.source} · scored <span style={{ color: scoreColor(scores[c.id] * 100) }}>{Math.round(scores[c.id] * 100)}</span>
                </span>
              </li>
            ))}
          </ul>
        </section>

        <section aria-labelledby="haz-title">
          <SectionTitle id="haz-title">Potential hazards</SectionTitle>
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
        <WeightSliders weights={weights} onChange={onChange} />
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
