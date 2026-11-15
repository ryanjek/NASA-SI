# NASA-SI

NASA Space Apps Challenge 2026 · Explore Mars landing terrain and search for Earth sites that resemble studied Mars analogs. The [published Earth rankings](outputs/all_15_sites_ranked_candidates.csv) cover 15 reference sites.

## Quick start

**Want a map you can open from GitHub today? Run the Arcadia prototype on [`mars-map`](https://github.com/ryanjek/NASA-SI/tree/mars-map/web).** It ships with its own map data and Leaflet. You only need Git and Python 3.

From an existing NASA-SI checkout:

```bash
git fetch origin
git worktree add --detach ../NASA-SI-mars-map-preview origin/mars-map
cd ../NASA-SI-mars-map-preview/web
python3 -m http.server 5199 --bind 127.0.0.1
```

Open [http://127.0.0.1:5199](http://127.0.0.1:5199) while the server is running. A fresh checkout can use `git clone --branch mars-map https://github.com/ryanjek/NASA-SI.git NASA-SI-mars-map-preview`, then run the same Python command inside `NASA-SI-mars-map-preview/web`. If the server runs on a remote VM, forward port 5199 to your computer before opening the link. Choose **No basemap (offline)** in the map if NASA's basemap tiles are unavailable.

### Stage 2 candidates (React app)

If you already have the team's **complete `stage2-candidates` worktree**, open a terminal in its `web/` folder. With Bun installed, run:

```bash
bun install
bun x vite --host 127.0.0.1 --port 5199 --strictPort
```

Open [http://127.0.0.1:5199](http://127.0.0.1:5199) and select **Stage 2 candidates** in the top bar. Use `bun test` to run tests and `bun x tsc --noEmit` to type-check from that same folder.

**Fresh-clone status:** `stage2-candidates` is not published on this repository's GitHub remote. The React shells on [`main`](web/) and [`more-gates`](https://github.com/ryanjek/NASA-SI/tree/more-gates/web) import files such as `web/src/data/marsSites.ts` and `web/src/lib/ranking.ts` that those branches do not contain. If you only have a GitHub clone, get the complete worktree or a published branch with the page and its supporting files before trying this quick start. The Arcadia prototype above is a separate app and has no Stage 2 tab.

## Folder structure

| Path | What's there |
|---|---|
| [`web/`](web/) | React/Vite frontend on `main`. `src/App.tsx` connects the app and `src/components/` contains maps, controls, and results. In the complete React worktree, `src/data/` supplies app data and `src/lib/` holds ranking and scoring helpers. On `mars-map`, this folder instead holds the standalone map, its `js/`, and its bundled `data/`. |
| [`data/phase1/`](data/phase1/) | Earth reference catalog and source metadata. `sites.csv` lists the Earth sites and `processes.csv` maps them to Martian processes or locations through `mars_target`. |
| [`data/prefilter_vm/`](data/prefilter_vm/) | Saved coarse survivor lists for the original three Earth references. Each row identifies a roughly 51 km parent patch to sample. |
| [`scripts/`](scripts/) | Pipeline entry points. `extraction.py` builds the Phase 2 reference table, `balance_search.py` samples coarse parents, and `sim_search.py` measures and ranks fine candidates using `batch_io.py` for shared reads. |
| [`src/nasa_si/`](src/nasa_si/) | Core Python package for grids, coarse screening, and the separate Mars landing-site screen. |
| [`outputs/`](outputs/) | Published Phase 3 CSVs. Filter the combined table by `nearest_reference_site` and sort by `site_rank`. |
| [`docs/`](docs/) | Methods, prefilter notes, and the full [Phase 1–3 workflow](docs/earth-analog-workflow.md). |
| `data/phase2/`, `data/phase3/` | Local reference tables, search outputs, and checkpoints. The saved VM reference table uses `data/phase 2/` with a space. Exact rerun inputs and large raw data are described in the workflow guide. |

## Reproduce the Earth search

Follow the [Phase 1–3 workflow](docs/earth-analog-workflow.md) to set up Python, measure the references, choose survivor patches, and run a one-cell smoke test. Phase 3 ranks **Earth cells** against Earth references. Use the Arcadia map for Mars landing terrain.
