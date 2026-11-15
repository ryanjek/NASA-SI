import type { CategoryId } from '../lib/scoring';
import { currentVariant, fixtureAnalogs } from '../lib/fixtures';

/**
 * SAMPLE DATA, illustrative only. Replace with the backend's per-category
 * similarity output (0..1 per scoring category) for the selected Mars site.
 * Place names are hypotheses to be tested by the pipeline, not results.
 */
export type ContinentId = 'NA' | 'SA' | 'EU' | 'AF' | 'AS' | 'OC';

export interface Analog {
  id: string;
  name: string;
  region: string;
  continent: ContinentId;
  lon: number;
  lat: number;
  known: boolean;
  /** Category similarity to the Mars target, 0..1. */
  base: Record<CategoryId, number>;
  breaks: string;
}

const s = (flatness: number, ice: number, dust: number, elevation: number, coverage: number) => ({ flatness, ice, dust, elevation, coverage });

const BASE_ANALOGS: readonly Analog[] = [
  { id: 'changtang', name: 'Changtang Plateau', region: 'Tibet, China', continent: 'AS', lon: 84, lat: 33, known: false, base: s(0.78, 0.88, 0.8, 0.66, 0.74), breaks: 'about 0.6 atm air pressure, 1 g, summer vegetation' },
  { id: 'qaidam', name: 'Qaidam Basin', region: 'Qinghai, China', continent: 'AS', lon: 95, lat: 37, known: false, base: s(0.84, 0.56, 0.9, 0.6, 0.9), breaks: 'thick air, 1 g, salt-lake chemistry differs' },
  { id: 'yakutia', name: 'Yakutian Lowlands', region: 'Sakha, Russia', continent: 'AS', lon: 130, lat: 63, known: false, base: s(0.82, 0.9, 0.5, 0.5, 0.62), breaks: 'boreal vegetation, wetter summers, 1 g' },
  { id: 'gobi', name: 'Gobi Desert', region: 'Mongolia', continent: 'AS', lon: 104, lat: 43, known: false, base: s(0.76, 0.42, 0.78, 0.58, 0.86), breaks: 'little ground ice, 1 g, much warmer summers' },
  { id: 'lut', name: 'Dasht-e Lut', region: 'Kerman, Iran', continent: 'AS', lon: 59, lat: 31, known: false, base: s(0.8, 0.12, 0.92, 0.7, 0.9), breaks: 'extreme heat, no ice, 1 g' },
  { id: 'taklamakan', name: 'Taklamakan Desert', region: 'Xinjiang, China', continent: 'AS', lon: 83, lat: 39, known: false, base: s(0.88, 0.2, 0.7, 0.64, 0.88), breaks: 'mobile dunes, no ice, 1 g' },
  { id: 'devon', name: 'Devon Island', region: 'Nunavut, Canada', continent: 'NA', lon: -89, lat: 75.5, known: true, base: s(0.7, 0.9, 0.64, 0.7, 0.9), breaks: 'wetter summers, 1 g, no perchlorate soils' },
  { id: 'greatbasin', name: 'Great Basin', region: 'Nevada, USA', continent: 'NA', lon: -117, lat: 40, known: false, base: s(0.68, 0.18, 0.74, 0.5, 0.94), breaks: 'warm, vegetated margins, no ground ice' },
  { id: 'barren', name: 'Barren Grounds', region: 'Nunavut, Canada', continent: 'NA', lon: -105, lat: 66, known: false, base: s(0.74, 0.82, 0.58, 0.55, 0.7), breaks: 'tundra vegetation, lakes, 1 g' },
  { id: 'northslope', name: 'North Slope', region: 'Alaska, USA', continent: 'NA', lon: -150, lat: 70, known: false, base: s(0.8, 0.86, 0.52, 0.52, 0.78), breaks: 'wet tundra, 1 g, thick air' },
  { id: 'altiplano', name: 'Altiplano', region: 'Potosí, Bolivia', continent: 'SA', lon: -67, lat: -20, known: false, base: s(0.9, 0.38, 0.78, 0.62, 0.9), breaks: 'little ground ice, thicker air, 1 g' },
  { id: 'atacama', name: 'Atacama Desert', region: 'Antofagasta, Chile', continent: 'SA', lon: -69, lat: -24, known: true, base: s(0.82, 0.2, 0.94, 0.7, 0.96), breaks: 'no ground ice, thick air, 1 g' },
  { id: 'patagonia', name: 'Patagonian Steppe', region: 'Santa Cruz, Argentina', continent: 'SA', lon: -70, lat: -46, known: false, base: s(0.72, 0.4, 0.5, 0.42, 0.84), breaks: 'grassland, wind erosion, 1 g' },
  { id: 'finnmark', name: 'Finnmark Plateau', region: 'Northern Norway', continent: 'EU', lon: 25, lat: 70, known: false, base: s(0.62, 0.68, 0.4, 0.38, 0.76), breaks: 'lichen cover, lakes, 1 g' },
  { id: 'tabernas', name: 'Tabernas Desert', region: 'Andalusia, Spain', continent: 'EU', lon: -2.4, lat: 37.1, known: true, base: s(0.6, 0.1, 0.72, 0.56, 0.94), breaks: 'warm, no ice, thick air, 1 g' },
  { id: 'gilfkebir', name: 'Gilf Kebir', region: 'Western Desert, Egypt', continent: 'AF', lon: 24, lat: 23, known: false, base: s(0.7, 0.08, 0.86, 0.6, 0.92), breaks: 'hyper-arid but hot, no ice, 1 g' },
  { id: 'tibesti', name: 'Tibesti Massif', region: 'Chad', continent: 'AF', lon: 18, lat: 21, known: false, base: s(0.5, 0.1, 0.8, 0.54, 0.78), breaks: 'steep volcanic terrain, hot, 1 g' },
  { id: 'pilbara', name: 'Pilbara', region: 'Western Australia', continent: 'OC', lon: 118, lat: -22, known: false, base: s(0.72, 0.06, 0.9, 0.62, 0.95), breaks: 'hot, no ice, ancient-rock focus differs' },
  { id: 'lakeeyre', name: 'Lake Eyre Basin', region: 'South Australia', continent: 'OC', lon: 137, lat: -28, known: false, base: s(0.9, 0.1, 0.76, 0.7, 0.93), breaks: 'episodic flooding, hot, 1 g' },
];

/** Dev-only `?data=` fixtures enter here, at the data boundary. Production always gets the base list. */
export const ANALOGS: readonly Analog[] = fixtureAnalogs(BASE_ANALOGS, currentVariant());

export const analogById = (id: string) => ANALOGS.find((a) => a.id === id);
