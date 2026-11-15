import { describe, expect, test } from 'bun:test';
import { BOUNDED_IDS, BOUND_SPECS, CATEGORY_IDS, DEFAULT_WEIGHTS, defaultBounds, isWithin, marsCategoryScores, membership, rebalance, setBound, siteValues, weightedScore, type MarsRaw, type Weights } from './scoring';
import { rankAnalogs } from './ranking';
import { MARS_SITES } from '../data/marsSites';
import { resultsFor } from './pipeline';

const sum = (w: Weights) => CATEGORY_IDS.reduce((s, id) => s + w[id], 0);

describe('rubric thresholds (from the scoring table)', () => {
  const base = { flatFraction: 0.5, swimMedian: 0, dci: 0.95, elevationM: -1000, coverage: 0.5 };

  test('default weights are 30/30/20/10/10 and sum to 100', () => {
    expect(DEFAULT_WEIGHTS).toEqual({ flatness: 30, ice: 30, dust: 20, elevation: 10, coverage: 10 });
    expect(sum(DEFAULT_WEIGHTS)).toBe(100);
  });

  test('ice: -1..+1 maps to 0..1', () => {
    expect(marsCategoryScores({ ...base, swimMedian: -1 }).ice).toBe(0);
    expect(marsCategoryScores({ ...base, swimMedian: 0 }).ice).toBe(0.5);
    expect(marsCategoryScores({ ...base, swimMedian: 1 }).ice).toBe(1);
  });

  test('dust: <=0.94 -> 0, >=0.96 -> 1, linear between', () => {
    expect(marsCategoryScores({ ...base, dci: 0.93 }).dust).toBe(0);
    expect(marsCategoryScores({ ...base, dci: 0.94 }).dust).toBe(0);
    expect(marsCategoryScores({ ...base, dci: 0.95 }).dust).toBeCloseTo(0.5, 10);
    expect(marsCategoryScores({ ...base, dci: 0.96 }).dust).toBe(1);
    expect(marsCategoryScores({ ...base, dci: 0.99 }).dust).toBe(1);
  });

  test('elevation: <=-2000 m -> 1, >=0 m -> 0, linear between', () => {
    expect(marsCategoryScores({ ...base, elevationM: -3400 }).elevation).toBe(1);
    expect(marsCategoryScores({ ...base, elevationM: -2000 }).elevation).toBe(1);
    expect(marsCategoryScores({ ...base, elevationM: -1000 }).elevation).toBe(0.5);
    expect(marsCategoryScores({ ...base, elevationM: 0 }).elevation).toBe(0);
    expect(marsCategoryScores({ ...base, elevationM: 500 }).elevation).toBe(0);
  });

  test('flatness and coverage pass through as fractions', () => {
    const s = marsCategoryScores({ ...base, flatFraction: 0.93, coverage: 0.96 });
    expect(s.flatness).toBe(0.93);
    expect(s.coverage).toBe(0.96);
  });

  test('weighted score under default weights', () => {
    const s = marsCategoryScores({ flatFraction: 0.93, swimMedian: 0.42, dci: 0.956, elevationM: -3400, coverage: 0.96 });
    // .3*.93 + .3*.71 + .2*.8 + .1*1 + .1*.96 = 0.848
    expect(weightedScore(s, DEFAULT_WEIGHTS)).toBeCloseTo(84.8, 6);
  });
});

describe('slider rebalancing', () => {
  test('always totals exactly 100 with non-negative integers', () => {
    let w = DEFAULT_WEIGHTS;
    let seed = 42;
    const next = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
    for (let i = 0; i < 500; i++) {
      const id = CATEGORY_IDS[Math.floor(next() * CATEGORY_IDS.length)];
      w = rebalance(w, id, Math.floor(next() * 101));
      expect(sum(w)).toBe(100);
      for (const k of CATEGORY_IDS) {
        expect(Number.isInteger(w[k])).toBe(true);
        expect(w[k]).toBeGreaterThanOrEqual(0);
      }
    }
  });

  test('setting one to 100 zeroes the rest; setting from all-zero spreads evenly', () => {
    const w = rebalance(DEFAULT_WEIGHTS, 'ice', 100);
    expect(w).toEqual({ flatness: 0, ice: 100, dust: 0, elevation: 0, coverage: 0 });
    const spread = rebalance(w, 'ice', 20);
    expect(sum(spread)).toBe(100);
    expect(spread.flatness).toBe(20);
  });

  test('others keep their proportions', () => {
    const w = rebalance(DEFAULT_WEIGHTS, 'flatness', 60); // others were 30/20/10/10 = 70, now share 40
    expect(w.flatness).toBe(60);
    expect(w.ice).toBe(17); // 30/70*40 = 17.14
    expect(w.dust).toBe(11); // 20/70*40 = 11.43
  });
});

