# NASA-SI | Earth analog search for Mars

NASA Space Apps Challenge 2026. This project starts with studied **Earth sites** that help explain particular features or processes on Mars. It measures those reference sites, screens broad areas of Earth, and ranks smaller Earth windows that resemble each reference. For example, University Valley is an Earth analog for ground ice near the Phoenix landing site. A Phase 3 result for University Valley is another location **on Earth**.

## Start here

- **See the results:** Open [the combined rankings for all 15 sites](outputs/all_15_sites_ranked_candidates.csv). Filter by `nearest_reference_site` and sort by `site_rank` to see one site's shortlist. [Separate run exports](outputs/) are also available.
- **Run a first search:** Follow Setup, then run the one-cell test in Phase 3. You can use the bundled survivor list and either generate Phase 2 references or copy the saved table from the project VM.
- **Repeat the full workflow:** Follow Phases 1–3 in order. The original expanded runs used a few VM-local inputs described below. The GitHub CSVs are enough to inspect results without those inputs.

| Step | Input | Output |
|---|---|---|
| Phase 1: catalog | Field studies and source citations | Earth seed coordinates, Mars target descriptions, and feature definitions |
| Phase 2: measure the references | The catalog, online raster and climate data, local geology and impact files | `reference_features.parquet`, one row per located, eligible reference site |
| Coarse screen: choose where to search | Seed coordinates, global coarse data, and filter rules | `survivors.csv` for each seed, and an optional union with seed membership |
| Phase 3: search and rank | Survivor patches and the Phase 2 table | Measurements and similarity rankings for sampled 1 km Earth windows |

## Setup

These commands use Bash on Linux or a VM. Run them in a terminal, or give them to Antigravity while it is connected to the VM. Commands after `cd NASA-SI` assume the repository root.

```bash
git clone https://github.com/ryanjek/NASA-SI.git
cd NASA-SI
python3 --version                   # Python 3.12 or later
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python scripts/extraction.py --help
.venv/bin/python scripts/sim_search.py --help
```

If you are on the project VM, go to the existing checkout with `cd ~/NASA-SI`. Reuse its `.venv` if it already works. If `sim_search.py --help` reports `No module named 'healpy'`, install it in that environment with `.venv/bin/python -m pip install healpy`. The terminal commands below use `.venv/bin/python` explicitly, so they work without activating the environment or running `uv`.

A fresh clone includes the site catalog, the original three-seed prefilter exports, and published rankings. Live extraction needs HTTPS access to Microsoft Planetary Computer and, for climate features, NASA POWER. Large raw rasters, the exact local GLiM geodatabase, the reference Parquet, and the expanded pilot inputs are VM-local.

## Phase 1 | Read and check the catalog

- [`data/phase1/sites.csv`](data/phase1/sites.csv) contains 30 Earth catalog entries. The search uses the 15 located `science_subregion` entries marked `transferable_to_similarity_search=true`. The other entries provide context, contrasts, or operational information.
- [`data/phase1/processes.csv`](data/phase1/processes.csv) joins to `sites.csv` on `site_id`. Its `mars_target` column describes the Martian feature or process. Some targets name a place, such as the Stimson Formation in Gale Crater.
- [`features.csv`](data/phase1/features.csv), [`feature_datasets.csv`](data/phase1/feature_datasets.csv), and [`datasets.csv`](data/phase1/datasets.csv) explain what each feature represents and where its data comes from. See the [methods and table schema](docs/phase1/methods.md) for the distinction between features we can screen globally and measurements that require field evidence.

```bash
.venv/bin/python scripts/validate_phase1.py
```

**Known catalog validation issue:** the current expanded `sites.csv` uses `approximate_point` for 12 rows. The validator does not recognize that label, and the corresponding 12 points are absent from the older `sites.geojson`. It reports 24 errors on the current catalog. The Phase 2 and 3 scripts read their inputs directly and the published runs used the CSV. Treat these coordinates as approximate and review any new validation errors separately.

## Phase 2 | Measure the 15 Earth references

Before running:

