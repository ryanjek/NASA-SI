import { CATEGORIES, type Weights } from '../lib/scoring';
import { CONTINENTS } from '../lib/geo';
import type { ContinentId } from '../data/analogs';
import { markerLabel, type MarsSite } from '../data/marsSites';
import type { Ranked } from '../lib/ranking';
import { EarthMap } from './EarthMap';
import { ResultsPanel } from './ResultsPanel';
import { SectionTitle } from './ui';

interface Props {
  site: MarsSite;
  weights: Weights;
  ranked: Ranked[];
  continent: ContinentId | null;
  selectedId: string | null;
  onContinent: (id: ContinentId | null) => void;
  onPick: (id: string) => void;
  onEditPriorities: () => void;
}

export function EarthView({ site, weights, ranked, continent, selectedId, onContinent, onPick, onEditPriorities }: Props) {
  const continentName = CONTINENTS.find((c) => c.id === continent)?.name;
  const countIn = (id: ContinentId) => ranked.filter((r) => r.continent === id).length;
  return (
    <div className="stage earth-stage">
      <div className="earth-canvas">
        <EarthMap ranked={ranked} continent={continent} selectedId={selectedId} onContinent={onContinent} onPick={onPick} />

        <div className="overlay top-left">
          <section className="context-card context-compact" aria-labelledby="ctx-title">
            <SectionTitle id="ctx-title">Searching for analogs of</SectionTitle>
            <div className="context-site">
              <span className="site-chip" aria-hidden="true">
                {markerLabel(site)}
              </span>
              <div>
                <strong>{site.name}</strong>
                <span>{site.region}, Mars</span>
              </div>
            </div>
            <ul className="context-weights">
              {CATEGORIES.map((c) => (
                <li key={c.id}>
                  <span>{c.label}</span>
                  <span>{weights[c.id]}%</span>
                  <i style={{ width: `${weights[c.id]}%`, background: c.color }} aria-hidden="true" />
                </li>
              ))}
            </ul>
            <button type="button" className="chip chip-button" onClick={onEditPriorities}>
              Edit site &amp; ranges
            </button>
          </section>
        </div>

        <div className="overlay demo-banner" role="note">
          <strong>Sample data</strong> — no pipeline results for this site yet. Rankings below are illustrative.
        </div>

        <div className="overlay top-center hide-compact">
          {continent ? (
            <p className="pill" role="status">
              <span className="faint">Continent</span>
              <strong className="accent-ice">{continentName}</strong>
            </p>
          ) : (
            <p className="pill" role="status">
              <span className="dot dot-good" aria-hidden="true" />
              Global ranking ready · Click a continent to see more sites
            </p>
          )}
        </div>

        <div className="overlay bottom-left hide-compact">
          <section className="legend-card" aria-labelledby="earth-key">
            <SectionTitle id="earth-key">Map key</SectionTitle>
            <p>
              <span className="key-pin" aria-hidden="true" />
              {continent ? `Sites in ${continentName} · global rank` : 'Top-3 analogs worldwide'}
            </p>
            <p>
              <span className="key-ring" aria-hidden="true" />
              Ring fill = similarity
            </p>
            {!continent && (
              <p>
                <span className="key-dot key-dot-ice" aria-hidden="true" />
                Other candidate sites
              </p>
            )}
          </section>
        </div>

        <div className="overlay bottom-right">
          <nav className="continent-bar" aria-label="Explore by continent">
            <span className="continent-bar-label">Explore by continent</span>
            {CONTINENTS.map((c) => {
              const n = countIn(c.id);
              return (
                <button key={c.id} type="button" className={`chip chip-button${c.id === continent ? ' chip-mars' : ''}`} aria-pressed={c.id === continent} disabled={n === 0} title={n === 0 ? `No analog sites found in ${c.name}` : undefined} onClick={() => onContinent(c.id === continent ? null : c.id)}>
                  {c.name}
                  <span className="count">{n}</span>
                </button>
              );
            })}
          </nav>
        </div>
      </div>

      <ResultsPanel ranked={ranked} weights={weights} siteName={site.name} continent={continent} selectedId={selectedId} onPick={onPick} onAll={() => onContinent(null)} />
    </div>
  );
}
