import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { test } from "node:test";

import {
  FAIL,
  createStabilityRun,
  evaluate,
  explainRegion,
  findRegions,
  normalizedWeights,
  parseDataset,
  regionsToCsv,
} from "./scoring.js";

const THRESHOLDS = [1, 5];
const FIELD_NAMES = [
  "elevation_median_m",
  "ice_median",
  "dci_median",
  "coverage",
  "slope_median_deg",
  "slope_p95_deg",
  "relief_m",
  "python_default_score",
];

function defaults() {
  return {
    latitude: { enabled: true, max: 50 },
    coverage: { enabled: true, min: 0.8, weight: 0.1 },
    flatness: { enabled: true, slopeDeg: 5, minFraction: 0, weight: 0.3 },
    ice: { enabled: true, min: -1, weight: 0.3 },
    dust: { enabled: true, dusty: 0.94, clear: 0.96, min: 0.85, weight: 0.2 },
    elevation: { enabled: true, preferredMax: -2000, margin: 2000, maxAllowed: 2000, weight: 0.1 },
    regions: { topPercent: 50, minAreaKm2: 0, count: 8, rankBy: "mean" },
    stability: { trials: 20, perturbation: 0.2 },
  };
}

function syntheticDataset(rows, cols, cell) {
  const n = rows * cols;
  const bytes = FIELD_NAMES.length * n * 4 + n * THRESHOLDS.length;
  const buffer = new ArrayBuffer(bytes);
  const fields = {};
  let offset = 0;
  FIELD_NAMES.forEach((name) => {
    fields[name] = { dtype: "float32", offset };
    offset += n * 4;
  });
  const manifest = {
    mars_radius_km: 3396.19,
    cell_count: n,
    grid: { west: 190, north: 45, cell_deg: 1, rows, cols },
    fields,
    flatness: { offset, thresholds_deg: THRESHOLDS, scale: 200, missing: 255 },
  };
  const views = Object.fromEntries(
    FIELD_NAMES.map((name) => [name, new Float32Array(buffer, fields[name].offset, n)]),
  );
  const flat = new Uint8Array(buffer, offset, n * THRESHOLDS.length);
  for (let i = 0; i < n; i += 1) {
    const values = cell(i);
    for (const name of FIELD_NAMES) views[name][i] = values[name] ?? 0;
    flat[i * 2] = Math.round((values.flat1 ?? 1) * 200);
    flat[i * 2 + 1] = Math.round((values.flat5 ?? 1) * 200);
  }
  return parseDataset(manifest, buffer);
}

const goodCell = {
  elevation_median_m: -4000,
  ice_median: 0,
  dci_median: 0.97,
  coverage: 1,
  flat1: 0.5,
  flat5: 1,
};

test("score is the normalized weighted sum of component scores", () => {
  const dataset = syntheticDataset(1, 1, () => goodCell);
  const evaluation = evaluate(dataset, defaults());
  assert.ok(Math.abs(evaluation.score[0] - (0.3 + 0.3 * 0.5 + 0.2 + 0.1 + 0.1)) < 1e-6);
});

test("disabling a criterion renormalizes the remaining weights", () => {
  const settings = defaults();
  settings.dust.enabled = false;
  const weights = normalizedWeights(settings);
  assert.equal(weights.dust, undefined);
  assert.ok(Math.abs(Object.values(weights).reduce((a, b) => a + b, 0) - 1) < 1e-9);
  assert.ok(Math.abs(weights.ice - 0.375) < 1e-9);
});

test("flatness uses the selected slope threshold", () => {
  const dataset = syntheticDataset(1, 1, () => goodCell);
  const settings = defaults();
  settings.flatness.slopeDeg = 1;
  assert.ok(Math.abs(evaluate(dataset, settings).components.flatness[0] - 0.5) < 1e-6);
});

test("requirements exclude cells and record every failure reason", () => {
  const dataset = syntheticDataset(1, 2, (i) => (i === 0 ? goodCell : { ...goodCell, ice_median: -0.5, coverage: 0.5 }));
  const settings = defaults();
  settings.ice.min = 0;
  const evaluation = evaluate(dataset, settings);
  assert.equal(evaluation.fail[0], 0);
  assert.equal(evaluation.fail[1], FAIL.ICE | FAIL.COVERAGE);
  assert.ok(Number.isNaN(evaluation.score[1]));
  assert.equal(evaluation.eligibleCount, 1);
});

test("unchecked criteria apply neither requirements nor weight", () => {
  const dataset = syntheticDataset(1, 1, () => ({ ...goodCell, ice_median: -0.9 }));
  const settings = defaults();
  settings.ice.min = 0;
  settings.ice.enabled = false;
  const evaluation = evaluate(dataset, settings);
  assert.equal(evaluation.fail[0], 0);
  assert.equal(evaluation.weights.ice, undefined);
});

test("cells without data are never eligible", () => {
  const dataset = syntheticDataset(1, 1, () => ({ ...goodCell, coverage: 0 }));
  const evaluation = evaluate(dataset, defaults());
  assert.equal(evaluation.fail[0], FAIL.NO_DATA);
});

