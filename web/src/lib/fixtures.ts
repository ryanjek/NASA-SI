import type { Analog } from '../data/analogs';
import type { MarsSite } from '../data/marsSites';

/**
 * Dev-only stress fixtures (break-ui). Select with `?data=worst|one|empty|many|nosites`.
 * They enter at the data boundary, so the components are exercised exactly as they would be with real data.
 * Never active in a production build.
 */
export type Variant = 'demo' | 'worst' | 'one' | 'empty' | 'many' | 'nosites';
export const VARIANTS: readonly Variant[] = ['demo', 'worst', 'one', 'empty', 'many', 'nosites'];

export function currentVariant(): Variant {
  try {
    if (!import.meta.env?.DEV || typeof location === 'undefined') return 'demo';
    const v = new URLSearchParams(location.search).get('data');
    return (VARIANTS as readonly string[]).includes(v ?? '') ? (v as Variant) : 'demo';
  } catch {
    return 'demo';
  }
}

// Realistic long place names and regions (real naming patterns, not random strings).
const LONG_NAMES = [
  'Qinghai–Tibet Plateau, Changtang Nature Reserve (Northern Plateau)',
  'Salar de Uyuni y Cordillera de Lípez',
  'Kuh-e Namak and Dasht-e Kavir (Great Salt Desert)',
  'Ellesmere Island, Quttinirpaaq National Park',
];
const LONG_REGIONS = [
  'Xizang Autonomous Region, People’s Republic of China',
  'Tierra del Fuego, Antártida e Islas del Atlántico Sur Province, Argentina',
  'Sakha (Yakutia) Republic, Russian Federation',
  'Qikiqtaaluk Region, Nunavut Territory, Canada',
];
const LONG_BREAKS = 'thicker air at lower altitude, 1 g instead of 0.38 g, seasonal vegetation and meltwater lakes, and perchlorate-free soils';
const LONG_DESC =
  'A broad, gently sloping plain on the north-western rim of the screened area, where regional slopes stay below five degrees over several tens of kilometres. Ice-consistency from radar, neutron and thermal evidence is strongest here, but boulder density from partial HiRISE coverage is uncertain and the dust signature varies seasonally. Elevation is comfortably below the −2,000 m threshold, which helps entry, descent and landing margins. Treat this as a screening candidate that still needs imagery review before any operational use.';

export function fixtureAnalogs(list: readonly Analog[], v: Variant): Analog[] {
  switch (v) {
    case 'worst':
      return list.map((a, i) => (i % 3 === 0 ? { ...a, name: LONG_NAMES[(i / 3) % LONG_NAMES.length | 0], region: LONG_REGIONS[(i / 3) % LONG_REGIONS.length | 0], breaks: LONG_BREAKS } : a));
    case 'one': {
      const seen = new Set<string>();
      return list.filter((a) => (seen.has(a.continent) ? false : (seen.add(a.continent), true)));
    }
    case 'empty':
      return list.filter((a) => a.continent !== 'OC' && a.continent !== 'EU');
    case 'many': {
      const asia = list.filter((a) => a.continent === 'AS');
      const extra: Analog[] = [];
      for (let n = 1; n <= 7; n++) for (const a of asia) extra.push({ ...a, id: `${a.id}-${n}`, name: `${a.name} – sector ${n + 1}`, lon: a.lon + ((n * 7) % 9) - 4, lat: a.lat + ((n * 5) % 7) - 3 });
      return [...list, ...extra];
    }
    default:
      return [...list];
  }
}

export function fixtureMars(list: readonly MarsSite[], v: Variant): MarsSite[] {
  switch (v) {
    case 'worst':
      return list.map((s, i) => (i === 1 ? { ...s, region: 'Arcadia Planitia, Northern Lowlands (Amazonis–Arcadia transition zone)', description: LONG_DESC, hazards: [...s.hazards, { text: 'Steep crater-rim slopes within the landing ellipse', tag: 'MOLA · regional baseline only', level: 'risk' as const }, { text: 'Possible thermal-contraction polygons', tag: 'CTX · partial', level: 'warn' as const }] } : s));
    case 'one':
      return list.slice(0, 1);
    case 'nosites':
      return [];
    default:
      return [...list];
  }
}
