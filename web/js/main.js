import { gradientCss, writeColor } from "./colormaps.js";
import {
  FAIL,
  NAMES,
  REGION_ORDER_LABELS,
  WEIGHTED,
  cloneSettings,
  createStabilityRun,
  eligibleDistribution,
  evaluate,
  explainCell,
  explainRegion,
  findRegions,
  flatIndex,
  flatnessAt,
  normalizedWeights,
  parseDataset,
  regionsToCsv,
} from "./scoring.js";

const L = window.L;
const CELL_SCALE = 4;
const HISTOGRAM_BINS = 40;
const TREK = "https://trek.nasa.gov/tiles/Mars/EQ";
const COMPONENT_COLORS = {
  flatness: "var(--flatness)",
  ice: "var(--ice)",
  dust: "var(--dust)",
  elevation: "var(--elevation)",
  coverage: "var(--coverage)",
};

const CRITERIA_UI = [
  {
    id: "latitude",
    title: "Latitude limit",
    bit: FAIL.LATITUDE,
    help: "Lower latitudes give more sunlight for solar power and milder temperatures. NASA's human landing-site workshop used ±50°.",
    histogram: { source: "lat", min: 0, max: 60, caption: "cell latitude (°N)" },
    sliders: [{ key: "max", label: "Require |latitude| ≤", min: 20, max: 60, step: 1, unit: "°", role: "requirement", side: "max" }],
  },
  {
    id: "flatness",
    title: "Flat terrain",
    bit: FAIL.FLATNESS,
    help: "Share of a cell's ~1 km MOLA pixels with slope at or below the threshold. Rocks and metre-scale slopes are invisible at this scale.",
    histogram: { source: "flatness", min: 0, max: 1, caption: "flat share per cell" },
    sliders: [
      { key: "slopeDeg", label: "Count as flat if slope ≤", min: 0.25, max: 10, step: 0.25, unit: "°", role: "parameter" },
      { key: "minFraction", label: "Require flat share ≥", min: 0, max: 1, step: 0.05, format: "pct", role: "requirement", side: "min" },
    ],
  },
  {
    id: "ice",
    title: "Ice evidence (SWIM)",
    bit: FAIL.ICE,
    help: "SWIM combines neutron, thermal, radar, and landform evidence: −1 inconsistent to +1 consistent with buried ice within ~5 m.",
    histogram: { source: "ice_median", min: -1, max: 1, caption: "median SWIM ice consistency" },
    sliders: [{ key: "min", label: "Require SWIM ≥", min: -1, max: 1, step: 0.05, digits: 2, role: "requirement", side: "min" }],
  },
  {
    id: "dust",
    title: "Low surface dust (TES DCI)",
    bit: FAIL.DUST,
    help: "Dust Cover Index ≤ 0.94 indicates dust-mantled ground and ≥ 0.96 relatively dust-free ground (Ruff & Christensen 2002). The score ramps between the two.",
    histogram: { source: "dci_median", min: 0.9, max: 0.99, caption: "median Dust Cover Index" },
    sliders: [
      { key: "dusty", label: "Zero credit at DCI ≤", min: 0.9, max: 0.98, step: 0.005, digits: 3, role: "parameter" },
      { key: "clear", label: "Full credit at DCI ≥", min: 0.905, max: 0.99, step: 0.005, digits: 3, role: "parameter" },
      { key: "min", label: "Require DCI ≥", min: 0.85, max: 0.99, step: 0.005, digits: 3, role: "requirement", side: "min" },
    ],
  },
  {
    id: "elevation",
    title: "Low elevation",
    bit: FAIL.ELEVATION,
    help: "Lower ground means more atmosphere to slow a lander. Full credit at or below the preferred elevation, falling to zero 2 km higher.",
    histogram: { source: "elevation_median_m", min: -5000, max: 2000, caption: "median elevation (m)" },
    sliders: [
      { key: "preferredMax", label: "Full credit at or below", min: -5000, max: 0, step: 100, unit: " m", role: "parameter" },
      { key: "maxAllowed", label: "Require median ≤", min: -5000, max: 2000, step: 100, unit: " m", role: "requirement", side: "max" },
    ],
  },
  {
    id: "coverage",
    title: "Data coverage",
    bit: FAIL.COVERAGE,
    help: "Share of a cell's pixels with valid data in every layer. Low coverage means a score rests on little data.",
    histogram: { source: "coverage", min: 0, max: 1, caption: "valid-data share per cell" },
    sliders: [{ key: "min", label: "Require coverage ≥", min: 0, max: 1, step: 0.05, format: "pct", role: "requirement", side: "min" }],
  },
];

