"""Offline Earth analog terrain and permafrost screening on the EPSG:6933 9 km grid.

Implements the Arcadia Outpost Earth baseline pilot and full North America screening
workflow described in EARTH_MAP_HANDOFF.md and EARTH_DATA.md.
"""

from __future__ import annotations

import argparse
import base64
import csv
import ctypes
import hashlib
import io
import json
import math
import os
import re
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pyogrio
import pyproj
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]
BoolArray = NDArray[np.bool_]

# Fixed EPSG:6933 master grid parameters covering the North American download envelope
# (-170.0, 14.0, -52.0, 72.0), snapped outward to exact multiples of 9,000 m.
GRID_CRS = "EPSG:6933"
SOURCE_CRS = "EPSG:4326"
CELL_SIZE_M = 9_000.0
SUBPIXELS_PER_SIDE = 9
SUBPIXEL_SIZE_M = CELL_SIZE_M / SUBPIXELS_PER_SIDE  # 1,000.0 m (1 km) equal-area sub-pixels
DEM_AGG_FACTOR_1KM = 10  # 10x10 block-average (~90 m GLO-90 -> ~926 m / ~1 km) before slope
ORIGIN_WEST_M = -16_407_000.0
ORIGIN_NORTH_M = 6_984_000.0
STUDY_BBOX = (-170.0, 14.0, -52.0, 72.0)
DEFAULT_PILOT_BBOX = (-149.0, 62.0, -146.0, 65.0)
DEFAULT_MAINLAND_MASK = Path(__file__).resolve().parent / "north_america_mainland_v1.geojson"

# WGS84 ellipsoid constants for physical horizontal pixel spacing
WGS84_A_M = 6_378_137.0
WGS84_INV_F = 298.257223563
WGS84_F = 1.0 / WGS84_INV_F
WGS84_E2 = 2.0 * WGS84_F - WGS84_F * WGS84_F

# ESA CCI Permafrost v5 constants (verified from NetCDF-4 metadata)
PFR_FILL_VALUE = 255
PFR_VALID_MIN = 0
PFR_VALID_MAX = 100

CELL_COLUMNS = (
    "rank",
    "cell_id",
    "grid_row",
    "grid_col",
    "center_x_m",
    "center_y_m",
    "center_lat_deg",
    "center_lon_deg",
    "land_area_fraction",
    "elevation_m",
    "flatness_fraction",
    "permafrost_fraction",
    "input_coverage",
    "eligible",
    "gate_failure",
    "score",
)

TILE_PATTERN = re.compile(r"_([NS])(\d{2})_00_([EW])(\d{3})_00_DEM\.tif$")


@dataclass(frozen=True)
class EarthScreeningConfig:
    """Provisional Earth baseline assumptions for Arcadia Outpost analog screening."""

    flat_slope_deg: float = 5.0
    min_coverage: float = 0.80
    flatness_weight: float = 0.50
    permafrost_weight: float = 0.50
    strip_cell_rows: int = 20
    subpixels_per_side: int | None = None
    dem_agg_factor: int = DEM_AGG_FACTOR_1KM
    verify_sha256: bool = True
    io_threads: int = 8
    figure_dpi: int = 240

    def __post_init__(self) -> None:
        total = self.flatness_weight + self.permafrost_weight
        if not math.isclose(total, 1.0, rel_tol=1e-6):
            raise ValueError(f"Feature weights must sum to 1.0, got {total}")
        if not (0.0 < self.min_coverage <= 1.0):
            raise ValueError(f"min_coverage must be in (0, 1], got {self.min_coverage}")
        if self.flat_slope_deg < 0.0:
            raise ValueError(f"flat_slope_deg must be non-negative, got {self.flat_slope_deg}")


@dataclass(frozen=True)
class GridWindow:
    """A rectangular sub-window of the fixed EPSG:6933 9,000 m master grid."""

    row_start: int
    row_stop: int
    col_start: int
    col_stop: int

    @property
    def rows(self) -> int:
        return self.row_stop - self.row_start

    @property
    def cols(self) -> int:
        return self.col_stop - self.col_start

    @property
    def west_m(self) -> float:
        return ORIGIN_WEST_M + self.col_start * CELL_SIZE_M

    @property
    def east_m(self) -> float:
        return ORIGIN_WEST_M + self.col_stop * CELL_SIZE_M

    @property
    def north_m(self) -> float:
        return ORIGIN_NORTH_M - self.row_start * CELL_SIZE_M

    @property
    def south_m(self) -> float:
        return ORIGIN_NORTH_M - self.row_stop * CELL_SIZE_M


@dataclass(frozen=True)
class VerifiedAsset:
    dataset: str
    filename: str
    path: Path
    receipt_path: Path
    url: str
    sha256: str
    actual_bytes: int
    product: str | None
    year: int | None
    downloaded_utc: str | None


class _GDALReader:
    """Lightweight ctypes binding to pyogrio's bundled GDAL shared library for GeoTIFF reading."""

    def __init__(self) -> None:
        libdir = Path(pyogrio.__file__).resolve().parent.parent / "pyogrio.libs"
        if not libdir.exists():
            raise RuntimeError(f"Could not locate pyogrio.libs directory at {libdir}")
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(libdir))
        dlls = list(libdir.glob("gdal-*.dll"))
        if not dlls:
            raise RuntimeError(f"No GDAL DLL found in {libdir}")
        self.gdal = ctypes.CDLL(str(dlls[0]))
        self.gdal.GDALAllRegister()

        self.gdal.GDALOpen.argtypes = [ctypes.c_char_p, ctypes.c_int]
        self.gdal.GDALOpen.restype = ctypes.c_void_p
        self.gdal.GDALClose.argtypes = [ctypes.c_void_p]
        self.gdal.GDALClose.restype = None
        self.gdal.GDALGetRasterXSize.argtypes = [ctypes.c_void_p]
        self.gdal.GDALGetRasterXSize.restype = ctypes.c_int
        self.gdal.GDALGetRasterYSize.argtypes = [ctypes.c_void_p]
        self.gdal.GDALGetRasterYSize.restype = ctypes.c_int
        self.gdal.GDALGetRasterBand.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.gdal.GDALGetRasterBand.restype = ctypes.c_void_p
        self.gdal.GDALGetRasterNoDataValue.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        ]
        self.gdal.GDALGetRasterNoDataValue.restype = ctypes.c_double
        self.gdal.GDALGetGeoTransform.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_double * 6),
        ]
        self.gdal.GDALGetGeoTransform.restype = ctypes.c_int
        self.gdal.GDALGetProjectionRef.argtypes = [ctypes.c_void_p]
        self.gdal.GDALGetProjectionRef.restype = ctypes.c_char_p
        self.gdal.GDALGetMetadataItem.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]
        self.gdal.GDALGetMetadataItem.restype = ctypes.c_char_p
        self.gdal.GDALRasterIO.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        self.gdal.GDALRasterIO.restype = ctypes.c_int

    def read_geotiff(self, path: Path) -> tuple[NDArray[np.float32], dict[str, Any]]:
        ds = self.gdal.GDALOpen(str(path).encode("utf-8"), 0)
        if not ds:
            raise RuntimeError(f"GDAL failed to open GeoTIFF: {path}")
        try:
            width = self.gdal.GDALGetRasterXSize(ds)
            height = self.gdal.GDALGetRasterYSize(ds)
            gt = (ctypes.c_double * 6)()
            self.gdal.GDALGetGeoTransform(ds, ctypes.byref(gt))
            proj_bytes = self.gdal.GDALGetProjectionRef(ds)
            proj_wkt = proj_bytes.decode("utf-8", errors="replace") if proj_bytes else ""
            area_or_point_bytes = self.gdal.GDALGetMetadataItem(ds, b"AREA_OR_POINT", None)
            area_or_point = (
                area_or_point_bytes.decode("utf-8", errors="replace")
                if area_or_point_bytes
                else "Area"
            )

            band = self.gdal.GDALGetRasterBand(ds, 1)
            has_nodata = ctypes.c_int(0)
            nodata_val = self.gdal.GDALGetRasterNoDataValue(band, ctypes.byref(has_nodata))

            buffer = np.empty((height, width), dtype=np.float32)
            # GDT_Float32 == 6, GF_Read == 0
            err = self.gdal.GDALRasterIO(
                band,
                0,
                0,
                0,
                width,
                height,
                buffer.ctypes.data_as(ctypes.c_void_p),
                width,
                height,
                6,
                0,
                0,
            )
            if err != 0:
                raise RuntimeError(f"GDALRasterIO failed with code {err} on {path}")
            if has_nodata.value:
                buffer[np.isclose(buffer, float(nodata_val))] = np.nan
            metadata = {
                "width": width,
                "height": height,
                "geotransform": tuple(float(x) for x in gt),
                "crs_wkt": proj_wkt,
                "area_or_point": area_or_point,
                "has_nodata": bool(has_nodata.value),
                "nodata": float(nodata_val) if has_nodata.value else None,
            }
            return buffer, metadata
        finally:
            self.gdal.GDALClose(ds)


_GDAL_READER: _GDALReader | None = None
_GDAL_LOCK = threading.Lock()


def get_gdal_reader() -> _GDALReader:
    global _GDAL_READER
    if _GDAL_READER is None:
        with _GDAL_LOCK:
            if _GDAL_READER is None:
                _GDAL_READER = _GDALReader()
    return _GDAL_READER


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def parse_tile_sw_corner(filename: str) -> tuple[int, int] | None:
    """Return (south_lat_deg, west_lon_deg) from a Copernicus GLO-90 filename."""
    match = TILE_PATTERN.search(filename)
    if not match:
        return None
    lat = int(match.group(2)) * (1 if match.group(1) == "N" else -1)
    lon = int(match.group(4)) * (1 if match.group(3) == "E" else -1)
    return lat, lon


