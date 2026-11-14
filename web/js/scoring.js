// Pure scoring, requirement, region, stability, and explanation logic for the
// Arcadia map. Mirrors nasa_si.model.score_cells; keep the two in sync.

export const WEIGHTED = ["flatness", "ice", "dust", "elevation", "coverage"];

export const FAIL = {
  NO_DATA: 1,
  LATITUDE: 2,
  COVERAGE: 4,
  FLATNESS: 8,
  ICE: 16,
  DUST: 32,
  ELEVATION: 64,
};

export const FAIL_LABELS = {
  [FAIL.NO_DATA]: "no valid data",
  [FAIL.LATITUDE]: "outside the latitude limit",
  [FAIL.COVERAGE]: "too little valid data",
  [FAIL.FLATNESS]: "not enough flat terrain",
  [FAIL.ICE]: "ice evidence below the requirement",
  [FAIL.DUST]: "too dusty for the requirement",
  [FAIL.ELEVATION]: "too high for the requirement",
};

export const NAMES = {
  flatness: "Flat terrain",
  ice: "Ice evidence",
  dust: "Low surface dust",
  elevation: "Low elevation",
  coverage: "Data coverage",
};

const clamp01 = (value) => Math.min(1, Math.max(0, value));

export function parseDataset(manifest, buffer) {
  const grid = manifest.grid;
  const n = grid.rows * grid.cols;
  if (n !== manifest.cell_count) throw new Error("Manifest cell count does not match grid");
  const fields = {};
  for (const [name, spec] of Object.entries(manifest.fields)) {
    fields[name] = new Float32Array(buffer, spec.offset, n);
  }
  const flat = manifest.flatness;
  const flatness = new Uint8Array(buffer, flat.offset, n * flat.thresholds_deg.length);

  const kmPerDeg = (Math.PI * manifest.mars_radius_km) / 180;
  const nsKm = grid.cell_deg * kmPerDeg;
  const lat = new Float32Array(n);
  const lon = new Float32Array(n);
  const areaKm2 = new Float32Array(n);
  for (let row = 0; row < grid.rows; row += 1) {
    const latitude = grid.north - (row + 0.5) * grid.cell_deg;
    const ewKm = nsKm * Math.cos((latitude * Math.PI) / 180);
    for (let col = 0; col < grid.cols; col += 1) {
      const i = row * grid.cols + col;
      lat[i] = latitude;
      lon[i] = grid.west + (col + 0.5) * grid.cell_deg;
      areaKm2[i] = nsKm * ewKm;
    }
  }
  return {
    manifest,
    grid,
    n,
    fields,
    flatness,
    flatThresholds: flat.thresholds_deg,
    flatScale: flat.scale,
    flatMissing: flat.missing,
    lat,
    lon,
    areaKm2,
  };
}

export function cloneSettings(settings) {
  return JSON.parse(JSON.stringify(settings));
}

export function flatIndex(dataset, slopeDeg) {
  let best = 0;
  dataset.flatThresholds.forEach((threshold, index) => {
    if (Math.abs(threshold - slopeDeg) < Math.abs(dataset.flatThresholds[best] - slopeDeg)) {
      best = index;
    }
  });
  return best;
}

export function flatnessAt(dataset, i, index) {
  const value = dataset.flatness[i * dataset.flatThresholds.length + index];
  return value === dataset.flatMissing ? NaN : value / dataset.flatScale;
}

export function normalizedWeights(settings) {
  const raw = {};
  let total = 0;
  for (const key of WEIGHTED) {
    const criterion = settings[key];
    if (criterion.enabled && criterion.weight > 0) {
      raw[key] = criterion.weight;
      total += criterion.weight;
    }
  }
  const weights = {};
  for (const [key, weight] of Object.entries(raw)) weights[key] = weight / total;
  return weights;
}

