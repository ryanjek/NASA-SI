# Equal-area cell grid (Mars and Earth)

Module: `src/nasa_si/equal_area.py` · Tests: `tests/test_equal_area.py`

## Decision
Every cell on both planets has the same area, so features and clusters mean the same thing
whatever the latitude. The scheme is **HEALPix** (RING ordering) via `healpy`.

| | Mars | Earth |
|---|---|---|
| radius used | 3396.19 km | 6371.0088 km |
| nside | 375 | 704 |
| cells | 1,687,500 | 5,947,392 |
| cell area | 85.89 km² | 85.76 km² |
| cell side | 9.27 km | 9.26 km |

Target side 9.26 km = the committed Mars candidate cell (10 MOLA pixels at 64 px/degree).
The two planets differ by ~0.15% in cell area because nside is an integer; that is below the
noise in any feature. Cell shape stays within a diagonal ratio of ~1.8 at every latitude. The
old lat/lon cell is 9.26 x 6.55 km at 45 N and far worse toward the poles.

RING (not NESTED) ordering is deliberate: NESTED forces nside to a power of two, which would
break the cross-planet area match.

## Using it
```python
from nasa_si.equal_area import EqualAreaGrid, aggregate, quantile, raster_cells

mars = EqualAreaGrid.for_planet("mars")      # or "earth"
cell = mars.cell_of(190.8, 44.2)             # lon (east, 0-360 or -180..180), lat
ids = raster_cells(mars, lons, lats)         # cell id for every pixel of a lon/lat raster
cells, flat = aggregate(ids, flat_mask_with_nan_for_invalid, "mean")
cells, p95 = quantile(ids, slope, 0.95)
cells, cover = aggregate(ids, any_layer, "valid_fraction")
```
Also: `neighbors(cells)`, `cell_boundary(cell)` (closed, unwrapped GeoJSON-ready ring),
`cells_within(lon, lat, radius_km)` (benchmark sites become regions), `cells_in_box(...)`.

## Aggregation rules (so Mars and Earth numbers are comparable)
- **Flatness:** `(slope <= 5)` with invalid pixels set to NaN, then `mean`. This is the rubric's
  "fraction of valid pixels with slope <= 5 deg".
- **Data coverage:** `valid_fraction` of the pixels that land in the cell.
- **Weights:** pass `weights=cos(lat)` when aggregating a lat/lon raster, so high-latitude
  pixels (which cover less ground) count for less.
- **Scale matching comes first.** Mars slope is measured on ~1 km MOLA pixels. Build Earth
  slope from a DEM aggregated to ~1 km, then aggregate into cells. Slope from a 30 m DEM
  would make Earth look rougher than Mars for the wrong reason.
- **Rasters coarser than a cell** (for example MODIS 0.05 deg ~ 5.6 km, the 3.7 km Dust Cover
  Index) put only a few pixels in each cell, so their cell values are noisy; report
  `count` alongside.

## Benchmarks (known analog sites)
Known sites are often smaller than one cell. Treat each as a region:
`cells_within(lon, lat, radius_km)` with a few km up to ~30 km (Haughton Crater is ~23 km
across). The number of cells for a given radius does not depend on latitude.

## Not done yet
- `screening.py` still uses the lat/lon candidate grid. Switching it to this grid changes
  Ryan's pipeline, so it is a deliberate follow-up, not a side effect.
- No Earth feature pipeline yet (no Earth rasters are wired to this grid).
- The web app still shows sample data and does not read cells.

## Dependency note
`healpy` was added to `pyproject.toml` and `uv.lock`. If `uv.lock` conflicts on merge, resolve
with `uv lock` rather than by hand.
