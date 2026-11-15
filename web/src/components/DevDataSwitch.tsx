import { VARIANTS, currentVariant, type Variant } from '../lib/fixtures';

const LABEL: Record<Variant, string> = { demo: 'Demo data', worst: 'Worst case', one: 'One each', empty: 'Empty', many: '1,000-ish', nosites: 'No sites' };

/** Dev-only. Reloads with `?data=<variant>` so a refresh keeps the selection. Not rendered in production builds. */
export function DevDataSwitch() {
  if (!import.meta.env.DEV) return null;
  const active = currentVariant();
  const go = (v: Variant) => {
    const u = new URL(location.href);
    if (v === 'demo') u.searchParams.delete('data');
    else u.searchParams.set('data', v);
    location.href = u.toString();
  };
  return (
    <div className="dev-switch" role="group" aria-label="Dev data fixtures">
      {VARIANTS.map((v) => (
        <button key={v} type="button" aria-pressed={v === active} className={v === active ? 'is-on' : undefined} onClick={() => go(v)}>
          {LABEL[v]}
        </button>
      ))}
    </div>
  );
}
