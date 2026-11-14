"""Offline Earth analog terrain and permafrost screening on the EPSG:6933 9 km grid.

Implements the Arcadia Outpost Earth baseline pilot and regional screening workflow
described in EARTH_MAP_HANDOFF.md and EARTH_DATA.md.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import json
import math
import os
import re
import time
import warnings
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
SUBPIXELS_PER_SIDE = 100
SUBPIXEL_SIZE_M = CELL_SIZE_M / SUBPIXELS_PER_SIDE  # 90.0 m equal-area sub-pixels
ORIGIN_WEST_M = -16_407_000.0
ORIGIN_NORTH_M = 6_984_000.0
STUDY_BBOX = (-170.0, 14.0, -52.0, 72.0)
DEFAULT_PILOT_BBOX = (-149.0, 62.0, -146.0, 65.0)

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
    verify_sha256: bool = True

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

    def read_geotiff(self, path: Path) -> tuple[FloatArray, dict[str, Any]]:
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
            values = buffer.astype(np.float64)
            if has_nodata.value:
                values[np.isclose(values, float(nodata_val))] = np.nan
            metadata = {
                "width": width,
                "height": height,
                "geotransform": tuple(float(x) for x in gt),
                "crs_wkt": proj_wkt,
                "area_or_point": area_or_point,
                "has_nodata": bool(has_nodata.value),
                "nodata": float(nodata_val) if has_nodata.value else None,
            }
            return values, metadata
        finally:
            self.gdal.GDALClose(ds)


_GDAL_READER: _GDALReader | None = None


def get_gdal_reader() -> _GDALReader:
    global _GDAL_READER
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


def compute_tile_slope_degrees(
    elevation_m: FloatArray,
    *,
    south_lat_deg: float,
    west_lon_deg: float,
    north_row: FloatArray | None = None,
    south_row: FloatArray | None = None,
    west_col: FloatArray | None = None,
    east_col: FloatArray | None = None,
) -> FloatArray:
    """Compute slope in degrees on a 1x1 deg Copernicus DEM tile with 1-pixel neighbor halo.

    If a neighbor strip is provided, it supplies the 1-pixel halo across that tile seam
    (interpolating along longitude if the northern or southern neighbor has a different
    column count across a latitude band boundary). If a neighbor strip is None, that halo
    edge is filled with NaN so unresolved tile boundaries are marked invalid (NaN).
    """
    if elevation_m.ndim != 2:
        raise ValueError("elevation_m must be a 2-D array")
    height, width = elevation_m.shape
    dlat_deg = 1.0 / height
    dlon_deg = 1.0 / width

    # With AREA_OR_POINT=Point and tiepoint at (west_lon_deg, south_lat_deg + 1.0):
    # row i has center latitude (south_lat_deg + 1.0) - i * dlat_deg
    # col j has center longitude west_lon_deg + j * dlon_deg
    row_lats = (south_lat_deg + 1.0) - np.arange(height, dtype=np.float64) * dlat_deg
    col_lons = west_lon_deg + np.arange(width, dtype=np.float64) * dlon_deg

    padded = np.full((height + 2, width + 2), np.nan, dtype=np.float64)
    padded[1:-1, 1:-1] = elevation_m

    def _align_row(row_values: FloatArray) -> FloatArray:
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
    # Row index increases southward, so north minus south is padded[:-2] - padded[2:]
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
    elevation_m: FloatArray,
    slope_deg: FloatArray,
    permafrost_fraction: FloatArray,
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

    elev_valid = np.where(joint_valid, elevation_m, np.nan)
    pfr_valid = np.where(joint_valid, permafrost_fraction, np.nan)
    flat_valid = joint_valid & (slope_deg <= config.flat_slope_deg)

    with warnings.catch_warnings(), np.errstate(divide="ignore", invalid="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        elev_median = np.nanmedian(elev_valid, axis=-1)
        flatness = np.where(valid_count > 0, flat_valid.sum(axis=-1) / valid_count, np.nan)
        pfr_mean = np.nanmean(pfr_valid, axis=-1)

    eligible = (land_count > 0) & (input_coverage >= config.min_coverage)
    eligible &= np.isfinite(flatness) & np.isfinite(pfr_mean)

    raw_score = (
        config.flatness_weight * flatness + config.permafrost_weight * pfr_mean
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


class _TileCache:
    """Bounded LRU cache for raw elevation arrays and edge strips."""

    def __init__(
        self,
        tiles: dict[tuple[int, int], VerifiedAsset],
        *,
        verify_sha256: bool = True,
        max_cached_tiles: int = 24,
    ) -> None:
        self.tiles = tiles
        self.verify_sha256 = verify_sha256
        self.max_cached_tiles = max_cached_tiles
        self._reader = get_gdal_reader()
        self._cache: dict[tuple[int, int], tuple[FloatArray, dict[str, Any]]] = {}
        self._order: list[tuple[int, int]] = []
        self.used_assets: dict[tuple[int, int], VerifiedAsset] = {}
        self.sample_metadata: dict[str, Any] | None = None

    def get_elevation(self, key: tuple[int, int]) -> FloatArray | None:
        if key not in self.tiles:
            return None
        if key in self._cache:
            self._order.remove(key)
            self._order.append(key)
            return self._cache[key][0]
        asset = self.tiles[key]
        if self.verify_sha256 and key not in self.used_assets:
            actual_hash = file_sha256(asset.path)
            if actual_hash != asset.sha256:
                raise ValueError(
                    f"SHA-256 mismatch for {asset.path}: expected {asset.sha256}, got {actual_hash}"
                )
        values, meta = self._reader.read_geotiff(asset.path)
        self.used_assets[key] = asset
        if self.sample_metadata is None:
            self.sample_metadata = {"filename": asset.filename, **meta}
        self._cache[key] = (values, meta)
        self._order.append(key)
        while len(self._order) > self.max_cached_tiles:
            oldest = self._order.pop(0)
            self._cache.pop(oldest, None)
        return values

    def get_elevation_and_slope(self, key: tuple[int, int]) -> tuple[FloatArray, FloatArray] | None:
        elev = self.get_elevation(key)
        if elev is None:
            return None
        lat_deg, lon_deg = key
        north_elev = self.get_elevation((lat_deg + 1, lon_deg))
        south_elev = self.get_elevation((lat_deg - 1, lon_deg))
        west_elev = self.get_elevation((lat_deg, lon_deg - 1))
        east_elev = self.get_elevation((lat_deg, lon_deg + 1))

        slope = compute_tile_slope_degrees(
            elev,
            south_lat_deg=float(lat_deg),
            west_lon_deg=float(lon_deg),
            north_row=north_elev[-1, :] if north_elev is not None else None,
            south_row=south_elev[0, :] if south_elev is not None else None,
            west_col=west_elev[:, -1] if west_elev is not None else None,
            east_col=east_elev[:, 0] if east_elev is not None else None,
        )
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

    def sample_grid(self, lat_vec: FloatArray, lon_vec: FloatArray) -> FloatArray:
        """Sample permafrost fraction (0..1) on a 2D cartesian grid (lat_vec[:, None], lon_vec[None, :])."""
        out = np.full((lat_vec.size, lon_vec.size), np.nan, dtype=np.float64)
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
        decoded_window = decode_permafrost_fraction(raw_window)

        lat_pos = np.where(valid_lat)[0]
        lon_pos = np.where(valid_lon)[0]
        out[np.ix_(lat_pos, lon_pos)] = decoded_window[
            np.ix_(r_idx_all[lat_pos] - r_min, c_idx_all[lon_pos] - c_min)
        ]
        return out

    def close(self) -> None:
        self.file.close()


def _load_optional_land_mask(
    land_mask_path: Path | None,
    bbox_4326: tuple[float, float, float, float],
):
    if land_mask_path is None:
        return None
    import shapely

    df = pyogrio.read_dataframe(land_mask_path, bbox=bbox_4326)
    if df.empty:
        return shapely.GeometryCollection()
    if df.crs and df.crs.to_epsg() != 4326:
        df = df.to_crs("EPSG:4326")
    return shapely.union_all(df.geometry.values)


def plot_five_panel_report(
    output_path: Path,
    *,
    window: GridWindow,
    results: dict[str, np.ndarray],
    config: EarthScreeningConfig,
    bbox: tuple[float, float, float, float],
    is_pilot: bool,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    extent_km = (
        window.west_m / 1000.0,
        window.east_m / 1000.0,
        window.south_m / 1000.0,
        window.north_m / 1000.0,
    )

    panels = (
        ("1. Median Elevation (m)", results["elevation_m"], "terrain", None, None),
        (
            f"2. Flatness Fraction (slope <= {config.flat_slope_deg:g} deg)",
            results["flatness_fraction"],
            "YlGnBu",
            0.0,
            1.0,
        ),
        (
            "3. Permafrost Fraction (CCI v5, 2023)",
            results["permafrost_fraction"],
            "PuBu",
            0.0,
            1.0,
        ),
        (
            f"4. Joint Valid Coverage (gate >= {config.min_coverage:.0%})",
            results["input_coverage"],
            "cividis",
            0.0,
            1.0,
        ),
        (
            "5. Earth Analog Score (gated)",
            results["score"],
            "viridis",
            0.0,
            1.0,
        ),
    )

    fig, axes = plt.subplots(1, 5, figsize=(21, 4.3), constrained_layout=True, sharey=True)
    rejected_mask = ~results["eligible"]

    for idx, (ax, (title, data, cmap_name, vmin, vmax)) in enumerate(zip(axes, panels)):
        cmap = plt.get_cmap(cmap_name).copy()
        cmap.set_bad("#d9d9d9")
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
        rows, cols = rejected_mask.shape
        y_centers = np.linspace(extent_km[3] - 4.5, extent_km[2] + 4.5, rows)
        x_centers = np.linspace(extent_km[0] + 4.5, extent_km[1] - 4.5, cols)

        # Hatch rejected / ineligible cells on the final score panel so they are unambiguous
        if idx == 4 and np.any(rejected_mask):
            r_idx, c_idx = np.where(rejected_mask)
            ax.scatter(
                x_centers[c_idx],
                y_centers[r_idx],
                marker="x",
                s=18,
                c="#aa2222",
                linewidths=0.9,
                alpha=0.85,
                label="Rejected (<80% coverage)",
            )
        if idx == 4 and "rank" in results:
            top_r, top_c = np.where((results["rank"] >= 1) & (results["rank"] <= 5))
            if top_r.size > 0:
                ax.scatter(
                    x_centers[top_c],
                    y_centers[top_r],
                    marker="o",
                    s=38,
                    facecolors="none",
                    edgecolors="white",
                    linewidths=1.4,
                    label="Top 5 ranked cells",
                )
        if idx == 4 and (np.any(rejected_mask) or "rank" in results):
            ax.legend(loc="lower right", fontsize=7.5, framealpha=0.9)

        ax.set_title(title, fontsize=10, fontweight="bold")
        ax.set_xlabel("EPSG:6933 Easting (km)", fontsize=9)
        if idx == 0:
            ax.set_ylabel("EPSG:6933 Northing (km)", fontsize=9)
        cbar = fig.colorbar(im, ax=ax, orientation="horizontal", shrink=0.82, pad=0.08)
        cbar.ax.tick_params(labelsize=8)

    status_tag = "PILOT RUN (unmasked DEM envelope)" if is_pilot else "STUDY RUN (masked)"
    west, south, east, north = bbox
    eligible_count = int(results["eligible"].sum())
    total_count = int(results["eligible"].size)
    fig.suptitle(
        f"Arcadia Outpost Earth Analog Screening — {status_tag}\n"
        f"Extent: [{west:.2f}°E, {south:.2f}°N, {east:.2f}°E, {north:.2f}°N] | "
        f"Grid: EPSG:6933 (9 km × 9 km) | "
        f"Eligible cells: {eligible_count}/{total_count} (coverage >= {config.min_coverage:.0%}, "
        f"weights: {config.flatness_weight:.0%} flatness + {config.permafrost_weight:.0%} permafrost)",
        fontsize=11.5,
        fontweight="bold",
    )
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


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
    to_4326 = pyproj.Transformer.from_crs(GRID_CRS, SOURCE_CRS, always_xy=True)

    # Full window cell arrays
    shape = (window.rows, window.cols)
    elevation_m = np.full(shape, np.nan, dtype=np.float64)
    flatness_fraction = np.full(shape, np.nan, dtype=np.float64)
    permafrost_fraction = np.full(shape, np.nan, dtype=np.float64)
    land_area_fraction = np.zeros(shape, dtype=np.float64)
    input_coverage = np.zeros(shape, dtype=np.float64)
    eligible = np.zeros(shape, dtype=bool)
    gate_failure = np.full(shape, "", dtype=object)
    score = np.full(shape, np.nan, dtype=np.float64)

    # Sub-pixel X centers in EPSG:6933 (1D across the window columns)
    sub_cols_total = window.cols * SUBPIXELS_PER_SIDE
    x_sub = window.west_m + (np.arange(sub_cols_total, dtype=np.float64) + 0.5) * SUBPIXEL_SIZE_M
    lon_sub, _ = to_4326.transform(x_sub, np.zeros_like(x_sub))

    tile_cache = _TileCache(terrain_tiles, verify_sha256=config.verify_sha256)
    pf_reader = _PermafrostReader(permafrost_asset, verify_sha256=config.verify_sha256)

    # Determine expanded geographic bounds of the grid window for optional vector mask
    _, win_south_deg = to_4326.transform(window.west_m, window.south_m)
    _, win_north_deg = to_4326.transform(window.east_m, window.north_m)
    land_geom = _load_optional_land_mask(
        land_mask_path,
        (float(lon_sub.min()), float(win_south_deg), float(lon_sub.max()), float(win_north_deg)),
    )

    try:
        for r0 in range(0, window.rows, config.strip_cell_rows):
            r1 = min(window.rows, r0 + config.strip_cell_rows)
            strip_rows = r1 - r0
            sub_rows_total = strip_rows * SUBPIXELS_PER_SIDE

            strip_north_m = window.north_m - r0 * CELL_SIZE_M
            y_sub = (
                strip_north_m
                - (np.arange(sub_rows_total, dtype=np.float64) + 0.5) * SUBPIXEL_SIZE_M
            )
            _, lat_sub = to_4326.transform(np.zeros_like(y_sub), y_sub)

            strip_elev = np.full((sub_rows_total, sub_cols_total), np.nan, dtype=np.float64)
            strip_slope = np.full((sub_rows_total, sub_cols_total), np.nan, dtype=np.float64)

            lat_min_floor = int(math.floor(float(lat_sub.min())))
            lat_max_floor = int(math.floor(float(lat_sub.max())))
            lon_min_floor = int(math.floor(float(lon_sub.min())))
            lon_max_floor = int(math.floor(float(lon_sub.max())))

            for t_lat in range(lat_min_floor, lat_max_floor + 1):
                lat_mask = (lat_sub >= t_lat) & (lat_sub <= t_lat + 1.0)
                if not np.any(lat_mask):
                    continue
                lat_indices = np.where(lat_mask)[0]
                for t_lon in range(lon_min_floor, lon_max_floor + 1):
                    lon_mask = (lon_sub >= t_lon) & (lon_sub <= t_lon + 1.0)
                    if not np.any(lon_mask):
                        continue
                    tile_data = tile_cache.get_elevation_and_slope((t_lat, t_lon))
                    if tile_data is None:
                        continue
                    t_elev, t_slope = tile_data
                    t_h, t_w = t_elev.shape
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

            if land_geom is not None:
                import shapely

                lon_2d, lat_2d = np.meshgrid(lon_sub, lat_sub)
                strip_land = shapely.contains_xy(land_geom, lon_2d, lat_2d)
            else:
                # In pilot mode without an external continental polygon mask, treat the
                # evaluated grid cells as candidate land area so missing tiles/permafrost
                # directly reduce input_coverage.
                strip_land = np.ones((sub_rows_total, sub_cols_total), dtype=bool)

            def _reshape_blocks(arr: np.ndarray) -> np.ndarray:
                return (
                    arr.reshape(
                        strip_rows,
                        SUBPIXELS_PER_SIDE,
                        window.cols,
                        SUBPIXELS_PER_SIDE,
                    )
                    .transpose(0, 2, 1, 3)
                    .reshape(strip_rows, window.cols, SUBPIXELS_PER_SIDE * SUBPIXELS_PER_SIDE)
                )

            strip_metrics = score_cell_subpixels(
                elevation_m=_reshape_blocks(strip_elev),
                slope_deg=_reshape_blocks(strip_slope),
                permafrost_fraction=_reshape_blocks(strip_pfr),
                land_mask=_reshape_blocks(strip_land),
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

    # Deterministic ranking among eligible cells (ties broken by higher coverage, then cell_id)
    rank = np.zeros(shape, dtype=np.int64)
    eligible_count = int(eligible.sum())
    if eligible_count > 0:
        order = np.lexsort(
            (
                np.arange(eligible.size),
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

    records: list[dict[str, Any]] = []
    for r in range(window.rows):
        for c in range(window.cols):
            g_row = int(window.row_start + r)
            g_col = int(window.col_start + c)
            cell_id = f"r{g_row:04d}_c{g_col:04d}"
            cell_rank = int(rank[r, c])
            records.append(
                {
                    "rank": cell_rank if cell_rank > 0 else "",
                    "cell_id": cell_id,
                    "grid_row": g_row,
                    "grid_col": g_col,
                    "center_x_m": round(float(center_x_m[0, c]), 2),
                    "center_y_m": round(float(center_y_m[r, 0]), 2),
                    "center_lat_deg": round(float(center_lat_1d[r]), 6),
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
            )

    is_pilot = land_mask_path is None
    prefix = "pilot" if is_pilot else "earth"
    cells_csv_path = output_dir / f"{prefix}_cells.csv"
    figure_path = output_dir / f"{prefix}_screening.png"
    manifest_path = output_dir / f"{prefix}_manifest.json"

    with cells_csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=CELL_COLUMNS)
        writer.writeheader()
        writer.writerows(records)

    results_arrays = {
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

    manifest = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "run_mode": "pilot" if is_pilot else "study",
        "pilot_warning": (
            "PILOT RESULT: Run on a test extent without a final continental polygon mask. "
            "Permafrost fraction is a modeled frozen-ground proxy and does not establish "
            "ground-ice abundance or landing safety."
            if is_pilot
            else "Permafrost fraction is a modeled frozen-ground proxy and does not establish "
            "ground-ice abundance or landing safety."
        ),
        "grid": {
            "crs": GRID_CRS,
            "cell_size_m": CELL_SIZE_M,
            "subpixel_size_m": SUBPIXEL_SIZE_M,
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
        "mask": {
            "source": str(land_mask_path) if land_mask_path else "unmasked_dem_envelope (pilot)",
            "version": "pilot-unmasked-v1" if is_pilot else "custom",
        },
        "scoring_assumptions": {
            "status": "provisional_earth_baseline_working_assumptions",
            **asdict(config),
        },
        "summary": {
            "cells_total": int(window.rows * window.cols),
            "cells_eligible": eligible_count,
            "cells_rejected": int(window.rows * window.cols - eligible_count),
            "missing_coverage_cells": int((input_coverage < config.min_coverage).sum()),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        },
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
            "manifest_json": str(manifest_path),
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    return {
        "cells_csv": cells_csv_path,
        "screening_png": figure_path,
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
        default=Path("data/earth/processed/pilot"),
        help="Directory to write processed table, five-panel map, and manifest",
    )
    parser.add_argument(
        "--bbox",
        type=float,
        nargs=4,
        default=list(DEFAULT_PILOT_BBOX),
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help="Target geographic extent in degrees (default: interior Alaska pilot -149 62 -146 65)",
    )
    parser.add_argument(
        "--land-mask",
        type=Path,
        default=None,
        help="Optional mainland study polygon file (GeoJSON/GPKG/Shapefile) for final run",
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
        "--strict-bbox-tiles",
        action="store_true",
        help="Only load terrain tiles whose 1x1 deg footprint intersects --bbox",
    )
    args = parser.parse_args(argv)

    config = EarthScreeningConfig(
        flat_slope_deg=args.flat_slope_deg,
        min_coverage=args.min_coverage,
        flatness_weight=args.flatness_weight,
        permafrost_weight=args.permafrost_weight,
    )
    result = run_earth_screening(
        inventory_path=args.inventory,
        raw_dir=args.raw_dir,
        output_dir=args.output_dir,
        bbox=tuple(args.bbox),
        land_mask_path=args.land_mask,
        config=config,
        strict_bbox_tiles=args.strict_bbox_tiles,
    )
    summary = result["manifest"]["summary"]
    print(
        f"Processed {summary['cells_total']} cells in {summary['elapsed_seconds']:.2f}s: "
        f"{summary['cells_eligible']} eligible, {summary['cells_rejected']} rejected "
        f"({summary['missing_coverage_cells']} below {config.min_coverage:.0%} coverage)."
    )
    print(f"Wrote cell table: {result['cells_csv']}")
    print(f"Wrote five-panel map: {result['screening_png']}")
    print(f"Wrote provenance manifest: {result['manifest_json']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
