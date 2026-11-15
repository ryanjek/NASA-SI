import type { CSSProperties } from 'react';
import { BOUNDED_IDS, BOUND_SPECS, CATEGORIES, setBound, siteValues, type BoundedId, type Bounds } from '../lib/scoring';
import type { MarsSite } from '../data/marsSites';
import { SectionTitle } from './ui';

interface Props {
  site: MarsSite;
  bounds: Bounds;
  onChange: (b: Bounds) => void;
}

/**
 * Lower and upper bound per variable, starting centred on the Mars site's value.
 * Narrower = stricter: Earth locations outside a bound are left out, and closeness to the site value scores higher.
 */
export function BoundsControls({ site, bounds, onChange }: Props) {
  const centre = siteValues(site.raw);
  return (
    <section className="weights bounds" aria-labelledby="bounds-title">
      <div className="weights-head">
        <SectionTitle id="bounds-title">How strict?</SectionTitle>
      </div>
      <p className="weights-note">
        Each range starts centred on this site&rsquo;s value. Narrow it to be stricter, widen it to be more lenient. Earth locations outside a range are left out.
      </p>
      {BOUNDED_IDS.map((id: BoundedId) => {
        const s = BOUND_SPECS[id];
        const b = bounds[id];
        const color = CATEGORIES.find((c) => c.id === id)?.color;
        const pct = (v: number) => `${((v - s.min) / (s.max - s.min)) * 100}%`;
        return (
          <div className="weight bound" key={id}>
            <div className="weight-top">
              <span className="bound-label">
                <span className="swatch" style={{ background: color }} aria-hidden="true" />
                {s.label}
              </span>
              <output className="weight-value" aria-live="polite">
                {s.format(b.lo)} – {s.format(b.hi)}
              </output>
            </div>
            <div className="bound-track" style={{ '--lo': pct(b.lo), '--hi': pct(b.hi), '--mid': pct(centre[id]) } as CSSProperties}>
              <input type="range" aria-label={`${s.label} lower bound`} min={s.min} max={s.max} step={s.step} value={b.lo} onChange={(e) => onChange(setBound(bounds, id, 'lo', Number(e.target.value)))} />
              <input type="range" aria-label={`${s.label} upper bound`} min={s.min} max={s.max} step={s.step} value={b.hi} onChange={(e) => onChange(setBound(bounds, id, 'hi', Number(e.target.value)))} />
            </div>
            <p className="hint">
              Site value {s.format(centre[id])} · {s.unit}
            </p>
          </div>
        );
      })}
    </section>
  );
}
