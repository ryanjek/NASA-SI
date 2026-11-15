"""Small global rasters for the stage-1 (50 km) pre-filter.

Every layer here is coarse (MODIS climate-modelling grid, 0.05 deg ~ 5.6 km), so a whole layer fits in
memory (3600 x 7200). Readers for fine layers (30 m DEM, Sentinel-2) are deliberately not here:
stage 1 must not need them. A layer becomes a ``GlobalRaster`` with NaN for invalid pixels, so the
rest of the code never sees fill values or scale factors.

Only MODIS CMG HDF4 is implemented (``pyhdf``, imported lazily). Other layers named in the Phase 1
catalog (NASA POWER climate, GLiM lithology, a global ~1 km DEM) are registered as deferred in
``config/prefilter_features.csv`` until their data are available.
"""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import ArrayLike, NDArray

from .patches import PatchAccumulator, PatchGrid

FloatArray = NDArray[np.float64]

KELVIN_OFFSET = 273.15
EARTH_RADIUS_KM = 6371.0088


def _FOOTPRINT_OFFSETS(hx: float, hy: float):
    return [(sx * hx, sy * hy) for sx in (-1, 0, 1) for sy in (-1, 0, 1) if (sx, sy) != (0, 0)]


@dataclass(frozen=True)
class GlobalRaster:
    """A regular lon/lat raster. Row 0 is the northernmost row; column 0 is the westernmost."""

    name: str
    values: NDArray[np.float32]  # NaN = invalid
    lon_west: float  # left edge of column 0, degrees east
    lat_north: float  # top edge of row 0
    dlon: float  # pixel size, degrees (positive)
    dlat: float
    unit: str = ""

    @property
    def rows(self) -> int:
        return int(self.values.shape[0])

    @property
    def cols(self) -> int:
        return int(self.values.shape[1])

    def lons(self) -> FloatArray:
        return self.lon_west + (np.arange(self.cols) + 0.5) * self.dlon

    def lats(self) -> FloatArray:
        return self.lat_north - (np.arange(self.rows) + 0.5) * self.dlat

    def window_values(self, lon: float, lat: float, radius_km: float) -> FloatArray:
        """Valid values of the pixel containing a point plus any pixel centred within ``radius_km``."""
        i = int(np.clip(math.floor((self.lat_north - lat) / self.dlat), 0, self.rows - 1))
        j = int(math.floor(((lon - self.lon_west) % 360.0) / self.dlon)) % self.cols
        picked = {(i, j)}
        if radius_km > 0:
            pad_r = int(math.ceil(radius_km / (111.19 * self.dlat))) + 1
            coslat = max(math.cos(math.radians(lat)), 0.05)
            pad_c = min(int(math.ceil(radius_km / (111.19 * self.dlon * coslat))) + 1, self.cols // 2)
            for ii in range(max(i - pad_r, 0), min(i + pad_r, self.rows - 1) + 1):
                la = self.lat_north - (ii + 0.5) * self.dlat
                for jj in range(j - pad_c, j + pad_c + 1):
                    jm = jj % self.cols
                    lo = self.lon_west + (jm + 0.5) * self.dlon
                    if _haversine_km(lon, lat, lo, la) <= radius_km:
                        picked.add((ii, jm))
        vals = np.array([self.values[ii, jj] for ii, jj in picked], dtype=np.float64)
        return vals[np.isfinite(vals)]

    def accumulate(self, acc: PatchAccumulator, grid: PatchGrid, *, row_block: int = 240) -> None:
        """Feed every pixel to ``acc``, crediting each pixel to all patches its footprint touches.

        The mean (and counts) use the pixel centre only; min and max also use the four corners and four
        edge midpoints, so a pixel straddling a patch boundary widens both patches' envelopes. Residual
        risk: a patch whose vertex pokes into a pixel without covering any of those nine points is
        missed; that needs a pixel comparable in size to the patch (here 5.6 km against 51 km).
        """
        lons = self.lons()
        half_lon, half_lat = self.dlon / 2 * 0.999, self.dlat / 2 * 0.999
        lats = np.clip(self.lats(), -90.0, 90.0)  # a half-pixel row can centre just past a pole
        for r0 in range(0, self.rows, row_block):
            r1 = min(r0 + row_block, self.rows)
            lat_c = lats[r0:r1, None] * np.ones((1, self.cols))
            lon_c = np.ones((r1 - r0, 1)) * lons[None, :]
            vals = self.values[r0:r1].astype(np.float64)
            weight = np.cos(np.radians(lat_c))
            acc.add(grid.patch_of(lon_c, lat_c), vals, weight)
            for dlo, dla in _FOOTPRINT_OFFSETS(half_lon, half_lat):
                corner_lat = np.clip(lat_c + dla, -90.0, 90.0)
                acc.add_extent(grid.patch_of(lon_c + dlo, corner_lat), vals)


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def combine_rasters(rasters: list[GlobalRaster], how: str, name: str) -> GlobalRaster:
    """Element-wise max, min, mean or sum over same-shaped rasters, ignoring NaN (all-NaN stays NaN)."""
    if not rasters:
        raise ValueError("no rasters to combine")
    if how not in ("max", "min", "mean", "sum"):
        raise ValueError("how must be 'max', 'min', 'mean' or 'sum'")
    first = rasters[0]
    for r in rasters[1:]:
        if r.values.shape != first.values.shape or (r.lon_west, r.lat_north, r.dlon, r.dlat) != (first.lon_west, first.lat_north, first.dlon, first.dlat):
            raise ValueError("rasters must share one grid to be combined")
    stack = np.stack([r.values for r in rasters]).astype(np.float64)
    all_nan = np.all(np.isnan(stack), axis=0)
    if how == "max":
        out = np.where(np.isnan(stack), -np.inf, stack).max(axis=0)
    elif how == "min":
        out = np.where(np.isnan(stack), np.inf, stack).min(axis=0)
    elif how == "mean":
        out = np.nansum(stack, axis=0) / np.maximum(np.sum(~np.isnan(stack), axis=0), 1)
    else:
        out = np.nansum(stack, axis=0)
    out[all_nan] = np.nan
    return GlobalRaster(name, out.astype(np.float32), first.lon_west, first.lat_north, first.dlon, first.dlat, first.unit)


def upsample(raster: GlobalRaster, factor: int) -> GlobalRaster:
    """Repeat each pixel ``factor`` x ``factor`` times, so a pixel is much smaller than a 50 km patch.

    The filter credits a pixel to a patch through nine points of its footprint; that is only safe when a
    pixel is well under the patch size. A 0.5 degree model grid is about the patch size, so it is repeated
    before use. No values change, only the pixel size.
    """
    if factor < 1:
        raise ValueError("factor must be at least 1")
    vals = np.repeat(np.repeat(raster.values, factor, axis=0), factor, axis=1)
    return GlobalRaster(raster.name, vals, raster.lon_west, raster.lat_north, raster.dlon / factor, raster.dlat / factor, raster.unit)


# ---------------------------------------------------------------------------------------------
# MODIS CMG monthly products (HDF4)
# ---------------------------------------------------------------------------------------------

_FILE_DATE = re.compile(r"\.A(\d{4})(\d{3})\.")


def month_of(path: Path | str) -> tuple[int, int]:
    """(year, month) from a MODIS file name such as ``MOD11C3.A2025182.061...hdf``."""
    match = _FILE_DATE.search(Path(path).name)
    if not match:
        raise ValueError(f"cannot read the acquisition date from {path}")
    year, doy = int(match.group(1)), int(match.group(2))
    day = dt.date(year, 1, 1) + dt.timedelta(days=doy - 1)
    return day.year, day.month


def _read_sds(path: Path, name: str) -> tuple[np.ndarray, dict]:
    from pyhdf.SD import SD, SDC  # lazy: only needed when real HDF4 files are read

    handle = SD(str(path), SDC.READ)
    sds = handle.select(name)
    return np.asarray(sds[:]), dict(sds.attributes())


def _cmg_geometry(shape: tuple[int, int]) -> tuple[float, float, float, float]:
    rows, cols = shape
    return -180.0, 90.0, 360.0 / cols, 180.0 / rows


def read_modis_lst(path: Path, *, night: bool = False) -> GlobalRaster:
    """MOD11C3/MYD11C3 monthly land-surface temperature in deg C (0 is the fill value)."""
    sds = "LST_Night_CMG" if night else "LST_Day_CMG"
    raw, attrs = _read_sds(Path(path), sds)
    scale = float(attrs.get("scale_factor", 0.02))
    kelvin = raw.astype(np.float64) * scale
    celsius = np.where(raw > 0, kelvin - KELVIN_OFFSET, np.nan).astype(np.float32)
    west, north, dlon, dlat = _cmg_geometry(celsius.shape)
    return GlobalRaster(f"{'night' if night else 'day'}_lst", celsius, west, north, dlon, dlat, "degC")


def read_modis_ndvi(path: Path) -> GlobalRaster:
    """MOD13C2 monthly NDVI, keeping only best-quality pixels (MODLAND QA bits 0-1 == 0).

    Dropped pixels become NaN, which the filter treats as *unknown* and never prunes on.
    """
    ndvi_raw, attrs = _read_sds(Path(path), "CMG 0.05 Deg Monthly NDVI")
    qa, _ = _read_sds(Path(path), "CMG 0.05 Deg Monthly VI Quality")
    scale = float(attrs.get("scale_factor", 10000.0))
    fill = attrs.get("_FillValue", -3000)
    good = (ndvi_raw != fill) & ((qa.astype(np.uint32) & 0b11) == 0)
    ndvi = np.where(good, ndvi_raw.astype(np.float64) / scale, np.nan).astype(np.float32)
    west, north, dlon, dlat = _cmg_geometry(ndvi.shape)
    return GlobalRaster("ndvi", ndvi, west, north, dlon, dlat, "1")


def snow_fraction(raw: np.ndarray) -> np.ndarray:
    """MOD10CM Snow_Cover_Monthly_CMG is percent snow cover 0-100; anything above 100 is a fill or flag."""
    pct = raw.astype(np.float64)
    return np.where(pct <= 100, pct / 100.0, np.nan).astype(np.float32)


def read_modis_snow(path: Path) -> GlobalRaster:
    """MOD10CM monthly snow cover as a 0-1 fraction of the 0.05 degree pixel."""
    raw, _ = _read_sds(Path(path), "Snow_Cover_Monthly_CMG")
    frac = snow_fraction(raw)
    west, north, dlon, dlat = _cmg_geometry(frac.shape)
    return GlobalRaster("snow", frac, west, north, dlon, dlat, "fraction")


def barren_fraction(pct: np.ndarray, fill: float = 255.0) -> np.ndarray:
    """MCD12C1 Land_Cover_Type_1_Percent for one class is percent of the pixel (0-100); above 100 is a fill."""
    p = pct.astype(np.float64)
    return np.where(p <= 100, p / 100.0, np.nan).astype(np.float32)


def read_modis_barren(path: Path) -> GlobalRaster:
    """MCD12C1 share of each 0.05 degree pixel classed 'barren or sparsely vegetated' (IGBP class 16), 0-1."""
    raw, _ = _read_sds(Path(path), "Land_Cover_Type_1_Percent")
    frac = barren_fraction(raw[:, :, 16])
    frac[raw[:, :, 0] == 100] = np.nan  # open water is not land: without this the ocean reads as 0% barren
    west, north, dlon, dlat = _cmg_geometry(frac.shape)
    return GlobalRaster("barren", frac, west, north, dlon, dlat, "fraction")


# Terrain file written by ``fetch_terrain.py``: per 0.05 degree cell, the minimum and maximum of the
# 1 km-window median slope, 95th-percentile slope and relief. Each feature is two rasters (``name`` is the
# minimum, ``name + HI`` the maximum) so a patch's envelope covers every 1 km window in it.
HI = "@hi"
TERRAIN_FILE = "gmted_terrain_0p05.npz"
TERRAIN_LAYERS = {"terrain_slope_median": "s50", "terrain_slope_p95": "s95", "terrain_relief": "rel"}


def read_terrain(path: Path) -> dict[str, GlobalRaster]:
    data = np.load(Path(path))
    west, north, d = float(data["lon_west"]), float(data["lat_north"]), float(data["d"])
    out: dict[str, GlobalRaster] = {}
    for layer, key in TERRAIN_LAYERS.items():
        unit = "m" if key == "rel" else "deg"
        out[layer] = GlobalRaster(layer, data[f"{key}_lo"].astype(np.float32), west, north, d, d, unit)
        out[layer + HI] = GlobalRaster(layer + HI, data[f"{key}_hi"].astype(np.float32), west, north, d, d, unit)
    return out


def open_water(path: Path) -> np.ndarray:
    """True where MCD12C1 says a 0.05 degree pixel is 100% water (class 0). Only whole-water pixels are masked."""
    raw, _ = _read_sds(Path(path), "Land_Cover_Type_1_Percent")
    return raw[:, :, 0] == 100


_MERRA_FILE = re.compile(r"\.(\d{4})(\d{2})\.nc4?$")
_MERRA_FILL = 1e14  # MERRA-2 land fields use 1e15 over ocean


def merra_month_of(path: Path | str) -> tuple[int, int]:
    match = _MERRA_FILE.search(Path(path).name)
    if not match:
        raise ValueError(f"cannot read the month from {path}")
    return int(match.group(1)), int(match.group(2))


def precip_mm_month(rate_kg_m2_s: np.ndarray, year: int, month: int) -> np.ndarray:
    """Convert a monthly-mean precipitation rate (kg m-2 s-1, so 1 kg m-2 = 1 mm) to mm for that month."""
    days = calendar.monthrange(year, month)[1]
    rate = rate_kg_m2_s.astype(np.float64)
    return np.where(np.isfinite(rate) & (np.abs(rate) < _MERRA_FILL), rate * 86400.0 * days, np.nan).astype(np.float32)


def read_merra2_precip(path: Path, *, upsample_by: int = 4) -> GlobalRaster:
    """MERRA-2 tavgM_2d_lnd_Nx PRECTOTLAND as mm for the month, on a finer grid (see ``upsample``)."""
    import netCDF4  # lazy: only needed when MERRA-2 files are read

    year, month = merra_month_of(path)
    with netCDF4.Dataset(str(path)) as ds:
        rate = np.ma.filled(ds.variables["PRECTOTLAND"][0].astype(np.float64), np.nan)
        lat = np.asarray(ds.variables["lat"][:], dtype=np.float64)
        lon = np.asarray(ds.variables["lon"][:], dtype=np.float64)
    if lat[0] > lat[-1]:  # make row 0 the northernmost
        rate, lat = rate[::-1], lat[::-1]
    # Row 0 should be the northernmost row; MERRA-2 lists latitude south to north, so it was flipped above.
    dlat, dlon = abs(lat[1] - lat[0]), abs(lon[1] - lon[0])
    raster = GlobalRaster("merra2_precip", precip_mm_month(rate, year, month), float(lon[0] - dlon / 2), float(lat[0] + dlat / 2), float(dlon), float(dlat), "mm")
    return upsample(raster, upsample_by)


def sha256_of(path: Path | str, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class InputFile:
    path: str
    layer: str
    year: int
    month: int
    sha256: str


def find_modis_files(raw_dir: Path | str) -> dict[str, list[Path]]:
    """Locate the monthly input files below ``raw_dir`` by product, sorted by month.

    MODIS products (``MOD11C3``, ``MOD13C2``, ``MOD10CM``) are matched by their file prefix, so the Aqua
    ``MYD*`` files are not picked up. ``MERRA2_lnd`` is the MERRA-2 monthly land file (precipitation).
    """
    root = Path(raw_dir)
    found: dict[str, list[Path]] = {}
    for product in ("MOD11C3", "MOD13C2", "MOD10CM"):
        found[product] = sorted((p for p in root.rglob(f"{product}.A*.hdf")), key=month_of)
    found["MCD12C1"] = sorted(root.rglob("MCD12C1.A*.hdf"))
    found["terrain"] = sorted(root.rglob(TERRAIN_FILE))
    found["MERRA2_lnd"] = sorted((p for p in root.rglob("MERRA2_*tavgM_2d_lnd_Nx.*.nc4")), key=merra_month_of)
    return found


# Layers a feature can ask for, and how the monthly rasters of each are reduced to one.
LAYER_COMBINER = {
    "modis_lst_day": "max",  # warmest month
    "modis_lst_night": "min",  # coldest month
    "modis_ndvi": "max",  # greenest month
    "modis_snow": "mean",  # share of the year with snow cover
    "merra2_precip": "sum",  # annual total, only when all 12 months are present
    "modis_barren": "mean",  # one annual land-cover file
}


def load_stage1_rasters(
    raw_dir: Path | str, *, months: tuple[int, ...] | None = None, layers: set[str] | None = None
) -> tuple[dict[str, GlobalRaster], list[InputFile], list[str]]:
    """Build the per-layer rasters the stage-1 features are derived from.

    Returns ``(rasters_by_layer, inputs, notes)``. ``rasters_by_layer`` keys are the layer ids used in
    ``config/prefilter_features.csv`` (see ``LAYER_COMBINER``); each is the element-wise reduction over the
    months found. ``months`` restricts which calendar months are used; ``layers`` restricts which layers
    are read (default: all). ``notes`` records anything a reader should know about coverage.
    """
    files = find_modis_files(raw_dir)
    inputs: list[InputFile] = []
    notes: list[str] = []
    per_layer: dict[str, list[GlobalRaster]] = {name: [] for name in LAYER_COMBINER}

    def want(*names: str) -> bool:
        return layers is None or any(n in layers for n in names)

    def keep(month: int) -> bool:
        return not months or month in months

    for path in files["MOD11C3"] if want("modis_lst_day", "modis_lst_night") else []:
        year, month = month_of(path)
        if keep(month):
            per_layer["modis_lst_day"].append(read_modis_lst(path, night=False))
            per_layer["modis_lst_night"].append(read_modis_lst(path, night=True))
            inputs.append(InputFile(str(path), "modis_lst", year, month, sha256_of(path)))
    for path in files["MOD13C2"] if want("modis_ndvi") else []:
        year, month = month_of(path)
        if keep(month):
            per_layer["modis_ndvi"].append(read_modis_ndvi(path))
            inputs.append(InputFile(str(path), "modis_ndvi", year, month, sha256_of(path)))
    for path in files["MOD10CM"] if want("modis_snow") else []:
        year, month = month_of(path)
        if keep(month):
            per_layer["modis_snow"].append(read_modis_snow(path))
            inputs.append(InputFile(str(path), "modis_snow", year, month, sha256_of(path)))
    for path in files["MCD12C1"][-1:] if want("modis_barren") else []:
        year, _ = month_of(path)
        per_layer["modis_barren"].append(read_modis_barren(path))
        inputs.append(InputFile(str(path), "modis_barren", year, 0, sha256_of(path)))
    terrain: dict[str, GlobalRaster] = {}
    if files["terrain"] and want(*TERRAIN_LAYERS):
        terrain = {k: v for k, v in read_terrain(files["terrain"][0]).items() if layers is None or k.removesuffix(HI) in layers}
        if files["MCD12C1"]:
            # GMTED2010 has values (zero) over the ocean; without this every ocean patch would count as land.
            water = open_water(files["MCD12C1"][-1])
            for k, r in terrain.items():
                if r.values.shape == water.shape:
                    terrain[k] = GlobalRaster(r.name, np.where(water, np.nan, r.values).astype(np.float32), r.lon_west, r.lat_north, r.dlon, r.dlat, r.unit)
            notes.append("terrain: pixels that are 100% water in MCD12C1 are masked")
        else:
            notes.append("terrain: WARNING no MCD12C1 file, so ocean pixels are not masked")
        inputs.append(InputFile(str(files["terrain"][0]), "terrain", 0, 0, sha256_of(files["terrain"][0])))
        notes.append("terrain: GMTED2010 via Earth Engine, 1 km-window slope and relief, min and max per 0.05 degree cell; no data south of 56 S or north of 84 N")
    if want("merra2_precip"):
        precip = [(merra_month_of(p), p) for p in files["MERRA2_lnd"]]
        years = sorted({y for (y, _), _ in precip})
        year = years[-1] if years else None  # one calendar year, the latest found
        chosen = [(m, p) for (y, m), p in precip if y == year]
        if len(chosen) == 12 and not months:
            for m, path in chosen:
                per_layer["merra2_precip"].append(read_merra2_precip(path))
                inputs.append(InputFile(str(path), "merra2_precip", year, m, sha256_of(path)))
        elif chosen:
            notes.append(f"merra2_precip: not used. An annual total needs all 12 months of one year with no --months filter; found {len(chosen)} month(s).")

    out: dict[str, GlobalRaster] = {}
    for layer, rasters in per_layer.items():
        if not rasters:
            continue
        how = layer_combiner(layer)
        out[layer] = combine_rasters(rasters, how, layer)
        have = sorted({i.month for i in inputs if i.layer == layer or (layer.startswith("modis_lst") and i.layer == "modis_lst")})
        notes.append(f"{layer}: {len(rasters)} month(s) combined by {how} (calendar months {have})")
    out.update(terrain)
    return out, inputs, notes


def layer_combiner(layer: str) -> str:
    """How monthly rasters of a layer are reduced (see ``LAYER_COMBINER``)."""
    return LAYER_COMBINER[layer]


def sample_point(raster: GlobalRaster, lon: ArrayLike, lat: ArrayLike) -> FloatArray:
    """Nearest-pixel value at points (NaN if invalid); mainly for tests and reports."""
    lon_a, lat_a = np.atleast_1d(np.asarray(lon, dtype=np.float64)), np.atleast_1d(np.asarray(lat, dtype=np.float64))
    i = np.clip(np.floor((raster.lat_north - lat_a) / raster.dlat).astype(int), 0, raster.rows - 1)
    j = (np.floor(((lon_a - raster.lon_west) % 360.0) / raster.dlon).astype(int)) % raster.cols
    return raster.values[i, j].astype(np.float64)