const REGION_UI = [
  { key: "topPercent", label: "Region cells: top", min: 1, max: 30, step: 1, unit: "% of eligible cells" },
  { key: "minAreaKm2", label: "Minimum region area", min: 0, max: 2000, step: 50, unit: " km²" },
  { key: "count", label: "Regions to show", min: 1, max: 15, step: 1, unit: "" },
];

const state = {
  manifest: null,
  ds: null,
  settings: null,
  evaluation: null,
  regionResult: null,
  explanations: [],
  stability: [],
  stabilityProgress: null,
  stabilityToken: 0,
  stabilityTimer: null,
  overlayKey: "score",
  opacity: 0.75,
  selectedRank: null,
  pending: false,
  histograms: new Map(),
};

const map = {};
const pct = (value) => `${Math.round(value * 100)}%`;
const toLng = (lonE) => (lonE > 180 ? lonE - 360 : lonE);
const toLonE = (lng) => (lng < 0 ? lng + 360 : lng);
const fmtInt = (value) => Math.round(value).toLocaleString("en-US");
const fmtLat = (lat) => `${Math.abs(lat).toFixed(2)}°${lat >= 0 ? "N" : "S"}`;
const fmtLon = (lonE) => `${lonE.toFixed(2)}°E`;

function formatSlider(spec, value) {
  if (spec.format === "pct") return pct(value);
  if (spec.digits !== undefined) return value.toFixed(spec.digits);
  return `${Number.isInteger(spec.step) ? fmtInt(value) : value}${spec.unit ?? ""}`;
}

async function load() {
  const manifest = await (await fetch("data/arcadia.json")).json();
  const buffer = await (await fetch(`data/${manifest.cells_file}`)).arrayBuffer();
  state.manifest = manifest;
  state.ds = parseDataset(manifest, buffer);
  state.settings = cloneSettings(manifest.defaults);
}

// ---------- map ----------

function setupMap() {
  const { grid, hillshade } = state.manifest;
  const bounds = L.latLngBounds([grid.south, toLng(grid.west)], [grid.north, toLng(grid.east)]);
  const leaflet = L.map("map", { crs: L.CRS.EPSG4326, zoomSnap: 0.25, minZoom: 1 });
  const trek = (layer, attribution) =>
    L.tileLayer(`${TREK}/${layer}/1.0.0/default/default028mm/{z}/{y}/{x}.jpg`, {
      tileSize: 256,
      noWrap: true,
      maxNativeZoom: 7,
      maxZoom: 10,
      bounds: [[-90, -180], [90, 180]],
      attribution,
    });
  const mola = trek("Mars_MGS_MOLA_ClrShade_merge_global_463m", "MOLA shaded relief: NASA/JPL/GSFC via NASA Mars Trek");
  const viking = trek("Mars_Viking_MDIM21_ClrMosaic_global_232m", "Viking colour mosaic: NASA/USGS via NASA Mars Trek");
  const offline = L.layerGroup();
  mola.addTo(leaflet);

  const hillshadeLayer = L.imageOverlay(
    `data/${hillshade.file}`,
    [[hillshade.south, toLng(hillshade.west)], [hillshade.north, toLng(hillshade.east)]],
    { attribution: "Study-area hillshade from MOLA MEGDR" },
  ).addTo(leaflet);

  const canvas = document.createElement("canvas");
  canvas.width = grid.cols;
  canvas.height = grid.rows;
  const scaled = document.createElement("canvas");
  scaled.width = grid.cols * CELL_SCALE;
  scaled.height = grid.rows * CELL_SCALE;
  const dataLayer = L.imageOverlay(scaled.toDataURL(), bounds, {
    opacity: state.opacity,
    className: "pixelated",
    attribution: "SWIM (PSI) · TES DCI (ASU)",
  }).addTo(leaflet);
  const markers = L.layerGroup().addTo(leaflet);

  L.control
    .layers(
      {
        "NASA Trek · MOLA shaded relief": mola,
        "NASA Trek · Viking colour": viking,
        "No basemap (offline)": offline,
      },
      {
        "Study-area hillshade (offline)": hillshadeLayer,
        "Score / data layer": dataLayer,
        "Region numbers": markers,
      },
      { collapsed: true },
    )
    .addTo(leaflet);

  Object.assign(map, { leaflet, bounds, canvas, scaled, dataLayer, markers });
  addLayerControl();
  addLegend();
  addCoordinates();
  leaflet.fitBounds(bounds, { padding: [10, 10] });
  leaflet.on("click", (event) => showCellPopup(event.latlng));
}