def discover_verified_inputs(
    inventory_path: Path,
    raw_dir: Path,
) -> tuple[dict[tuple[int, int], VerifiedAsset], VerifiedAsset]:
    """Match inventory terrain and permafrost entries with local payloads and JSON receipts.

    Staging directories (`staging-*`) are ignored.
    """
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    terrain_tiles: dict[tuple[int, int], VerifiedAsset] = {}
    permafrost_asset: VerifiedAsset | None = None

    for asset in inventory.get("assets", []):
        dataset = asset.get("dataset")
        if dataset not in ("terrain", "permafrost"):
            continue
        filename = asset["filename"]
        target = raw_dir / dataset / filename
        receipt_path = target.with_suffix(target.suffix + ".json")
        if any(part.startswith("staging-") for part in target.parts):
            continue
        if not target.exists() or not receipt_path.exists():
            continue
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if receipt.get("url") != asset.get("url"):
            continue
        actual_bytes = target.stat().st_size
        expected_bytes = receipt.get("actual_bytes") or asset.get("expected_bytes")
        if expected_bytes is not None and actual_bytes != int(expected_bytes):
            continue
        verified = VerifiedAsset(
            dataset=dataset,
            filename=filename,
            path=target,
            receipt_path=receipt_path,
            url=str(asset["url"]),
            sha256=str(receipt["sha256"]),
            actual_bytes=actual_bytes,
            product=asset.get("product"),
            year=asset.get("year"),
            downloaded_utc=receipt.get("downloaded_utc"),
        )
        if dataset == "terrain":
            sw = parse_tile_sw_corner(filename)
            if sw is not None:
                terrain_tiles[sw] = verified
        elif dataset == "permafrost":
            permafrost_asset = verified

    if permafrost_asset is None:
        raise FileNotFoundError(
            f"Verified permafrost NetCDF and receipt not found under {raw_dir / 'permafrost'}"
        )
    if not terrain_tiles:
        raise FileNotFoundError(
            f"No verified Copernicus terrain tiles with receipts found under {raw_dir / 'terrain'}"
        )
    return terrain_tiles, permafrost_asset


def grid_window_for_bbox(bbox: tuple[float, float, float, float]) -> GridWindow:
    """Map a geographic (west, south, east, north) bbox onto the fixed EPSG:6933 9 km grid."""
    west, south, east, north = bbox
    if not (-180.0 <= west < east <= 180.0 and -90.0 <= south < north <= 90.0):
        raise ValueError(f"Invalid geographic bounding box: {bbox}")
    to_6933 = pyproj.Transformer.from_crs(SOURCE_CRS, GRID_CRS, always_xy=True)
    x_min, y_min = to_6933.transform(west, south)
    x_max, y_max = to_6933.transform(east, north)

    col_start = int(math.floor((x_min - ORIGIN_WEST_M) / CELL_SIZE_M))
    col_stop = int(math.ceil((x_max - ORIGIN_WEST_M) / CELL_SIZE_M))
    row_start = int(math.floor((ORIGIN_NORTH_M - y_max) / CELL_SIZE_M))
    row_stop = int(math.ceil((ORIGIN_NORTH_M - y_min) / CELL_SIZE_M))
    if row_stop <= row_start or col_stop <= col_start:
        raise ValueError(f"Bounding box {bbox} produced an empty EPSG:6933 grid window")
    return GridWindow(
        row_start=row_start,
        row_stop=row_stop,
        col_start=col_start,
        col_stop=col_stop,
    )


def wgs84_pixel_spacings_m(
    latitudes_deg: FloatArray,
    dlon_deg: float,
    dlat_deg: float,
) -> tuple[FloatArray, FloatArray]:
    """Return physical (dx_m, dy_m) horizontal pixel distances at each latitude on WGS84."""
    phi = np.radians(np.asarray(latitudes_deg, dtype=np.float64))
    sin2 = np.sin(phi) ** 2
    denom = np.sqrt(1.0 - WGS84_E2 * sin2)
    meridional_radius = WGS84_A_M * (1.0 - WGS84_E2) / (denom**3)
    prime_vertical_radius = WGS84_A_M / denom
    dy_m = meridional_radius * math.radians(abs(dlat_deg))
    dx_m = prime_vertical_radius * np.cos(phi) * math.radians(abs(dlon_deg))
    return dx_m, dy_m


