import type { ReactNode } from 'react';

export const scoreColor = (v: number) => (v >= 70 ? 'var(--good)' : v >= 50 ? 'var(--warn)' : 'var(--risk)');

export function ScoreBar({ label, value }: { label: string; value: number }) {
  return (
    <div className="score-bar">
      <div className="score-bar-head">
        <span>{label}</span>
        <span className="strong">{Math.round(value)}</span>
      </div>
      <div className="track" role="img" aria-label={`${label} ${Math.round(value)} out of 100`}>
        <div className="fill" style={{ width: `${Math.max(2, value)}%`, background: scoreColor(value) }} />
      </div>
    </div>
  );
}

export function Chip({ children, tone = 'plain' }: { children: ReactNode; tone?: 'plain' | 'mars' | 'good' | 'warn' }) {
  return <span className={`chip chip-${tone}`}>{children}</span>;
}

/** Plain sentence-case group heading (the heading carries itself; no uppercase mono label above it). */
export const SectionTitle = ({ children, id }: { children: ReactNode; id?: string }) => (
  <h3 className="section-title" id={id}>
    {children}
  </h3>
);
