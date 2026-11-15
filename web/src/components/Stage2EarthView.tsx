import { useEffect, useMemo, useState } from 'react';
import type { ContinentId } from '../data/analogs';
import { markerLabel, type MarsSite } from '../data/marsSites';
import { CONTINENTS, continentOf } from '../lib/geo';
import { FEATURE_NOTES, LITHOLOGY, type Stage2Cell, type Stage2Group } from '../lib/pipeline';
import { placeText, placesFor, type Place } from '../lib/places';
import { plural } from '../lib/format';
import { FEATURES } from './CandidatesView';
import { EarthMap, type MapItem } from './EarthMap';
import { Icon } from './Icon';
import { Chip, SectionTitle } from './ui';

/** Feature values shown on every card: the ones a reader can picture. Full list is in the detail view. */
const CARD_FEATURES = ['elev_m', 'slope_median_deg', 'precip_mm_yr', 'lst_summer_max_c', 'lst_winter_min_c', 'snow_persistence'];
const FEATURE = Object.fromEntries(FEATURES.map((f) => [f.id, f]));

const id = (c: Stage2Cell) => `${c.seed}:${c.rank}`;
const coord = (c: Stage2Cell) => `${Math.abs(c.lat).toFixed(2)}°${c.lat < 0 ? 'S' : 'N'}, ${Math.abs(c.lon).toFixed(2)}°${c.lon < 0 ? 'W' : 'E'}`;
const fmt = (c: Stage2Cell, f: string) => {
  const v = c.features[f];
  const spec = FEATURE[f];
  return v == null ? 'Unavailable' : `${v.toLocaleString('en-US', { maximumFractionDigits: spec.digits, minimumFractionDigits: spec.digits })} ${spec.unit}`.trim();
};

function CellCard({ cell, title, region, selected, onSelect }: { cell: Stage2Cell; title: string; region: string; selected?: boolean; onSelect: () => void }) {
  return (
    <article className={`analog-card${selected ? ' is-selected' : ''}`} onClick={onSelect}>
      <header className="analog-head">
        <span className="rank-badge">{cell.rank}</span>
        <div className="analog-titles">
          <h3>
            <button type="button" className="plain-button" onClick={onSelect}>
              {title}
            </button>
          </h3>
          <p>{region}</p>
        </div>
        <div className="analog-sim" title="Pipeline similarity for this seed. Not comparable across seeds.">
          <strong>{cell.similarity == null ? '–' : cell.similarity.toFixed(2)}</strong>
          <span>similarity</span>
        </div>
      </header>
      <div className="analog-tags">
        {cell.lithologyMatch === true && <Chip tone="mars">Lithology match</Chip>}
        {cell.lithologyMatch === false && <Chip>No lithology match</Chip>}
        <Chip>{cell.featuresMatched} features compared</Chip>
        {cell.lithology && <Chip>{LITHOLOGY[cell.lithology] ?? cell.lithology}</Chip>}
      </div>
      {!CARD_FEATURES.some((f) => cell.features[f] != null) ? (
        <p className="faint card-note">Feature values unavailable: {cell.featureError ?? 'not computed for this cell'}.</p>
      ) : (
        <dl className="cell-facts">
          {CARD_FEATURES.map((f) => (
            <div key={f} className={cell.features[f] == null ? 'is-missing' : undefined}>
              <dt>{FEATURE[f].label}</dt>
              <dd>{fmt(cell, f)}</dd>
            </div>
          ))}
        </dl>
      )}
    </article>
  );
}

interface Props {
  site: MarsSite;
  groups: Stage2Group[];
  onEditSite: () => void;
}

