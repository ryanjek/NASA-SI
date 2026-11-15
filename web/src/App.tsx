import { useEffect, useMemo, useRef, useState } from 'react';
import { DEFAULT_WEIGHTS, defaultBounds, type Bounds, type Weights } from './lib/scoring';
import { rankAnalogs } from './lib/ranking';
import { siteById } from './data/marsSites';
import { analogById, type ContinentId } from './data/analogs';
import { TopBar, type Step } from './components/TopBar';
import { MarsView } from './components/MarsView';
import { EarthView } from './components/EarthView';
import { MethodDialog } from './components/MethodDialog';
import { DevDataSwitch } from './components/DevDataSwitch';

export default function App() {
  const [stage, setStage] = useState<'mars' | 'earth'>('mars');
  const [siteId, setSiteId] = useState<string | null>(null);
  const [weights, setWeights] = useState<Weights>(DEFAULT_WEIGHTS);
  const [bounds, setBounds] = useState<Bounds | undefined>(undefined);
  const [continent, setContinent] = useState<ContinentId | null>(null);
  const [analogId, setAnalogId] = useState<string | null>(null);
  const [ran, setRan] = useState(false);
  const [searching, setSearching] = useState(false);
  const [method, setMethod] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  const site = siteId ? siteById(siteId) : undefined;
  const ranked = useMemo(() => (site ? rankAnalogs(site.id, weights, bounds) : []), [site, weights, bounds]);

  const step: Step = stage === 'earth' ? 3 : site ? 2 : 1;
  const reached: Step = ran ? 3 : site ? 2 : 1;

  const goStep = (s: Step) => {
    if (s === 1) {
      setStage('mars');
      setSiteId(null);
    } else if (s === 2) {
      setStage('mars');
    } else {
      setStage('earth');
    }
  };

  const run = () => {
    if (searching) return;
    setContinent(null);
    setAnalogId(null);
    setRan(true);
    // A short, honest "working" beat so the swap to Earth reads as a result, not a jump cut.
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      setStage('earth');
      return;
    }
    setSearching(true);
    timer.current = window.setTimeout(() => {
      setSearching(false);
      setStage('earth');
    }, 700);
  };

  const pickAnalog = (id: string) => {
    const a = analogById(id);
    if (!a) return;
    setContinent(a.continent);
    setAnalogId(id);
  };

  const chooseContinent = (id: ContinentId | null) => {
    setContinent(id);
    setAnalogId(null);
  };

  return (
    <div className="app">
      <TopBar step={step} reached={reached} onStep={goStep} onMethod={() => setMethod(true)} />
      <main>
        {stage === 'mars' || !site ? (
          <MarsView
            site={site}
            weights={weights}
            bounds={bounds}
            busy={searching}
            onSelect={(id) => {
              setSiteId(id);
              const next = siteById(id);
              setBounds(next ? defaultBounds(next.raw) : undefined);
              setRan(false);
            }}
            onClose={() => setSiteId(null)}
            onWeights={(w) => {
              setWeights(w);
              setRan(false);
            }}
            onBounds={(b) => {
              setBounds(b);
              setRan(false);
            }}
            onReset={() => {
              setWeights(DEFAULT_WEIGHTS);
              setBounds(site ? defaultBounds(site.raw) : undefined);
              setRan(false);
            }}
            onRun={run}
          />
        ) : (
          <EarthView site={site} weights={weights} ranked={ranked} continent={continent} selectedId={analogId} onContinent={chooseContinent} onPick={pickAnalog} onEditPriorities={() => setStage('mars')} />
        )}
        {searching && (
          <div className="searching" role="status" aria-live="polite">
            <p className="pill">
              <span className="spinner" aria-hidden="true" />
              Ranking {ranked.length} Earth sites against {site?.name}…
            </p>
          </div>
        )}
      </main>
      <MethodDialog open={method} onClose={() => setMethod(false)} />
      <DevDataSwitch />
    </div>
  );
}
