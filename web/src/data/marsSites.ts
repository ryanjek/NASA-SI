import type { SiteRaw } from '../lib/scoring';
import { currentVariant, fixtureMars } from '../lib/fixtures';
import marsValues from './mars_values.json';

/**
 * Mars candidate sites, copied from docs/handoff/mars_sites_for_frontend.csv (main checkout).
 * Map positions are computed from `lat`/`lon` in MarsMap, never hand-placed.
 */
export interface Hazard {
  text: string;
  tag: string;
  level: 'warn' | 'risk';
}

export interface MarsSite {
  id: string;
  name: string;
  /** Region name, optionally followed by a locality in brackets, e.g. "Vastitas Borealis (Green Valley)". */
  region: string;
  lat: number;
  /** Degrees east, 0–360. */
  lon: number;
  /** `null` = not computed for this site; the UI shows "Unavailable" and leaves it out of the score. */
  raw: SiteRaw;
  description: string;
  hazards: Hazard[];
  /** Why a value is null, per raw field (shown next to "Unavailable"). */
  missing?: Partial<Record<keyof SiteRaw, string>>;
  /** Caveat on the computed values, e.g. when the coordinate is approximate. */
  valueNote?: string;
  /** True for placeholder sites whose coordinates and values were invented for the demo. */
  isSample: boolean;
  /** True when the CSV marks the coordinates "approximate, from memory; verify". */
  approxCoords: boolean;
  /**
   * Earth search seeds. Starts from the CSV's `mapped_earth_seeds`, plus the Phase 1 sub-sites (data/phase1/sites.csv)
   * of the same analog that now have stage 2 rankings in all_15_sites_ranked_candidates.csv.
   */
  seeds: string[];
}

/** Region name without the locality, used to group sites for the map outline. */
export const regionName = (s: MarsSite) => s.region.split(' (')[0];

/** Short marker label: "G-04" -> "G4", "N-01" -> "N1". */
export const markerLabel = (s: MarsSite) => s.id.replace(/-0?/, '');

// Values come from mars_values.json, written by scripts/compute_mars_values.py from MOLA MEGDR, TES DCI and SWIM
// (10x10 MOLA-pixel cell, ~9 km, around each site). Nulls stay null with a reason. Sanity check against the handoff
// CSV's `approx_elevation_m_literature` (never shown as a measurement): G-04 -4,121 vs about -4,100 m,
// N-02 -1,934 vs -1,900 m, N-03 -1,383 vs -1,400 m. N-01 is -955 vs -4,500 m: see its valueNote.
type Computed = SiteRaw & { missing?: Partial<Record<keyof SiteRaw, string>> };
const COMPUTED = (marsValues as unknown as { sites: Record<string, Computed> }).sites;
const NONE: SiteRaw = { flatFraction: null, swimMedian: null, dci: null, elevationM: null, coverage: null };
const valuesFor = (id: string): Pick<MarsSite, 'raw' | 'missing'> => {
  const v = COMPUTED[id];
  if (!v) return { raw: NONE };
  return { raw: { flatFraction: v.flatFraction, swimMedian: v.swimMedian, dci: v.dci, elevationM: v.elevationM, coverage: v.coverage }, missing: v.missing };
};

const BASE_SITES: readonly MarsSite[] = [
  {
    id: 'G-04', name: 'Phoenix landing site', region: 'Vastitas Borealis (Green Valley)', lat: 68.2188, lon: 234.2508,
    isSample: false, approxCoords: false, seeds: ['university_valley', 'drill_hill'], ...valuesFor('G-04'), hazards: [],
    description: 'Landing site of NASA’s Phoenix lander (May 2008). Phoenix’s trenches exposed water ice a few centimetres below the surface.',
  },
  {
    id: 'N-01', name: 'Gale crater', region: 'Gale crater', lat: -5.4, lon: 137.8,
    isSample: false, approxCoords: true, seeds: ['dyngjusandur', 'dyngjusandur_central', 'dyngjusandur_sw', 'bm_askja'], ...valuesFor('N-01'), hazards: [],
    valueNote: 'Computed at the approximate coordinate. It gives −955 m with ~23° slopes, far above the crater floor (about −4,500 m in the handoff CSV), so the point is probably on the central mound, not the floor. Verify the coordinate.',
    description: 'Crater explored by NASA’s Curiosity rover since 2012. Target here is the Stimson formation, a sandstone of wind-blown basaltic sand (Earth analog: the Askja sand sheet, Iceland).',
  },
  {
    id: 'N-02', name: 'Gusev crater', region: 'Gusev crater', lat: -14.57, lon: 175.47,
    isSample: false, approxCoords: true, seeds: ['hiseas_pahoehoe_radius', 'bm_hi_seas'], ...valuesFor('N-02'), hazards: [],
    description: 'Crater explored by NASA’s Spirit rover (2004–2010). Target here is basalt weathering (Earth analog: Hawaiian pāhoehoe lava).',
  },
  {
    id: 'N-03', name: 'Meridiani Planum', region: 'Meridiani Planum', lat: -1.95, lon: 354.47,
    isSample: false, approxCoords: true, seeds: ['hiseas_pahoehoe_radius', 'bm_hi_seas'], ...valuesFor('N-03'), hazards: [],
    description: 'Plain explored by NASA’s Opportunity rover (2004–2018). Same basalt-weathering target and Earth seeds as Gusev crater.',
  },
  {
    id: 'N-04', name: 'Arsia Mons flank', region: 'Arsia Mons', lat: -8.3, lon: 239.9,
    isSample: false, approxCoords: true, seeds: ['mauna_loa_lava_tubes', 'bm_craters_of_the_moon', 'bm_khaybar'], ...valuesFor('N-04'), hazards: [],
    valueNote: 'Computed at the volcano centre (summit area, about 16 km high), not at a skylight on the flank.',
    description: 'Tharsis shield volcano with cave-skylight candidates on its flanks (target: lava tubes). This coordinate is the volcano centre, not a skylight; the exact skylight is still to be picked with the Mars team.',
  },
];

/** Dev-only `?data=` fixtures enter here, at the data boundary. Production always gets the base list. */
export const MARS_SITES: readonly MarsSite[] = fixtureMars(BASE_SITES, currentVariant());

export const siteById = (id: string) => MARS_SITES.find((s) => s.id === id);