const OVERLAYS = {
  score: { label: "Screening score (eligible cells)", cmap: "viridis", dimExcluded: true },
  raw: { label: "Score ignoring requirements", cmap: "viridis" },
  ice: { label: "SWIM ice consistency", cmap: "rdbu" },
  dust: { label: "TES Dust Cover Index", cmap: "cividis" },
  flatness: { label: "Flat share", cmap: "viridis" },
  elevation: { label: "Median elevation (m)", cmap: "elevation" },
  coverage: { label: "Data coverage", cmap: "viridis" },
  none: { label: "None" },
};

function overlayData(key) {
  const { ds, evaluation, settings } = state;
  const percentileRange = (values) => {
    const finite = Float32Array.from(values.filter(Number.isFinite)).sort();
    return finite.length ? [finite[Math.floor(finite.length * 0.02)], finite[Math.floor(finite.length * 0.98)]] : [0, 1];
  };
  switch (key) {
    case "score": {
      const sorted = eligibleDistribution(evaluation, "score");
      const range = sorted.length ? [sorted[Math.floor(sorted.length * 0.02)], sorted[sorted.length - 1]] : [0, 1];
      return { values: evaluation.score, range, digits: 2 };
    }
    case "raw":
      return { values: evaluation.raw, range: percentileRange(evaluation.raw), digits: 2 };
    case "ice":
      return { values: ds.fields.ice_median, range: [-0.7, 0.7], digits: 1 };
    case "dust":
      return { values: ds.fields.dci_median, range: [settings.dust.dusty - 0.02, settings.dust.clear + 0.01], digits: 3 };
    case "flatness":
      return { values: evaluation.components.flatness, range: [0, 1], digits: 1, suffix: ` (slope ≤ ${settings.flatness.slopeDeg}°)` };
    case "elevation":
      return { values: ds.fields.elevation_median_m, range: percentileRange(ds.fields.elevation_median_m), digits: 0 };
    case "coverage":
      return { values: ds.fields.coverage, range: [0, 1], digits: 1 };
    default:
      return null;
  }
}

function renderOverlay() {
  const { grid } = state.ds;
  const { rows, cols } = grid;
  const context = map.canvas.getContext("2d");
  const image = context.createImageData(cols, rows);
  const spec = OVERLAYS[state.overlayKey];
  const data = overlayData(state.overlayKey);
  if (data) {
    const [low, high] = data.range;
    const span = high - low || 1;
    for (let i = 0; i < state.ds.n; i += 1) {
      const value = data.values[i];
      const offset = i * 4;
      if (Number.isFinite(value)) {
        writeColor(spec.cmap, (value - low) / span, image.data, offset);
        image.data[offset + 3] = 255;
      } else if (spec.dimExcluded && state.evaluation.fail[i] && !(state.evaluation.fail[i] & FAIL.NO_DATA)) {
        image.data.set([110, 110, 110, 150], offset);
      }
    }
  }
  context.putImageData(image, 0, 0);

  const scaled = map.scaled.getContext("2d");
  scaled.clearRect(0, 0, map.scaled.width, map.scaled.height);
  scaled.imageSmoothingEnabled = false;
  scaled.drawImage(map.canvas, 0, 0, map.scaled.width, map.scaled.height);
  drawRegionOutlines(scaled);
  map.dataLayer.setUrl(map.scaled.toDataURL());
  renderLegend(spec, data);
}

function drawRegionOutlines(context) {
  const { rows, cols } = state.ds.grid;
  const regionOf = state.regionResult.regionOf;
  const normal = new Path2D();
  const selected = new Path2D();
  const segment = (path, x1, y1, x2, y2) => {
    path.moveTo(x1 * CELL_SCALE, y1 * CELL_SCALE);
    path.lineTo(x2 * CELL_SCALE, y2 * CELL_SCALE);
  };
  for (let row = 0; row < rows; row += 1) {
    for (let col = 0; col < cols; col += 1) {
      const i = row * cols + col;
      const id = regionOf[i];
      if (!id) continue;
      const path = id === state.selectedRank ? selected : normal;
      if (row === 0 || regionOf[i - cols] !== id) segment(path, col, row, col + 1, row);
      if (row === rows - 1 || regionOf[i + cols] !== id) segment(path, col, row + 1, col + 1, row + 1);
      if (col === 0 || regionOf[i - 1] !== id) segment(path, col, row, col, row + 1);
      if (col === cols - 1 || regionOf[i + 1] !== id) segment(path, col + 1, row, col + 1, row + 1);
    }
  }
  context.lineCap = "square";
  for (const [path, color, width] of [[normal, "#ffffff", 1.5], [selected, "#ff7a45", 2.5]]) {
    context.strokeStyle = "rgba(0,0,0,0.85)";
    context.lineWidth = width + 2;
    context.stroke(path);
    context.strokeStyle = color;
    context.lineWidth = width;
    context.stroke(path);
  }
}

