import { describe, expect, test } from 'bun:test';
import { MARS_SITES } from '../data/marsSites';
import { ANTARCTIC_LAT, STAGE1, STAGE2, isAntarctic, loadSurvivors, resultsFor, selectResults, type Stage1Seed, type Stage2Cell } from './pipeline';

const cell = (seed: string, rank: number, similarity: number): Stage2Cell => ({
  seed, rank, similarity, fineCellId: rank, patchId: 0, lat: 0, lon: 0, distance: null, featuresMatched: 0, matched: [], features: {}, opticalError: false,
});
const s1 = (survivors: number): Stage1Seed => ({ name: 'x', lon: null, lat: null, survivors });

describe('tier selection', () => {
  test('stage 2 wins when any mapped seed has ranked cells', () => {
    const r = selectResults(['a', 'b'], [cell('b', 1, 0.5)], { a: s1(10) });
    expect(r.tier).toBe('stage2');
    if (r.tier === 'stage2') expect(r.groups.map((g) => g.seed)).toEqual(['b']);
  });

  test('stage 1 when there are no stage 2 cells but survivors exist', () => {
    const r = selectResults(['a', 'b'], [cell('z', 1, 0.5)], { b: s1(42) });
    expect(r).toEqual({ tier: 'stage1', groups: [{ seed: 'b', label: 'x', survivors: 42 }] });
  });

  test('demo only when neither exists', () => {
    expect(selectResults(['a'], [], {}).tier).toBe('demo');
    expect(selectResults([], [cell('a', 1, 1)], { a: s1(1) }).tier).toBe('demo');
    expect(selectResults(['a'], [], { a: s1(0) }).tier).toBe('demo');
  });
});

describe('no cross-seed similarity comparison', () => {
  test('each seed keeps its own rank order, even when another seed scores far higher', () => {
    const cells = [cell('low', 2, 0.002), cell('high', 1, 0.5), cell('low', 1, 0.001), cell('high', 2, 0.9)];
    const r = selectResults(['low', 'high'], cells, {});
    expect(r.tier).toBe('stage2');
    if (r.tier !== 'stage2') return;
    // Groups follow the seed order of the Mars site, never the scores.
    expect(r.groups.map((g) => g.seed)).toEqual(['low', 'high']);
    for (const g of r.groups) {
      expect(g.cells.every((c) => c.seed === g.seed)).toBe(true);
      expect(g.cells.map((c) => c.rank)).toEqual([1, 2]);
    }
    // Rank, not similarity, orders cells: 'high' rank 1 (0.5) stays ahead of rank 2 (0.9).
    expect(r.groups[1].cells.map((c) => c.similarity)).toEqual([0.5, 0.9]);
  });
});

describe('real data wiring', () => {
  test('Phoenix gets stage 2 results; University Valley cells are all Antarctic, so only Drill Hill remains', () => {
    const r = resultsFor(MARS_SITES.find((s) => s.id === 'G-04')!);
    expect(r.tier).toBe('stage2');
    if (r.tier === 'stage2') expect(r.groups.map((g) => g.seed)).toEqual(['drill_hill']);
  });

  test('Antarctica is never suggested', () => {
    expect(STAGE2.some((c) => isAntarctic(c.lat))).toBe(false);
    for (const s of MARS_SITES) {
      const r = resultsFor(s);
      if (r.tier === 'stage2') for (const g of r.groups) expect(g.cells.every((c) => c.lat > ANTARCTIC_LAT)).toBe(true);
    }
  });

  test('new rankings: Gale uses the Dyngjusandur seeds, Arsia Mons the Mauna Loa lava tubes', () => {
    const tiers = Object.fromEntries(MARS_SITES.map((s) => [s.id, resultsFor(s)]));
    const seedsOf = (k: string) => { const r = tiers[k]; return r.tier === 'stage2' ? r.groups.map((g) => g.seed) : r.tier; };
    expect(seedsOf('N-01')).toEqual(['dyngjusandur', 'dyngjusandur_central', 'dyngjusandur_sw']);
    expect(seedsOf('N-02')).toEqual(['hiseas_pahoehoe_radius']);
    expect(seedsOf('N-04')).toEqual(['mauna_loa_lava_tubes']);
  });

  test('every Mars site resolves to a tier, and every number comes from the data files', () => {
    for (const s of MARS_SITES) {
      const r = resultsFor(s);
      if (r.tier === 'stage2') for (const g of r.groups) expect(g.cells.length).toBe(STAGE2.filter((c) => c.seed === g.seed).length);
      if (r.tier === 'stage1') for (const g of r.groups) expect(g.survivors).toBe(STAGE1[g.seed].survivors);
    }
  });
});

test('stage 1 survivors contain no Antarctic patches', async () => {
  const pts = await loadSurvivors();
  for (const flat of Object.values(pts)) for (let i = 1; i < flat.length; i += 2) expect(flat[i]).toBeGreaterThan(ANTARCTIC_LAT);
});

test('Mars values come from mars_values.json; missing values keep a reason', () => {
  const g = MARS_SITES.find((s) => s.id === 'G-04')!;
  expect(g.raw.elevationM).toBe(-4121);
  expect(g.raw.dci).toBeNull();
  expect(g.missing?.dci).toBeTruthy();
  for (const s of MARS_SITES) for (const [k, v] of Object.entries(s.raw)) if (v == null) expect(s.missing?.[k as keyof typeof s.raw]).toBeTruthy();
});
