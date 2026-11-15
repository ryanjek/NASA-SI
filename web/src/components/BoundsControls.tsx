import type { CSSProperties } from 'react';
import { BOUNDED_IDS, BOUND_SPECS, CATEGORIES, setBound, siteCentres, type BoundedId, type Bounds } from '../lib/scoring';
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
  const centre = siteCentres(site.raw);
  const none = BOUNDED_IDS.every((id) => centre[id] == null);
  return (
    <section className="weights bounds" aria-labelledby="bounds-title">
      <div className="weights-head">
        <SectionTitle id="bounds-title">How strict?</SectionTitle>
      </div>
      <p className="weights-note">
        Each range starts centred on this site&rsquo;s value. Narrow it to be stricter, widen it to be more lenient.
      </p>
      <p className="weights-note">
        <strong>How it changes results:</strong> an Earth site outside any range is removed. Inside a range, it scores 100 on that variable when it matches this site&rsquo;s value and drops to 0 at the range edge, so narrowing a range removes sites and also lowers near-misses. Data coverage has no range.
      </p>
      {none && (
        <p className="hint">
          <span className="tag tag-demo">Unavailable</span> This site has no measured values yet, so no range can be set. Ranges only affect the sample-data fallback; pipeline results are ranked by the pipeline.
        </p>
      )}
      {BOUNDED_IDS.map((id: BoundedId) => {
        const s = BOUND_SPECS[id];
        const b = bounds[id];
        const color = CATEGORIES.find((c) => c.id === id)?.color;
        const mid = centre[id];
        if (mid == null) {
          return none ? null : (
            <div className="weight bound" key={id}>
              <span className="bound-label">
                <span className="swatch" style={{ background: color }} aria-hidden="true" />
                {s.label}
              </span>
              <p className="hint">Unavailable: no site value, so this variable doesn&rsquo;t filter results.</p>
            </div>
          );
        }
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
            <div className="bound-track" style={{ '--lo': pct(b.lo), '--hi': pct(b.hi), '--mid': pct(mid) } as CSSProperties}>
              <input type="range" aria-label={`${s.label} lower bound`} min={s.min} max={s.max} step={s.step} value={b.lo} onChange={(e) => onChange(setBound(bounds, id, 'lo', Number(e.target.value)))} />
              <input type="range" aria-label={`${s.label} upper bound`} min={s.min} max={s.max} step={s.step} value={b.hi} onChange={(e) => onChange(setBound(bounds, id, 'hi', Number(e.target.value)))} />
            </div>
            <p className="hint">
              Site value {s.format(mid)} · {s.unit}
            </p>
          </div>
        );
      })}
    </section>
  );
}