function addLayerControl() {
  const control = L.control({ position: "topright" });
  control.onAdd = () => {
    const div = L.DomUtil.create("div", "map-control");
    const options = Object.entries(OVERLAYS)
      .map(([key, spec]) => `<option value="${key}">${spec.label}</option>`)
      .join("");
    div.innerHTML = `
      <label for="overlay-select">Map layer</label>
      <select id="overlay-select">${options}</select>
      <label for="overlay-opacity">Layer opacity</label>
      <input id="overlay-opacity" type="range" min="0" max="1" step="0.05" value="${state.opacity}">`;
    L.DomEvent.disableClickPropagation(div);
    L.DomEvent.disableScrollPropagation(div);
    div.querySelector("select").addEventListener("change", (event) => {
      state.overlayKey = event.target.value;
      renderOverlay();
    });
    div.querySelector("input").addEventListener("input", (event) => {
      state.opacity = Number(event.target.value);
      map.dataLayer.setOpacity(state.opacity);
    });
    return div;
  };
  control.addTo(map.leaflet);
}

function addLegend() {
  const control = L.control({ position: "bottomright" });
  control.onAdd = () => {
    map.legend = L.DomUtil.create("div", "map-control legend");
    return map.legend;
  };
  control.addTo(map.leaflet);
}

function renderLegend(spec, data) {
  if (!data) {
    map.legend.innerHTML = `<label>No data layer shown</label>`;
    return;
  }
  const [low, high] = data.range;
  map.legend.innerHTML = `
    <label>${spec.label}${data.suffix ?? ""}</label>
    <div class="legend-bar" style="background:${gradientCss(spec.cmap)}"></div>
    <div class="legend-range"><span>${low.toFixed(data.digits)}</span><span>${high.toFixed(data.digits)}</span></div>
    ${spec.dimExcluded ? `<div class="legend-note"><i></i>excluded by a requirement</div>` : ""}
    <div class="legend-note">Outlines: ranked regions · click any cell for details</div>`;
}

function addCoordinates() {
  const control = L.control({ position: "bottomleft" });
  control.onAdd = () => {
    const div = L.DomUtil.create("div", "map-control coords");
    div.textContent = "Move over the map";
    map.leaflet.on("mousemove", (event) => {
      div.textContent = `${fmtLat(event.latlng.lat)}  ${fmtLon(toLonE(event.latlng.lng))}`;
    });
    return div;
  };
  control.addTo(map.leaflet);
}

function cellAt(latlng) {
  const { grid } = state.ds;
  const col = Math.floor((toLonE(latlng.lng) - grid.west) / grid.cell_deg);
  const row = Math.floor((grid.north - latlng.lat) / grid.cell_deg);
  if (row < 0 || row >= grid.rows || col < 0 || col >= grid.cols) return -1;
  return row * grid.cols + col;
}

