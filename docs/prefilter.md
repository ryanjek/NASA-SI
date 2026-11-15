# Stage 1: 50 km coarse pre-filter

Searching every ~1 km window on Earth against each analog seed is wasteful. Stage 1 cuts Earth to the
~51 km patches that **could** contain a match. Stage 2 (fine 1 km comparison, a teammate) runs only
inside the survivors.

```bash
uv sync --extra earth                  # pyhdf for the MODIS HDF4 files
uv run nasa-si prefilter               # all four located seeds
uv run nasa-si prefilter --seed drill_hill --months 1,7
```

## Grid and hand-off

HEALPix **NESTED**: coarse nside 128 (196,608 patches, ~2,594 km2, ~50.9 km) and fine nside 8192
(~0.63 km2, ~0.80 km), so each patch holds exactly 4,096 fine cells with ids
`patch*4096 ... patch*4096+4095`. No lookup table: `parent = fine_id // 4096`.

Stage 2 reads `data/processed/prefilter/union/survivors_union.csv`
(`patch_id, lon, lat, child_start, n_children, coarse_nside, fine_nside, seeds`) or a single seed's
`survivors.csv`. The 0.8 km fine cell is the nearest power of two to "1 km"; confirm it with the stage-2
teammate. The team must also agree on one grid (the Mars code uses lat/lon cells, the Earth map uses
EPSG:6933).

## What it keeps

A patch is dropped only if some gate fails. A gate fails only when the patch's `[min, max]` for that
feature cannot overlap the seed's acceptance interval `[seed value - tol, seed value + tol]`.
Consequences, all tested:

* any patch holding a fine cell with data inside every gate survives (recall property);
* a patch with no data at all is unknown and survives;
* widening a tolerance never removes survivors;
* the seed's own patch survives (self-recall).

Coarse pixels that straddle patches widen every patch they touch (centre, corners and edge midpoints).

## Seeds and gates

Seeds are Phase 1 sites that are located and `transferable_to_similarity_search = true`
(`config/phase1/`, copied from Ryan's catalog). Candidate gates are the `global_search` features that
support the processes of the seed's analog family. `config/prefilter_features.csv` marks each one
`gate`, `informative` or `deferred` with a reason. Elevation is never a gate.

**Every feature names its dataset.** Each row carries the `dataset_id` it is built from, and the loader
rejects any dataset that is not in `datasets.csv` and linked to that feature in `feature_datasets.csv`.
The Drive folder holds only catalog tables, no rasters (all datasets are `cataloged_not_downloaded`).
NDVI, snow and precipitation use coarse monthly products that are not catalogued either, so each is a
labelled stand-in row in our copy of the catalog (see below).

**Stand-in.** The Drive names `modis_lst` = MOD11A1 (daily, 1 km), too large to pull globally for a coarse
filter. We read MOD11C3 (monthly, 0.05 deg, the same algorithm aggregated) and added it to our copy of the
catalog as `modis_mod11c3`, an `alternate` for both temperature features, flagged
`downloaded_stand_in` (see `config/phase1/README.md`). Monthly means read summer maxima a little cool and
winter minima a little warm; the 5 degC tolerance absorbs that. Stage 2 should use MOD11A1 at 1 km.

| gate | proxy | tolerance |
|---|---|---|
| `lst_summer_max_c` | max monthly daytime LST (MOD11C3, 0.05 deg) | 15 degC (calibrated, below) |
| `lst_winter_min_c` | min monthly night LST (MOD11C3) | 15 degC (calibrated, below) |
| `ndvi_summer_median` | greenest month of monthly NDVI (MOD13C2, stand-in for Sentinel-2) | 0.15, **not calibrated** |
| `snow_persistence` | mean of monthly snow-cover fraction (MOD10CM, stand-in for MOD10A1) | 0.3, **not calibrated** |
| `precip_mm_yr` | annual sum of MERRA-2 PRECTOTLAND (stand-in for NASA POWER), all 12 months of one year | 300 mm/yr, **not calibrated** |

The temperature tolerance is fixed by the calibration below, not tuned to recover any site. The NDVI, snow
and precipitation tolerances are wide values fixed before the run, because no fine-scale reference for them
is on hand to calibrate against (the same method needs MOD13A1, MOD10A1 and station or POWER data).
**Ryan should review all of them.** The MERRA-2 grid (about 55 km) is the size of a patch, so it is repeated
onto a 4x finer grid before use; otherwise the nine-point footprint credit would not be safe.

Which gates a seed gets depends on its processes and on whether the seed itself has a valid value. A feature
with no valid value at the seed is unknown and prunes nothing: NDVI and precipitation have none at the
Antarctic seeds (no good-quality NDVI on ice; MERRA-2 land excludes land ice).

## Latest run (temperature, NDVI, snow, precipitation; all 12 months of 2025)

| seed | gates | land patches | survive | removed |
|---|---|---|---|---|
| university_valley | 3 (LST x2, snow) | 65,189 | 4,593 | 93.0% |
| beacon_central_polygon | 3 (LST x2, snow) | 65,189 | 4,619 | 92.9% |
| hiseas_pahoehoe_radius | 1 (NDVI) | 65,189 | 29,030 | 55.5% |
| drill_hill | 2 (LST x2) | 65,189 | 12,338 | 81.1% |

Union across seeds: 35,531 patches. The land-patch count rose from 65,021 because MERRA-2 and MOD10CM add
a few coastal patches with no valid temperature; those are unknown on temperature and pass. NDVI gives
HI-SEAS its first gate. Snow adds little: the Antarctic seeds read 100% snow cover at 5.6 km, and the gate removes only 9 (University
Valley) and 16 (Beacon) patches beyond what the two temperature gates already remove.
The seed patch survives for every seed. Not yet done for the three new gates: the pixel-level miss check and
a fine-scale recall test.

## Earlier run (temperature only, MOD11C3, all 12 months of 2025)

| seed | gates | land patches | survive | removed |
|---|---|---|---|---|
| university_valley | 2 | 65,021 | 4,434 | 93.2% |
| beacon_central_polygon | 2 | 65,021 | 4,467 | 93.1% |
| hiseas_pahoehoe_radius | 0 | 65,021 | 65,021 | 0% |
| drill_hill | 2 | 65,021 | 12,170 | 81.3% |

(Tolerance 15 degC; with the earlier 5 degC these were 1,672 / 1,891 / 1,582 survivors.)

HI-SEAS has only deferred gates (terrain, lithology, EMIT, bare fraction) once NDVI is excluded, so it
prunes nothing until a Copernicus GLO-30 or GLiM layer is added; the union across seeds is all land.

Checks on this run: every 0.05 deg pixel inside all of a seed's gate ranges (about 100,000 to 350,000 per
seed) falls in a surviving patch (0 missed); survivors rise smoothly with tolerance; reruns give identical
output. With a full year the gates follow each hemisphere's own summer and winter: Drill Hill (75 N) keeps
the Canadian Arctic, Greenland and Siberian coast plus the Tibetan plateau, and the Antarctic seeds keep
Antarctic margins.

