import candidatesData from '../data/candidates.json';
import survivorsIndex from '../data/survivors_index.json';
import type { MarsSite } from '../data/marsSites';

/**
 * Which Earth results a Mars site gets, best first:
 *   stage2 - ranked 0.8 km cells from the stage 2 pipeline (web/src/data/candidates.json)
 *   stage1 - ~50 km patches that passed the stage 1 gates (web/src/data/survivors*.json); NOT ranked, no score
 *   demo   - the illustrative sample analogs (web/src/data/analogs.ts)
 * A site's seeds come from `mapped_earth_seeds` in docs/handoff/mars_sites_for_frontend.csv.
 */
export type Tier = 'stage2' | 'stage1' | 'demo';

export interface Stage2Cell {
  seed: string;
  rank: number;
  fineCellId: number;
  patchId: number;
  lat: number;
  lon: number;
  distance: number | null;
  similarity: number | null;
  featuresMatched: number;
  matched: string[];
  features: Record<string, number | null>;
  opticalError: boolean;
  /** From the ranking file; `null` when the pipeline did not test lithology for this cell. */
  lithologyMatch?: boolean | null;
  sourceBatch?: string | null;
  /** GLiM lithology class at the cell, from the features file. */
  lithology?: string | null;
  /** OpenStreetMap place name for the cell (island/natural area, else town or county) and its state, country. */
  place?: { name: string; area: string | null } | null;
  /** False when no feature value exists for this cell (all features then show "Unavailable"). */
  hasFeatures?: boolean;
  /** Why values are missing: "not in the features file" or the extractor's own error. */
  featureError?: string | null;
}

export interface Stage1Seed {
  name: string;
  lon: number | null;
  lat: number | null;
  survivors: number;
}

/** Stage 2 cells for one seed, in that seed's own rank order. Seeds are never merged or compared. */
export interface Stage2Group {
  seed: string;
  label: string;
  cells: Stage2Cell[];
}
export interface Stage1Group {
  seed: string;
  label: string;
  survivors: number;
}

export type Results = { tier: 'stage2'; groups: Stage2Group[] } | { tier: 'stage1'; groups: Stage1Group[] } | { tier: 'demo' };

/** Antarctica (south of 60 S) is excluded from every Earth suggestion, by request. */
export const ANTARCTIC_LAT = -60;
export const isAntarctic = (lat: number) => lat <= ANTARCTIC_LAT;

/** Phase 1 catalog notes per feature (data/phase1/features.csv), carried in candidates.json. */
export const FEATURE_NOTES: Readonly<Record<string, { definition: string | null; why: string | null; limits: string | null }>> =
  (candidatesData as { featureNotes?: Record<string, { definition: string | null; why: string | null; limits: string | null }> }).featureNotes ?? {};

/** GLiM lithology classes (Hartmann & Moosdorf 2012). */
export const LITHOLOGY: Readonly<Record<string, string>> = {
  su: 'Unconsolidated sediments', ss: 'Siliciclastic sedimentary rocks', sm: 'Mixed sedimentary rocks', sc: 'Carbonate sedimentary rocks',
  py: 'Pyroclastics', ev: 'Evaporites', mt: 'Metamorphic rocks', pa: 'Acid plutonic rocks', pi: 'Intermediate plutonic rocks',
  pb: 'Basic plutonic rocks', va: 'Acid volcanic rocks', vi: 'Intermediate volcanic rocks', vb: 'Basic volcanic rocks',
  ig: 'Ice and glaciers', wb: 'Water bodies', nd: 'No data',
};

export const STAGE2: readonly Stage2Cell[] = (candidatesData as { candidates: Stage2Cell[] }).candidates.filter((c) => !isAntarctic(c.lat));
export const STAGE1: Readonly<Record<string, Stage1Seed>> = (survivorsIndex as { seeds: Record<string, Stage1Seed> }).seeds;

const PHASE1_LABELS: Record<string, string> = {
  university_valley: 'University Valley',
  beacon_central_polygon: 'Beacon Valley',
  drill_hill: 'Drill Hill',
  hiseas_pahoehoe_radius: 'HI-SEAS pāhoehoe',
  // Names from data/phase1/sites.csv (main checkout).
  dyngjusandur: 'Dyngjusandur sand sheet',
  dyngjusandur_central: 'Dyngjusandur central',
  dyngjusandur_sw: 'Dyngjusandur southwest',
  mauna_loa_lava_tubes: 'Mauna Loa lava tubes',
  don_juan_pond: 'Don Juan Pond',
  haughton_central_uplift: 'Haughton central uplift',
  haughton_formation: 'Haughton Formation',
  haughton_melt_breccia: 'Haughton melt breccia',
  haughton_polygons: 'Haughton polygons',
  haughton_rim_faults: 'Haughton rim faults',
  haughton_uplift_margin: 'Haughton uplift margin',
};

export const seedLabel = (seed: string, stage1: Readonly<Record<string, Stage1Seed>> = STAGE1) => PHASE1_LABELS[seed] ?? stage1[seed]?.name ?? seed;

/** Pure tier choice, so it can be tested with any data. */
export function selectResults(seeds: readonly string[], stage2: readonly Stage2Cell[], stage1: Readonly<Record<string, Stage1Seed>>): Results {
  const s2 = seeds
    .map((seed) => ({ seed, label: seedLabel(seed, stage1), cells: stage2.filter((c) => c.seed === seed).sort((a, b) => a.rank - b.rank) }))
    .filter((g) => g.cells.length > 0);
  if (s2.length) return { tier: 'stage2', groups: s2 };
  const s1 = seeds.filter((seed) => stage1[seed]?.survivors).map((seed) => ({ seed, label: seedLabel(seed, stage1), survivors: stage1[seed].survivors }));
  if (s1.length) return { tier: 'stage1', groups: s1 };
  return { tier: 'demo' };
}

export const resultsFor = (site: MarsSite): Results => selectResults(site.seeds, STAGE2, STAGE1);

export const TIER_CHIP: Record<Tier, { text: string; tone: 'good' | 'warn'; title: string }> = {
  stage2: { text: 'Stage 2 results', tone: 'good', title: 'Earth results are ranked stage 2 pipeline cells. Mars site values are not computed yet.' },
  stage1: { text: 'Stage 1 only · unranked', tone: 'warn', title: 'Earth results are stage 1 survivors: patches that pass the prefilter gates, not yet ranked.' },
  demo: { text: 'Sample data', tone: 'warn', title: 'No pipeline results for this site yet; Earth results are illustrative sample data.' },
};

/** Stage 1 patch centres, flat [lon, lat, ...] per seed. Large, so loaded only when needed. */
let survivorsPromise: Promise<Record<string, number[]>> | null = null;
export function loadSurvivors(): Promise<Record<string, number[]>> {
  survivorsPromise ??= import('../data/survivors.json').then((m) => m.default as Record<string, number[]>);
  return survivorsPromise;
}
