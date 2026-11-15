import { MARS_SITES, type MarsSite } from '../data/marsSites';
import { partialScore, siteScores, type Bounds, type Weights } from '../lib/scoring';
import { plural } from '../lib/format';
import { MarsMap } from './MarsMap';
import { SitePopup } from './SitePopup';
import { SectionTitle } from './ui';

interface Props {
  site: MarsSite | undefined;
  weights: Weights;
  bounds: Bounds | undefined;
  busy: boolean;
  onSelect: (id: string) => void;
  onClose: () => void;
  onBounds: (b: Bounds) => void;
  onReset: () => void;
  onRun: () => void;
}

export function MarsView({ site, weights, bounds, busy, onSelect, onClose, onBounds, onReset, onRun }: Props) {
  const scoreOf = (s: MarsSite) => partialScore(siteScores(s.raw), weights);
  const samples = MARS_SITES.filter((s) => s.isSample).length;
  const n = MARS_SITES.length;
  return (
    <div className="stage mars-stage">
      <MarsMap sites={MARS_SITES} selectedId={site?.id ?? null} scoreOf={scoreOf} onSelect={onSelect} />
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
            Candidate site
          </p>
          <p>
            <span className="key-bar" aria-hidden="true" />
            Suitability low → high
          </p>
          <p className="faint">Sites placed by coordinates; terrain is illustrative.{samples > 0 && ` ${samples} of ${MARS_SITES.length} sites are sample data.`}</p>
          <p className="faint">Scroll or pinch to zoom · drag to pan · double-click to reset.</p>
        </section>
      </div>
      {site && bounds && <SitePopup site={site} weights={weights} bounds={bounds} busy={busy} onBounds={onBounds} onReset={onReset} onClose={onClose} onRun={onRun} />}
    </div>
  );
}
