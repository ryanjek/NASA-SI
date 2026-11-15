# Mars twins: an Earth stand-in for every place on Mars

Every place on Mars (196,383 windows of 59 km) is compared with every land place on Earth (224,195 windows of the same size) on five factors that both planets have in the same units: terrain shape, albedo, surface temperature, shallow ground ice and vegetation. The viewer in `viewer/` shows the ten best Earth twins for any Mars place, a Mars-likeness map of Earth and an analog-gap map of Mars.

Open `viewer/index.html` in a browser. It needs no server and no login.

## Run it

Data goes in a folder outside the repo (called `twin_data` below). The steps, in order:

```bash
python -m twin.mars_climate --data twin_data          # Mars surface temperature maps (Mars Climate Database)
python -m twin.build mars  --data twin_data
python -m twin.build earth --data twin_data --earth-raw data/earth/raw
python -m twin.search   --data twin_data              # all pairs; uses a GPU if there is one
python -m twin.validate --data twin_data              # the frozen checks
python -m twin.robustness --data twin_data            # tolerance sensitivity and the SpaceX positive control
python -m pytest twin/tests                           # unit tests on synthetic arrays
python -m twin.export   --data twin_data --out twin/viewer
python -m twin.handoff  --data twin_data --out twin_data/handoff_regions.csv
python -m twin.network train --data twin_data --earth-raw data/earth/raw   # optional
python -m twin.network check --data twin_data --earth-raw data/earth/raw
```

`fine_terrain.py` runs on its own (numpy and rasterio only) and measures 1 km slope and relief from Copernicus GLO-90 for each hand-off region.

Raw files expected in `twin_data/raw/`:

- `mars/megt90n000fb.img` and `megc90n000fb.img` with their `.lbl` labels: MGS MOLA gridded elevation and shot counts, 32 pixels per degree (PDS Geosciences Node).
- `mars/Mars_MGS_TES_Albedo_mosaic_global_7410m.tif`: TES albedo (USGS Astrogeology).
- `mars/SWIM2_c0_1.tif` and `SWIM2_c1_5.tif`: SWIM 2.0 ice-consistency composites for 0-1 m and 1-5 m (PSI).
- `mars/mcd/`: written by `twin.mars_climate`.
- `earth/ETOPO_2022_v1_60s_N90W180_surface.nc`: NOAA ETOPO 2022 at 60 arc-seconds.

The Earth layers come from the team's downloader in `data/earth/raw/` (MODIS MCD43C3, MYD11C3, MOD13C2, MCD12C1 and ESA CCI Permafrost).

## Method in short

- Places are HEALPix NESTED cells (nside 128 on Mars, 256 on Earth). Each window is 32 x 32 pixels of 1.853 km, cut in its own azimuthal equidistant projection.
- Terrain shape is eleven numbers in physical units: tilt, relief, roughness, slope at four scales, skewness, kurtosis, grain and flat share. Elevation is averaged east-west so every latitude has the ground resolution of the equator.
- Each factor is divided by a tolerance (albedo 0.03, temperature 5 K, ice 0.5, NDVI 0.15, shape twice the median distance between neighbouring places after whitening) and turned into a similarity `exp(-d^2 / 2)`. The match is the mean over factors. Twins are ranked with the CSLS hub correction.
- Earth land is MODIS land cover minus water, where "permanent ice" over a below-sea-level ETOPO pixel counts as water (Arctic sea ice is labelled as permanent ice). Missing data stays missing. Mars ice is mapped only between 60S and 60N; Earth temperature is missing at 751 places.

## Checks

Seven published Mars-Earth analogies were fixed before the first run, with a pass rule: the known Earth region must rank in the top 5% of Earth land for the Mars query and beat a trivial "coldest, barest place" baseline. With all five factors 3 of 7 pass; with terrain shape alone 5 of 7. Three of four negative controls rank below the median; Hudson Bay Lowlands does not. Full numbers are in `validate.py`'s output. Seven pairs describe the method; they are not a statistic. The neural network in `network.py` passed the sensor tests (recall at 1 through a Mars shot mask 0.99, against 0.94 for the hand-made numbers; 91% of masked Earth windows still classed as Earth) but beat the hand-made shape numbers on only 1 of the 7 pairs (2 of 7 with a second training seed; the two runs share 62% of their top-50 twins), so the search uses the hand-made numbers. The eight HiRISE locations imaged as SpaceX Starship candidates (chosen for shallow ground ice on flat ground) get flat, icy Earth twins 26% of the time, against 18% for random Mars places at 30-50N and 10% for Earth land overall: a weak pass. A second version that lets any one factor veto a match (geometric mean of albedo, ice, shape and vegetation; `--combine geometric`) did worse: 2 of 7 pairs, and it moved the Great Plains above the median while fixing Hudson Bay, so the mean stays. Halving every tolerance keeps 53% of the top-10 twins and doubling keeps 89%, so the tolerances are part of every result.

## Limits

Mars is colder than 98.5% of Earth's land, so temperature rarely scores. Mars temperatures come from a climate model. Earth layers are for one year (2025). Features smaller than about 2 km are invisible. A 59 km window that mixes bright ice and dark rock can look Mars-like on average, and a window may be up to half sea (its flat surface then counts as terrain); the hand-off list only takes windows that are at least 90% land. Gravity, air pressure and radiation have no Earth match.

## Results in this folder

`results/` holds the outputs behind every number quoted: `validation.json` (frozen checks), `validation_geometric.json` (veto version), `robustness.json` (sensitivity and SpaceX control), `network_check.json`, `network_check-seed1.json` and `network_stability.json` (the network and its second seed), and `validation_run1_before_sea_ice_fix.json` (the first run, before the sea-ice fix). `handoff_regions.csv` is the hand-off list with 1 km terrain. The large arrays (place tables, twins, embeddings, weights) are not in git; the commands above rebuild them.

## Use of AI

The code in this folder, the viewer and the checks were written with Claude (Anthropic) in Claude Code, at the team's direction; team members reviewed them. Each file says so in its header.