## Fine-scale sample test (1 km MOD11A1, two tiles)

Downloaded the Drive's own `modis_lst` (MOD11A1, daily 1 km) for the tile around University Valley
(h21v16) and Drill Hill (h15v01), one summer and one winter month each (`data/raw/sample/`, 561 MB). The
test asks: take every 1 km pixel in the tile whose daily summer max and winter min are within 5 degC of the
seed's own 1 km values, and check its 50 km patch survived the coarse filter.

| seed | fine pixels matching | in surviving patches | seed gap fine - coarse (summer / winter) |
|---|---|---|---|
| drill_hill | 218,434 | 99.97% | +5.7 / -6.3 degC |
| university_valley | 19,864 | **89.9%** | +9.5 / -1.2 degC |

Fine daily extremes sit well away from the coarse monthly means: over each tile, fine minus coarse has
median about +7 degC (summer max) and -7 to -9 degC (winter min), p05 to p95 about 9 degC wide. The filter
stays recall-preserving only if the tolerance covers *how much that gap differs between the seed and a
candidate*. At 5 degC it does for Drill Hill and misses about 10% of University Valley's matches, because
that valley's coarse value (-20 degC) is unusually cold next to its 1 km value (-11 degC).

Recall of those fine matches against the coarse tolerance (survivors as share of land in brackets):

| coarse tolerance | university_valley | drill_hill |
|---|---|---|
| 5 degC | 89.9% (2.6%) | 100% (2.4%) |
| 8 degC | 98.8% (4.0%) | 100% (4.8%) |
| 10 degC | 99.3% (4.9%) | 100% (7.7%) |
| 12 degC | 100% (5.8%) | 100% (13.1%) |

### Calibration (non-seed tiles)

Phase 1 forbids tuning to recover seeds, so the tolerance comes from tiles that are not seeds: Sahara,
Tibet, Atacama, Siberia, Greenland and East Antarctica (MOD11A1 daily 1 km, January and July 2025,
2.5 GB in `data/raw/sample/`). For every 1 km pixel, gap = fine daily extreme (summer max, winter min) minus
the coarse MOD11C3 value built from the same two months. Gaps are large and vary by place: summer-max
median +5.5 (Sahara) to +10.2 (Tibet), p95 up to +16; winter-min median -4.4 to -10.4.

A seed and a candidate can sit in different tiles, so the filter must cover the *difference* between their
gaps. Rule, fixed before the search: tolerance = fine tolerance (5 degC) + p95 of |gap_seed - gap_candidate|
over random pairs pooled across the six tiles, rounded up. That is 5 + 9.5 (summer max) and 5 + 9.7 (winter
min), so **15 degC** for both (`data/processed/prefilter/gap_calibration.json`).

Check on the seed tiles (not used to set it): with 15 degC every 1 km pixel matching the seed's own values
within 5 degC is in a surviving patch (University Valley 19,864 of 19,864, Drill Hill 218,434 of 218,434).
The cost is selectivity: the filter now keeps 7 to 19% of land per seed instead of 2 to 3%. Pair-sampling
ignores that a patch's min/max already widens with within-patch variation, so 15 degC is conservative.
Quality flags: MOD11A1 daytime LST near University Valley is only ever "other quality", so both quality
levels are accepted on the 1 km side. Limits: January and July only, one year, temperature gates only.

## Limits

* **One year only (2025).** "Summer max" is the hottest monthly-mean daytime LST of the year and
  "winter min" the coldest monthly-mean night LST. A single year can be unusually warm or cold; a
  multi-year climatology would be steadier.
* Only four located seeds, so this tests plumbing and self-consistency, not generalization. The
  benchmark sites pruned in the report are informational, not an accuracy score.
* The filter is coarse (5.6 km pixels vs 1 km features); padded tolerances cover that, at the cost of
  keeping more patches.
* Terrain (slope, relief), snow, climate (days above 0 degC, precipitation, humidity), lithology and
  EMIT are deferred (see the csv for each reason). A seed whose only candidate gates are deferred
  prunes nothing. Terrain needs a global ~1 km DEM (Phase B).
* A fine cell with missing data is not a candidate at stage 2 and cannot keep a patch alive.
* Residual risk: a patch vertex poking into a pixel without covering any of its nine footprint points.
