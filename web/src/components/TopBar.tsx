import { TIER_CHIP, type Tier } from '../lib/pipeline';

export type Step = 1 | 2 | 3;

const STEPS = ['Pick a Mars site', 'Set ranges', 'Explore Earth analogs'] as const;

interface Props {
  step: Step;
  /** Highest step the user can jump back to. */
  reached: Step;
  onStep: (s: Step) => void;
  onMethod: () => void;
  onCandidates?: () => void;
  candidatesOn?: boolean;
  /** Data tier behind the selected site's Earth results; `null` when no site is picked. */
  tier?: Tier | null;
}

export function TopBar({ step, reached, onStep, onMethod, onCandidates, candidatesOn, tier }: Props) {
  const chip = tier ? TIER_CHIP[tier] : null;
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
          // In the Stage 2 candidates view every reached step (including the current one) is a way back out.
          const enabled = n <= reached && (candidatesOn || n !== step);
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
        {chip && (
          <span className={`chip chip-${chip.tone}`} title={chip.title}>
            {chip.text}
          </span>
        )}
        <span className="chip hide-narrow">Scoring profile · Arcadia</span>
        {onCandidates && (
          <button type="button" className={`chip chip-button${candidatesOn ? ' chip-mars' : ''}`} aria-pressed={candidatesOn} onClick={onCandidates}>
            {candidatesOn ? 'Close Stage 2 candidates ✕' : 'Stage 2 candidates'}
          </button>
        )}
        <button type="button" className="chip chip-button" onClick={onMethod}>
          Method &amp; data
        </button>
      </div>
    </header>
  );
}