/** Step 3 for sites with stage 2 results: the card layout of the sample view, filled only with pipeline output. */
export function Stage2EarthView({ site, groups, onEditSite }: Props) {
  const [seed, setSeed] = useState(groups[0].seed);
  const [continent, setContinent] = useState<ContinentId | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  // Detail layout (selected card + list) is open after a pick or a continent choice; cells outside the outlines still open it.
  const [detail, setDetail] = useState(false);
  const [places, setPlaces] = useState<Map<string, Place | null>>(new Map());
  const group = groups.find((g) => g.seed === seed) ?? groups[0];

  // Country names for every cell of every seed (loaded once).
  useEffect(() => {
    let live = true;
    placesFor(groups.flatMap((g) => g.cells), id, (c) => [c.lon, c.lat]).then((m) => live && setPlaces(m));
    return () => {
      live = false;
    };
  }, [groups]);

  const cont = (c: Stage2Cell) => continentOf(c.lon, c.lat);
  const title = (c: Stage2Cell) => c.place?.name ?? placeText(places.get(id(c))) ?? coord(c);
  const region = (c: Stage2Cell) => {
    if (c.place) return `${c.place.area ? `${c.place.area} · ` : ''}${coord(c)}`;
    const name = CONTINENTS.find((x) => x.id === cont(c))?.name;
    return placeText(places.get(id(c))) ? `${coord(c)}${name ? ` · ${name}` : ''}` : name ?? 'Outside the continent outlines';
  };

  // Map items: index order = rank within the seed, so the map's numbering equals the seed rank.
  const items: MapItem[] = useMemo(
    () => group.cells.map((c) => ({ id: id(c), name: title(c), region: coord(c), lon: c.lon, lat: c.lat, similarity: (c.similarity ?? 0) * 100, continent: cont(c) })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [group, places],
  );
  const byId = new Map(group.cells.map((c) => [id(c), c]));
  const countIn = (cid: ContinentId) => group.cells.filter((c) => cont(c) === cid).length;
  const continentName = CONTINENTS.find((c) => c.id === continent)?.name;

  const pick = (key: string) => {
    const c = byId.get(key);
    if (!c) return;
    setContinent(cont(c));
    setSelected(key);
    setDetail(true);
  };
  const chooseSeed = (s: string) => {
    setSeed(s);
    setSelected(null);
    setContinent(null);
    setDetail(false);
  };

  const list = continent ? group.cells.filter((c) => cont(c) === continent) : group.cells;
  const active = detail ? (list.find((c) => id(c) === selected) ?? list[0]) : undefined;
  const openContinent = (c: ContinentId | null) => {
    setContinent(c);
    setSelected(null);
    setDetail(c != null);
  };

  return (
    <div className="stage earth-stage">
      <div className="earth-canvas">
        <EarthMap ranked={items} continent={continent} selectedId={active ? id(active) : null} onContinent={openContinent} onPick={pick} />

        <div className="overlay top-left">
          <section className="context-card context-compact" aria-labelledby="s2ctx-title">
            <SectionTitle id="s2ctx-title">Searching for analogs of</SectionTitle>
            <div className="context-site">
              <span className="site-chip" aria-hidden="true">
                {markerLabel(site)}
              </span>
              <div>
                <strong>{site.name}</strong>
                <span>{site.region}, Mars</span>
              </div>
            </div>
            <button type="button" className="link-button context-edit" onClick={onEditSite}>
              Edit site
            </button>
          </section>
        </div>

        <div className="overlay top-center hide-compact">
          <p className="pill" role="status">
            <span className="dot dot-good" aria-hidden="true" />
            {continent ? (
              <>
                <span className="faint">Continent</span> <strong className="accent-ice">{continentName}</strong>
              </>
            ) : (
              'Stage 2 pipeline results · Click a continent to see more cells'
            )}
          </p>
        </div>

        <div className="overlay bottom-left hide-compact">
          <section className="legend-card" aria-labelledby="s2key">
            <SectionTitle id="s2key">Map key</SectionTitle>
            <p>
              <span className="key-pin" aria-hidden="true" />
              {continent ? `Cells in ${continentName} · rank within ${group.label}` : `Top 3 cells for ${group.label}`}
            </p>
            <p>
              <span className="key-ring" aria-hidden="true" />
              Ring fill = pipeline similarity
            </p>
            {!continent && (
              <p>
                <span className="key-dot key-dot-ice" aria-hidden="true" />
                Other ranked cells
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
                <button key={c.id} type="button" className={`chip chip-button${c.id === continent ? ' chip-mars' : ''}`} aria-pressed={c.id === continent} disabled={n === 0} title={n === 0 ? `No ${group.label} cells in ${c.name}` : undefined} onClick={() => openContinent(c.id === continent ? null : c.id)}>
                  {c.name}
                  <span className="count">{n}</span>
                </button>
              );
            })}
          </nav>
        </div>
      </div>

      <aside className="panel" aria-label="Stage 2 pipeline results">
        {groups.length > 1 && (
          <div className="seed-chips seed-tabs" role="tablist" aria-label="Earth seed">
            {groups.map((g) => (
              <button key={g.seed} type="button" role="tab" aria-selected={g.seed === group.seed} className={`chip chip-button${g.seed === group.seed ? ' chip-mars' : ''}`} onClick={() => chooseSeed(g.seed)}>
                {g.label}
                <span className="count">{g.cells.length}</span>
              </button>
            ))}
          </div>
        )}

        {!detail ? (
          <>
            <header className="panel-head">
              <h2>{group.cells.length === 1 ? 'Top Earth analog' : `Top ${Math.min(3, group.cells.length)} Earth analogs`}</h2>
              <p>
                Stage 2 pipeline · ranked for {site.name} against {group.label}
              </p>
            </header>
            {group.cells.slice(0, 3).map((c) => (
              <CellCard key={id(c)} cell={c} title={title(c)} region={region(c)} onSelect={() => pick(id(c))} />
            ))}
            <p className="faint card-note">Similarity is the pipeline&rsquo;s score for this seed only; it is not comparable across seeds. Place names © OpenStreetMap contributors.</p>
          </>
        ) : (
          <>
            <p className="sr-only" aria-live="polite">
              Showing {plural(list.length, 'cell', 'cells')}{continentName ? ` in ${continentName}` : ''}
              {active ? `, rank ${active.rank} selected` : ''}
            </p>
            <header className="panel-head">
              <button type="button" className="link-button back-link" onClick={() => openContinent(null)}>
                <Icon name="arrow-left" size={12} /> {continent ? 'All continents' : 'Top 3'}
              </button>
              <h2>
                {continentName ?? group.label} · {plural(list.length, 'cell', 'cells')}
              </h2>
              <p>Ranks are within {group.label}, across all continents</p>
            </header>
            {list.length === 0 && <p className="empty">No {group.label} cells in {continentName}. Try another continent or seed.</p>}
            {active && (
              <>
                <CellCard cell={active} title={title(active)} region={region(active)} selected onSelect={() => setSelected(id(active))} />
                <section className="why" aria-labelledby="why2-title">
                  <SectionTitle id="why2-title">Why it ranked</SectionTitle>
                  <p className="why-line">
                    Feature distance <strong>{active.distance?.toFixed(2) ?? 'Unavailable'}</strong> to {group.label} (lower is closer), over {active.featuresMatched} features.
                    {active.lithologyMatch != null && <> Rock type {active.lithologyMatch ? 'matches' : 'does not match'} the seed{active.lithology ? ` (here: ${(LITHOLOGY[active.lithology] ?? active.lithology).toLowerCase()})` : ''}.</>}
                  </p>
                  <ul className="why-list">
                    {active.matched.map((f) => (
                      <li key={f}>
                        <div className="why-row">
                          <span>{FEATURE[f]?.label ?? f}</span>
                          <strong className={active.features[f] == null ? 'is-missing' : undefined}>{FEATURE[f] ? fmt(active, f) : '–'}</strong>
                        </div>
                        {FEATURE_NOTES[f]?.why && <p className="why-note">{FEATURE_NOTES[f].why}</p>}
                      </li>
                    ))}
                  </ul>
                </section>
                {FEATURES.some((f) => !active.matched.includes(f.id)) && (
                  <>
                  <SectionTitle>Other measured features</SectionTitle>
                  <table className="feature-table">
                    <tbody>
                      {FEATURES.filter((f) => !active.matched.includes(f.id)).map((f) => (
                        <tr key={f.id} className={active.features[f.id] == null ? 'is-missing' : undefined}>
                          <th scope="row">{f.label}</th>
                          <td>{fmt(active, f.id)}</td>
                          <td className="faint">not compared</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  </>
                )}
                <section className="breakdown" aria-labelledby="break2-title">
                  <SectionTitle id="break2-title">Where the analogy breaks down</SectionTitle>
                  <p>Earth cannot reproduce Mars’s 0.38 g gravity, its ~6 mbar CO₂ atmosphere, surface radiation or perchlorate soils.</p>
                </section>
              </>
            )}
            <ul className="rows">
              {list
                .filter((c) => c !== active)
                .map((c) => (
                  <li key={id(c)}>
                    <button type="button" className="row" onClick={() => setSelected(id(c))}>
                      <span className="rank-badge rank-sm">{c.rank}</span>
                      <span className="row-text">
                        <strong>{title(c)}</strong>
                        <span>{c.place?.area ? `${c.place.area} · ` : ''}{coord(c)}</span>
                      </span>
                      <span className="row-sim">{c.similarity?.toFixed(2) ?? '–'}</span>
                    </button>
                  </li>
                ))}
            </ul>
          </>
        )}
      </aside>
    </div>
  );
}
