export type Step = 1 | 2 | 3;

const STEPS = ['Pick a Mars site', 'Tune priorities', 'Explore Earth analogs'] as const;

interface Props {
  step: Step;
  /** Highest step the user can jump back to. */
  reached: Step;
  onStep: (s: Step) => void;
  onMethod: () => void;
}

export function TopBar({ step, reached, onStep, onMethod }: Props) {
  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-planet" aria-hidden="true" />
        <div className="brand-text">
          <span className="brand-name">Mars Analog Finder</span>
          <span className="brand-tag">Where on Earth can we rehearse Mars?</span>
        </div>
      </div>
      <nav className="steps" aria-label="Progress">
        {STEPS.map((label, i) => {
          const n = (i + 1) as Step;
          const state = n === step ? 'on' : n < step ? 'done' : 'todo';
          const enabled = n <= reached && n !== step;
          return (
            <div className="step-wrap" key={label}>
              <button type="button" className={`step step-${state}`} disabled={!enabled} aria-current={n === step ? 'step' : undefined} onClick={() => onStep(n)}>
                <span className="step-dot">{n}</span>
                <span className="step-label">{label}</span>
              </button>
              {i < 2 && <span className="step-sep" aria-hidden="true" />}
            </div>
          );
        })}
      </nav>
      <div className="topbar-actions">
        <span className="chip chip-warn" title="Site values and Earth results are illustrative until the data pipeline is connected">
          Sample data
        </span>
        <span className="chip hide-narrow">Scoring profile · Arcadia</span>
        <button type="button" className="chip chip-button" onClick={onMethod}>
          Method &amp; data
        </button>
      </div>
    </header>
  );
}
