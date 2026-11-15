# NASA-SI

NASA Space Apps Challenge 2026 · Explore Mars landing terrain and search for Earth sites that resemble studied Mars analogs. The [published Earth rankings](outputs/all_15_sites_ranked_candidates.csv) cover 15 reference sites.

## Quick start: Stage 2 candidates

Install Bun, then open a separate checkout of the [`stage2-candidates` branch](https://github.com/ryanjek/NASA-SI/tree/stage2-candidates/web). From an existing `~/NASA-SI` checkout:

```bash
cd ~/NASA-SI
git fetch origin
git worktree add --detach ../NASA-SI-stage2 origin/stage2-candidates
cd ../NASA-SI-stage2/web
bun install
bun x vite --host 127.0.0.1 --port 5199 --strictPort
```

If you already have the worktree, start at `cd ~/NASA-SI-stage2/web`. For a fresh standalone checkout, use `git clone --branch stage2-candidates https://github.com/ryanjek/NASA-SI.git NASA-SI-stage2` and run the Bun commands from its `web/` folder.

Open [http://127.0.0.1:5199](http://127.0.0.1:5199) and click **Stage 2 candidates** in the top bar. If Vite is running on a VM, forward port 5199 to your computer first. For example, run `ssh -N -L 5199:127.0.0.1:5199 USER@VM_HOST` from your computer in a separate terminal. From `web/`, use `bun test` to run the tests or `bun x tsc --noEmit` to type-check.

The branch includes starter JSON in [`web/src/data/`](https://github.com/ryanjek/NASA-SI/tree/stage2-candidates/web/src/data), so you can open the demo before downloading any files from Drive. Use `stage2-candidates` for this UI. The React files on `main` and `more-gates` lack some required modules and data.

## Shared data: Drive → VM → frontend

Use the [shared Drive data folder](https://drive.google.com/drive/folders/15tnedGHKtgXQrA3XtBYE5GPlCjbuc1K_) as a mirror of **`~/NASA-SI/data/`** on the VM. Download and extract its contents, then copy the folders *inside* the downloaded `data/` folder into the VM's `data/`. Avoid an extra `data/data/` level. The minimum layout for refreshing the Stage 2 candidates is:

```text
Drive: data/                              VM: ~/NASA-SI/data/
├── phase1/
│   └── features.csv                       feature descriptions (also tracked in Git)
└── phase3/
    ├── all_15_sites_candidate_features.parquet
    └── all_15_sites_ranked_candidates.csv
```

Keep other pipeline data at its existing paths if it is shared as well: `phase 2/reference_features.parquet` (with the space) and `earth/processed/prefilter/expanded_15seeds/<seed>/survivors.csv` plus `profile.json`. The reference table is for running the search. The **candidate features Parquet** above is the table used to refresh the frontend. Use the ranking CSV and Parquet from the same run.

The browser reads bundled JSON under **`~/NASA-SI-stage2/web/src/data/`**. To load an updated dataset, use the Python environment from the pipeline checkout (with Pandas and PyArrow) to rebuild the JSON:

```bash
cd ~/NASA-SI-stage2
data_root="$HOME/NASA-SI/data"
test -s "$data_root/phase3/all_15_sites_candidate_features.parquet" &&
test -s "$data_root/phase3/all_15_sites_ranked_candidates.csv" &&
"$HOME/NASA-SI/.venv/bin/python" scripts/build_candidates_json.py \
  "$data_root/phase3/all_15_sites_candidate_features.parquet" \
  "$data_root/phase3/all_15_sites_ranked_candidates.csv" \
  web/src/data/candidates.json \
  "$data_root/processed/geocode_cache.json" \
  "$data_root/phase1/features.csv"
```

The geocode cache is optional. Without it, regenerated candidates show coordinates without the saved place names. Keep the bundled `survivors.json`, `survivors_index.json`, and `mars_values.json` unless you're refreshing those layers separately. Once the converter reports the rows written, restart Vite or reload the page and open **Stage 2 candidates** again.

### Arcadia map prototype

For the separate [Arcadia map on `mars-map`](https://github.com/ryanjek/NASA-SI/tree/mars-map/web), no Bun or Drive files are needed. From the main checkout:

```bash
git fetch origin
git worktree add --detach ../NASA-SI-mars-map-preview origin/mars-map
cd ../NASA-SI-mars-map-preview/web
python3 -m http.server 5199 --bind 127.0.0.1
```

Open [http://127.0.0.1:5199](http://127.0.0.1:5199). Stop the Stage 2 server first if it is using that port. Choose **No basemap (offline)** if the external basemap is unavailable.

## Folder structure

| Path | What's there |
|---|---|
| [`web/`](web/) | React/Vite frontend on `main`. `src/App.tsx` connects the app and `src/components/` contains maps, controls, and results. On `stage2-candidates`, `src/data/` supplies the JSON and `src/lib/` holds ranking and scoring helpers. On `mars-map`, this folder instead holds the standalone map, its `js/`, and its bundled `data/`. |
| [`data/phase1/`](data/phase1/) | Earth reference catalog and source metadata. `sites.csv` lists the Earth sites and `processes.csv` maps them to Martian processes or locations through `mars_target`. |
| [`data/prefilter_vm/`](data/prefilter_vm/) | Saved coarse survivor lists for the original three Earth references. Each row identifies a roughly 51 km parent patch to sample. |
| [`scripts/`](scripts/) | Pipeline entry points. `extraction.py` builds the Phase 2 reference table, `balance_search.py` samples coarse parents, and `sim_search.py` measures and ranks fine candidates using `batch_io.py` for shared reads. |
| [`src/nasa_si/`](src/nasa_si/) | Core Python package for grids, coarse screening, and the separate Mars landing-site screen. |
| [`outputs/`](outputs/) | Published Phase 3 CSVs. Filter the combined table by `nearest_reference_site` and sort by `site_rank`. |
| [`docs/`](docs/) | Methods, prefilter notes, and the full [Phase 1–3 workflow](docs/earth-analog-workflow.md). |
| `data/phase2/`, `data/phase3/` | Local reference tables, search outputs, and checkpoints. The saved VM reference table uses `data/phase 2/` with a space. Exact rerun inputs and large raw data are described in the workflow guide. |

## Reproduce the Earth search

Follow the [Phase 1–3 workflow](docs/earth-analog-workflow.md) to set up Python, measure the references, choose survivor patches, and run a one-cell smoke test. Phase 3 ranks **Earth cells** against Earth references. Use the frontend quick start above to view the published candidate data.