export function evaluate(dataset, settings) {
  const { n, fields } = dataset;
  const weights = normalizedWeights(settings);
  const weightEntries = Object.entries(weights);
  const tIndex = flatIndex(dataset, settings.flatness.slopeDeg);
  const { latitude, coverage, flatness, ice, dust, elevation } = settings;
  const dustSpan = Math.max(1e-6, dust.clear - dust.dusty);

  const components = {};
  for (const key of WEIGHTED) components[key] = new Float32Array(n);
  const raw = new Float32Array(n);
  const score = new Float32Array(n);
  const fail = new Uint8Array(n);
  let eligibleCount = 0;

  for (let i = 0; i < n; i += 1) {
    const cov = fields.coverage[i];
    if (!(cov > 0)) {
      fail[i] = FAIL.NO_DATA;
      for (const key of WEIGHTED) components[key][i] = NaN;
      raw[i] = NaN;
      score[i] = NaN;
      continue;
    }
    const flat = flatnessAt(dataset, i, tIndex);
    const iceMedian = fields.ice_median[i];
    const dci = fields.dci_median[i];
    const elev = fields.elevation_median_m[i];

    components.coverage[i] = cov;
    components.flatness[i] = flat;
    components.ice[i] = clamp01((iceMedian + 1) / 2);
    components.dust[i] = clamp01((dci - dust.dusty) / dustSpan);
    components.elevation[i] = clamp01(
      (elevation.preferredMax + elevation.margin - elev) / elevation.margin,
    );

    let total = weightEntries.length ? 0 : NaN;
    for (const [key, weight] of weightEntries) total += weight * components[key][i];
    raw[i] = total;

    let failed = 0;
    if (latitude.enabled && Math.abs(dataset.lat[i]) > latitude.max) failed |= FAIL.LATITUDE;
    if (coverage.enabled && cov < coverage.min) failed |= FAIL.COVERAGE;
    if (flatness.enabled && !(flat >= flatness.minFraction)) failed |= FAIL.FLATNESS;
    if (ice.enabled && !(iceMedian >= ice.min)) failed |= FAIL.ICE;
    if (dust.enabled && !(dci >= dust.min)) failed |= FAIL.DUST;
    if (elevation.enabled && !(elev <= elevation.maxAllowed)) failed |= FAIL.ELEVATION;
    if (!Number.isFinite(total)) failed |= FAIL.NO_DATA;
    fail[i] = failed;
    score[i] = failed ? NaN : total;
    if (!failed) eligibleCount += 1;
  }
  return {
    settings,
    weights,
    flatIndex: tIndex,
    components,
    raw,
    score,
    fail,
    eligibleCount,
    distributions: new Map(),
  };
}

export function eligibleDistribution(evaluation, key) {
  if (!evaluation.distributions.has(key)) {
    const source = key === "score" ? evaluation.score : evaluation.components[key];
    const values = [];
    for (let i = 0; i < source.length; i += 1) {
      if (evaluation.fail[i] === 0 && Number.isFinite(source[i])) values.push(source[i]);
    }
    evaluation.distributions.set(key, Float32Array.from(values).sort());
  }
  return evaluation.distributions.get(key);
}

function lowerBound(sorted, value) {
  let low = 0;
  let high = sorted.length;
  while (low < high) {
    const mid = (low + high) >>> 1;
    if (sorted[mid] < value) low = mid + 1;
    else high = mid;
  }
  return low;
}

export function rankStats(sorted, value, tolerance = 1e-4) {
  if (!sorted.length || !Number.isFinite(value)) return { below: NaN, atLeast: NaN, median: NaN };
  const below = lowerBound(sorted, value - tolerance) / sorted.length;
  const atLeast = 1 - below;
  return { below, atLeast, median: sorted[Math.floor((sorted.length - 1) / 2)] };
}

export function scoreCutoff(evaluation, topPercent) {
  const sorted = eligibleDistribution(evaluation, "score");
  if (!sorted.length) return NaN;
  const fraction = Math.min(1, Math.max(0, topPercent / 100));
  const topCount = Math.max(1, Math.ceil(fraction * sorted.length - 1e-9));
  return sorted[sorted.length - Math.min(topCount, sorted.length)];
}

export const REGION_ORDER = {
  mean: (a, b) => b.meanScore - a.meanScore || b.peakScore - a.peakScore,
  peak: (a, b) => b.peakScore - a.peakScore || b.meanScore - a.meanScore,
  area: (a, b) => b.areaKm2 - a.areaKm2 || b.meanScore - a.meanScore,
};

export const REGION_ORDER_LABELS = {
  mean: "highest mean score",
  peak: "highest single-cell score",
  area: "largest contiguous area",
};