function showCellPopup(latlng) {
  const i = cellAt(latlng);
  if (i < 0) return;
  const cell = explainCell(state.ds, state.evaluation, i, state.settings);
  const region = state.regionResult.regionOf[i];
  const status = cell.eligible
    ? `<p class="eligible">Eligible · score ${cell.score.toFixed(3)}, better than ${pct(cell.percentile)} of eligible cells</p>`
    : `<p class="excluded">Excluded: ${cell.reasons.join("; ")}</p>`;
  const rows = cell.contributions
    .map(
      (item) => `<tr><td>${item.name}</td><td class="muted">${item.description}</td>
        <td class="num">${pct(item.weight)}</td><td class="num">${Number.isFinite(item.contribution) ? item.contribution.toFixed(3) : "n/a"}</td></tr>`,
    )
    .join("");
  L.popup({ maxWidth: 420 })
    .setLatLng(latlng)
    .setContent(`
      <div class="cell-popup">
        <h3>Cell at ${fmtLat(cell.lat)} ${fmtLon(cell.lon)}</h3>
        ${status}
        ${region ? `<p>Part of candidate region #${region}</p>` : ""}
        <table><tr class="muted"><td>Criterion</td><td>Value</td><td class="num">Weight</td><td class="num">Adds</td></tr>${rows}</table>
        <p class="muted">Relief ${fmtInt(cell.values.relief_m)} m · median slope ${cell.values.slope_median_deg.toFixed(2)}°</p>
      </div>`)
    .openOn(map.leaflet);
}

function renderMarkers() {
  map.markers.clearLayers();
  state.regionResult.regions.forEach((region, index) => {
    const tone = Number.isFinite(state.stability[index]) ? state.explanations[index].stabilityTone : "pending";
    const selected = region.rank === state.selectedRank ? " selected" : "";
    const marker = L.marker([region.lat, toLng(region.lon)], {
      icon: L.divIcon({
        className: `region-marker tone-${tone}${selected}`,
        html: `<span>${region.rank}</span>`,
        iconSize: [26, 26],
      }),
      title: `Region #${region.rank}`,
      keyboard: true,
    });
    marker.on("click", () => selectRegion(region.rank, { scroll: true }));
    map.markers.addLayer(marker);
  });
}

// ---------- controls ----------

function histogramValues(source) {
  const { ds, settings } = state;
  if (source === "flatness") {
    const index = flatIndex(ds, settings.flatness.slopeDeg);
    return Array.from({ length: ds.n }, (_, i) => flatnessAt(ds, i, index));
  }
  if (source === "lat") return ds.lat;
  return ds.fields[source];
}

function histogramCounts(spec) {
  const counts = new Array(HISTOGRAM_BINS).fill(0);
  const values = histogramValues(spec.source);
  const coverage = state.ds.fields.coverage;
  for (let i = 0; i < state.ds.n; i += 1) {
    const value = values[i];
    if (!(coverage[i] > 0) || !Number.isFinite(value)) continue;
    const t = (value - spec.min) / (spec.max - spec.min);
    counts[Math.min(HISTOGRAM_BINS - 1, Math.max(0, Math.floor(t * HISTOGRAM_BINS)))] += 1;
  }
  return counts;
}

function drawHistogram(criterion) {
  const entry = state.histograms.get(criterion.id);
  const { canvas, counts } = entry;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 300;
  const height = canvas.clientHeight || 46;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  context.clearRect(0, 0, width, height);
  const spec = criterion.histogram;
  const max = Math.max(...counts, 1);
  const barWidth = width / HISTOGRAM_BINS;
  context.fillStyle = "#4b5a6e";
  counts.forEach((count, bin) => {
    const barHeight = Math.sqrt(count / max) * (height - 4);
    context.fillRect(bin * barWidth + 0.5, height - barHeight, barWidth - 1, barHeight);
  });
  const x = (value) => ((value - spec.min) / (spec.max - spec.min)) * width;
  const settings = state.settings[criterion.id];
  for (const slider of criterion.sliders) {
    if (slider.key === "slopeDeg") continue;
    const value = settings[slider.key];
    if (slider.role === "requirement") {
      const position = Math.min(width, Math.max(0, x(value)));
      context.fillStyle = "rgba(248, 81, 73, 0.22)";
      if (slider.side === "min") context.fillRect(0, 0, position, height);
      else context.fillRect(position, 0, width - position, height);
      context.fillStyle = "#ff7a45";
      context.fillRect(position - 1, 0, 2, height);
    } else {
      context.strokeStyle = "#58a6ff";
      context.setLineDash([3, 3]);
      context.beginPath();
      context.moveTo(x(value), 0);
      context.lineTo(x(value), height);
      context.stroke();
      context.setLineDash([]);
    }
  }
}

function sliderRow(spec, value, onInput) {
  const wrapper = document.createElement("div");
  wrapper.className = `slider ${spec.role ?? "parameter"}`;
  const id = `slider-${Math.random().toString(36).slice(2)}`;
  const roleLabel = spec.role === "requirement" ? "Requirement" : spec.role === "weight" ? "Weight" : "Scoring";
  wrapper.innerHTML = `
    <label for="${id}"><span><span class="role">${roleLabel}</span>${spec.label}</span><output></output></label>
    <input id="${id}" type="range" min="${spec.min}" max="${spec.max}" step="${spec.step}">`;
  const input = wrapper.querySelector("input");
  const output = wrapper.querySelector("output");
  input.value = value;
  output.textContent = formatSlider(spec, value);
  input.addEventListener("input", () => {
    const next = Number(input.value);
    output.textContent = formatSlider(spec, next);
    onInput(next);
  });
  wrapper.setValue = (next) => {
    input.value = next;
    output.textContent = formatSlider(spec, next);
  };
  return wrapper;
}

