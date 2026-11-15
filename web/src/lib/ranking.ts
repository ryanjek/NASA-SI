import { ANALOGS, type Analog } from '../data/analogs';
import { siteById } from '../data/marsSites';
import { BOUNDED_IDS, CATEGORY_IDS, clamp01, defaultBounds, isWithin, membership, siteCentres, weightedScore, type BoundedId, type Bounds, type CategoryId, type Scores, type SiteRaw, type SiteValues, type Weights } from './scoring';

/**
 * Client-side stand-in for the backend call:  rank(siteId, weights) -> ranked analogs.
 * Swap `rankAnalogs` for a fetch to the real service; the Ranked shape is the contract.
 */
export type Confidence = 'High' | 'Medium' | 'Low';

export interface Ranked extends Analog {
  scores: Scores; // 0..1 per category
  similarity: number; // 0..100 weighted
  /** Points each category contributes to `similarity` under the current weights. */
  points: Record<CategoryId, number>;
  confidence: Confidence;
  /** The analog's value for each bounded variable, in the same units as the bounds. */
  values: SiteValues;
}

/**
 * SAMPLE ONLY. The real backend returns each Earth candidate's measured value per variable. Until then, a
 * sample value is recovered by inverting the rubric ramp from the analog's illustrative 0..1 score.
 */
export function sampleValues(scores: Scores): SiteValues {
  return {
    flatness: scores.flatness,
    ice: scores.ice * 2 - 1,
    dust: 0.94 + scores.dust * 0.02,
    elevation: -scores.elevation * 4000, // sample frame spans the Mars sites (about -3.5 km); real values come from the backend
  };
}

function hash(str: string): number {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/** Deterministic +/-6% variation so different Mars sites give different sample rankings. */
const jitter = (siteId: string, analogId: string, cat: string) => ((hash(`${siteId}|${analogId}|${cat}`) % 1000) / 1000 - 0.5) * 0.12;

/**
 * rank(siteId, weights, bounds) -> ranked analogs.
 * Bounds are a hard filter (an analog outside any bound is dropped) and shape the score: each bounded category
 * scores 1 at the Mars site's value and falls to 0 at the bound. Coverage has no bounds. Omitting `bounds`
 * uses the defaults centred on the site.
 */
export function rankAnalogs(siteId: string, weights: Weights, bounds?: Bounds, raw?: SiteRaw): Ranked[] {
  const values = raw ?? siteById(siteId)?.raw; // `raw` lets tests and callers rank against a profile directly
  const centre = values ? siteCentres(values) : undefined;
  const use = bounds ?? (values ? defaultBounds(values) : undefined);
  const total = CATEGORY_IDS.reduce((s, id) => s + weights[id], 0) || 1;
  const out: Ranked[] = [];
  for (const a of ANALOGS) {
    const measured = Object.fromEntries(CATEGORY_IDS.map((id) => [id, clamp01(a.base[id] + jitter(siteId, a.id, id))])) as Scores;
    const values = sampleValues(measured);
    const scores = { ...measured };
    let inside = true;
    if (use && centre) {
      for (const id of BOUNDED_IDS as readonly BoundedId[]) {
        const c = centre[id];
        if (c == null) continue; // no site value: this variable neither filters nor reshapes the score
        if (!isWithin(use[id], values[id])) inside = false;
        scores[id] = membership(values[id], c, use[id]);
      }
    }
    if (!inside) continue;
    const points = Object.fromEntries(CATEGORY_IDS.map((id) => [id, (weights[id] / total) * scores[id] * 100])) as Record<CategoryId, number>;
    const confidence: Confidence = scores.coverage >= 0.85 ? 'High' : scores.coverage >= 0.7 ? 'Medium' : 'Low';
    out.push({ ...a, scores, values, similarity: weightedScore(scores, weights), points, confidence });
  }
  return out.sort((x, y) => y.similarity - x.similarity);
}
