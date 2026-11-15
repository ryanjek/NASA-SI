"""Align Phase 1 Mars rasters onto one planetocentric longitude/latitude grid.

Every source used here is a spherical equidistant-cylindrical (simple cylindrical)
raster with its standard parallel on the equator, so it is linear in longitude and
latitude. Converting each affine transform to degrees lets all layers share one grid
without a datum transformation between the slightly different Mars spheres.
Longitudes are degrees east in the 0-360 convention used by MOLA and most Mars maps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from affine import Affine
from numpy.typing import NDArray
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject
from rasterio.windows import Window

from .raster import DCI_TRANSFORM, dci_valid_mask, read_dci_pds

FloatArray = NDArray[np.float64]

MARS_GEOGRAPHIC = CRS.from_wkt(
    'GEOGCS["GCS_Mars_2000_Sphere",'
    'DATUM["Mars_2000_(Sphere)",SPHEROID["Mars_2000_Sphere_IAU_IAG",3396190,0]],'
    'PRIMEM["Reference_Meridian",0],'
    'UNIT["degree",0.0174532925199433]]'
)
MOLA_PIXELS_PER_DEGREE = 64
_TOLERANCE_DEG = 1e-6


@dataclass(frozen=True)
class StudyGrid:
    west: float
    north: float
    pixel_deg: float
    width: int
    height: int

    @property
    def east(self) -> float:
        return self.west + self.width * self.pixel_deg

    @property
    def south(self) -> float:
        return self.north - self.height * self.pixel_deg

    @property
    def transform(self) -> Affine:
        return from_origin(self.west, self.north, self.pixel_deg, self.pixel_deg)

    def row_latitudes(self) -> FloatArray:
        return self.north - (np.arange(self.height) + 0.5) * self.pixel_deg

    def col_longitudes(self) -> FloatArray:
        return self.west + (np.arange(self.width) + 0.5) * self.pixel_deg


def study_grid(
    *,
    west: float,
    east: float,
    south: float,
    north: float,
    cell_pixels: int,
    pixels_per_degree: int = MOLA_PIXELS_PER_DEGREE,
) -> StudyGrid:
    """Snap bounds inward to the MOLA pixel grid and trim to whole candidate cells."""
    if cell_pixels < 1:
        raise ValueError("cell_pixels must be at least 1")
    first_col = math.ceil(west * pixels_per_degree - _TOLERANCE_DEG)
    last_col = math.floor(east * pixels_per_degree + _TOLERANCE_DEG)
    first_row = math.ceil((90.0 - north) * pixels_per_degree - _TOLERANCE_DEG)
    last_row = math.floor((90.0 - south) * pixels_per_degree + _TOLERANCE_DEG)
    width = (last_col - first_col) // cell_pixels * cell_pixels
    height = (last_row - first_row) // cell_pixels * cell_pixels
    if width <= 0 or height <= 0:
        raise ValueError("Study bounds are smaller than one candidate cell")
    pixel_deg = 1.0 / pixels_per_degree
    return StudyGrid(
        west=first_col * pixel_deg,
        north=90.0 - first_row * pixel_deg,
        pixel_deg=pixel_deg,
        width=width,
        height=height,
    )


def degree_transform(transform: Affine, crs: CRS) -> tuple[Affine, float]:
    """Convert a simple-cylindrical metre transform to degrees east (0-360).

    Returns the degree transform and the sphere radius in metres.
    """
    params = crs.to_dict()
    if params.get("proj") != "eqc" or "R" not in params:
        raise ValueError(f"Expected a spherical equidistant-cylindrical CRS, got {params}")
    if params.get("lat_ts", 0) != 0:
        raise ValueError("Only equatorial standard parallels are linear in longitude")
    if transform.b or transform.d:
        raise ValueError("Rotated rasters are not supported")
    radius = float(params["R"])
    deg_per_m = 180.0 / (math.pi * radius)
    west = params.get("lon_0", 0.0) + (transform.c - params.get("x_0", 0.0)) * deg_per_m
    north = params.get("lat_0", 0.0) + (transform.f - params.get("y_0", 0.0)) * deg_per_m
    return (
        Affine(transform.a * deg_per_m, 0.0, west % 360.0, 0.0, transform.e * deg_per_m, north),
        radius,
    )


def raster_bounds_deg(path: Path) -> tuple[float, float, float, float]:
    """Return (west, east, south, north) in degrees east (0-360)."""
    with rasterio.open(path) as source:
        transform, _ = degree_transform(source.transform, source.crs)
        return (
            transform.c,
            transform.c + source.width * transform.a,
            transform.f + source.height * transform.e,
            transform.f,
        )


def resample_to_grid(
    values: FloatArray,
    source_transform: Affine,
    grid: StudyGrid,
    resampling: Resampling = Resampling.bilinear,
) -> FloatArray:
    """Resample a degree-gridded array onto the study grid; NaN marks missing data."""
    destination = np.full((grid.height, grid.width), np.nan, dtype=np.float64)
    reproject(
        np.asarray(values, dtype=np.float64),
        destination,
        src_transform=source_transform,
        src_crs=MARS_GEOGRAPHIC,
        src_nodata=np.nan,
        dst_transform=grid.transform,
        dst_crs=MARS_GEOGRAPHIC,
        dst_nodata=np.nan,
        resampling=resampling,
    )
    return destination


def load_mola(labels: list[Path], grid: StudyGrid) -> tuple[FloatArray, float]:
    """Mosaic native MOLA MEGDR elevation (metres) onto the study grid.

    Returns the elevation array and the MOLA sphere's metres per degree.
    """
    elevation = np.full((grid.height, grid.width), np.nan, dtype=np.float64)
    grid_col0 = round(grid.west / grid.pixel_deg)
    grid_row0 = round((90.0 - grid.north) / grid.pixel_deg)
    metres_per_degree = math.nan
    for label in labels:
        with rasterio.open(label) as source:
            transform, radius = degree_transform(source.transform, source.crs)
            if not math.isclose(transform.a, grid.pixel_deg, rel_tol=1e-9):
                raise ValueError(f"{label} is not on the {grid.pixel_deg} degree MOLA grid")
            metres_per_degree = math.pi * radius / 180.0
            src_col0 = round(transform.c / grid.pixel_deg)
            src_row0 = round((90.0 - transform.f) / grid.pixel_deg)
            col_start = max(grid_col0, src_col0)
            col_stop = min(grid_col0 + grid.width, src_col0 + source.width)
            row_start = max(grid_row0, src_row0)
            row_stop = min(grid_row0 + grid.height, src_row0 + source.height)
            if col_start >= col_stop or row_start >= row_stop:
                continue
            window = Window(
                col_start - src_col0,
                row_start - src_row0,
                col_stop - col_start,
                row_stop - row_start,
            )
            elevation[
                row_start - grid_row0 : row_stop - grid_row0,
                col_start - grid_col0 : col_stop - grid_col0,
            ] = source.read(1, window=window).astype(np.float64)
    if np.isnan(elevation).any():
        raise ValueError("MOLA tiles do not cover the full study grid; download both tiles")
    return elevation, metres_per_degree


def load_swim(path: Path, grid: StudyGrid) -> FloatArray:
    """Resample SWIM ice consistency (-1 inconsistent .. +1 consistent with ice)."""
    with rasterio.open(path) as source:
        transform, _ = degree_transform(source.transform, source.crs)
        values = source.read(1, masked=True).astype(np.float64).filled(np.nan)
    return resample_to_grid(values, transform, grid)


def load_dci(path: Path, grid: StudyGrid) -> FloatArray:
    """Resample the TES Dust Cover Index after masking fill and moving to 0-360 east."""
    values = np.array(read_dci_pds(path), dtype=np.float64)
    values[~dci_valid_mask(values)] = np.nan
    shift = round(-DCI_TRANSFORM.c / DCI_TRANSFORM.a)
    values = np.roll(values, -shift, axis=1)
    transform = from_origin(0.0, DCI_TRANSFORM.f, DCI_TRANSFORM.a, -DCI_TRANSFORM.e)
    return resample_to_grid(values, transform, grid)


def cell_blocks(values: np.ndarray, cell_pixels: int) -> np.ndarray:
    """Reshape (rows, cols) into (cell_rows, cell_cols, pixels_per_cell)."""
    rows, cols = values.shape
    if rows % cell_pixels or cols % cell_pixels:
        raise ValueError("Array shape must be a whole number of cells")
    return (
        values.reshape(rows // cell_pixels, cell_pixels, cols // cell_pixels, cell_pixels)
        .transpose(0, 2, 1, 3)
        .reshape(rows // cell_pixels, cols // cell_pixels, cell_pixels * cell_pixels)
    )
