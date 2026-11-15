import { useEffect, useRef } from 'react';
import { CATEGORIES } from '../lib/scoring';
import { Icon } from './Icon';
import { SectionTitle } from './ui';

export function MethodDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);

  return (
    <dialog ref={ref} className="dialog" aria-labelledby="method-title" onClose={onClose} onClick={(e) => e.target === ref.current && onClose()}>
      <div className="dialog-body">
        <header className="dialog-head">
          <h2 id="method-title">Method &amp; data</h2>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close">
            <Icon name="close" />
          </button>
        </header>
        <p>
          Candidate Mars sites pass hard gates first, then are scored with five categories. The same five weights, which you set with the sliders, steer the search for Earth locations that match the chosen site.
        </p>
        <table className="rubric">
          <thead>
            <tr>
              <th scope="col">Scoring category</th>
              <th scope="col" className="num">
                Default weight
              </th>
              <th scope="col">How it is scored</th>
            </tr>
          </thead>
          <tbody>
            {CATEGORIES.map((c) => (
              <tr key={c.id}>
                <td>{c.label}</td>
                <td className="num">{c.defaultWeight}%</td>
                <td>{c.how}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <SectionTitle>Limits</SectionTitle>
        <ul className="limits">
          <li>This is candidate screening, not landing certification.</li>
          <li>Earth cannot reproduce Mars’s gravity, thin CO₂ atmosphere, radiation or perchlorate chemistry.</li>
          <li>
            <strong>All site values and Earth results in this build are illustrative sample data</strong> until the pipeline output is connected.
          </li>
        </ul>
      </div>
    </dialog>
  );
}