describe('analog ranking', () => {
  test('deterministic, sorted, similarity in range, contributions sum to similarity', () => {
    const a = rankAnalogs('G-04', DEFAULT_WEIGHTS);
    const b = rankAnalogs('G-04', DEFAULT_WEIGHTS);
    expect(a.map((r) => r.id)).toEqual(b.map((r) => r.id));
    for (let i = 1; i < a.length; i++) expect(a[i - 1].similarity).toBeGreaterThanOrEqual(a[i].similarity);
    for (const r of a) {
      expect(r.similarity).toBeGreaterThanOrEqual(0);
      expect(r.similarity).toBeLessThanOrEqual(100);
      expect(CATEGORY_IDS.reduce((s, id) => s + r.points[id], 0)).toBeCloseTo(r.similarity, 6);
    }
  });

  test('moving weight to ice puts the closest ice match first', () => {
    const iceHeavy = rebalance(DEFAULT_WEIGHTS, 'ice', 90);
    const all = rankAnalogs('G-04', iceHeavy);
    const best = Math.max(...all.map((r) => r.scores.ice));
    expect(all[0].scores.ice).toBeGreaterThanOrEqual(best - 0.2);
    expect(all[0].scores.ice).toBeGreaterThan(all[all.length - 1].scores.ice);
  });

  test('every Mars site that falls back to sample data yields a ranking', () => {
    for (const s of MARS_SITES) if (resultsFor(s).tier === 'demo') expect(rankAnalogs(s.id, DEFAULT_WEIGHTS).length).toBeGreaterThan(0);
  });
});

describe('user-set bounds', () => {
  // Bounds maths needs a site with every value; no real site has computed values yet, so use a fixed profile.
  const raw: MarsRaw = { flatFraction: 0.93, swimMedian: 0.42, dci: 0.956, elevationM: -3400, coverage: 0.96 };

  test('default bounds are centred on the site value and inside the domain', () => {
    const b = defaultBounds(raw);
    const c = siteValues(raw);
    for (const id of BOUNDED_IDS) {
      const s = BOUND_SPECS[id];
      expect(b[id].lo).toBeGreaterThanOrEqual(s.min);
      expect(b[id].hi).toBeLessThanOrEqual(s.max);
      expect(isWithin(b[id], c[id])).toBe(true);
    }
    expect(b.elevation.lo).toBe(c.elevation - 1750);
    expect(b.elevation.hi).toBe(c.elevation + 1750);
  });

  test('setBound never crosses the other end and stays in the domain', () => {
    const b = defaultBounds(raw);
    expect(setBound(b, 'ice', 'lo', 5).ice.lo).toBe(b.ice.hi);
    expect(setBound(b, 'ice', 'hi', -5).ice.hi).toBe(b.ice.lo);
    expect(setBound(b, 'elevation', 'lo', -99999).elevation.lo).toBe(BOUND_SPECS.elevation.min);
  });

  test('membership is 1 at the centre, 0 at and beyond the bounds, linear between', () => {
    const b = { lo: 0.6, hi: 1 };
    expect(membership(0.8, 0.8, b)).toBe(1);
    expect(membership(0.6, 0.8, b)).toBe(0);
    expect(membership(1, 0.8, b)).toBe(0);
    expect(membership(0.7, 0.8, b)).toBeCloseTo(0.5, 10);
    expect(membership(0.5, 0.8, b)).toBe(0);
    expect(membership(0.9, 0.8, { lo: 0.6, hi: 1 })).toBeCloseTo(0.5, 10);
  });

  test('narrowing a bound never adds results; widening never removes them (hard filter)', () => {
    const base = defaultBounds(raw);
    const n0 = rankAnalogs('profile', DEFAULT_WEIGHTS, base, raw).length;
    for (const id of BOUNDED_IDS) {
      const s = BOUND_SPECS[id];
      const wide = setBound(setBound(base, id, 'lo', s.min), id, 'hi', s.max);
      const narrow = setBound(setBound(base, id, 'lo', siteValues(raw)[id]), id, 'hi', siteValues(raw)[id]);
      const nWide = rankAnalogs('profile', DEFAULT_WEIGHTS, wide, raw).length;
      const nNarrow = rankAnalogs('profile', DEFAULT_WEIGHTS, narrow, raw).length;
      expect(nWide).toBeGreaterThanOrEqual(n0);
      expect(nNarrow).toBeLessThanOrEqual(n0);
    }
  });

  test('everything returned is inside every bound', () => {
    const b = defaultBounds(raw);
    for (const r of rankAnalogs('profile', DEFAULT_WEIGHTS, b, raw)) for (const id of BOUNDED_IDS) expect(isWithin(b[id], r.values[id])).toBe(true);
  });

  test('zero-width bounds on every variable can leave nothing', () => {
    const c = siteValues(raw);
    let b = defaultBounds(raw);
    for (const id of BOUNDED_IDS) b = setBound(setBound(b, id, 'lo', c[id]), id, 'hi', c[id]);
    expect(rankAnalogs('profile', DEFAULT_WEIGHTS, b, raw).length).toBe(0);
  });
});
