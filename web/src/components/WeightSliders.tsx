import { useState, type CSSProperties } from 'react';
import { CATEGORIES, rebalance, type CategoryId, type Weights } from '../lib/scoring';
import { SectionTitle } from './ui';

interface Props {
  weights: Weights;
  onChange: (w: Weights) => void;
}

/** The five rubric categories as linked weight sliders. Moving one rescales the rest so the total stays 100%. */
export function WeightSliders({ weights, onChange }: Props) {
  const [rules, setRules] = useState(false);
  return (
    <section className="weights" aria-labelledby="weights-title">
      <div className="weights-head">
        <SectionTitle id="weights-title">What matters most?</SectionTitle>
        <div className="weights-meta">
          <span className="total">Total 100%</span>
          <button type="button" className="link-button" aria-expanded={rules} onClick={() => setRules((v) => !v)}>
            {rules ? 'Hide' : 'Show'} scoring rules
          </button>
        </div>
      </div>
      <p className="weights-note">Moving one slider rebalances the others so the total stays 100%.</p>
      {CATEGORIES.map((c) => {
        const v = weights[c.id];
        const inputId = `w-${c.id}`;
        return (
          <div className="weight" key={c.id}>
            <div className="weight-top">
              <label htmlFor={inputId}>
                <span className="swatch" style={{ background: c.color }} aria-hidden="true" />
                {c.label}
              </label>
              <output htmlFor={inputId} className="weight-value">
                {v}%
              </output>
            </div>
            <input
              id={inputId}
              type="range"
              min={0}
              max={100}
              step={1}
              value={v}
              aria-describedby={rules ? `${inputId}-hint` : undefined}
              style={{ '--fill': `${v}%` } as CSSProperties}
              onChange={(e) => onChange(rebalance(weights, c.id as CategoryId, Number(e.target.value)))}
            />
            {rules && (
              <p className="hint" id={`${inputId}-hint`}>
                {c.how}
              </p>
            )}
          </div>
        );
      })}
    </section>
  );
}
