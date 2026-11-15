import type { MarsRaw } from '../lib/scoring';
import { currentVariant, fixtureMars } from '../lib/fixtures';

/**
 * SAMPLE DATA. Replace with the backend's threshold-screened candidate list.
 * `pos` is in the 1440x836 stylised Arcadia map space.
 */
export interface Hazard {
  text: string;
  tag: string;
  level: 'warn' | 'risk';
}

export interface MarsSite {
  id: string;
  name: string;
  region: string;
  lat: number;
  lon: number;
  pos: { x: number; y: number };
  raw: MarsRaw;
  description: string;
  hazards: Hazard[];
}

const BASE_SITES: readonly MarsSite[] = [
  {
    id: 'A-01', name: 'Site A-01', region: 'Arcadia Planitia', lat: 45.1, lon: 188.4, pos: { x: 470, y: 330 },
    raw: { flatFraction: 0.9, swimMedian: 0.3, dci: 0.952, elevationM: -3100, coverage: 0.93 },
    description: 'Broad, gently sloping plain in the north-west of the region. Good ice-consistency signal with a moderate dust signature.',
    hazards: [{ text: 'Boulder fields possible', tag: 'HiRISE · partial', level: 'warn' }, { text: 'Seasonal dust storms', tag: 'Global · seasonal', level: 'warn' }],
  },
  {
    id: 'A-02', name: 'Site A-02', region: 'Arcadia Planitia', lat: 44.2, lon: 190.8, pos: { x: 790, y: 300 },
    raw: { flatFraction: 0.93, swimMedian: 0.42, dci: 0.956, elevationM: -3400, coverage: 0.96 },
    description: 'A smooth northern plain with low regional slopes and the strongest ice-consistency evidence in the region. Low elevation helps landing.',
    hazards: [{ text: 'Boulder fields possible', tag: 'HiRISE · partial', level: 'warn' }, { text: 'Seasonal dust storms', tag: 'Global · seasonal', level: 'warn' }],
  },
  {
    id: 'A-03', name: 'Site A-03', region: 'Arcadia Planitia', lat: 42.6, lon: 187.9, pos: { x: 560, y: 520 },
    raw: { flatFraction: 0.88, swimMedian: 0.35, dci: 0.948, elevationM: -2800, coverage: 0.91 },
    description: 'Mid-region plain with moderate ice evidence. Dust cover is higher here, which reduces thermal-inertia confidence.',
    hazards: [{ text: 'Higher dust cover', tag: 'TES DCI', level: 'warn' }, { text: 'Crater rim slopes nearby', tag: 'MOLA', level: 'warn' }],
  },
  {
    id: 'A-04', name: 'Site A-04', region: 'Arcadia Planitia', lat: 42.9, lon: 192.7, pos: { x: 880, y: 530 },
    raw: { flatFraction: 0.84, swimMedian: 0.28, dci: 0.944, elevationM: -2500, coverage: 0.88 },
    description: 'South-eastern margin of the plain. Flat enough for landing but with weaker ice evidence and partial data coverage.',
    hazards: [{ text: 'Partial HiRISE coverage', tag: 'Data gap', level: 'risk' }, { text: 'Higher dust cover', tag: 'TES DCI', level: 'warn' }],
  },
  {
    id: 'A-05', name: 'Site A-05', region: 'Arcadia Planitia', lat: 41.4, lon: 189.5, pos: { x: 700, y: 650 },
    raw: { flatFraction: 0.79, swimMedian: 0.22, dci: 0.95, elevationM: -2200, coverage: 0.85 },
    description: 'Southern edge of the screened area, close to rougher terrain. Included for contrast against the better-supported northern sites.',
    hazards: [{ text: 'Rougher terrain to the south', tag: 'MOLA', level: 'warn' }, { text: 'Lowest data coverage', tag: 'Data gap', level: 'risk' }],
  },
];

/** Dev-only `?data=` fixtures enter here, at the data boundary. Production always gets the base list. */
export const MARS_SITES: readonly MarsSite[] = fixtureMars(BASE_SITES, currentVariant());

export const siteById = (id: string) => MARS_SITES.find((s) => s.id === id);