- Check for a local GLiM vector dataset under `data/phase1/source_data/glim/`. The published run used **`LiMW_GIS 2015.gdb`**, layer `GLiM_export`. Copy that geodatabase directory from the project data if you need matching lithology. The bundled raster-format GLiM files are a different product layout. If the vector is unavailable, lithology is reported as missing.
- The repo includes [the impact coordinates CSV](data/phase1/source_data/impact/earth_impact_craters.csv). The published VM run also had the same catalog under `confirmed_impacts.csv`.
- The published measurements used **2022–2024**. Use `--modis-source pc` to fetch MODIS through Planetary Computer. This path does not require Earth Engine credentials.

Run a single reference first. `--only` takes the exact `site_id` from the catalog.

```bash
.venv/bin/python -u scripts/extraction.py \
  --phase1 data/phase1 --out data/phase2_smoke \
  --modis-source pc --start 2022 --end 2024 --only drill_hill
cat data/phase2_smoke/extraction_status.csv
```

Then generate the full reference table, or use the published run's saved table if it is already on the VM.

```bash
.venv/bin/python -u scripts/extraction.py \
  --phase1 data/phase1 --out data/phase2 \
  --modis-source pc --start 2022 --end 2024
cat data/phase2/extraction_status.csv
```

The primary output is `data/phase2/reference_features.parquet`. **The existing VM run is saved under `data/phase 2/reference_features.parquet`**, with a space. In Phase 3, pass the path you actually have and quote it. Read `<site>/<site>_all_sources_report.json` for provenance and missing features. The published table had 15 populated features per reference. Three EMIT mineral fields were missing; the current extractor can discover EMIT granules but does not calculate those mineral fractions.

## Coarse screen | Prepare the survivor list

A survivor is an eligible **parent patch**, about 51 km on an equal-area HEALPix NESTED grid. The prefilter compares seed feature bands with each patch's min/max values. A survivor may contain a good match somewhere inside it. Phase 3 tests that with finer measurements.

**Fast start:** Use the tracked [three-seed balanced list](data/prefilter_vm/union/survivors_mvp_stratified.csv) or [full three-seed union](data/prefilter_vm/union/survivors_union.csv). For custom sampling, prepare a separate CSV of the parents you want and keep the corresponding `seeds` assignments. See [the sampler](scripts/balance_search.py) for the original three-seed logic. Running it writes to its configured path and its current sample count can differ from the published 240-cell run.

**Generate a new baseline screen:** With monthly MOD11C3 HDF4 files in a local raw directory, install `pyhdf` and run the stock prefilter:

```bash
.venv/bin/python -m pip install pyhdf
.venv/bin/nasa-si prefilter \
  --raw data/earth/raw --out data/phase3/coarse_baseline \
  --seed university_valley --seed beacon_central_polygon --seed drill_hill
```

Use the `--raw` directory containing your own monthly files on a fresh machine. This CLI uses the older `config/phase1` catalog and checked-in coarse feature configuration. The saved VM exports include additional layers and different gates, so a stock CLI rerun produces a new baseline, not necessarily the original counts. The [prefilter notes](docs/prefilter.md) explain the grid and calibration; saved per-run `profile.json` and `manifest.json` record the actual inputs and thresholds.

**Expanded search on the project VM:** HI-SEAS reused the saved survivor list under `data/earth/processed/prefilter/expanded_15seeds/hiseas_pahoehoe_radius/`. The other 11 coarse lists were produced with the VM-local `data/phase3/new_site_coarse_11_v1_driver.py`, using the saved `patches.csv` next to that HI-SEAS list. To regenerate them into a new directory on the VM:

```bash
COARSE_OUT=data/phase3/new_site_coarse_11_rerun PYTHONPATH=src \
  .venv/bin/python data/phase3/new_site_coarse_11_v1_driver.py
```

