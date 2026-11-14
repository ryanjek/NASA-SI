# NASA-SI: Mars Analog Finder

NASA Space Apps Challenge 2026: identify Earth analogs for future Mars landing and outpost sites.

**Phase 1 (this code):** screen Arcadia Planitia for plausible landing / temporary-outpost
zones using open orbital data. These zones become the reference profile for the Earth analog
search in Phase 2. This is exploratory regional screening, **not** landing-site certification.

## Quick start

```bash
uv sync
uv run nasa-si download core     # MOLA topography, SWIM ice, TES Dust Cover Index (~350 MB)
uv run nasa-si screen            # align, score, and rank Arcadia candidate cells (~20 s)
uv run pytest                    # unit tests
```

If `nasa-si` reports `No module named 'nasa_si'`, macOS has hidden the editable-install `.pth`
file (iCloud "Desktop & Documents" sync does this to `.venv` inside `~/Documents`). Either move
the repository outside iCloud-synced folders or run:

```bash
PYTHONPATH=src .venv/bin/python -m nasa_si.cli screen
```

## Data

| Layer | Source | Use | Native resolution |
| --- | --- | --- | --- |
| Elevation | [MOLA MEGDR](https://pds-geosciences.wustl.edu/missions/mgs/megdr.html) | elevation, slope, relief | 1/64 deg (~0.93 km) |
| Ice consistency | [SWIM Arcadia pilot](https://swim.psi.edu/PilotStudyProducts.php) | subsurface-ice evidence, -1 to +1 | 3 km |
| Dust Cover Index | [TES DCI](https://tes.asu.edu/~ruff/DCI/dci.html) | surface dust, emissivity 0.85-0.99 | 1/16 deg (~3.7 km) |

`nasa-si catalog` lists every URL; `nasa-si inspect FILE...` prints projection, NoData, and
value ranges. Downloads write a `manifest.json` with SHA-256 checksums.

Important data facts the pipeline handles:

- All three are simple-cylindrical rasters, so they are aligned on MOLA's 1/64 degree
  grid in planetocentric latitude and **0-360 deg east** longitude.
- MOLA is centred on 180 deg E; SWIM uses metres on a slightly different Mars sphere; the DCI
  file starts at -180 deg. Each is converted explicitly rather than assumed.
- DCI pixels equal to `0.85` are unobserved fill, not dusty terrain, and are masked.

## Method

1. Crop to the SWIM Arcadia swath (188-208 deg E, 0-60 deg N).
2. Compute slope from MOLA at native resolution, correcting east-west pixel width by latitude.
3. Group pixels into ~9 km candidate cells (`--cell-pixels 10`).
4. Score each cell from five transparent components (weights in `ScreeningProfile`):

| Component | Weight | Definition |
| --- | --- | --- |
| Flatness | 0.30 | fraction of pixels with slope <= 5 deg at ~1 km |
| Ice | 0.30 | median SWIM consistency rescaled from -1..1 to 0..1 |
| Dust | 0.20 | DCI ramp: <= 0.94 dust-mantled (0) to >= 0.96 relatively dust-free (1) |
| Elevation | 0.10 | 1 at <= -2 km, falling to 0 at the MOLA datum |
| Coverage | 0.10 | fraction of pixels with valid data in every layer |

5. Apply hard gates: |latitude| <= 50 deg and >= 80% valid coverage.
6. Report the top candidates at least 150 km apart, so they represent distinct zones.

## Outputs (`data/processed/`)

| File | Contents |
| --- | --- |
| `arcadia_screening.png` | maps of every layer with candidates, plus score breakdowns |
| `arcadia_candidates.csv` | distinct shortlisted cells with all metrics |
| `arcadia_cells.csv` | every cell, its gates, and its global rank |
| `arcadia_layers.tif` | aligned 5-band raster for QGIS (elevation, slope, ice, DCI, dust score) |
| `arcadia_cell_scores.tif` | per-cell score components for QGIS |
| `arcadia_run.json` | inputs, grid, profile, candidates, and caveats for reproducibility |

## Current findings and limitations

- At ~1 km resolution nearly all Arcadia plains are flat and below -2 km, so flatness and
  elevation barely discriminate; the ranking is driven by ice evidence and dust.
- Most of Arcadia is dust-mantled (median DCI below 0.94); top candidates are rare low-dust patches.
- The DCI shows north-south striping from TES orbit tracks. Verify that each low-dust candidate
  is not a single-track artifact before treating it as real.
- Ice evidence strengthens poleward, so several candidates sit near the 50 deg latitude gate.
- MOLA cannot resolve rocks, small craters, or metre-scale slopes. Shortlisted cells need CTX,
  HiRISE, and THEMIS thermal-inertia review before becoming reference zones.