function buildCriterionCard(criterion) {
  const settings = state.settings[criterion.id];
  const weighted = WEIGHTED.includes(criterion.id);
  const card = document.createElement("section");
  card.className = "card criterion";
  card.dataset.id = criterion.id;
  card.innerHTML = `
    <header>
      <label class="toggle"><input type="checkbox"><span class="title">${criterion.title}</span></label>
      <span class="share"></span>
    </header>
    <p class="help">${criterion.help}</p>
    <canvas class="histogram" aria-hidden="true"></canvas>
    <div class="hist-caption"><span>${criterion.histogram.min}</span><span>${criterion.histogram.caption}</span><span>${criterion.histogram.max}</span></div>
    <div class="sliders"></div>
    <p class="feedback"></p>`;
  const checkbox = card.querySelector("input[type=checkbox]");
  checkbox.checked = settings.enabled;
  checkbox.addEventListener("change", () => {
    settings.enabled = checkbox.checked;
    syncCardState(card, settings.enabled);
    scheduleRecompute();
  });

  const sliders = card.querySelector(".sliders");
  const rows = {};
  for (const spec of criterion.sliders) {
    rows[spec.key] = sliderRow(spec, settings[spec.key], (value) => {
      settings[spec.key] = value;
      if (criterion.id === "dust") keepDustOrder(spec.key, rows);
      if (spec.key === "slopeDeg") state.histograms.get("flatness").counts = histogramCounts(criterion.histogram);
      drawHistogram(criterion);
      scheduleRecompute();
    });
    sliders.append(rows[spec.key]);
  }
  if (weighted) {
    const weightSpec = { key: "weight", label: "Weight", min: 0, max: 1, step: 0.05, digits: 2, role: "weight" };
    sliders.append(
      sliderRow(weightSpec, settings.weight, (value) => {
        settings.weight = value;
        scheduleRecompute();
      }),
    );
  }
  state.histograms.set(criterion.id, {
    canvas: card.querySelector("canvas"),
    counts: histogramCounts(criterion.histogram),
  });
  syncCardState(card, settings.enabled);
  return card;
}

function keepDustOrder(changed, rows) {
  const dust = state.settings.dust;
  const gap = 0.005;
  if (changed === "dusty" && dust.dusty > dust.clear - gap) {
    dust.clear = Math.min(0.99, dust.dusty + gap);
    rows.clear.setValue(dust.clear);
  }
  if (changed === "clear" && dust.clear < dust.dusty + gap) {
    dust.dusty = Math.max(0.9, dust.clear - gap);
    rows.dusty.setValue(dust.dusty);
  }
}

function syncCardState(card, enabled) {
  card.classList.toggle("off", !enabled);
  card.querySelectorAll("input[type=range]").forEach((input) => {
    input.disabled = !enabled;
  });
}

function buildRegionCard() {
  const settings = state.settings.regions;
  const card = document.createElement("section");
  card.className = "card";
  card.innerHTML = `
    <header><span class="title">Candidate regions</span></header>
    <p class="help">Adjacent cells in the top share of eligible scores are grouped into regions, then ranked. A minimum area filters out isolated single-cell results that are more likely to be noise.</p>
    <div class="sliders"></div>`;
  const sliders = card.querySelector(".sliders");
  for (const spec of REGION_UI) {
    sliders.append(
      sliderRow({ ...spec, role: "parameter" }, settings[spec.key], (value) => {
        settings[spec.key] = value;
        scheduleRecompute();
      }),
    );
  }
  const rankBy = document.createElement("div");
  rankBy.className = "slider parameter";
  rankBy.innerHTML = `<label for="rank-by"><span><span class="role">Scoring</span>Rank regions by</span></label>
    <select id="rank-by">${Object.entries(REGION_ORDER_LABELS)
      .map(([key, label]) => `<option value="${key}">${label}</option>`)
      .join("")}</select>`;
  const select = rankBy.querySelector("select");
  select.value = settings.rankBy;
  select.addEventListener("change", () => {
    settings.rankBy = select.value;
    scheduleRecompute();
  });
  sliders.append(rankBy);
  const stability = state.settings.stability;
  sliders.append(
    sliderRow(
      { key: "perturbation", label: "Stability test: nudge by ±", min: 0.05, max: 0.5, step: 0.05, format: "pct", role: "parameter" },
      stability.perturbation,
      (value) => {
        stability.perturbation = value;
        scheduleRecompute();
      },
    ),
  );
  return card;
}

