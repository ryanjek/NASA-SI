import { useEffect, useMemo, useRef, useState } from 'react';
import { DEFAULT_WEIGHTS, defaultBounds, type Bounds } from './lib/scoring';
import { rankAnalogs } from './lib/ranking';
import { siteById } from './data/marsSites';
import { analogById, type ContinentId } from './data/analogs';
import { TopBar, type Step } from './components/TopBar';
import { MarsView } from './components/MarsView';
import { EarthView } from './components/EarthView';
import { MethodDialog } from './components/MethodDialog';
import { CandidatesView } from './components/CandidatesView';
import { PipelineEarthView } from './components/PipelineEarthView';
import { Stage2EarthView } from './components/Stage2EarthView';
import { resultsFor } from './lib/pipeline';

export default function App() {
  const [stage, setStage] = useState<'mars' | 'earth'>('mars');
  const [siteId, setSiteId] = useState<string | null>(null);
  // Fixed rubric weights (30/30/20/10/10). Users no longer re-weight categories; they set ranges instead.
  const weights = DEFAULT_WEIGHTS;
  const [bounds, setBounds] = useState<Bounds | undefined>(undefined);
  const [continent, setContinent] = useState<ContinentId | null>(null);
  const [analogId, setAnalogId] = useState<string | null>(null);
  const [ran, setRan] = useState(false);
  const [searching, setSearching] = useState(false);
  const [method, setMethod] = useState(false);
  const [cands, setCands] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  const site = siteId ? siteById(siteId) : undefined;
  // Earth results: stage 2 cells first, then stage 1 survivors, and sample analogs only when neither exists.
  const results = useMemo(() => (site ? resultsFor(site) : null), [site]);
  const ranked = useMemo(() => (site && results?.tier === 'demo' ? rankAnalogs(site.id, weights, bounds) : []), [site, results, weights, bounds]);

  // Escape leaves the Stage 2 candidates view (unless a dialog is open on top).
  useEffect(() => {
    if (!cands) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !document.querySelector('dialog[open]')) setCands(false);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [cands]);

  const step: Step = stage === 'earth' ? 3 : site ? 2 : 1;
  const reached: Step = ran ? 3 : site ? 2 : 1;

  const goStep = (s: Step) => {
    setCands(false); // any step leaves the Stage 2 candidates view
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
      <TopBar step={step} reached={reached} onStep={goStep} onMethod={() => setMethod(true)} onCandidates={() => setCands((v) => !v)} candidatesOn={cands} tier={results?.tier ?? null} />
      <main>
        {cands ? (
          <CandidatesView />
        ) : stage === 'mars' || !site ? (
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
            onBounds={(b) => {
              setBounds(b);
              setRan(false);
            }}
            onReset={() => {
              setBounds(site ? defaultBounds(site.raw) : undefined);
              setRan(false);
            }}
            onRun={run}
          />
        ) : results?.tier === 'stage2' ? (
          <Stage2EarthView key={site.id} site={site} groups={results.groups} onEditSite={() => setStage('mars')} />
        ) : results && results.tier !== 'demo' ? (
          <PipelineEarthView key={site.id} site={site} results={results} onEditSite={() => setStage('mars')} />
        ) : (
          <EarthView site={site} weights={weights} ranked={ranked} continent={continent} selectedId={analogId} onContinent={chooseContinent} onPick={pickAnalog} onEditPriorities={() => setStage('mars')} />
        )}
        {searching && (
          <div className="searching" role="status" aria-live="polite">
            <p className="pill">
              <span className="spinner" aria-hidden="true" />
              {results?.tier === 'demo' ? `Ranking ${ranked.length} sample Earth sites against ${site?.name}…` : `Loading pipeline results for ${site?.name}…`}
            </p>
          </div>
        )}
      </main>
      <MethodDialog open={method} onClose={() => setMethod(false)} />
    </div>
  );
}
