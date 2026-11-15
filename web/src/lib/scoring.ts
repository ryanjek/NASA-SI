/**
 * Scoring rubric for Mars candidate sites. Mirrors the rubric table exactly:
 *
 *  Flatness      30%  fraction of valid pixels with slope <= 5 deg
 *  Ice evidence  30%  median SWIM ice-consistency, scaled -1..+1 -> 0..1
 *  Dust cover    20%  TES Dust Cover Index <= 0.94 -> 0, >= 0.96 -> 1, linear between
 *  Elevation     10%  median elevation <= -2000 m -> 1, >= 0 m -> 0, linear between
 *  Data coverage 10%  fraction of pixels with valid data across all input layers
 *
 * The same five categories are the user-adjustable weights in the UI.
 */

export type CategoryId = 'flatness' | 'ice' | 'dust' | 'elevation' | 'coverage';

export interface Category {
  id: CategoryId;
  label: string;
  defaultWeight: number;
  /** "How it was scored" text from the rubric. */
  how: string;
  /** Source dataset(s), shown beside the value. */
  source: string;
  color: string;
}

export const CATEGORIES: readonly Category[] = [
  { id: 'flatness', label: 'Flatness', defaultWeight: 30, how: 'Fraction of valid pixels with slope ≤ 5°', source: 'MOLA', color: '#72C8EA' },
  { id: 'ice', label: 'Ice evidence', defaultWeight: 30, how: 'Median SWIM ice-consistency value, scaled from −1 to +1 into 0–1', source: 'SWIM 2.0', color: '#9DB4FF' },
  { id: 'dust', label: 'Dust cover', defaultWeight: 20, how: 'TES Dust Cover Index ≤ 0.94 scored 0, ≥ 0.96 scored 1, linear between', source: 'TES DCI', color: '#F2B64A' },
  { id: 'elevation', label: 'Elevation', defaultWeight: 10, how: 'Median elevation ≤ −2,000 m scored 1, ≥ 0 m scored 0, linear between', source: 'MOLA', color: '#E5683A' },
  { id: 'coverage', label: 'Data coverage', defaultWeight: 10, how: 'Fraction of pixels with valid data across all input layers', source: 'All layers', color: '#A2AABC' },
];

export const CATEGORY_IDS = CATEGORIES.map((c) => c.id);

/** Percentages (integers) that always sum to 100. */
export type Weights = Record<CategoryId, number>;

export const DEFAULT_WEIGHTS: Weights = Object.fromEntries(CATEGORIES.map((c) => [c.id, c.defaultWeight])) as Weights;

/** Raw measurements for one Mars site (units noted). */
export interface MarsRaw {
  flatFraction: number; // 0..1, share of valid pixels with slope <= 5 deg
  swimMedian: number; // -1..+1
  dci: number; // TES Dust Cover Index
  elevationM: number; // metres
  coverage: number; // 0..1
}

export type Scores = Record<CategoryId, number>; // each 0..1

/** A site's measurements as stored: `null` means not computed for this site (shown as "Unavailable"). */
export type SiteRaw = { [K in keyof MarsRaw]: MarsRaw[K] | null };

const RAW_KEY: Record<CategoryId, keyof MarsRaw> = { flatness: 'flatFraction', ice: 'swimMedian', dust: 'dci', elevation: 'elevationM', coverage: 'coverage' };

export const clamp01 = (x: number) => Math.min(1, Math.max(0, x));

export function marsCategoryScores(raw: MarsRaw): Scores {
  return {
    flatness: clamp01(raw.flatFraction),
    ice: clamp01((raw.swimMedian + 1) / 2),
    dust: clamp01((raw.dci - 0.94) / (0.96 - 0.94)),
    elevation: clamp01((0 - raw.elevationM) / 2000),
    coverage: clamp01(raw.coverage),
  };
}

/** Category scores for the measurements a site actually has. Missing measurements are left out, never guessed. */
export function siteScores(raw: SiteRaw): Partial<Scores> {
  const filled = Object.fromEntries(Object.entries(raw).map(([k, v]) => [k, v ?? 0])) as unknown as MarsRaw;
  const all = marsCategoryScores(filled);
  const out: Partial<Scores> = {};
  for (const id of CATEGORY_IDS) if (raw[RAW_KEY[id]] != null) out[id] = all[id];
  return out;
}

/** Weighted score 0..100 over the categories present, re-normalising their weights. `null` when none are present. */
export function partialScore(scores: Partial<Scores>, weights: Weights): number | null {
  const ids = CATEGORY_IDS.filter((id) => scores[id] != null);
  const total = ids.reduce((s, id) => s + weights[id], 0);
  if (!ids.length || !total) return null;
  return (ids.reduce((s, id) => s + weights[id] * (scores[id] as number), 0) / total) * 100;
}

/** Weighted score 0..100. Weights are normalised so they need not sum to 100. */
export function weightedScore(scores: Scores, weights: Weights): number {
  const total = CATEGORY_IDS.reduce((s, id) => s + weights[id], 0) || 1;
  return (CATEGORY_IDS.reduce((s, id) => s + weights[id] * scores[id], 0) / total) * 100;
}

/**
 * Set one weight and rescale the others proportionally so the total stays 100.
 * Integer output via largest-remainder rounding.
 */