function buildControls() {
  state.histograms.clear();
  const container = document.getElementById("criteria");
  container.replaceChildren(...CRITERIA_UI.map(buildCriterionCard));
  document.getElementById("region-settings").replaceChildren(buildRegionCard());
  CRITERIA_UI.forEach(drawHistogram);
}

function renderFeedback() {
  const { evaluation, ds } = state;
  const weights = normalizedWeights(state.settings);
  const excluded = Object.fromEntries(CRITERIA_UI.map((criterion) => [criterion.id, 0]));
  for (let i = 0; i < ds.n; i += 1) {
    const fail = evaluation.fail[i];
    if (!fail || fail & FAIL.NO_DATA) continue;
    for (const criterion of CRITERIA_UI) if (fail & criterion.bit) excluded[criterion.id] += 1;
  }
  for (const criterion of CRITERIA_UI) {
    const card = document.querySelector(`.criterion[data-id="${criterion.id}"]`);
    const settings = state.settings[criterion.id];
    card.querySelector(".share").textContent = WEIGHTED.includes(criterion.id)
      ? settings.enabled && weights[criterion.id]
        ? `${pct(weights[criterion.id])} of score`
        : "not scored"
      : "requirement only";
    const count = excluded[criterion.id];
    card.querySelector(".feedback").innerHTML = !settings.enabled
      ? "Off: neither required nor scored."
      : count
        ? `<span class="excluded">Requirement excludes ${fmtInt(count)} cells.</span>`
        : "Requirement currently excludes no cells.";
  }
}

// ---------- results ----------

function renderComponentKey() {
  document.getElementById("component-key").innerHTML = WEIGHTED.map(
    (key) => `<span style="--swatch:${COMPONENT_COLORS[key]}">${NAMES[key]}</span>`,
  ).join("");
}

function renderRegions() {
  const { regions, allRegionCount, cutoff } = state.regionResult;
  const { evaluation, settings } = state;
  state.explanations = regions.map((region, index) =>
    explainRegion(evaluation, region, state.stability[index], settings),
  );
  const note = document.getElementById("results-note");
  if (!Object.keys(evaluation.weights).length) {
    note.textContent = "Every weighted criterion is off or has zero weight, so nothing can be ranked. Turn at least one back on.";
  } else if (!regions.length) {
    note.textContent = evaluation.eligibleCount
      ? `No region meets the ${fmtInt(settings.regions.minAreaKm2)} km² minimum area. Lower the minimum area or widen the top share of cells.`
      : "No cells satisfy every requirement. Relax a requirement slider.";
  } else {
    note.textContent = `Showing ${regions.length} of ${fmtInt(allRegionCount)} regions formed from the top ${settings.regions.topPercent}% of ${fmtInt(evaluation.eligibleCount)} eligible cells (score ≥ ${cutoff.toFixed(3)}), ranked by ${REGION_ORDER_LABELS[settings.regions.rankBy]}. Bar = each criterion's contribution to the mean score.`;
  }

  const list = document.getElementById("regions");
  list.replaceChildren(
    ...regions.map((region, index) => {
      const explanation = state.explanations[index];
      const tone = explanation.stabilityTone;
      const item = document.createElement("li");
      item.className = `region${region.rank === state.selectedRank ? " selected" : ""}`;
      item.dataset.rank = region.rank;
      const bar = Object.entries(evaluation.weights)
        .map(([key, weight]) => {
          const contribution = weight * region.components[key];
          return `<span style="width:${contribution * 100}%;background:${COMPONENT_COLORS[key]}" title="${NAMES[key]}: ${contribution.toFixed(3)}"></span>`;
        })
        .join("");
      item.innerHTML = `
        <button type="button" class="region-head">
          <span class="rank tone-${tone}">${region.rank}</span>
          <span class="where">
            <strong>${fmtLat(region.lat)} ${fmtLon(region.lon)}</strong>
            <small>${fmtInt(region.areaKm2)} km² · ${region.cellCount} cell${region.cellCount === 1 ? "" : "s"} · mean ${region.meanScore.toFixed(3)} · peak ${region.peakScore.toFixed(3)}</small>
          </span>
        </button>
        <p class="summary">${explanation.summary}</p>
        <div class="contrib" aria-label="Score contributions">${bar}</div>
        <p class="stability tone-${tone}">${explanation.stabilityText}</p>
        <details${region.rank === state.selectedRank ? " open" : ""}>
          <summary>Why it ranks here</summary>
          <ul class="factors">${explanation.lines.map((line) => `<li class="${line.tone}">${line.text}</li>`).join("")}</ul>
          <ul class="caveats">${explanation.caveats.map((caveat) => `<li>${caveat}</li>`).join("")}</ul>
        </details>`;
      item.querySelector(".region-head").addEventListener("click", () => selectRegion(region.rank, { fly: true }));
      return item;
    }),
  );
}

