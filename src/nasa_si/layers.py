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
        lats = self.lats()
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
    """Element-wise max or min over same-shaped rasters, ignoring NaN (all-NaN stays NaN)."""
    if not rasters:
        raise ValueError("no rasters to combine")
    first = rasters[0]
    for r in rasters[1:]:
        if r.values.shape != first.values.shape or (r.lon_west, r.lat_north, r.dlon, r.dlat) != (first.lon_west, first.lat_north, first.dlon, first.dlat):
            raise ValueError("rasters must share one grid to be combined")
    stack = np.stack([r.values for r in rasters]).astype(np.float64)
    all_nan = np.all(np.isnan(stack), axis=0)
    filled = np.where(np.isnan(stack), -np.inf if how == "max" else np.inf, stack)
    if how == "max":
        out = filled.max(axis=0)
    elif how == "min":
        out = filled.min(axis=0)
    else:
        raise ValueError("how must be 'max' or 'min'")
    out[all_nan] = np.nan
    return GlobalRaster(name, out.astype(np.float32), first.lon_west, first.lat_north, first.dlon, first.dlat, first.unit)


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
    """Locate MODIS monthly files below ``raw_dir`` by product, sorted by acquisition month."""
    root = Path(raw_dir)
    found: dict[str, list[Path]] = {"MOD11C3": [], "MOD13C2": []}
    for product in found:
        found[product] = sorted((p for p in root.rglob(f"{product}.A*.hdf")), key=month_of)
    return found


def load_stage1_rasters(
    raw_dir: Path | str, *, months: tuple[int, ...] | None = None, layers: set[str] | None = None
) -> tuple[dict[str, GlobalRaster], list[InputFile], list[str]]:
    """Build the per-layer rasters the stage-1 features are derived from.

    Returns ``(rasters_by_layer, inputs, notes)``. ``rasters_by_layer`` keys are the layer ids used in
    ``config/prefilter_features.csv``: ``modis_lst_day``, ``modis_lst_night`` and ``modis_ndvi``; each is
    the element-wise max or min over the months found (see ``layer_combiner``). ``months`` restricts
    which calendar months are used; ``layers`` restricts which layers are read (default: all). ``notes`` records anything a reader should know about coverage.
    """
    files = find_modis_files(raw_dir)
    inputs: list[InputFile] = []
    notes: list[str] = []
    per_layer: dict[str, list[GlobalRaster]] = {"modis_lst_day": [], "modis_lst_night": [], "modis_ndvi": []}

    want = lambda *names: layers is None or any(n in layers for n in names)  # noqa: E731
    for path in files["MOD11C3"] if want("modis_lst_day", "modis_lst_night") else []:
        year, month = month_of(path)
        if months and month not in months:
            continue
        per_layer["modis_lst_day"].append(read_modis_lst(path, night=False))
        per_layer["modis_lst_night"].append(read_modis_lst(path, night=True))
        inputs.append(InputFile(str(path), "modis_lst", year, month, sha256_of(path)))
    for path in files["MOD13C2"] if want("modis_ndvi") else []:
        year, month = month_of(path)
        if months and month not in months:
            continue
        per_layer["modis_ndvi"].append(read_modis_ndvi(path))
        inputs.append(InputFile(str(path), "modis_ndvi", year, month, sha256_of(path)))

    out: dict[str, GlobalRaster] = {}
    for layer, rasters in per_layer.items():
        if not rasters:
            continue
        out[layer] = combine_rasters(rasters, layer_combiner(layer), layer)
        have = sorted({m for m in (month_of(i.path)[1] for i in inputs if i.layer == layer.replace("_day", "").replace("_night", ""))})
        notes.append(f"{layer}: {len(rasters)} month(s) combined by {layer_combiner(layer)} (calendar months {have})")
    return out, inputs, notes


def layer_combiner(layer: str) -> str:
    """How monthly rasters are reduced: warmest day LST and greenest NDVI use max, coldest night LST min."""
    return "min" if layer == "modis_lst_night" else "max"


def sample_point(raster: GlobalRaster, lon: ArrayLike, lat: ArrayLike) -> FloatArray:
    """Nearest-pixel value at points (NaN if invalid); mainly for tests and reports."""
    lon_a, lat_a = np.atleast_1d(np.asarray(lon, dtype=np.float64)), np.atleast_1d(np.asarray(lat, dtype=np.float64))
    i = np.clip(np.floor((raster.lat_north - lat_a) / raster.dlat).astype(int), 0, raster.rows - 1)
    j = (np.floor(((lon_a - raster.lon_west) % 360.0) / raster.dlon).astype(int)) % raster.cols
    return raster.values[i, j].astype(np.float64)
