# Earth analog map handoff for Antigravity

## Goal and current state

Build the Earth side of Mars Analog Finder for the **Arcadia Outpost** profile. The first map should find broad areas of North America with both flat terrain and substantial permafrost. It is a screening map for Earth field analogs. Permafrost fraction is a frozen-ground proxy and does not establish ground-ice abundance or landing safety.

Work in the existing Windows NASA-SI checkout on branch `earthmap`. Read [EARTH_DATA.md](EARTH_DATA.md), `scripts/earth_data.py`, and `tests/test_earth_data.py` first. The existing script does data acquisition. Grid construction, feature extraction, Earth scoring, and plotting are still to be built. Keep the downloader and any active download intact.

## Start here while downloads run

1. Read `data/earth/inventory.json`. Match its terrain and permafrost entries with completed payloads and their matching JSON receipts under `data/earth/raw/`. Ignore staging directories. Select a small covered North American area with several adjacent terrain tiles and valid permafrost data. Inspect the actual GeoTIFF and NetCDF metadata before choosing variables, units, scale factors, or CRS. Do not assume the NetCDF coordinates are latitude and longitude.
2. Implement a separate processing command that accepts the inventory, data root, output directory, and a small test extent. Make it runnable offline once the inputs are local. Use a bounded-memory workflow that reads tiles or windows rather than loading all of North America at native DEM resolution at once. Write pilot outputs under `data/earth/processed/`.
3. Run the pilot on the actual local files. Show a few output rows and a map, and document the exact command to rerun it. Clearly label pilot results and report missing input coverage. Once the pilot works, run the same processing on the intended study region when its inputs are ready.

## Grid, features, and scoring

- Use a fixed, documented **EPSG:6933 grid with 9,000 m × 9,000 m cells** and deterministic cell IDs. The study region is mainland Canada, mainland Alaska, the contiguous US, and mainland Mexico. Exclude Greenland and Canadian Arctic islands. Use a versioned land mask for the final run. The catalog bounding box is just a download envelope. If a final mask is unavailable, keep the result explicitly labeled as a pilot.
- Use **Copernicus GLO-90 terrain GeoTIFFs** for median surface elevation and flatness. Compute slope in degrees at fine resolution before aggregating. Use physical horizontal distances and handle tile edges with neighboring pixels or mark unresolved edges invalid. Flatness is the area fraction of valid terrain with slope **≤ 5°**. Keep elevation as a displayed feature.
- Use **ESA CCI v5 permafrost fraction, 2023**, for the area-weighted frozen-ground feature. Read units, fill values, and quality information from the actual product. Put valid values on a 0–1 scale. Preserve missing values and distinguish them from a valid fraction of zero.
- Output one row per cell. Include cell ID, grid position, center coordinates, land area fraction, elevation in meters, flatness fraction, permafrost fraction, input coverage, eligibility, and score. Use a shared valid land area for scoring and track coverage relative to the cell's land area.
- As a **provisional Earth baseline**, gate at 80% joint valid land coverage and give flatness and permafrost equal weight on their 0–1 scales. Keep the cutoff and weights configurable and record them in the output metadata. Leave failed cells unscored, and state these settings are working assumptions. Do not copy Mars latitude or elevation cutoffs to Earth.
- Follow the Mars report layout with **five map panels in one image** on the same grid. Use elevation, flatness, permafrost fraction, valid-data coverage, and final Earth score. Show missing or rejected cells clearly. Retain the cell table so rankings can be audited.

## How this relates to the Mars branch

Inspect the Mars code without switching branches, for example with `git show origin/mars-map:src/nasa_si/screening.py`. Mars gates on absolute latitude ≤ 50° and joint coverage ≥ 80%. Its score uses flatness (30%, slope ≤ 5°), SWIM ice evidence (30%), TES surface dust cover (20%), elevation (10%), and valid-data coverage (10%). The Earth baseline compares the two features we can defend with current inputs. Mars SWIM and Earth permafrost are distinct indicators. MERRA-2 airborne dust and TES surface dust describe different things, and the Mars elevation reference is planet-specific.

## Local inputs and reproducibility

- Permafrost was already saved and terrain was still downloading at handoff. The two are enough for the pilot. Optional layers can wait. Temperature and precipitation are the next candidates if the team wants to explicitly reward cold and dry conditions. Vegetation, land cover, snow, albedo, and airborne dust are outside the baseline score.
- If terrain is missing and the full download has stopped, `python scripts/earth_data.py download --datasets terrain permafrost` resumes only those inputs from the existing inventory. The script reuses verified files. The full command also queues optional products. If using albedo later, the downloader limits 2025 to January 15, April 15, July 15, and October 15 even with the older daily inventory.
- Keep raw data and large generated outputs under ignored `data/`. Track code, dependencies, settings, and a compact manifest of the exact inputs used, including source URLs, versions, and SHA-256 hashes from receipts. Record the grid origin, mask source and version, coverage rules, and scoring configuration. Share the processed table separately if it is too large for Git.
- Check that a level surface qualifies as flat, steep terrain does not, tile seams do not create artificial slopes, and valid zero permafrost differs from missing data. Reject incomplete cells under the stated coverage rule. Run the existing tests with `python -m unittest discover -s tests -v`.

## Prompt to paste into Antigravity

> Read EARTH_MAP_HANDOFF.md and EARTH_DATA.md. Implement the offline terrain and permafrost pilot on earthmap using tiles that already have download receipts. Verify source metadata, produce the cell table and five-panel report for a small area, and give me the exact command to reproduce it. Keep the active download running and avoid a full-region run until the pilot works.