function selectRegion(rank, { scroll = false, fly = false } = {}) {
  state.selectedRank = rank;
  const region = state.regionResult.regions.find((candidate) => candidate.rank === rank);
  renderOverlay();
  renderMarkers();
  renderRegions();
  const item = document.querySelector(`.region[data-rank="${rank}"]`);
  if (item) {
    item.querySelector("details").open = true;
    if (scroll) item.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  if (fly && region) {
    const { bounds } = region;
    map.leaflet.flyToBounds(
      [[bounds.south, toLng(bounds.west)], [bounds.north, toLng(bounds.east)]],
      { maxZoom: 7, padding: [60, 60], duration: 0.8 },
    );
  }
}

function renderStatus() {
  const { evaluation, ds } = state;
  const progress = state.stabilityProgress;
  const stability = progress ? ` · stability ${progress.done}/${progress.trials}` : "";
  document.getElementById("status").textContent =
    `${fmtInt(evaluation.eligibleCount)} of ${fmtInt(ds.n)} cells eligible${stability}`;
}

function renderSources() {
  const { sources, caveats } = state.manifest;
  document.getElementById("sources").innerHTML = `
    <strong>Data</strong><br>${sources.map((source) => `<a href="${source.url}" target="_blank" rel="noopener">${source.name}</a>`).join("<br>")}
    <br><br><strong>Limitations</strong><br>${caveats.join("<br>")}`;
}

// ---------- pipeline ----------

function recompute() {
  const { ds, settings } = state;
  state.evaluation = evaluate(ds, settings);
  state.regionResult = findRegions(ds, state.evaluation, settings);
  state.stability = new Array(state.regionResult.regions.length).fill(NaN);
  if (!state.regionResult.regions.some((region) => region.rank === state.selectedRank)) {
    state.selectedRank = null;
  }
  renderOverlay();
  renderRegions();
  renderMarkers();
  renderFeedback();
  renderStatus();
  scheduleStability();
}

function scheduleRecompute() {
  if (state.pending) return;
  state.pending = true;
  requestAnimationFrame(() => {
    state.pending = false;
    recompute();
  });
}

function scheduleStability() {
  const token = ++state.stabilityToken;
  clearTimeout(state.stabilityTimer);
  state.stabilityProgress = null;
  const regions = state.regionResult.regions;
  if (!regions.length) return;
  state.stabilityTimer = setTimeout(() => {
    const run = createStabilityRun(state.ds, cloneSettings(state.settings), regions);
    const tick = () => {
      if (token !== state.stabilityToken) return;
      const finished = run.step(3);
      state.stabilityProgress = { done: run.done, trials: run.trials };
      renderStatus();
      if (!finished) {
        setTimeout(tick, 0);
        return;
      }
      state.stability = run.result();
      state.stabilityProgress = null;
      renderRegions();
      renderMarkers();
      renderStatus();
    };
    tick();
  }, 350);
}

function downloadCsv() {
  const { regions } = state.regionResult;
  const blob = new Blob([regionsToCsv(regions, state.explanations, state.stability)], { type: "text/csv" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "arcadia_candidate_regions.csv";
  link.click();
  URL.revokeObjectURL(link.href);
}

async function main() {
  try {
    await load();
  } catch (error) {
    document.getElementById("status").textContent =
      "Could not load web/data. Run `nasa-si export-web`, then open the map through `nasa-si serve`.";
    throw error;
  }
  setupMap();
  buildControls();
  renderComponentKey();
  renderSources();
  document.getElementById("reset").addEventListener("click", () => {
    state.settings = cloneSettings(state.manifest.defaults);
    state.selectedRank = null;
    buildControls();
    recompute();
  });
  document.getElementById("download").addEventListener("click", downloadCsv);
  window.addEventListener("resize", () => CRITERIA_UI.forEach(drawHistogram));
  recompute();
}

main();
