import { MARS_SITES, type MarsSite } from '../data/marsSites';
import { marsCategoryScores, weightedScore, type Weights } from '../lib/scoring';
import { plural } from '../lib/format';
import { MarsMap, MiniMars } from './MarsMap';
import { SitePopup } from './SitePopup';
import { SectionTitle } from './ui';

interface Props {
  site: MarsSite | undefined;
  weights: Weights;
  busy: boolean;
  onSelect: (id: string) => void;
  onClose: () => void;
  onWeights: (w: Weights) => void;
  onReset: () => void;
  onRun: () => void;
}

export function MarsView({ site, weights, busy, onSelect, onClose, onWeights, onReset, onRun }: Props) {
  const scoreOf = (s: MarsSite) => weightedScore(marsCategoryScores(s.raw), weights);
  const n = MARS_SITES.length;
  return (
    <div className="stage mars-stage">
      <MarsMap sites={MARS_SITES} selectedId={site?.id ?? null} scoreOf={scoreOf} onSelect={onSelect} />
      <div className="overlay top-left">
        <MiniMars />
      </div>
      {!site && (
        <div className="overlay top-center">
          <p className="pill" role="status">
            <span className={`dot ${n ? 'dot-good' : 'dot-warn'}`} aria-hidden="true" />
            {n ? `${plural(n, 'candidate site', 'candidate sites')} found · Click a site to inspect it` : 'No candidate sites passed the gates for this profile'}
          </p>
        </div>
      )}
      <div className="overlay bottom-left hide-narrow">
        <section className="legend-card" aria-labelledby="mars-key">
          <SectionTitle id="mars-key">Candidate sites</SectionTitle>
          <p>
            <span className="key-dot" aria-hidden="true" />
            Passes slope, dust &amp; elevation gates
          </p>
          <p>
            <span className="key-bar" aria-hidden="true" />
            Suitability low → high
          </p>
          <p className="faint">Screening only — not landing certification.</p>
        </section>
      </div>
      {site && <SitePopup site={site} weights={weights} busy={busy} onChange={onWeights} onReset={onReset} onClose={onClose} onRun={onRun} />}
    </div>
  );
}