export function findRegions(dataset, evaluation, settings) {
  const { rows, cols } = dataset.grid;
  const n = dataset.n;
  const cutoff = scoreCutoff(evaluation, settings.regions.topPercent);
  const regionOf = new Int16Array(n);
  if (!Number.isFinite(cutoff)) return { regions: [], allRegionCount: 0, regionOf, cutoff };

  const candidate = new Uint8Array(n);
  for (let i = 0; i < n; i += 1) {
    candidate[i] = evaluation.fail[i] === 0 && evaluation.score[i] >= cutoff ? 1 : 0;
  }
  const label = new Int32Array(n).fill(-1);
  const found = [];
  const stack = [];
  for (let start = 0; start < n; start += 1) {
    if (!candidate[start] || label[start] !== -1) continue;
    const id = found.length;
    const cells = [];
    label[start] = id;
    stack.push(start);
    while (stack.length) {
      const i = stack.pop();
      cells.push(i);
      const row = Math.floor(i / cols);
      const col = i - row * cols;
      const neighbours = [
        row > 0 ? i - cols : -1,
        row < rows - 1 ? i + cols : -1,
        col > 0 ? i - 1 : -1,
        col < cols - 1 ? i + 1 : -1,
      ];
      for (const j of neighbours) {
        if (j >= 0 && candidate[j] && label[j] === -1) {
          label[j] = id;
          stack.push(j);
        }
      }
    }
    found.push(summarizeRegion(dataset, evaluation, cells));
  }

  const kept = found
    .filter((region) => region.areaKm2 >= settings.regions.minAreaKm2)
    .sort(REGION_ORDER[settings.regions.rankBy] ?? REGION_ORDER.mean)
    .slice(0, settings.regions.count);
  kept.forEach((region, index) => {
    region.rank = index + 1;
    for (const i of region.cells) regionOf[i] = region.rank;
  });
  return { regions: kept, allRegionCount: found.length, regionOf, cutoff };
}

function summarizeRegion(dataset, evaluation, cells) {
  const { fields } = dataset;
  let area = 0;
  let scoreSum = 0;
  let latSum = 0;
  let lonSum = 0;
  let peak = cells[0];
  const componentSums = Object.fromEntries(WEIGHTED.map((key) => [key, 0]));
  const fieldSums = Object.fromEntries(Object.keys(fields).map((key) => [key, 0]));
  let minRow = Infinity;
  let maxRow = -Infinity;
  let minCol = Infinity;
  let maxCol = -Infinity;
  const cols = dataset.grid.cols;
  for (const i of cells) {
    const a = dataset.areaKm2[i];
    area += a;
    scoreSum += evaluation.score[i];
    latSum += dataset.lat[i] * a;
    lonSum += dataset.lon[i] * a;
    if (evaluation.score[i] > evaluation.score[peak]) peak = i;
    for (const key of WEIGHTED) componentSums[key] += evaluation.components[key][i];
    for (const key of Object.keys(fields)) fieldSums[key] += fields[key][i];
    const row = Math.floor(i / cols);
    const col = i - row * cols;
    minRow = Math.min(minRow, row);
    maxRow = Math.max(maxRow, row);
    minCol = Math.min(minCol, col);
    maxCol = Math.max(maxCol, col);
  }
  const count = cells.length;
  const { grid } = dataset;
  return {
    cells,
    cellCount: count,
    areaKm2: area,
    meanScore: scoreSum / count,
    peakScore: evaluation.score[peak],
    peakCell: peak,
    lat: latSum / area,
    lon: lonSum / area,
    bounds: {
      north: grid.north - minRow * grid.cell_deg,
      south: grid.north - (maxRow + 1) * grid.cell_deg,
      west: grid.west + minCol * grid.cell_deg,
      east: grid.west + (maxCol + 1) * grid.cell_deg,
    },
    components: Object.fromEntries(WEIGHTED.map((key) => [key, componentSums[key] / count])),
    values: Object.fromEntries(Object.keys(fields).map((key) => [key, fieldSums[key] / count])),
  };
}

