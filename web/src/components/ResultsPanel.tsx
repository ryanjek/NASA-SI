import { CATEGORIES, type Weights } from '../lib/scoring';
import { CONTINENTS } from '../lib/geo';
import { plural } from '../lib/format';
import type { ContinentId } from '../data/analogs';
import type { Ranked } from '../lib/ranking';
import { Icon } from './Icon';
import { Chip, ScoreBar, SectionTitle } from './ui';

function AnalogCard({ item, rank, selected, onSelect }: { item: Ranked; rank: number; selected?: boolean; onSelect: () => void }) {
  return (
    <article className={`analog-card${selected ? ' is-selected' : ''}`} onClick={onSelect}>
      <header className="analog-head">
        <span className="rank-badge">{rank}</span>
        <div className="analog-titles">
          <h3>
            <button type="button" className="plain-button" onClick={onSelect}>
              {item.name}
            </button>
          </h3>
          <p>{item.region}</p>
        </div>
        <div className="analog-sim">
          <strong>{Math.round(item.similarity)}%</strong>
          <span>similarity</span>
        </div>
      </header>
      <div className="analog-tags">
        <Chip tone={item.known ? 'plain' : 'mars'}>{item.known ? 'Known analog' : 'Lesser-known'}</Chip>
        <Chip>Confidence · {item.confidence}</Chip>
      </div>
      <div className="analog-bars">
        {CATEGORIES.map((c) => (
          <ScoreBar key={c.id} label={c.label} value={item.scores[c.id] * 100} />
        ))}
      </div>
      <p className="analog-breaks">Breaks down: {item.breaks}</p>
    </article>
  );
}

function Contributions({ item, weights }: { item: Ranked; weights: Weights }) {
  const summary = CATEGORIES.map((c) => `${c.label} ${item.points[c.id].toFixed(0)} points at ${weights[c.id]} percent weight`).join('; ');
  return (
    <section className="why" aria-labelledby="why-title">
      <SectionTitle id="why-title">Why it ranked</SectionTitle>
      <div className="stack" role="img" aria-label={`Contributions to ${Math.round(item.similarity)} percent similarity: ${summary}`}>
        {CATEGORIES.map((c) => (
          <span key={c.id} style={{ width: `${item.points[c.id]}%`, background: c.color }} />
        ))}
      </div>
      <ul className="legend-inline">
        {CATEGORIES.map((c) => (
          <li key={c.id}>
            <span className="swatch" style={{ background: c.color }} aria-hidden="true" />
            {c.label} +{item.points[c.id].toFixed(0)} <span className="faint">({weights[c.id]}%)</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

interface Props {
  ranked: Ranked[];
  weights: Weights;
  siteName: string;
  continent: ContinentId | null;
  selectedId: string | null;
  onPick: (id: string) => void;
  onAll: () => void;
}

export function ResultsPanel({ ranked, weights, siteName, continent, selectedId, onPick, onAll }: Props) {
  if (!continent) {
    const top = ranked.slice(0, 3);
    return (
      <aside className="panel" aria-label="Top Earth analogs">
        <header className="panel-head">
          <h2>{top.length === 1 ? 'Top Earth analog' : `Top ${top.length} Earth analogs`}</h2>
          <p>Ranked for {siteName} · weighted by your priorities</p>
        </header>
        {top.length === 0 && <p className="empty">No Earth analog sites fall inside your ranges. Widen the ranges on the site panel to be more lenient.</p>}
        {top.map((r, i) => (
          <AnalogCard key={r.id} item={r} rank={i + 1} onSelect={() => onPick(r.id)} />
        ))}
      </aside>
    );
  }

  const list = ranked.filter((r) => r.continent === continent);
  const name = CONTINENTS.find((c) => c.id === continent)?.name ?? continent;
  const active = list.find((r) => r.id === selectedId) ?? list[0];
  const activeRank = active ? list.indexOf(active) + 1 : 0;

  return (
    <aside className="panel" aria-label={`Analog sites in ${name}`}>
      <p className="sr-only" aria-live="polite">
        Showing {plural(list.length, 'analog site', 'analog sites')} in {name}
        {active ? `, ${active.name} selected` : ''}
      </p>
      <header className="panel-head">
        <button type="button" className="link-button back-link" onClick={onAll}>
          <Icon name="arrow-left" size={12} /> All continents
        </button>
        <h2>
          {name} · {plural(list.length, 'analog site', 'analog sites')}
        </h2>
        <p>Ranked within this continent</p>
      </header>
      {list.length === 0 && <p className="empty">No analog sites found in {name} for this site and these priorities. Try another continent, or adjust your priorities.</p>}
      {active && (
        <>
          <AnalogCard item={active} rank={activeRank} selected onSelect={() => onPick(active.id)} />
          <Contributions item={active} weights={weights} />
          <section className="breakdown" aria-labelledby="break-title">
            <SectionTitle id="break-title">Where the analogy breaks down</SectionTitle>
            <p>
              Earth cannot reproduce Mars’s 0.38 g gravity, its ~6 mbar CO₂ atmosphere, surface radiation or perchlorate soils. For this site specifically: {active.breaks}.
            </p>
          </section>
        </>
      )}
      <ul className="rows">
        {list
          .filter((r) => r.id !== active?.id)
          .map((r) => (
            <li key={r.id}>
              <button type="button" className="row" onClick={() => onPick(r.id)}>
                <span className="rank-badge rank-sm">{list.indexOf(r) + 1}</span>
                <span className="row-text">
                  <strong>{r.name}</strong>
                  <span>{r.region}</span>
                </span>
                <span className="row-sim">{Math.round(r.similarity)}%</span>
              </button>
            </li>
          ))}
      </ul>
    </aside>
  );
}