That experimental screen reused patch statistics and omitted a temperature gate. It used terrain and sparse cover for Dyngjusandur, nearby known craters and terrain for Haughton's impact sites, snow and terrain for Haughton Polygons, the HI-SEAS parents for the lava tubes, and sparse cover with a relief check for Don Juan Pond. It produced 11 lists and 29,541 unique parents in their union. On a fresh clone, obtain those VM-local assets or use the bundled three-seed input. The folder named `expanded_15seeds` also contains `bm_*` benchmark IDs, which have no matching reference rows. Use the specific lists or prepared pilot CSVs.

**CSV format:** The search needs `patch_id,child_start,n_children,coarse_nside,fine_nside,seeds`. A plain per-seed `survivors.csv` normally lacks `seeds`. Add the corresponding catalog ID to that column before running Phase 3. Join multiple eligible IDs with `|`. Keep one row per parent when sharing measurements across seeds. In the current grid, `child_start = patch_id * 4096` and `n_children = 4096`. Preserve those values when you subset a CSV.

## Phase 3 | Sample, measure, and rank fine cells

1. Pick the survivor CSV and reference Parquet. Use a fresh output directory for each experiment. Start from the repo root.
2. Try **one cell** before launching a larger run. Use the tracked balanced input and whichever reference path you have. The example below assumes you generated the full Phase 2 table in `data/phase2`. If you only generated the single Drill Hill smoke reference, use the full reference table instead, because normalization needs multiple reference values.

```bash
.venv/bin/python -u scripts/sim_search.py \
  --survivors data/prefilter_vm/union/survivors_mvp_stratified.csv \
  --references data/phase2/reference_features.parquet \
  --phase1 data/phase1 --out data/phase3/smoke_readme \
  --start 2022 --end 2024 --sources base,power,modis,glim,impact \
  --max-parents 1 --children-per-parent 1 --max-cells 1 \
  --stac-mode batched --workers 1 --asset-workers 8
```

3. Inspect the output, then repeat with your prepared CSV and a larger budget. For a single three-patch search, set `--max-parents 3 --children-per-parent 10 --max-cells 30`. The limits apply to the input file as a whole. `--max-parents` takes rows from the top, so prepare the intended sample **before** running. A `0` limit means unlimited on that dimension. For a small, already balanced file you can use `--max-parents 0` with an explicit cell cap.
4. For a long run, use a persistent terminal or background job and capture stdout/stderr in a log. Resume with the same input files, arguments, code, and output directory. The manifest checks for changes and the checkpoint skips saved cell IDs, including saved rows with source errors. Use a different output directory for new settings.

Each child ID supplies a centre point. Extraction measures a **1 km square** around it. Batched mode processes one parent group at a time and reuses catalog searches and source reads where possible. Asset workers handle raster I/O. The search can fall back to per-cell lookup for some queries.

| `--sources` flag | Data and features |
|---|---|
| `base` | Planetary Computer Copernicus GLO-30 terrain and Sentinel-2/Landsat optical imagery. Elevation, slopes, relief, TRI, NDVI, and a bare-ground proxy. |
| `power` | NASA POWER climate data. Days above freezing, annual precipitation, and mean humidity. |
| `modis` | Planetary Computer daily MOD11A1 and MOD10A1. Summer and winter surface temperature and snow persistence. |
| `glim` | Local GLiM vector map. Dominant mapped lithology within 25 km. |
| `impact` | Local impact catalog. Distance to the nearest listed impact structure. |

### Repeat the published pilots on the VM

Use the command above with `--references "data/phase 2/reference_features.parquet"`. Substitute the listed input, sources, and limits. Write reruns to fresh output directories. The exact sample CSVs below are saved on the VM. GitHub contains their [ranked outputs](outputs/).