export function mulberry32(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function perturbSettings(dataset, settings, rng, amount) {
  const jitter = () => 1 + amount * (2 * rng() - 1);
  const next = cloneSettings(settings);
  for (const key of WEIGHTED) next[key].weight *= jitter();
  const thresholds = dataset.flatThresholds;
  next.flatness.slopeDeg = Math.min(
    thresholds[thresholds.length - 1],
    Math.max(thresholds[0], next.flatness.slopeDeg * jitter()),
  );
  const span = next.dust.clear - next.dust.dusty;
  next.dust.dusty += span * amount * (2 * rng() - 1);
  next.dust.clear = Math.max(next.dust.dusty + span * 0.25, next.dust.clear + span * amount * (2 * rng() - 1));
  next.elevation.preferredMax += next.elevation.margin * amount * (2 * rng() - 1);
  next.regions.topPercent *= jitter();
  return next;
}

export function createStabilityRun(dataset, settings, baseRegions, options = {}) {
  const trials = options.trials ?? settings.stability.trials;
  const amount = options.perturbation ?? settings.stability.perturbation;
  const rng = mulberry32(options.seed ?? 20261114);
  const hits = new Array(baseRegions.length).fill(0);
  let done = 0;
  return {
    trials,
    get done() {
      return done;
    },
    step(count) {
      const stop = Math.min(trials, done + count);
      for (; done < stop; done += 1) {
        const trial = perturbSettings(dataset, settings, rng, amount);
        const { regionOf } = findRegions(dataset, evaluate(dataset, trial), trial);
        baseRegions.forEach((region, index) => {
          if (region.cells.some((i) => regionOf[i] > 0)) hits[index] += 1;
        });
      }
      return done >= trials;
    },
    result() {
      return hits.map((hit) => (done ? hit / done : NaN));
    },
  };
}

const pct = (value) => `${Math.round(value * 100)}%`;
const fixed = (value, digits) => (Number.isFinite(value) ? value.toFixed(digits) : "n/a");

export function describeValue(key, values, settings, components) {
  switch (key) {
    case "flatness":
      return `${pct(components.flatness)} of terrain has slope ≤ ${settings.flatness.slopeDeg}° at ~1 km (95th-percentile slope ${fixed(values.slope_p95_deg, 1)}°)`;
    case "ice": {
      const ice = values.ice_median;
      const label = ice >= 0.25 ? "strongly consistent with ice" : ice >= 0 ? "leans consistent with ice" : "leans inconsistent with ice";
      return `SWIM ${fixed(ice, 2)}, ${label}`;
    }
    case "dust": {
      const dci = values.dci_median;
      const label = dci >= settings.dust.clear ? "relatively dust-free" : dci <= settings.dust.dusty ? "dust-mantled" : "intermediate dust";
      return `DCI ${fixed(dci, 3)}, ${label}`;
    }
    case "elevation":
      return `median ${Math.round(values.elevation_median_m).toLocaleString("en-US")} m`;
    case "coverage":
      return `${pct(values.coverage)} of pixels valid in every layer`;
    default:
      return "";
  }
}

export function explainRegion(evaluation, region, stability, settings) {
  const factors = Object.entries(evaluation.weights).map(([key, weight]) => {
    const value = region.components[key];
    const stats = rankStats(eligibleDistribution(evaluation, key), value);
    return {
      key,
      weight,
      value,
      ...stats,
      advantage: weight * (value - stats.median),
      shared: stats.atLeast > 0.5,
    };
  });
  factors.sort((a, b) => b.advantage - a.advantage);

  const lines = factors.map((factor) => {
    const what = `${NAMES[factor.key]} (${pct(factor.weight)} of score): ${describeValue(factor.key, region.values, settings, region.components)}`;
    let tone;
    let verdict;
    if (factor.shared) {
      tone = "neutral";
      verdict = `shared by ${pct(factor.atLeast)} of eligible cells, so it does not set this region apart`;
    } else if (factor.advantage > 0) {
      tone = "positive";
      verdict = `better than ${pct(factor.below)} of eligible cells`;
    } else {
      tone = "negative";
      verdict = `weaker than ${pct(factor.atLeast)} of eligible cells; this holds it back`;
    }
    return { key: factor.key, tone, text: `${what} — ${verdict}.` };
  });

  const drivers = factors.filter((factor) => factor.advantage > 0 && !factor.shared);
  const driverText = drivers
    .slice(0, 2)
    .map((factor) => NAMES[factor.key].toLowerCase())
    .join(" and ");
  const area = `${Math.round(region.areaKm2).toLocaleString("en-US")} km²`;
  let summary;
  if (settings.regions.rankBy === "area") {
    summary = `Ranked #${region.rank} for its size: ${area} of contiguous top-scoring terrain`;
    summary += drivers.length ? `, standing out for ${driverText}.` : ".";
  } else if (!drivers.length) {
    summary = `Ranked #${region.rank}, but no criterion clearly sets it apart; treat it as a near-tie.`;
  } else if (settings.regions.rankBy === "peak") {
    summary = `Ranked #${region.rank} for its best cell (score ${region.peakScore.toFixed(3)}), mainly for ${driverText}.`;
  } else {
    summary = `Ranked #${region.rank} mainly for ${driverText}.`;
  }

  const caveats = [];
  if (drivers.slice(0, 2).some((factor) => factor.key === "dust")) {
    caveats.push("Low dust is a main reason: check the Dust layer for TES orbit-track stripes before trusting it.");
  }
  if (region.cellCount <= 2) {
    caveats.push(`Only ${region.cellCount} cell${region.cellCount === 1 ? "" : "s"}; small regions are more likely to be noise.`);
  }
  if (evaluation.weights.flatness) {
    caveats.push("Flatness is measured on ~1 km pixels; rocks, small craters, and metre-scale slopes are not assessed.");
  }
  caveats.push("Exploratory ranking, not landing certification: inspect CTX and HiRISE imagery next.");

  let stabilityText = "Stability is being computed…";
  let stabilityTone = "pending";
  if (Number.isFinite(stability)) {
    stabilityTone = stability >= 0.8 ? "robust" : stability >= 0.5 ? "moderate" : "fragile";
    stabilityText = `${stabilityTone[0].toUpperCase()}${stabilityTone.slice(1)}: stays in the top ${settings.regions.count} in ${pct(stability)} of runs with weights and scoring thresholds nudged ±${pct(settings.stability.perturbation)} (requirements held fixed).`;
  }
  return { summary, lines, caveats, stabilityText, stabilityTone };
}

export function failureReasons(mask) {
  return Object.entries(FAIL_LABELS)
    .filter(([bit]) => mask & Number(bit))
    .map(([, label]) => label);
}

export function explainCell(dataset, evaluation, i, settings) {
  const values = Object.fromEntries(
    Object.keys(dataset.fields).map((key) => [key, dataset.fields[key][i]]),
  );
  const components = Object.fromEntries(
    WEIGHTED.map((key) => [key, evaluation.components[key][i]]),
  );
  const contributions = Object.entries(evaluation.weights).map(([key, weight]) => ({
    key,
    name: NAMES[key],
    weight,
    component: components[key],
    contribution: weight * components[key],
    description: describeValue(key, values, settings, components),
  }));
  const fail = evaluation.fail[i];
  return {
    lat: dataset.lat[i],
    lon: dataset.lon[i],
    eligible: fail === 0,
    reasons: failureReasons(fail),
    score: evaluation.raw[i],
    percentile: fail === 0 ? rankStats(eligibleDistribution(evaluation, "score"), evaluation.score[i]).below : NaN,
    contributions,
    values,
  };
}

export function regionsToCsv(regions, explanations, stability) {
  const header = [
    "rank", "latitude_deg", "longitude_east_deg", "area_km2", "cells", "mean_score", "peak_score",
    ...WEIGHTED.map((key) => `${key}_score`),
    "ice_median", "dci_median", "elevation_median_m", "stability", "summary",
  ];
  const quote = (value) => `"${String(value).replaceAll('"', '""')}"`;
  const rows = regions.map((region, index) => [
    region.rank,
    region.lat.toFixed(4),
    region.lon.toFixed(4),
    region.areaKm2.toFixed(0),
    region.cellCount,
    region.meanScore.toFixed(4),
    region.peakScore.toFixed(4),
    ...WEIGHTED.map((key) => region.components[key].toFixed(4)),
    region.values.ice_median.toFixed(3),
    region.values.dci_median.toFixed(4),
    region.values.elevation_median_m.toFixed(0),
    Number.isFinite(stability[index]) ? stability[index].toFixed(2) : "",
    quote(explanations[index].summary),
  ]);
  return [header.join(","), ...rows.map((row) => row.join(","))].join("\n") + "\n";
}