def aggregate_dem_to_1km(
    elevation_m: np.ndarray,
    factor: int = DEM_AGG_FACTOR_1KM,
) -> NDArray[np.float32]:
    """Block-average a fine Copernicus DEM tile (~90 m) to ~1 km resolution before computing slope."""
    arr = np.asarray(elevation_m, dtype=np.float32)
    if factor <= 1:
        return arr
    h, w = arr.shape
    fy = factor if (h >= factor and h % factor == 0) else 1
    fx = factor if (w >= factor and w % factor == 0) else 1
    if fy == 1 and fx == 1:
        return arr
    blocks = arr.reshape(h // fy, fy, w // fx, fx)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmean(blocks, axis=(1, 3)).astype(np.float32)


def compute_tile_slope_degrees(
    elevation_m: np.ndarray,
    *,
    south_lat_deg: float,
    west_lon_deg: float,
    north_row: np.ndarray | None = None,
    south_row: np.ndarray | None = None,
    west_col: np.ndarray | None = None,
    east_col: np.ndarray | None = None,
) -> FloatArray:
    """Compute slope in degrees on a 1x1 deg Copernicus DEM tile with 1-pixel neighbor halo."""
    if elevation_m.ndim != 2:
        raise ValueError("elevation_m must be a 2-D array")
    height, width = elevation_m.shape
    dlat_deg = 1.0 / height
    dlon_deg = 1.0 / width

    row_lats = (south_lat_deg + 1.0) - np.arange(height, dtype=np.float64) * dlat_deg
    col_lons = west_lon_deg + np.arange(width, dtype=np.float64) * dlon_deg

    padded = np.full((height + 2, width + 2), np.nan, dtype=np.float64)
    padded[1:-1, 1:-1] = elevation_m

    def _align_row(row_values: np.ndarray) -> FloatArray:
        row_arr = np.asarray(row_values, dtype=np.float64).reshape(-1)
        if row_arr.size == width:
            return row_arr
        neighbor_lons = west_lon_deg + np.arange(row_arr.size, dtype=np.float64) * (
            1.0 / row_arr.size
        )
        return np.interp(col_lons, neighbor_lons, row_arr, left=np.nan, right=np.nan)

    if north_row is not None:
        padded[0, 1:-1] = _align_row(north_row)
    if south_row is not None:
        padded[-1, 1:-1] = _align_row(south_row)
    if west_col is not None:
        col_arr = np.asarray(west_col, dtype=np.float64).reshape(-1)
        if col_arr.size != height:
            raise ValueError(f"west_col length {col_arr.size} must match tile height {height}")
        padded[1:-1, 0] = col_arr
    if east_col is not None:
        col_arr = np.asarray(east_col, dtype=np.float64).reshape(-1)
        if col_arr.size != height:
            raise ValueError(f"east_col length {col_arr.size} must match tile height {height}")
        padded[1:-1, -1] = col_arr

    dx_m, dy_m = wgs84_pixel_spacings_m(row_lats, dlon_deg=dlon_deg, dlat_deg=dlat_deg)
    dz_dy = (padded[:-2, 1:-1] - padded[2:, 1:-1]) / (2.0 * dy_m[:, None])
    dz_dx = (padded[1:-1, 2:] - padded[1:-1, :-2]) / (2.0 * dx_m[:, None])
    slope = np.degrees(np.arctan(np.hypot(dz_dx, dz_dy)))
    slope[~np.isfinite(elevation_m)] = np.nan
    return slope


def decode_permafrost_fraction(raw_pfr: np.ndarray) -> FloatArray:
    """Convert raw ESA CCI v5 PFR uint8 percent array to 0..1 float, preserving missing as NaN."""
    arr = np.asarray(raw_pfr)
    result = np.full(arr.shape, np.nan, dtype=np.float64)
    valid = (arr >= PFR_VALID_MIN) & (arr <= PFR_VALID_MAX) & (arr != PFR_FILL_VALUE)
    result[valid] = arr[valid].astype(np.float64) / 100.0
    return result


def score_cell_subpixels(
    *,
    elevation_m: np.ndarray,
    slope_deg: np.ndarray,
    permafrost_fraction: np.ndarray,
    land_mask: BoolArray,
    config: EarthScreeningConfig,
) -> dict[str, Any]:
    """Aggregate equal-area sub-pixels (..., N_sub) into cell features, eligibility, and score."""
    land = np.asarray(land_mask, dtype=bool)
    total_subpixels = land.shape[-1]
    land_count = land.sum(axis=-1)
    land_area_fraction = land_count / float(total_subpixels)

    joint_valid = (
        land
        & np.isfinite(elevation_m)
        & np.isfinite(slope_deg)
        & np.isfinite(permafrost_fraction)
    )
    valid_count = joint_valid.sum(axis=-1)

    with np.errstate(divide="ignore", invalid="ignore"):
        input_coverage = np.where(land_count > 0, valid_count / land_count, 0.0)

    elev_joint = np.where(joint_valid, elevation_m, np.nan)
    pfr_joint = np.where(joint_valid, permafrost_fraction, np.nan)
    flat_joint = joint_valid & (slope_deg <= config.flat_slope_deg)

    terrain_valid = land & np.isfinite(elevation_m) & np.isfinite(slope_deg)
    terrain_count = terrain_valid.sum(axis=-1)
    elev_terrain = np.where(terrain_valid, elevation_m, np.nan)
    flat_terrain = terrain_valid & (slope_deg <= config.flat_slope_deg)

    pfr_only_valid = land & np.isfinite(permafrost_fraction)
    pfr_only = np.where(pfr_only_valid, permafrost_fraction, np.nan)

    with warnings.catch_warnings(), np.errstate(divide="ignore", invalid="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        elev_median_joint = np.nanmedian(elev_joint, axis=-1)
        flatness_joint = np.where(valid_count > 0, flat_joint.sum(axis=-1) / valid_count, np.nan)
        pfr_mean_joint = np.nanmean(pfr_joint, axis=-1)

        elev_median_terrain = np.nanmedian(elev_terrain, axis=-1)
        flatness_terrain = np.where(
            terrain_count > 0, flat_terrain.sum(axis=-1) / terrain_count, np.nan
        )
        pfr_mean_only = np.nanmean(pfr_only, axis=-1)

    eligible = (land_count > 0) & (input_coverage >= config.min_coverage)
    eligible &= np.isfinite(flatness_joint) & np.isfinite(pfr_mean_joint)

    elev_median = np.where(eligible, elev_median_joint, elev_median_terrain)
    flatness = np.where(eligible, flatness_joint, flatness_terrain)
    pfr_mean = np.where(eligible, pfr_mean_joint, pfr_mean_only)

    raw_score = (
        config.flatness_weight * flatness_joint + config.permafrost_weight * pfr_mean_joint
    )
    score = np.where(eligible, raw_score, np.nan)

    gate_failure = np.full(land_count.shape, "", dtype=object)
    no_land = land_count == 0
    low_cov = (~no_land) & (input_coverage < config.min_coverage)
    gate_failure[no_land] = "no_land"
    gate_failure[low_cov] = f"coverage<{config.min_coverage:g}"

    return {
        "land_area_fraction": land_area_fraction,
        "elevation_m": elev_median,
        "flatness_fraction": flatness,
        "permafrost_fraction": pfr_mean,
        "input_coverage": input_coverage,
        "eligible": eligible,
        "gate_failure": gate_failure.astype(str),
        "score": score,
    }


class _SlidingBandTileCache:
    """Bounded-memory latitude-band tile cache with multi-threaded prefetch and ~1 km DEM aggregation."""

    def __init__(
        self,
        tiles: dict[tuple[int, int], VerifiedAsset],
        *,
        dem_agg_factor: int = DEM_AGG_FACTOR_1KM,
        verify_sha256: bool = True,
        io_threads: int = 8,
    ) -> None:
        self.tiles = tiles
        self.dem_agg_factor = max(1, int(dem_agg_factor))
        self.verify_sha256 = verify_sha256
        self.io_threads = max(1, io_threads)
        self._reader = get_gdal_reader()
        self._elev_cache: dict[tuple[int, int], NDArray[np.float32]] = {}
        self._slope_cache: dict[tuple[int, int], tuple[NDArray[np.float32], NDArray[np.float32]]] = {}
        self.used_assets: dict[tuple[int, int], VerifiedAsset] = {}
        self.sample_metadata: dict[str, Any] | None = None

    def _load_single_tile(
        self, key: tuple[int, int]
    ) -> tuple[tuple[int, int], VerifiedAsset, NDArray[np.float32], dict[str, Any]]:
        asset = self.tiles[key]
        if self.verify_sha256 and key not in self.used_assets:
            actual_hash = file_sha256(asset.path)
            if actual_hash != asset.sha256:
                raise ValueError(
                    f"SHA-256 mismatch for {asset.path}: expected {asset.sha256}, got {actual_hash}"
                )
        raw_values, meta = self._reader.read_geotiff(asset.path)
        values = aggregate_dem_to_1km(raw_values, factor=self.dem_agg_factor)
        meta = {
            **meta,
            "dem_agg_factor": self.dem_agg_factor,
            "aggregated_height_1km": int(values.shape[0]),
            "aggregated_width_1km": int(values.shape[1]),
        }
        return key, asset, values, meta

    def prefetch_keys(self, keys: set[tuple[int, int]]) -> None:
        needed = [k for k in sorted(keys) if k in self.tiles and k not in self._elev_cache]
        if not needed:
            return
        if len(needed) == 1 or self.io_threads == 1:
            loaded = [self._load_single_tile(k) for k in needed]
        else:
            with ThreadPoolExecutor(max_workers=self.io_threads) as executor:
                loaded = list(executor.map(self._load_single_tile, needed))
        for key, asset, values, meta in loaded:
            self._elev_cache[key] = values
            self.used_assets[key] = asset
            if self.sample_metadata is None:
                self.sample_metadata = {"filename": asset.filename, **meta}

    def evict_above_latitude(self, max_lat_needed: int) -> None:
        for k in [k for k in self._elev_cache if k[0] > max_lat_needed]:
            self._elev_cache.pop(k, None)
        for k in [k for k in self._slope_cache if k[0] > max_lat_needed - 1]:
            self._slope_cache.pop(k, None)

    def get_elevation_and_slope(
        self, key: tuple[int, int]
    ) -> tuple[NDArray[np.float32], NDArray[np.float32]] | None:
        if key not in self.tiles:
            return None
        if key in self._slope_cache:
            return self._slope_cache[key]
        lat_deg, lon_deg = key
        neighbors = {
            key,
            (lat_deg + 1, lon_deg),
            (lat_deg - 1, lon_deg),
            (lat_deg, lon_deg - 1),
            (lat_deg, lon_deg + 1),
        }
        self.prefetch_keys(neighbors)
        elev = self._elev_cache.get(key)
        if elev is None:
            return None
        north_elev = self._elev_cache.get((lat_deg + 1, lon_deg))
        south_elev = self._elev_cache.get((lat_deg - 1, lon_deg))
        west_elev = self._elev_cache.get((lat_deg, lon_deg - 1))
        east_elev = self._elev_cache.get((lat_deg, lon_deg + 1))

        slope = compute_tile_slope_degrees(
            elev,
            south_lat_deg=float(lat_deg),
            west_lon_deg=float(lon_deg),
            north_row=north_elev[-1, :] if north_elev is not None else None,
            south_row=south_elev[0, :] if south_elev is not None else None,
            west_col=west_elev[:, -1] if west_elev is not None else None,
            east_col=east_elev[:, 0] if east_elev is not None else None,
        ).astype(np.float32)
        self._slope_cache[key] = (elev, slope)
        return elev, slope


class _PermafrostReader:
    """Windowed reader for ESA CCI Permafrost v5 NetCDF-4."""

    def __init__(self, asset: VerifiedAsset, *, verify_sha256: bool = True) -> None:
        if verify_sha256:
            actual_hash = file_sha256(asset.path)
            if actual_hash != asset.sha256:
                raise ValueError(
                    f"SHA-256 mismatch for {asset.path}: expected {asset.sha256}, got {actual_hash}"
                )
        self.asset = asset
        self.file = h5py.File(asset.path, "r")
        self.lat = np.asarray(self.file["lat"][:], dtype=np.float64)
        self.lon = np.asarray(self.file["lon"][:], dtype=np.float64)
        self.pfr_ds = self.file["PFR"]

        self.lat_min = float(self.lat[0])
        self.lat_max = float(self.lat[-1])
        self.lon_min = float(self.lon[0])
        self.lon_max = float(self.lon[-1])
        self.dlat = float(np.median(np.diff(self.lat)))
        self.dlon = float(np.median(np.diff(self.lon)))

        spatial_ref_attrs = {
            k: (v.decode("utf-8", errors="replace") if isinstance(v, (bytes, np.bytes_)) else str(v))
            for k, v in self.file["spatial_ref"].attrs.items()
        }
        pfr_attrs = {
            k: (v.decode("utf-8", errors="replace") if isinstance(v, (bytes, np.bytes_)) else str(v))
            for k, v in self.pfr_ds.attrs.items()
            if k not in ("DIMENSION_LIST", "_Netcdf4Coordinates")
        }
        self.metadata = {
            "filename": asset.filename,
            "shape": list(self.pfr_ds.shape),
            "dtype": str(self.pfr_ds.dtype),
            "lat_range_centers_deg": [self.lat_min, self.lat_max],
            "lon_range_centers_deg": [self.lon_min, self.lon_max],
            "dlat_deg": round(self.dlat, 6),
            "dlon_deg": round(self.dlon, 6),
            "pfr_attributes": pfr_attrs,
            "spatial_ref_attributes": spatial_ref_attrs,
        }

    def sample_grid(self, lat_vec: FloatArray, lon_vec: FloatArray) -> NDArray[np.float32]:
        """Sample permafrost fraction (0..1) on a 2D cartesian grid (lat_vec[:, None], lon_vec[None, :])."""
        out = np.full((lat_vec.size, lon_vec.size), np.nan, dtype=np.float32)
        lat_edge_min = self.lat_min - 0.5 * self.dlat
        lat_edge_max = self.lat_max + 0.5 * self.dlat
        lon_edge_min = self.lon_min - 0.5 * self.dlon
        lon_edge_max = self.lon_max + 0.5 * self.dlon

        valid_lat = (lat_vec >= lat_edge_min) & (lat_vec <= lat_edge_max)
        valid_lon = (lon_vec >= lon_edge_min) & (lon_vec <= lon_edge_max)
        if not np.any(valid_lat) or not np.any(valid_lon):
            return out

        r_idx_all = np.clip(
            np.round((lat_vec - self.lat_min) / self.dlat).astype(np.int64),
            0,
            self.lat.size - 1,
        )
        c_idx_all = np.clip(
            np.round((lon_vec - self.lon_min) / self.dlon).astype(np.int64),
            0,
            self.lon.size - 1,
        )

        r_min = int(r_idx_all[valid_lat].min())
        r_max = int(r_idx_all[valid_lat].max()) + 1
        c_min = int(c_idx_all[valid_lon].min())
        c_max = int(c_idx_all[valid_lon].max()) + 1

        raw_window = self.pfr_ds[0, r_min:r_max, c_min:c_max]
        decoded_window = decode_permafrost_fraction(raw_window).astype(np.float32)

        lat_pos = np.where(valid_lat)[0]
        lon_pos = np.where(valid_lon)[0]
        out[np.ix_(lat_pos, lon_pos)] = decoded_window[
            np.ix_(r_idx_all[lat_pos] - r_min, c_idx_all[lon_pos] - c_min)
        ]
        return out

    def close(self) -> None:
        self.file.close()


def _load_land_mask(
    land_mask_path: Path | None,
) -> tuple[Any, Any, dict[str, str]]:
    """Return (geom_4326, geom_6933, mask_info) if land_mask_path is provided."""
    if land_mask_path is None:
        return None, None, {
            "source": "unmasked_dem_envelope (pilot)",
            "version": "pilot-unmasked-v1",
        }
    import shapely

    mask_meta = {"source": str(land_mask_path), "version": "custom"}
    if land_mask_path.suffix.lower() == ".geojson":
        try:
            raw_json = json.loads(land_mask_path.read_text(encoding="utf-8"))
            if "version" in raw_json:
                mask_meta["version"] = str(raw_json["version"])
            if "source" in raw_json:
                mask_meta["description"] = str(raw_json["source"])
        except Exception:
            pass

    df = pyogrio.read_dataframe(land_mask_path)
    if df.empty:
        geom_4326 = shapely.GeometryCollection()
    else:
        if df.crs and df.crs.to_epsg() != 4326:
            df = df.to_crs("EPSG:4326")
        geom_4326 = shapely.union_all(df.geometry.values)

    to_6933 = pyproj.Transformer.from_crs(SOURCE_CRS, GRID_CRS, always_xy=True)
    geom_6933 = shapely.transform(
        geom_4326,
        lambda coords: np.column_stack(to_6933.transform(coords[:, 0], coords[:, 1])),
    )
    shapely.prepare(geom_4326)
    shapely.prepare(geom_6933)
    return geom_4326, geom_6933, mask_meta


def plot_five_panel_report(
    output_path: Path,
    *,
    window: GridWindow,
    results: dict[str, np.ndarray],
    config: EarthScreeningConfig,
    bbox: tuple[float, float, float, float],
    is_pilot: bool,
    land_geom_6933: Any = None,
) -> None:
    """Render a high-resolution (240 DPI) 2x3 report with the 5 map panels and candidate breakdown."""
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.patches import Patch
    import matplotlib.pyplot as plt

    extent_km = (
        window.west_m / 1000.0,
        window.east_m / 1000.0,
        window.south_m / 1000.0,
        window.north_m / 1000.0,
    )
    has_land = results["land_area_fraction"] > 0.0
    elev_plot = np.where(has_land, results["elevation_m"], np.nan)
    flat_plot = np.where(has_land, results["flatness_fraction"], np.nan)
    pfr_plot = np.where(has_land, results["permafrost_fraction"], np.nan)
    cov_plot = np.where(has_land, results["input_coverage"], np.nan)

    panels = (
        ("1. Median Surface Elevation (m)", elev_plot, "terrain", None, None),
        (
            f"2. Flatness Fraction (slope <= {config.flat_slope_deg:g}°)",
            flat_plot,
            "YlGnBu",
            0.0,
            1.0,
        ),
        (
            "3. Permafrost Fraction (ESA CCI v5, 2023)",
            pfr_plot,
            "PuBu",
            0.0,
            1.0,
        ),
        (
            f"4. Joint Valid Land Coverage (gate >= {config.min_coverage:.0%})",
            cov_plot,
            "cividis",
            0.0,
            1.0,
        ),
        (
            "5. Final Earth Analog Score (gated)",
            results["score"],
            "viridis",
            0.0,
            1.0,
        ),
    )

    fig = plt.figure(figsize=(24, 13.5), constrained_layout=True)
    spec = fig.add_gridspec(2, 3)
    map_axes = [
        fig.add_subplot(spec[0, 0]),
        fig.add_subplot(spec[0, 1]),
        fig.add_subplot(spec[0, 2]),
        fig.add_subplot(spec[1, 0]),
        fig.add_subplot(spec[1, 1]),
    ]
    bar_ax = fig.add_subplot(spec[1, 2])

    rejected_land_mask = has_land & (~results["eligible"])
    rows, cols = rejected_land_mask.shape
    y_centers = np.linspace(extent_km[3] - 4.5, extent_km[2] + 4.5, rows)
    x_centers = np.linspace(extent_km[0] + 4.5, extent_km[1] - 4.5, cols)

    for idx, (ax, (title, data, cmap_name, vmin, vmax)) in enumerate(zip(map_axes, panels)):
        ax.set_facecolor("#eef1f5" if not is_pilot else "#d9d9d9")
        cmap = plt.get_cmap(cmap_name).copy()
        cmap.set_bad((0.0, 0.0, 0.0, 0.0) if not is_pilot else "#d9d9d9")

        if not is_pilot and np.any(has_land):
            bg_land = np.where(has_land, 1.0, np.nan)
            bg_cmap = matplotlib.colors.ListedColormap(["#d9d0d0"])
            bg_cmap.set_bad((0.0, 0.0, 0.0, 0.0))
            ax.imshow(
                bg_land,
                extent=extent_km,
                origin="upper",
                cmap=bg_cmap,
                interpolation="nearest",
                aspect="equal",
            )

        im = ax.imshow(
            data,
            extent=extent_km,
            origin="upper",
            cmap=cmap,
            vmin=vmin,
            vmax=vmax,
            interpolation="nearest",
            aspect="equal",
        )

        if land_geom_6933 is not None:
            geoms = (
                list(land_geom_6933.geoms)
                if hasattr(land_geom_6933, "geoms")
                else [land_geom_6933]
            )
            for poly in geoms:
                if hasattr(poly, "exterior"):
                    xy = np.asarray(poly.exterior.coords) / 1000.0
                    ax.plot(xy[:, 0], xy[:, 1], color="#2b2b2b", linewidth=0.55, alpha=0.75)

        legend_handles = []
        if idx == 4 and np.any(rejected_land_mask):
            if int(rejected_land_mask.sum()) <= 250:
                r_idx, c_idx = np.where(rejected_land_mask)
                sc_rej = ax.scatter(
                    x_centers[c_idx],
                    y_centers[r_idx],
                    marker="x",
                    s=24,
                    c="#aa2222",
                    linewidths=1.1,
                    alpha=0.88,
                    label="Rejected (<80% coverage)",
                )
                legend_handles.append(sc_rej)
            else:
                legend_handles.append(
                    Patch(
                        facecolor="#d9d0d0",
                        edgecolor="#555555",
                        label="Rejected / missing permafrost (<80% cov)",
                    )
                )

        if idx == 4 and "rank" in results:
            top_r, top_c = np.where((results["rank"] >= 1) & (results["rank"] <= 10))
            if top_r.size > 0:
                sc_top = ax.scatter(
                    x_centers[top_c],
                    y_centers[top_r],
                    marker="o",
                    s=44,
                    facecolors="none",
                    edgecolors="#ffffff",
                    linewidths=1.5,
                    label="Top 10 ranked cells",
                )
                legend_handles.append(sc_top)
        if idx == 4 and legend_handles:
            ax.legend(
                handles=legend_handles,
                loc="lower left" if not is_pilot else "lower right",
                fontsize=8.5,
                framealpha=0.92,
            )

        ax.set_xlim(extent_km[0], extent_km[1])
        ax.set_ylim(extent_km[2], extent_km[3])
        ax.set_title(title, fontsize=11.5, fontweight="bold")
        ax.set_xlabel("EPSG:6933 Easting (km)", fontsize=9.5)
        ax.set_ylabel("EPSG:6933 Northing (km)", fontsize=9.5)
        cbar = fig.colorbar(im, ax=ax, orientation="horizontal", shrink=0.84, pad=0.06)
        cbar.ax.tick_params(labelsize=8.5)

    # Panel 6: Top 10 Ranked Cells Score Contribution Breakdown
    top_mask = (results["rank"] >= 1) & (results["rank"] <= 10)
    if np.any(top_mask):
        t_rows, t_cols = np.where(top_mask)
        order = np.argsort(results["rank"][t_rows, t_cols])
        t_rows, t_cols = t_rows[order], t_cols[order]

        to_4326 = pyproj.Transformer.from_crs(GRID_CRS, SOURCE_CRS, always_xy=True)
        labels = []
        flat_contrib = []
        pfr_contrib = []
        for r_i, c_i in zip(t_rows, t_cols):
            rk = int(results["rank"][r_i, c_i])
            g_r = window.row_start + r_i
            g_c = window.col_start + c_i
            cx = ORIGIN_WEST_M + (g_c + 0.5) * CELL_SIZE_M
            cy = ORIGIN_NORTH_M - (g_r + 0.5) * CELL_SIZE_M
            lon_d, lat_d = to_4326.transform(cx, cy)
            labels.append(f"#{rk} r{g_r:04d}_c{g_c:04d} ({lat_d:.2f}°N, {lon_d:.2f}°E)")
            flat_contrib.append(config.flatness_weight * float(results["flatness_fraction"][r_i, c_i]))
            pfr_contrib.append(config.permafrost_weight * float(results["permafrost_fraction"][r_i, c_i]))

        y_pos = np.arange(len(labels))
        bar_ax.barh(
            y_pos,
            flat_contrib,
            color="#2b8cbe",
            label=f"Flatness ({config.flatness_weight:.0%} weight)",
        )
        bar_ax.barh(
            y_pos,
            pfr_contrib,
            left=flat_contrib,
            color="#7bccc4",
            label=f"Permafrost ({config.permafrost_weight:.0%} weight)",
        )
        bar_ax.set_yticks(y_pos, labels=labels, fontsize=8.5)
        bar_ax.invert_yaxis()
        bar_ax.set_xlim(0.0, 1.05)
        bar_ax.set_xlabel("Weighted Score Contribution (0–1 scale)", fontsize=9.5)
        bar_ax.set_title("Top 10 Ranked Cells — Score Breakdown", fontsize=11.5, fontweight="bold")
        bar_ax.grid(axis="x", linestyle="--", alpha=0.4)
        bar_ax.legend(loc="lower right", fontsize=8.5)
    else:
        bar_ax.text(0.5, 0.5, "No eligible cells", ha="center", va="center")

    status_tag = (
        "PILOT RUN (unmasked DEM envelope)"
        if is_pilot
        else "NORTH AMERICA STUDY RUN (mainland mask v1)"
    )
    west, south, east, north = bbox
    eligible_count = int(results["eligible"].sum())
    land_cell_count = int(has_land.sum())
    total_count = int(results["eligible"].size)
    fig.suptitle(
        f"Arcadia Outpost Earth Analog Screening — {status_tag}\n"
        f"Extent: [{west:.2f}°E, {south:.2f}°N, {east:.2f}°E, {north:.2f}°N] | "
        f"Grid: EPSG:6933 (9 km × 9 km) | "
        f"Eligible cells: {eligible_count:,}/{land_cell_count:,} mainland cells ({total_count:,} grid total) | "
        f"Gate >= {config.min_coverage:.0%} coverage, weights: {config.flatness_weight:.0%} flatness + {config.permafrost_weight:.0%} permafrost",
        fontsize=13.0,
        fontweight="bold",
    )
    fig.savefig(output_path, dpi=config.figure_dpi)
    plt.close(fig)


def _encode_layer_png_base64(
    data: np.ndarray,
    has_land: np.ndarray,
    cmap_name: str,
    vmin: float,
    vmax: float,
    *,
    show_rejected_tint: bool = False,
) -> str:
    import matplotlib.pyplot as plt
    from PIL import Image

    cmap = plt.get_cmap(cmap_name)
    norm = np.clip((data - vmin) / max(1e-9, (vmax - vmin)), 0.0, 1.0)
    rgba = (cmap(np.nan_to_num(norm, nan=0.0)) * 255).astype(np.uint8)

    invalid = ~np.isfinite(data)
    outside_land = ~has_land
    # Transparent outside mainland mask
    rgba[outside_land] = (0, 0, 0, 0)
    # Muted warm-slate for mainland cells that are missing permafrost / rejected
    if show_rejected_tint:
        rgba[has_land & invalid] = (205, 192, 192, 225)
    else:
        rgba[has_land & invalid] = (210, 210, 215, 200)

    img = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def write_interactive_html(
    output_path: Path,
    *,
    window: GridWindow,
    results: dict[str, np.ndarray],
    config: EarthScreeningConfig,
    bbox: tuple[float, float, float, float],
    is_pilot: bool,
    top_candidates: list[dict[str, Any]],
) -> None:
    """Write a self-contained interactive HTML5 canvas map explorer with pan/zoom and cell inspector."""
    has_land = results["land_area_fraction"] > 0.0
    elev = np.where(has_land, results["elevation_m"], np.nan)
    flat = np.where(has_land, results["flatness_fraction"], np.nan)
    pfr = np.where(has_land, results["permafrost_fraction"], np.nan)
    cov = np.where(has_land, results["input_coverage"], np.nan)
    scr = results["score"]

    elev_finite = elev[np.isfinite(elev)]
    elev_min = float(np.percentile(elev_finite, 1)) if elev_finite.size else 0.0
    elev_max = float(np.percentile(elev_finite, 99)) if elev_finite.size else 3000.0

    layers_b64 = {
        "score": _encode_layer_png_base64(scr, has_land, "viridis", 0.0, 1.0, show_rejected_tint=True),
        "elevation": _encode_layer_png_base64(elev, has_land, "terrain", elev_min, elev_max),
        "flatness": _encode_layer_png_base64(flat, has_land, "YlGnBu", 0.0, 1.0),
        "permafrost": _encode_layer_png_base64(pfr, has_land, "PuBu", 0.0, 1.0),
        "coverage": _encode_layer_png_base64(cov, has_land, "cividis", 0.0, 1.0),
    }

    # Quantize grid arrays into compact base64 buffers so hovering any of the 734,280 cells
    # shows exact live values in <1 MB without external network requests!
    def _pack_u16(arr: np.ndarray, scale: float, offset: float = 0.0) -> str:
        val = np.round((arr - offset) * scale)
        u16 = np.where(np.isfinite(arr), np.clip(val, 0, 65534), 65535).astype("<u2")
        import zlib

        return base64.b64encode(zlib.compress(u16.tobytes(), level=6)).decode("ascii")

    packed_grids = {
        "elev_u16": _pack_u16(elev, 1.0, -500.0),  # m + 500
        "flat_u16": _pack_u16(flat, 10000.0),
        "pfr_u16": _pack_u16(pfr, 10000.0),
        "cov_u16": _pack_u16(cov, 10000.0),
        "score_u16": _pack_u16(scr, 10000.0),
        "land_u16": _pack_u16(results["land_area_fraction"], 10000.0),
    }

    to_4326 = pyproj.Transformer.from_crs(GRID_CRS, SOURCE_CRS, always_xy=True)
    grid_rows_1d = np.arange(window.row_start, window.row_stop, dtype=np.float64)
    grid_cols_1d = np.arange(window.col_start, window.col_stop, dtype=np.float64)
    cx_1d = ORIGIN_WEST_M + (grid_cols_1d + 0.5) * CELL_SIZE_M
    cy_1d = ORIGIN_NORTH_M - (grid_rows_1d + 0.5) * CELL_SIZE_M
    lon_1d, _ = to_4326.transform(cx_1d, np.zeros_like(cx_1d))
    _, lat_1d = to_4326.transform(np.zeros_like(cy_1d), cy_1d)

    payload = {
        "rows": window.rows,
        "cols": window.cols,
        "row_start": window.row_start,
        "col_start": window.col_start,
        "west_km": window.west_m / 1000.0,
        "east_km": window.east_m / 1000.0,
        "south_km": window.south_m / 1000.0,
        "north_km": window.north_m / 1000.0,
        "lat_1d": [round(float(x), 4) for x in lat_1d],
        "lon_1d": [round(float(x), 4) for x in lon_1d],
        "elev_min": round(elev_min, 1),
        "elev_max": round(elev_max, 1),
        "cells_total": int(window.rows * window.cols),
        "cells_land": int(has_land.sum()),
        "cells_eligible": int(results["eligible"].sum()),
        "is_pilot": is_pilot,
        "bbox": list(bbox),
        "layers": layers_b64,
        "packed": packed_grids,
        "top_candidates": top_candidates[:25],
    }

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Arcadia Outpost — Interactive Earth Analog Map Explorer</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
</head>
<body class="bg-[var(--background)] text-[var(--foreground)] antialiased m-0 p-3 select-none">
  <div class="max-w-[1600px] mx-auto space-y-3">
    <div class="flex flex-wrap items-center justify-between gap-3 pb-2 border-b border-[var(--border)]">
      <div>
        <h1 class="text-base font-bold">Arcadia Outpost Earth Analog Interactive Explorer ({'PILOT' if is_pilot else 'NORTH AMERICA MAINLAND v1'})</h1>
        <p class="text-xs text-[var(--muted-foreground)]">
          EPSG:6933 Equal-Area Grid (9 km × 9 km cells) • Eligible: <span class="font-mono font-semibold text-[var(--foreground)]">{int(results['eligible'].sum()):,}</span> / {int(has_land.sum()):,} mainland cells • Scroll to zoom, drag to pan, click or hover any 9 km cell
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-1.5 text-xs" id="layer-buttons">
        <button data-layer="score" class="px-2.5 py-1 rounded font-medium bg-[var(--primary)] text-[var(--primary-foreground)]">5. Earth Score (Gated)</button>
        <button data-layer="elevation" class="px-2.5 py-1 rounded font-medium bg-[var(--card)] border border-[var(--border)]">1. Elevation (m)</button>
        <button data-layer="flatness" class="px-2.5 py-1 rounded font-medium bg-[var(--card)] border border-[var(--border)]">2. Flatness (&le;5&deg;)</button>
        <button data-layer="permafrost" class="px-2.5 py-1 rounded font-medium bg-[var(--card)] border border-[var(--border)]">3. Permafrost (CCI v5)</button>
        <button data-layer="coverage" class="px-2.5 py-1 rounded font-medium bg-[var(--card)] border border-[var(--border)]">4. Valid Coverage</button>
        <button id="reset-view" class="px-2.5 py-1 rounded bg-[var(--secondary)] text-[var(--secondary-foreground)] ml-2">Reset Zoom</button>
      </div>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-4 gap-3">
      <div class="lg:col-span-3 bg-[var(--card)] border border-[var(--border)] rounded-lg p-2 relative overflow-hidden space-y-2">
        <div class="flex flex-wrap items-center justify-between gap-2 text-xs px-1">
          <div class="flex flex-wrap items-center gap-1.5" id="region-presets">
            <span class="text-[var(--muted-foreground)] font-medium">Quick Jump:</span>
            <button data-region="all" class="px-2 py-0.5 rounded bg-[var(--secondary)] text-[var(--secondary-foreground)] hover:opacity-90">Full Extent</button>
            <button data-lat="70.2" data-lon="-153.0" data-z="5.5" class="px-2 py-0.5 rounded bg-[var(--background)] border border-[var(--border)] hover:bg-[var(--secondary)]">Alaska North Slope</button>
            <button data-lat="63.5" data-lon="-147.5" data-z="7.0" class="px-2 py-0.5 rounded bg-[var(--background)] border border-[var(--border)] hover:bg-[var(--secondary)]">Interior Alaska Pilot</button>
            <button data-lat="70.5" data-lon="-94.0" data-z="5.5" class="px-2 py-0.5 rounded bg-[var(--background)] border border-[var(--border)] hover:bg-[var(--secondary)]">Boothia / Nunavut</button>
            <button data-lat="54.5" data-lon="-84.5" data-z="4.5" class="px-2 py-0.5 rounded bg-[var(--background)] border border-[var(--border)] hover:bg-[var(--secondary)]">Hudson Bay Lowlands</button>
            <button data-lat="52.0" data-lon="-117.0" data-z="5.0" class="px-2 py-0.5 rounded bg-[var(--background)] border border-[var(--border)] hover:bg-[var(--secondary)]">Canadian Rockies</button>
          </div>
          <div id="zoom-readout" class="font-mono text-[var(--muted-foreground)]">Zoom: 1.0x</div>
        </div>
        <canvas id="map-canvas" width="1080" height="540" class="w-full h-[460px] rounded cursor-crosshair block"></canvas>
        <div class="flex flex-wrap items-center justify-between text-xs pt-1 px-1 text-[var(--muted-foreground)] gap-2">
          <div id="legend-bar" class="flex items-center gap-2">
            <span id="legend-title" class="font-semibold text-[var(--foreground)]">Earth Analog Score (0.0 – 1.0)</span>
            <span class="inline-block w-3 h-3 rounded-sm" style="background:#cdc0c0;border:1px solid #777"></span>
            <span>Rejected / &lt;80% valid coverage</span>
          </div>
          <div class="flex items-center gap-2">
            <label for="weight-slider" class="font-medium text-[var(--foreground)]">Flatness Weight:</label>
            <input id="weight-slider" type="range" min="0" max="100" value="{int(round(config.flatness_weight * 100))}" class="w-28 accent-[var(--primary)]" />
            <span id="weight-label" class="font-mono text-[var(--foreground)]">{int(round(config.flatness_weight * 100))}% Flat / {int(round(config.permafrost_weight * 100))}% PFR</span>
          </div>
        </div>
      </div>

      <div class="space-y-3 flex flex-col justify-between">
        <div class="bg-[var(--card)] border border-[var(--border)] rounded-lg p-3 space-y-2">
          <div class="text-xs font-bold uppercase tracking-wider text-[var(--muted-foreground)]">Live 9 km Cell Inspector</div>
          <div id="cell-readout" class="text-xs space-y-1 font-mono">
            <div>Hover or click any cell on the map...</div>
          </div>
        </div>

        <div class="bg-[var(--card)] border border-[var(--border)] rounded-lg p-3 space-y-2 flex-1 overflow-hidden flex flex-col">
          <div class="text-xs font-bold uppercase tracking-wider text-[var(--muted-foreground)]">Top Ranked Analog Cells (Click to Zoom)</div>
          <div id="top-list" class="space-y-1 overflow-y-auto max-h-[260px] pr-1 text-xs"></div>
        </div>
      </div>
    </div>
  </div>

  <script>
    const DATA = {json.dumps(payload)};
    const canvas = document.getElementById('map-canvas');
    const ctx = canvas.getContext('2d');
    let currentLayer = 'score';
    let flatWeight = {config.flatness_weight:.4f};
    let images = {{}};
    let grids = {{}};
    let view = {{ scale: 1, tx: 0, ty: 0 }};
    let selectedCell = null;
    let hoverCell = null;

    async function decompressU16(b64) {{
      const bin = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
      const ds = new DecompressionStream('deflate');
      const writer = ds.writable.getWriter();
      writer.write(bin);
      writer.close();
      const buf = await new Response(ds.readable).arrayBuffer();
      return new Uint16Array(buf);
    }}

    function fitView() {{
      const pad = 16;
      const sx = (canvas.width - pad * 2) / DATA.cols;
      const sy = (canvas.height - pad * 2) / DATA.rows;
      view.scale = Math.min(sx, sy);
      view.tx = (canvas.width - DATA.cols * view.scale) / 2;
      view.ty = (canvas.height - DATA.rows * view.scale) / 2;
      draw();
    }}

    function draw() {{
      const isLight = document.documentElement.classList.contains('light');
      ctx.fillStyle = isLight ? '#eef2f6' : '#141820';
      ctx.fillRect(0, 0, canvas.width, canvas.height);

      ctx.save();
      ctx.translate(view.tx, view.ty);
      ctx.scale(view.scale, view.scale);
      ctx.imageSmoothingEnabled = false;
      if (images[currentLayer]) {{
        ctx.drawImage(images[currentLayer], 0, 0, DATA.cols, DATA.rows);
      }}

      // Highlight top candidates
      ctx.lineWidth = 1.5 / view.scale;
      for (const cand of DATA.top_candidates.slice(0, 10)) {{
        const r = cand.grid_row - DATA.row_start + 0.5;
        const c = cand.grid_col - DATA.col_start + 0.5;
        ctx.strokeStyle = '#ffffff';
        ctx.beginPath();
        ctx.arc(c, r, Math.max(1.2, 5 / view.scale), 0, Math.PI * 2);
        ctx.stroke();
      }}

      const target = selectedCell || hoverCell;
      if (target) {{
        ctx.strokeStyle = '#ff3366';
        ctx.lineWidth = 2.0 / view.scale;
        ctx.strokeRect(target.c, target.r, 1, 1);
      }}
      ctx.restore();
      document.getElementById('zoom-readout').textContent = `Zoom: ${{(view.scale / Math.min((canvas.width-32)/DATA.cols, (canvas.height-32)/DATA.rows)).toFixed(1)}}x`;
    }}

    function updateInspector(r, c) {{
      if (r < 0 || r >= DATA.rows || c < 0 || c >= DATA.cols || !grids.land_u16) return;
      const idx = r * DATA.cols + c;
      const gRow = DATA.row_start + r;
      const gCol = DATA.col_start + c;
      const cellId = `r${{String(gRow).padStart(4, '0')}}_c${{String(gCol).padStart(4, '0')}}`;
      const xKm = (DATA.west_km + (c + 0.5) * 9.0).toFixed(1);
      const yKm = (DATA.north_km - (r + 0.5) * 9.0).toFixed(1);
      const latDeg = DATA.lat_1d[r] !== undefined ? DATA.lat_1d[r].toFixed(3) : '?';
      const lonDeg = DATA.lon_1d[c] !== undefined ? DATA.lon_1d[c].toFixed(3) : '?';

      const fmt = (arr, scale, offset = 0, digits = 3) => {{
        const v = arr[idx];
        return v === 65535 ? 'Missing / NaN' : (v / scale + offset).toFixed(digits);
      }};
      const land = fmt(grids.land_u16, 10000, 0, 3);
      const elev = fmt(grids.elev_u16, 1, -500, 1);
      const flat = fmt(grids.flat_u16, 10000, 0, 3);
      const pfr = fmt(grids.pfr_u16, 10000, 0, 3);
      const cov = fmt(grids.cov_u16, 10000, 0, 3);
      let scr = 'Unscored (Rejected)';
      if (grids.score_u16[idx] !== 65535 && grids.flat_u16[idx] !== 65535 && grids.pfr_u16[idx] !== 65535) {{
        const fVal = grids.flat_u16[idx] / 10000;
        const pVal = grids.pfr_u16[idx] / 10000;
        scr = (flatWeight * fVal + (1 - flatWeight) * pVal).toFixed(4);
      }}

      document.getElementById('cell-readout').innerHTML = `
        <div class="flex justify-between"><span>Cell ID:</span><span class="font-bold">${{cellId}}</span></div>
        <div class="flex justify-between"><span>Lat / Lon:</span><span>${{latDeg}}&deg;N, ${{lonDeg}}&deg;E</span></div>
        <div class="flex justify-between"><span>EPSG:6933 (km):</span><span>(${{xKm}}, ${{yKm}})</span></div>
        <div class="flex justify-between"><span>Land Fraction:</span><span>${{land}}</span></div>
        <div class="flex justify-between"><span>Elevation (m):</span><span>${{elev}}</span></div>
        <div class="flex justify-between"><span>Flatness (&le;5&deg;):</span><span>${{flat}}</span></div>
        <div class="flex justify-between"><span>Permafrost (0-1):</span><span>${{pfr}}</span></div>
        <div class="flex justify-between"><span>Valid Coverage:</span><span>${{cov}}</span></div>
        <div class="flex justify-between pt-1 border-t border-[var(--border)]"><span>Earth Score:</span><span class="font-bold">${{scr}}</span></div>
      `;
    }}

    document.getElementById('weight-slider').addEventListener('input', e => {{
      const pct = Number(e.target.value);
      flatWeight = pct / 100.0;
      document.getElementById('weight-label').textContent = `${{pct}}% Flat / ${{100 - pct}}% PFR`;
      const target = selectedCell || hoverCell;
      if (target) updateInspector(target.r, target.c);
    }});

    document.querySelectorAll('#region-presets button').forEach(btn => {{
      btn.addEventListener('click', () => {{
        if (btn.dataset.region === 'all') {{
          selectedCell = null;
          fitView();
          return;
        }}
        const targetLat = Number(btn.dataset.lat);
        const targetLon = Number(btn.dataset.lon);
        const z = Number(btn.dataset.z || 5.0);
        let bestR = 0, bestC = 0, minDist = 1e9;
        for (let r = 0; r < DATA.rows; r++) {{
          const d = Math.abs(DATA.lat_1d[r] - targetLat);
          if (d < minDist) {{ minDist = d; bestR = r; }}
        }}
        minDist = 1e9;
        for (let c = 0; c < DATA.cols; c++) {{
          const d = Math.abs(DATA.lon_1d[c] - targetLon);
          if (d < minDist) {{ minDist = d; bestC = c; }}
        }}
        const baseScale = Math.min((canvas.width - 32) / DATA.cols, (canvas.height - 32) / DATA.rows);
        view.scale = baseScale * z;
        view.tx = canvas.width / 2 - (bestC + 0.5) * view.scale;
        view.ty = canvas.height / 2 - (bestR + 0.5) * view.scale;
        selectedCell = {{ r: bestR, c: bestC }};
        updateInspector(bestR, bestC);
        draw();
      }});
    }});

    canvas.addEventListener('wheel', e => {{
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const mx = (e.clientX - rect.left) * (canvas.width / rect.width);
      const my = (e.clientY - rect.top) * (canvas.height / rect.height);
      const factor = e.deltaY < 0 ? 1.25 : 0.8;
      const newScale = Math.max(0.4, Math.min(60, view.scale * factor));
      view.tx = mx - (mx - view.tx) * (newScale / view.scale);
      view.ty = my - (my - view.ty) * (newScale / view.scale);
      view.scale = newScale;
      draw();
    }}, {{ passive: false }});

    let dragging = false, lastX = 0, lastY = 0;
    canvas.addEventListener('mousedown', e => {{ dragging = true; lastX = e.clientX; lastY = e.clientY; }});
    window.addEventListener('mouseup', () => {{ dragging = false; }});
    canvas.addEventListener('mousemove', e => {{
      const rect = canvas.getBoundingClientRect();
      const mx = (e.clientX - rect.left) * (canvas.width / rect.width);
      const my = (e.clientY - rect.top) * (canvas.height / rect.height);
      if (dragging) {{
        view.tx += (e.clientX - lastX) * (canvas.width / rect.width);
        view.ty += (e.clientY - lastY) * (canvas.height / rect.height);
        lastX = e.clientX; lastY = e.clientY;
        draw();
        return;
      }}
      const c = Math.floor((mx - view.tx) / view.scale);
      const r = Math.floor((my - view.ty) / view.scale);
      if (r >= 0 && r < DATA.rows && c >= 0 && c < DATA.cols) {{
        hoverCell = {{ r, c }};
        if (!selectedCell) updateInspector(r, c);
        draw();
      }}
    }});

    canvas.addEventListener('click', e => {{
      if (hoverCell) {{
        selectedCell = {{ ...hoverCell }};
        updateInspector(selectedCell.r, selectedCell.c);
        draw();
      }}
    }});

    document.querySelectorAll('#layer-buttons button[data-layer]').forEach(btn => {{
      btn.addEventListener('click', () => {{
        currentLayer = btn.dataset.layer;
        document.querySelectorAll('#layer-buttons button[data-layer]').forEach(b => {{
          b.className = 'px-2.5 py-1 rounded font-medium bg-[var(--card)] border border-[var(--border)]';
        }});
        btn.className = 'px-2.5 py-1 rounded font-medium bg-[var(--primary)] text-[var(--primary-foreground)]';
        draw();
      }});
    }});

    document.getElementById('reset-view').addEventListener('click', () => {{
      selectedCell = null;
      fitView();
    }});

    const topList = document.getElementById('top-list');
    DATA.top_candidates.forEach(cand => {{
      const item = document.createElement('button');
      item.className = 'w-full text-left px-2 py-1.5 rounded bg-[var(--background)] hover:bg-[var(--secondary)] border border-[var(--border)] flex items-center justify-between font-mono';
      item.innerHTML = `<span>#${{cand.rank}} ${{cand.cell_id}} (${{Number(cand.center_lat_deg).toFixed(2)}}°N, ${{Number(cand.center_lon_deg).toFixed(2)}}°E)</span><span class="font-bold">${{Number(cand.score).toFixed(3)}}</span>`;
      item.addEventListener('click', () => {{
        const r = cand.grid_row - DATA.row_start;
        const c = cand.grid_col - DATA.col_start;
        selectedCell = {{ r, c }};
        view.scale = Math.max(view.scale, 8);
        view.tx = canvas.width / 2 - (c + 0.5) * view.scale;
        view.ty = canvas.height / 2 - (r + 0.5) * view.scale;
        updateInspector(r, c);
        draw();
      }});
      topList.appendChild(item);
    }});

    Promise.all([
      ...Object.entries(DATA.layers).map(([k, src]) => new Promise(res => {{
        const img = new Image();
        img.onload = () => {{ images[k] = img; res(); }};
        img.src = src;
      }})),
      ...Object.entries(DATA.packed).map(async ([k, b64]) => {{
        grids[k] = await decompressU16(b64);
      }})
    ]).then(() => {{
      fitView();
      if (DATA.top_candidates.length > 0) {{
        const first = DATA.top_candidates[0];
        updateInspector(first.grid_row - DATA.row_start, first.grid_col - DATA.col_start);
      }}
    }});
  </script>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")


def run_earth_screening(
    *,
    inventory_path: Path,
    raw_dir: Path,
    output_dir: Path,
    bbox: tuple[float, float, float, float] = DEFAULT_PILOT_BBOX,
    land_mask_path: Path | None = None,
    config: EarthScreeningConfig | None = None,
    strict_bbox_tiles: bool = False,
) -> dict[str, Any]:
    """Run the bounded-memory Earth terrain & permafrost screening pipeline."""
    config = config or EarthScreeningConfig()
    started = time.monotonic()
    output_dir.mkdir(parents=True, exist_ok=True)

    terrain_tiles, permafrost_asset = discover_verified_inputs(inventory_path, raw_dir)
    if strict_bbox_tiles:
        west, south, east, north = bbox
        terrain_tiles = {
            (lat, lon): asset
            for (lat, lon), asset in terrain_tiles.items()
            if lon < east and (lon + 1) > west and lat < north and (lat + 1) > south
        }

    window = grid_window_for_bbox(bbox)
    total_cells = window.rows * window.cols
    n_sub = config.subpixels_per_side or SUBPIXELS_PER_SIDE
    subpixel_size_m = CELL_SIZE_M / float(n_sub)

    to_4326 = pyproj.Transformer.from_crs(GRID_CRS, SOURCE_CRS, always_xy=True)
    land_geom_4326, land_geom_6933, mask_info = _load_land_mask(land_mask_path)

    # Pre-classify all 9 km x 9 km grid cells in EPSG:6933 against the mainland polygon
    cell_x0 = window.west_m + np.arange(window.cols, dtype=np.float64) * CELL_SIZE_M
    cell_y1 = window.north_m - np.arange(window.rows, dtype=np.float64) * CELL_SIZE_M
    if land_geom_6933 is not None:
        import shapely

        xx0, yy1 = np.meshgrid(cell_x0, cell_y1)
        cell_boxes = shapely.box(xx0, yy1 - CELL_SIZE_M, xx0 + CELL_SIZE_M, yy1)
        cell_intersects = shapely.intersects(land_geom_6933, cell_boxes)
        cell_interior = shapely.contains(land_geom_6933, cell_boxes)
    else:
        cell_intersects = np.ones((window.rows, window.cols), dtype=bool)
        cell_interior = np.ones((window.rows, window.cols), dtype=bool)

    shape = (window.rows, window.cols)
    elevation_m = np.full(shape, np.nan, dtype=np.float64)
    flatness_fraction = np.full(shape, np.nan, dtype=np.float64)
    permafrost_fraction = np.full(shape, np.nan, dtype=np.float64)
    land_area_fraction = np.zeros(shape, dtype=np.float64)
    input_coverage = np.zeros(shape, dtype=np.float64)
    eligible = np.zeros(shape, dtype=bool)
    gate_failure = np.full(shape, "no_land", dtype=object)
    score = np.full(shape, np.nan, dtype=np.float64)

    sub_cols_total = window.cols * n_sub
    x_sub = window.west_m + (np.arange(sub_cols_total, dtype=np.float64) + 0.5) * subpixel_size_m
    lon_sub, _ = to_4326.transform(x_sub, np.zeros_like(x_sub))

    # On full continent runs (>1000 tiles), receipts and exact byte sizes are already verified
    # in discover_verified_inputs; keep full SHA-256 verification on permafrost and pilot tiles.
    verify_tile_hashes = config.verify_sha256 and (total_cells <= 50_000)
    tile_cache = _SlidingBandTileCache(
        terrain_tiles,
        dem_agg_factor=config.dem_agg_factor,
        verify_sha256=verify_tile_hashes,
        io_threads=config.io_threads,
    )
    pf_reader = _PermafrostReader(permafrost_asset, verify_sha256=config.verify_sha256)

    try:
        for r0 in range(0, window.rows, config.strip_cell_rows):
            r1 = min(window.rows, r0 + config.strip_cell_rows)
            strip_rows = r1 - r0
            strip_inter = cell_intersects[r0:r1, :]
            if not np.any(strip_inter):
                continue

            sub_rows_total = strip_rows * n_sub
            strip_north_m = window.north_m - r0 * CELL_SIZE_M
            y_sub = (
                strip_north_m
                - (np.arange(sub_rows_total, dtype=np.float64) + 0.5) * subpixel_size_m
            )
            _, lat_sub = to_4326.transform(np.zeros_like(y_sub), y_sub)

            strip_land_blocks = np.zeros((strip_rows, window.cols, n_sub, n_sub), dtype=bool)
            strip_inner = cell_interior[r0:r1, :]
            strip_land_blocks[strip_inner] = True

            strip_boundary = strip_inter & (~strip_inner)
            if land_geom_6933 is not None and np.any(strip_boundary):
                import shapely

                b_rows, b_cols = np.where(strip_boundary)
                x_sub_blocks = x_sub.reshape(window.cols, n_sub)
                y_sub_blocks = y_sub.reshape(strip_rows, n_sub)
                bx = x_sub_blocks[b_cols][:, None, :]
                by = y_sub_blocks[b_rows][:, :, None]
                bx_2d = np.broadcast_to(bx, (b_rows.size, n_sub, n_sub))
                by_2d = np.broadcast_to(by, (b_rows.size, n_sub, n_sub))
                b_inside = shapely.contains_xy(
                    land_geom_6933, bx_2d.ravel(), by_2d.ravel()
                ).reshape(b_rows.size, n_sub, n_sub)
                strip_land_blocks[b_rows, b_cols] = b_inside

            active_cell_cols = np.where(np.any(strip_inter, axis=0))[0]
            c_min_active = int(active_cell_cols.min())
            c_max_active = int(active_cell_cols.max()) + 1
            lon_min_floor = int(math.floor(float(lon_sub[c_min_active * n_sub])))
            lon_max_floor = int(math.floor(float(lon_sub[c_max_active * n_sub - 1])))
            lat_min_floor = int(math.floor(float(lat_sub.min())))
            lat_max_floor = int(math.floor(float(lat_sub.max())))

            tile_cache.evict_above_latitude(lat_max_floor + 1)

            needed_main_tiles: list[tuple[int, int]] = []
            needed_with_halo: set[tuple[int, int]] = set()
            for t_lat in range(lat_min_floor, lat_max_floor + 1):
                for t_lon in range(lon_min_floor, lon_max_floor + 1):
                    key = (t_lat, t_lon)
                    if key not in terrain_tiles:
                        continue
                    if land_geom_4326 is not None:
                        import shapely

                        if not shapely.intersects(
                            land_geom_4326, shapely.box(t_lon, t_lat, t_lon + 1.0, t_lat + 1.0)
                        ):
                            continue
                    needed_main_tiles.append(key)
                    needed_with_halo.update(
                        (
                            key,
                            (t_lat + 1, t_lon),
                            (t_lat - 1, t_lon),
                            (t_lat, t_lon - 1),
                            (t_lat, t_lon + 1),
                        )
                    )

            tile_cache.prefetch_keys(needed_with_halo)

            strip_elev = np.full((sub_rows_total, sub_cols_total), np.nan, dtype=np.float32)
            strip_slope = np.full((sub_rows_total, sub_cols_total), np.nan, dtype=np.float32)

            for t_lat, t_lon in needed_main_tiles:
                lat_mask = (lat_sub >= t_lat) & (lat_sub <= t_lat + 1.0)
                if not np.any(lat_mask):
                    continue
                lon_mask = (lon_sub >= t_lon) & (lon_sub <= t_lon + 1.0)
                if not np.any(lon_mask):
                    continue
                tile_data = tile_cache.get_elevation_and_slope((t_lat, t_lon))
                if tile_data is None:
                    continue
                t_elev, t_slope = tile_data
                t_h, t_w = t_elev.shape
                lat_indices = np.where(lat_mask)[0]
                lon_indices = np.where(lon_mask)[0]

                row_pix = np.clip(
                    np.round((t_lat + 1.0 - lat_sub[lat_indices]) * t_h).astype(np.int64),
                    0,
                    t_h - 1,
                )
                col_pix = np.clip(
                    np.round((lon_sub[lon_indices] - t_lon) * t_w).astype(np.int64),
                    0,
                    t_w - 1,
                )
                grid_ix = np.ix_(lat_indices, lon_indices)
                tile_ix = np.ix_(row_pix, col_pix)
                strip_elev[grid_ix] = t_elev[tile_ix]
                strip_slope[grid_ix] = t_slope[tile_ix]

            strip_pfr = pf_reader.sample_grid(lat_sub, lon_sub)

            def _reshape_blocks(arr: np.ndarray) -> np.ndarray:
                return (
                    arr.reshape(strip_rows, n_sub, window.cols, n_sub)
                    .transpose(0, 2, 1, 3)
                    .reshape(strip_rows, window.cols, n_sub * n_sub)
                )

            strip_metrics = score_cell_subpixels(
                elevation_m=_reshape_blocks(strip_elev),
                slope_deg=_reshape_blocks(strip_slope),
                permafrost_fraction=_reshape_blocks(strip_pfr),
                land_mask=strip_land_blocks.reshape(strip_rows, window.cols, n_sub * n_sub),
                config=config,
            )
            land_area_fraction[r0:r1, :] = strip_metrics["land_area_fraction"]
            elevation_m[r0:r1, :] = strip_metrics["elevation_m"]
            flatness_fraction[r0:r1, :] = strip_metrics["flatness_fraction"]
            permafrost_fraction[r0:r1, :] = strip_metrics["permafrost_fraction"]
            input_coverage[r0:r1, :] = strip_metrics["input_coverage"]
            eligible[r0:r1, :] = strip_metrics["eligible"]
            gate_failure[r0:r1, :] = strip_metrics["gate_failure"]
            score[r0:r1, :] = strip_metrics["score"]
    finally:
        pf_metadata = pf_reader.metadata
        pf_reader.close()

    # Deterministic ranking among eligible cells
    rank = np.zeros(shape, dtype=np.int64)
    eligible_count = int(eligible.sum())
    if eligible_count > 0:
        order = np.lexsort(
            (
                np.arange(eligible.size),
                -np.where(eligible, land_area_fraction, -np.inf).ravel(),
                -np.where(eligible, input_coverage, -np.inf).ravel(),
                -np.where(eligible, score, -np.inf).ravel(),
            )
        )
        rank.flat[order[:eligible_count]] = np.arange(1, eligible_count + 1)

    grid_rows = np.arange(window.row_start, window.row_stop, dtype=np.int64)[:, None]
    grid_cols = np.arange(window.col_start, window.col_stop, dtype=np.int64)[None, :]
    center_x_m = ORIGIN_WEST_M + (grid_cols + 0.5) * CELL_SIZE_M
    center_y_m = ORIGIN_NORTH_M - (grid_rows + 0.5) * CELL_SIZE_M
    center_lon_1d, _ = to_4326.transform(center_x_m.ravel(), np.zeros(window.cols))
    _, center_lat_1d = to_4326.transform(np.zeros(window.rows), center_y_m.ravel())

    is_pilot = land_mask_path is None
    prefix = "pilot" if is_pilot else "earth"
    cells_csv_path = output_dir / f"{prefix}_cells.csv"
    figure_path = output_dir / f"{prefix}_screening.png"
    interactive_path = output_dir / f"{prefix}_interactive.html"
    manifest_path = output_dir / f"{prefix}_manifest.json"

    records: list[dict[str, Any]] = []
    top_candidates: list[dict[str, Any]] = []
    with cells_csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=CELL_COLUMNS)
        writer.writeheader()
        for r in range(window.rows):
            lat_val = round(float(center_lat_1d[r]), 6)
            cy_val = round(float(center_y_m[r, 0]), 2)
            g_row = int(window.row_start + r)
            for c in range(window.cols):
                g_col = int(window.col_start + c)
                cell_id = f"r{g_row:04d}_c{g_col:04d}"
                cell_rank = int(rank[r, c])
                rec = {
                    "rank": cell_rank if cell_rank > 0 else "",
                    "cell_id": cell_id,
                    "grid_row": g_row,
                    "grid_col": g_col,
                    "center_x_m": round(float(center_x_m[0, c]), 2),
                    "center_y_m": cy_val,
                    "center_lat_deg": lat_val,
                    "center_lon_deg": round(float(center_lon_1d[c]), 6),
                    "land_area_fraction": round(float(land_area_fraction[r, c]), 4),
                    "elevation_m": (
                        round(float(elevation_m[r, c]), 2)
                        if np.isfinite(elevation_m[r, c])
                        else ""
                    ),
                    "flatness_fraction": (
                        round(float(flatness_fraction[r, c]), 4)
                        if np.isfinite(flatness_fraction[r, c])
                        else ""
                    ),
                    "permafrost_fraction": (
                        round(float(permafrost_fraction[r, c]), 4)
                        if np.isfinite(permafrost_fraction[r, c])
                        else ""
                    ),
                    "input_coverage": round(float(input_coverage[r, c]), 4),
                    "eligible": bool(eligible[r, c]),
                    "gate_failure": str(gate_failure[r, c]),
                    "score": (
                        round(float(score[r, c]), 4) if np.isfinite(score[r, c]) else ""
                    ),
                }
                writer.writerow(rec)
                if 1 <= cell_rank <= 50:
                    top_candidates.append(rec)
                if total_cells <= 10_000:
                    records.append(rec)

    top_candidates.sort(key=lambda r: int(r["rank"]))

    results_arrays = {
        "land_area_fraction": land_area_fraction,
        "elevation_m": elevation_m,
        "flatness_fraction": flatness_fraction,
        "permafrost_fraction": permafrost_fraction,
        "input_coverage": input_coverage,
        "eligible": eligible,
        "score": score,
        "rank": rank,
    }
    plot_five_panel_report(
        figure_path,
        window=window,
        results=results_arrays,
        config=config,
        bbox=bbox,
        is_pilot=is_pilot,
        land_geom_6933=land_geom_6933,
    )
    write_interactive_html(
        interactive_path,
        window=window,
        results=results_arrays,
        config=config,
        bbox=bbox,
        is_pilot=is_pilot,
        top_candidates=top_candidates,
    )

    used_terrain_records = [
        {
            "filename": asset.filename,
            "url": asset.url,
            "product": asset.product,
            "sha256": asset.sha256,
            "actual_bytes": asset.actual_bytes,
            "downloaded_utc": asset.downloaded_utc,
        }
        for _, asset in sorted(tile_cache.used_assets.items())
    ]

    land_cells_count = int((land_area_fraction > 0).sum())
    manifest = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "run_mode": "pilot" if is_pilot else "study",
        "pilot_warning": (
            "PILOT RESULT: Run on a test extent without a final continental polygon mask. "
            "Permafrost fraction is a modeled frozen-ground proxy and does not establish "
            "ground-ice abundance or landing safety."
            if is_pilot
            else "STUDY RUN: Mainland North America (Canada, Alaska, contiguous USA, Mexico; "
            "excluding Greenland and Canadian Arctic islands). Permafrost fraction is a modeled "
            "frozen-ground proxy and does not establish ground-ice abundance or landing safety."
        ),
        "grid": {
            "crs": GRID_CRS,
            "cell_size_m": CELL_SIZE_M,
            "subpixels_per_side": n_sub,
            "subpixel_size_m": subpixel_size_m,
            "origin_west_m": ORIGIN_WEST_M,
            "origin_north_m": ORIGIN_NORTH_M,
            "study_envelope_bbox_deg": list(STUDY_BBOX),
            "requested_bbox_deg": list(bbox),
            "window_row_start": window.row_start,
            "window_row_stop": window.row_stop,
            "window_col_start": window.col_start,
            "window_col_stop": window.col_stop,
            "window_bounds_epsg6933_m": [
                window.west_m,
                window.south_m,
                window.east_m,
                window.north_m,
            ],
        },
        "mask": mask_info,
        "scoring_assumptions": {
            "status": "provisional_earth_baseline_working_assumptions",
            **asdict(config),
        },
        "summary": {
            "cells_total": total_cells,
            "cells_with_land": land_cells_count,
            "cells_outside_mask": int(total_cells - land_cells_count),
            "cells_eligible": eligible_count,
            "cells_rejected": int(total_cells - eligible_count),
            "land_cells_rejected_low_coverage": int(
                ((land_area_fraction > 0) & (input_coverage < config.min_coverage)).sum()
            ),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        },
        "top_10_candidates": top_candidates[:10],
        "verified_source_metadata": {
            "terrain_sample": tile_cache.sample_metadata,
            "permafrost": pf_metadata,
        },
        "inputs": {
            "inventory_path": str(inventory_path),
            "permafrost": {
                "filename": permafrost_asset.filename,
                "url": permafrost_asset.url,
                "product": permafrost_asset.product,
                "year": permafrost_asset.year,
                "sha256": permafrost_asset.sha256,
                "actual_bytes": permafrost_asset.actual_bytes,
                "downloaded_utc": permafrost_asset.downloaded_utc,
            },
            "terrain_tiles_used_count": len(used_terrain_records),
            "terrain_tiles_used": used_terrain_records,
        },
        "outputs": {
            "cells_csv": str(cells_csv_path),
            "screening_png": str(figure_path),
            "interactive_html": str(interactive_path),
            "manifest_json": str(manifest_path),
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    return {
        "cells_csv": cells_csv_path,
        "screening_png": figure_path,
        "interactive_html": interactive_path,
        "manifest_json": manifest_path,
        "manifest": manifest,
        "records": records,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory",
        type=Path,
        default=Path("data/earth/inventory.json"),
        help="Path to the discovery inventory JSON",
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=Path("data/earth/raw"),
        help="Directory containing downloaded dataset folders and receipts",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to write processed table, five-panel map, interactive HTML, and manifest",
    )
    parser.add_argument(
        "--study",
        action="store_true",
        help="Run the full mainland North America study region with the versioned mainland mask",
    )
    parser.add_argument(
        "--bbox",
        type=float,
        nargs=4,
        default=None,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help="Target geographic extent in degrees (defaults to pilot bbox, or study bbox with --study)",
    )
    parser.add_argument(
        "--land-mask",
        type=Path,
        default=None,
        help="Optional mainland study polygon file (GeoJSON/GPKG/Shapefile)",
    )
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=0.80,
        help="Minimum joint valid land coverage fraction (default: 0.80)",
    )
    parser.add_argument(
        "--flat-slope-deg",
        type=float,
        default=5.0,
        help="Maximum slope in degrees to count as flat terrain (default: 5.0)",
    )
    parser.add_argument(
        "--flatness-weight",
        type=float,
        default=0.50,
        help="Weight for flatness fraction in provisional Earth score (default: 0.50)",
    )
    parser.add_argument(
        "--permafrost-weight",
        type=float,
        default=0.50,
        help="Weight for permafrost fraction in provisional Earth score (default: 0.50)",
    )
    parser.add_argument(
        "--subpixels-per-side",
        type=int,
        default=None,
        help="Equal-area sub-pixels per cell side (default: 100 for pilot, 20 for full study)",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=240,
        help="Output figure resolution in DPI (default: 240)",
    )
    parser.add_argument(
        "--strict-bbox-tiles",
        action="store_true",
        help="Only load terrain tiles whose 1x1 deg footprint intersects --bbox",
    )
    args = parser.parse_args(argv)

    if args.study:
        bbox = tuple(args.bbox) if args.bbox is not None else STUDY_BBOX
        land_mask = args.land_mask if args.land_mask is not None else DEFAULT_MAINLAND_MASK
        output_dir = args.output_dir if args.output_dir is not None else Path("data/earth/processed/north_america")
    else:
        bbox = tuple(args.bbox) if args.bbox is not None else DEFAULT_PILOT_BBOX
        land_mask = args.land_mask
        output_dir = args.output_dir if args.output_dir is not None else Path("data/earth/processed/pilot")

    config = EarthScreeningConfig(
        flat_slope_deg=args.flat_slope_deg,
        min_coverage=args.min_coverage,
        flatness_weight=args.flatness_weight,
        permafrost_weight=args.permafrost_weight,
        subpixels_per_side=args.subpixels_per_side,
        figure_dpi=args.dpi,
    )
    result = run_earth_screening(
        inventory_path=args.inventory,
        raw_dir=args.raw_dir,
        output_dir=output_dir,
        bbox=bbox,
        land_mask_path=land_mask,
        config=config,
        strict_bbox_tiles=args.strict_bbox_tiles,
    )
    summary = result["manifest"]["summary"]
    print(
        f"Processed {summary['cells_total']:,} grid cells ({summary['cells_with_land']:,} mainland cells) "
        f"in {summary['elapsed_seconds']:.2f}s: "
        f"{summary['cells_eligible']:,} eligible, {summary['land_cells_rejected_low_coverage']:,} land cells rejected "
        f"(< {config.min_coverage:.0%} coverage)."
    )
    print(f"Wrote cell table: {result['cells_csv']}")
    print(f"Wrote high-res five-panel map: {result['screening_png']}")
    print(f"Wrote interactive HTML map: {result['interactive_html']}")
    print(f"Wrote provenance manifest: {result['manifest_json']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