| Run | Survivor input on the VM | Sources | Parent / child / cell limits |
|---|---|---|---|
| University Valley, Beacon, Drill Hill | `data/prefilter_vm/union/survivors_mvp_stratified_24.csv` | `base,power,modis,glim,impact` | `0 / 10 / 240` |
| HI-SEAS Pāhoehoe | `data/phase3/hiseas_pilot_inputs/survivors_hiseas_3.csv` | `base,glim` | `3 / 10 / 30` |
| Three Dyngjusandur sites | `data/phase3/task_c_inputs/survivors_dyngjusandur_group.csv` | `base,glim` | `3 / 10 / 30` |
| Mauna Loa lava tubes | `data/phase3/task_c_inputs/survivors_mauna_loa_lava_tubes.csv` | `base,glim` | `3 / 10 / 30` |
| Five Haughton impact sites | `data/phase3/task_c_inputs/survivors_haughton_crater_group.csv` | `base,glim,impact` | `3 / 10 / 30` |
| Haughton Polygons | `data/phase3/task_c_inputs/survivors_haughton_polygons.csv` | `base,modis` | `3 / 10 / 30` |
| Don Juan Pond | `data/phase3/task_c_inputs/survivors_don_juan_pond.csv` | `base,power` | `3 / 10 / 30` |

One extracted cell can be compared with several references if its parent carries multiple seed IDs. The published combined CSV has **600 cell-to-reference rankings**: 80 per original seed and 30 per new seed. Those rows can share fine cell IDs.

### Combine rerun rankings

After completing separate runs, concatenate their ranking tables. Keep the batch name and compute a rank within each reference. The example uses the published per-run tables and also works on a fresh clone. To combine your own runs, replace those paths with the corresponding `ranked_candidates.csv` files.

```bash
.venv/bin/python - <<'PYCODE'
from pathlib import Path
import pandas as pd

runs = {
    "balanced_240": "outputs/balanced_240/ranked_candidates.csv",
    "hiseas_30": "outputs/hiseas_30/hiseas_30_ranked_candidates.csv",
    "dyngjusandur": "outputs/task_c_11_sites/dyngjusandur_group_ranked_candidates.csv",
    "mauna_loa": "outputs/task_c_11_sites/mauna_loa_lava_tubes_ranked_candidates.csv",
    "haughton_crater": "outputs/task_c_11_sites/haughton_crater_group_ranked_candidates.csv",
    "haughton_polygons": "outputs/task_c_11_sites/haughton_polygons_ranked_candidates.csv",
    "don_juan_pond": "outputs/task_c_11_sites/don_juan_pond_ranked_candidates.csv",
}
rows = pd.concat([
    pd.read_csv(path).assign(source_batch=name) for name, path in runs.items()
], ignore_index=True)
key = ["nearest_reference_site", "fine_cell_id"]
if rows.duplicated(key).any():
    raise ValueError("A cell was included twice for the same reference. Check the input runs.")
rows = rows.sort_values(
    ["nearest_reference_site", "similarity_score", "features_matched", "fine_cell_id"],
    ascending=[True, False, False, True],
)
rows["site_rank"] = rows.groupby("nearest_reference_site").cumcount() + 1
out = Path("data/phase3/combined_rankings_rerun.csv")
out.parent.mkdir(parents=True, exist_ok=True)
if out.exists():
    raise FileExistsError(out)
rows.to_csv(out, index=False)
print(rows.groupby("nearest_reference_site").size().to_string())
PYCODE
```

### Read and troubleshoot a run

- `fine_cell_tasks.csv` lists the candidate centres. Check the counts per parent and seed.
- `candidate_features.parquet` contains the measurements, `features_computed`, and JSON `errors` and `provenance` columns. This file is also the resume checkpoint.
- `ranked_candidates.csv` contains one row per scored cell–reference pair. Higher `similarity_score` means smaller differences in the numeric measurements used. The default requires three common usable numeric features. `matched_features` shows what went into the score. `lithology_match` is reported separately.
- `run_manifest.json` records inputs, settings, and code hashes. `run_metrics.json` records elapsed time and source activity.

If rankings are empty or incomplete, check that the input's seed IDs match the reference table, then check `errors` and feature coverage. An optical gap or absent geology file can leave a partially measured cell in the checkpoint. Compare candidates within a reference site and inspect feature coverage before interpreting scores; different pilots used different source sets. A high score suggests a place to inspect more closely. It does not establish an identical geological process.

The [separate Mars landing-site screen](src/nasa_si/screening.py) answers a different question. Its outputs under `data/processed/` are distinct from the Earth analog search.