export function rebalance(weights: Weights, changed: CategoryId, value: number): Weights {
  const v = Math.round(Math.min(100, Math.max(0, value)));
  const others = CATEGORY_IDS.filter((id) => id !== changed);
  const remaining = 100 - v;
  const oldOtherSum = others.reduce((s, id) => s + weights[id], 0);
  const raw = others.map((id) => (oldOtherSum > 0 ? (weights[id] / oldOtherSum) * remaining : remaining / others.length));
  const floors = raw.map(Math.floor);
  let leftover = remaining - floors.reduce((a, b) => a + b, 0);
  const order = raw.map((r, i) => ({ i, frac: r - floors[i] })).sort((a, b) => b.frac - a.frac);
  for (const { i } of order) {
    if (leftover <= 0) break;
    floors[i] += 1;
    leftover -= 1;
  }
  const next = { ...weights, [changed]: v } as Weights;
  others.forEach((id, i) => {
    next[id] = floors[i];
  });
  return next;
}

/* ------------------------------------------------------------------ */
/* User-set bounds                                                     */
/* ------------------------------------------------------------------ */

/** The four categories a user can bound. Coverage is data quality, not a criterion, so it has no bounds. */
export type BoundedId = Exclude<CategoryId, 'coverage'>;
export const BOUNDED_IDS: readonly BoundedId[] = ['flatness', 'ice', 'dust', 'elevation'];

export interface BoundSpec {
  label: string;
  unit: string;
  /** Slider domain, in the variable's own units. */
  min: number;
  max: number;
  step: number;
  /**
   * Starting half-width either side of the site value: a quarter of the variable's domain. Fixed here, before any search, and never tuned to
   * recover known analogs (Phase 1 rule). Users widen or narrow from this.
   */
  halfWidth: number;
  format: (v: number) => string;
}

export const BOUND_SPECS: Record<BoundedId, BoundSpec> = {
  flatness: { label: 'Flatness', unit: '% of pixels ≤ 5°', min: 0, max: 1, step: 0.01, halfWidth: 0.25, format: (v) => `${Math.round(v * 100)}%` },
  ice: { label: 'Ice evidence', unit: 'SWIM, −1 to +1', min: -1, max: 1, step: 0.01, halfWidth: 0.5, format: (v) => (v < 0 ? '−' : '+') + Math.abs(v).toFixed(2) },
  dust: { label: 'Dust cover', unit: 'TES DCI', min: 0.9, max: 1, step: 0.001, halfWidth: 0.025, format: (v) => v.toFixed(3) },
  // Domain spans MOLA's full range (-8,199 to 21,218 m) so real sites such as Arsia Mons fit; halfWidth unchanged.
  elevation: { label: 'Elevation', unit: 'metres', min: -8200, max: 21300, step: 50, halfWidth: 1750, format: (v) => `${Math.round(v).toLocaleString('en-US').replace('-', '−')} m` },
};

export interface Bound {
  lo: number;
  hi: number;
}
export type Bounds = Record<BoundedId, Bound>;
export type SiteValues = Record<BoundedId, number>;

/** A Mars site's raw measurements in the units the bounds use. These are the bound centres. */
export function siteValues(raw: MarsRaw): SiteValues {
  return { flatness: raw.flatFraction, ice: raw.swimMedian, dust: raw.dci, elevation: raw.elevationM };
}

const clampTo = (x: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, x));
/** Round to the slider step so repeated edits do not drift (0.1 + 0.2 style errors). */
const snap = (x: number, step: number) => Math.round(x / step) * step;

/** Bound centres a site actually has. A missing value has no centre, so it cannot filter or shape scores. */
export function siteCentres(raw: SiteRaw): Partial<SiteValues> {
  const out: Partial<SiteValues> = {};
  for (const id of BOUNDED_IDS) {
    const v = raw[RAW_KEY[id]];
    if (v != null) out[id] = v;
  }
  return out;
}

/** Bounds centred on the site value, clamped to each variable's domain. A missing value gets the full domain. */
export function defaultBounds(raw: SiteRaw): Bounds {
  const c = siteCentres(raw);
  return Object.fromEntries(
    BOUNDED_IDS.map((id) => {
      const s = BOUND_SPECS[id];
      const v = c[id];
      if (v == null) return [id, { lo: s.min, hi: s.max }];
      return [id, { lo: snap(clampTo(v - s.halfWidth, s.min, s.max), s.step), hi: snap(clampTo(v + s.halfWidth, s.min, s.max), s.step) }];
    }),
  ) as Bounds;
}

/** Set one end of one bound. The ends can touch but never cross, and stay inside the domain. */
export function setBound(bounds: Bounds, id: BoundedId, end: 'lo' | 'hi', value: number): Bounds {
  const s = BOUND_SPECS[id];
  const b = bounds[id];
  const v = snap(clampTo(value, s.min, s.max), s.step);
  const next = end === 'lo' ? { lo: Math.min(v, b.hi), hi: b.hi } : { lo: b.lo, hi: Math.max(v, b.lo) };
  return { ...bounds, [id]: next };
}

export const isWithin = (b: Bound, v: number) => v >= b.lo && v <= b.hi;

/**
 * Closeness to the site value, given the user's bounds: 1 at the centre, 0 at either bound, 0 outside.
 * Asymmetric bounds give a different ramp on each side, so a narrower bound is a stricter test both
 * as a cut-off and as a score.
 */
export function membership(value: number, centre: number, b: Bound): number {
  if (value < b.lo || value > b.hi) return 0;
  if (value === centre) return 1;
  if (value < centre) return b.lo >= centre ? 1 : (value - b.lo) / (centre - b.lo);
  return b.hi <= centre ? 1 : (b.hi - value) / (b.hi - centre);
}
