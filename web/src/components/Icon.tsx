type Name = 'close' | 'arrow-right' | 'arrow-left' | 'chevron-right';

const PATHS: Record<Name, string> = {
  close: 'M3.5 3.5l9 9M12.5 3.5l-9 9',
  'arrow-right': 'M2.5 8h11M9 3.5L13.5 8 9 12.5',
  'arrow-left': 'M13.5 8h-11M7 3.5L2.5 8 7 12.5',
  'chevron-right': 'M6 3.5L10.5 8 6 12.5',
};

/** One consistent stroke icon set (replaces unicode glyphs). Decorative: pair with visible text or an aria-label on the button. */
export function Icon({ name, size = 14 }: { name: Name; size?: number }) {
  return (
    <svg className="icon" width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      <path d={PATHS[name]} />
    </svg>
  );
}