test("regions group adjacent top cells and rank by mean score", () => {
  // Two high-scoring clusters separated by a poor column.
  const strong = new Set([0, 1, 4, 5]);
  const weaker = new Set([3, 7]);
  const dataset = syntheticDataset(2, 4, (i) => {
    if (strong.has(i)) return { ...goodCell, ice_median: 0.8 };
    if (weaker.has(i)) return { ...goodCell, ice_median: 0.4 };
    return { ...goodCell, ice_median: -1, dci_median: 0.9 };
  });
  const settings = defaults();
  settings.regions.topPercent = 100;
  const evaluation = evaluate(dataset, settings);
  settings.regions.topPercent = 75;
  const { regions, regionOf } = findRegions(dataset, evaluation, settings);
  assert.equal(regions.length, 2);
  assert.deepEqual([...regions[0].cells].sort(), [0, 1, 4, 5]);
  assert.equal(regionOf[0], 1);
  assert.equal(regionOf[3], 2);
  assert.ok(regions[0].meanScore > regions[1].meanScore);
});

test("regions can be ranked by contiguous area instead of mean score", () => {
  const dataset = syntheticDataset(1, 5, (i) => {
    if (i === 0) return { ...goodCell, ice_median: 0.9 };
    if (i === 1) return { ...goodCell, ice_median: -1, dci_median: 0.9 };
    return { ...goodCell, ice_median: 0.5 };
  });
  const settings = defaults();
  settings.regions.topPercent = 80;
  const evaluation = evaluate(dataset, settings);
  assert.deepEqual(findRegions(dataset, evaluation, settings).regions[0].cells, [0]);
  settings.regions.rankBy = "area";
  assert.equal(findRegions(dataset, evaluation, settings).regions[0].cellCount, 3);
});

test("minimum area removes small regions", () => {
  const dataset = syntheticDataset(1, 3, (i) => (i === 1 ? { ...goodCell, ice_median: -1 } : goodCell));
  const settings = defaults();
  settings.regions.topPercent = 66;
  const evaluation = evaluate(dataset, settings);
  assert.equal(findRegions(dataset, evaluation, settings).regions.length, 2);
  settings.regions.minAreaKm2 = dataset.areaKm2[0] * 1.5;
  assert.equal(findRegions(dataset, evaluation, settings).regions.length, 0);
});

test("stability is a reproducible fraction between 0 and 1", () => {
  const dataset = syntheticDataset(3, 3, (i) => ({ ...goodCell, ice_median: i === 4 ? 0.9 : -0.2 }));
  const settings = defaults();
  settings.regions.topPercent = 20;
  const evaluation = evaluate(dataset, settings);
  const { regions } = findRegions(dataset, evaluation, settings);
  const first = createStabilityRun(dataset, settings, regions);
  first.step(100);
  const second = createStabilityRun(dataset, settings, regions);
  second.step(100);
  assert.deepEqual(first.result(), second.result());
  for (const value of first.result()) assert.ok(value >= 0 && value <= 1);
});

test("explanations name the driving criterion and flag shared ones", () => {
  const dataset = syntheticDataset(1, 4, (i) => ({ ...goodCell, ice_median: i === 0 ? 0.8 : -0.2 }));
  const settings = defaults();
  settings.regions.topPercent = 25;
  const evaluation = evaluate(dataset, settings);
  const { regions } = findRegions(dataset, evaluation, settings);
  const explanation = explainRegion(evaluation, regions[0], 0.9, settings);
  assert.match(explanation.summary, /mainly for ice evidence/);
  assert.equal(explanation.lines[0].key, "ice");
  assert.ok(explanation.lines.some((line) => line.key === "flatness" && line.tone === "neutral"));
  assert.match(explanation.stabilityText, /^Robust/);
  assert.match(regionsToCsv(regions, [explanation], [0.9]), /^rank,latitude_deg/);
});

const manifestPath = new URL("../data/arcadia.json", import.meta.url);

test("default browser scores match the Python model on the exported Arcadia data", { skip: !existsSync(manifestPath) }, () => {
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  const bytes = readFileSync(new URL(`../data/${manifest.cells_file}`, import.meta.url));
  const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  const dataset = parseDataset(manifest, buffer);
  const evaluation = evaluate(dataset, manifest.defaults);
  const reference = dataset.fields.python_default_score;
  let compared = 0;
  let close = 0;
  let maxDifference = 0;
  for (let i = 0; i < dataset.n; i += 1) {
    if (!Number.isFinite(reference[i])) continue;
    const difference = Math.abs(evaluation.raw[i] - reference[i]);
    compared += 1;
    if (difference < 1e-3) close += 1;
    maxDifference = Math.max(maxDifference, difference);
  }
  assert.ok(compared > 40000);
  assert.ok(close / compared > 0.995, `only ${close}/${compared} within 1e-3`);
  assert.ok(maxDifference < 0.05, `max difference ${maxDifference}`);
});
