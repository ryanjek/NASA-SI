"""Global elevation grids for Mars and Earth, read into one common form.

Both grids are simple cylindrical with a pixel of about 1.85 km at the equator (MOLA 32 pixels per
degree, ETOPO 60 arc-seconds), so the two planets are compared at the same ground resolution.

Code written with Claude (Anthropic); see twin/README.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import netCDF4
import numpy as np
from numpy.typing import NDArray
from pyhdf.SD import SD, SDC
from scipy.ndimage import uniform_filter1d

RADIUS_M = {"mars": 3_396_190.0, "earth": 6_371_008.8}
PLACE_NSIDE = {"mars": 128, "earth": 256}  # about 27 km and 25 km between place centres
TILE_SPACING_M = 1853.0
TILE_SIZE = 32  # 32 x 1.853 km = 59 km window
MAX_SMOOTHING_WIDTH = 65
WATER_CLASS = 0  # MCD12C1 IGBP class for water
PERMANENT_ICE_CLASS = 15
LAND_COVER_FACTOR = 3  # 0.05 degree land cover over a 1/60 degree elevation grid


@dataclass(frozen=True)
class Grid:
    """A lon/lat raster. Row 0 is the northernmost row; column 0 starts at ``lon_west``."""

    values: NDArray
    pixels_per_degree: float
    lon_west: float
    lat_north: float = 90.0  # top edge of row 0
    rows_per_degree: float | None = None  # only for grids whose latitude step differs from the longitude step

    def pixel_position(self, lon: NDArray, lat: NDArray) -> tuple[NDArray, NDArray]:
        row = (self.lat_north - lat) * (self.rows_per_degree or self.pixels_per_degree) - 0.5
        col = ((lon - self.lon_west) % 360.0) * self.pixels_per_degree - 0.5
        return row, col


def _label_value(label: str, key: str) -> float:
    return float(re.search(rf"^\s*{key}\s*=\s*([-\d.]+)", label, re.M).group(1))


def read_mola(image: Path) -> Grid:
    """Read a MOLA gridded record (16-bit big-endian), taking its shape from the PDS label."""
    label = image.with_suffix(".lbl").read_text(encoding="latin-1")
    shape = int(_label_value(label, "LINES")), int(_label_value(label, "LINE_SAMPLES"))
    values = np.fromfile(image, dtype=">i2").reshape(shape).astype(np.int16)
    return Grid(values, _label_value(label, "MAP_RESOLUTION"), _label_value(label, "WESTERNMOST_LONGITUDE"))


def read_water_mask(land_cover: Path, surface: Path) -> Grid:
    """Water on the ETOPO grid: open water, plus "permanent ice" pixels whose surface is below sea level.

    MCD12C1 labels Arctic sea ice as permanent snow and ice; ETOPO's height there is the sea floor, so
    without this rule frozen sea passes as rugged land. The test runs per ETOPO pixel because a 0.05
    degree land-cover pixel on a coast mixes land and sea.
    """
    cover = SD(str(land_cover), SDC.READ).select("Majority_Land_Cover_Type_1").get()
    with netCDF4.Dataset(surface) as dataset:
        below_sea, lat = np.asarray(dataset["z"][:], dtype=np.float32) < 0, dataset["lat"][:]
    if lat[0] < lat[-1]:
        below_sea = below_sea[::-1]
    return Grid(water_mask(cover, below_sea), 20.0 * LAND_COVER_FACTOR, -180.0)


def water_mask(cover: NDArray, below_sea: NDArray) -> NDArray:
    """Land-cover classes on the coarse grid and below-sea-level flags on the fine grid, to fine water."""
    factor = below_sea.shape[0] // cover.shape[0]
    upscale = lambda mask: np.repeat(np.repeat(mask, factor, 0), factor, 1)  # noqa: E731
    return upscale(cover == WATER_CLASS) | (upscale(cover == PERMANENT_ICE_CLASS) & below_sea)


def read_etopo(surface: Path, water: Grid) -> Grid:
    """Earth surface heights in whole metres, with open water set to sea level.

    Heights are rounded to whole metres because MOLA is stored that way; otherwise very flat ground
    would look smoother on Earth than the Mars data can express.
    """
    with netCDF4.Dataset(surface) as dataset:
        lat, lon = dataset["lat"][:], dataset["lon"][:]
        heights = np.rint(np.asarray(dataset["z"][:], dtype=np.float32)).astype(np.int16)
    if lat[0] < lat[-1]:
        heights = heights[::-1]
    step = float(abs(lon[1] - lon[0]))
    heights[water.values & (heights < 0)] = 0
    return Grid(heights, 1.0 / step, float(lon[0]) - step / 2)


def smooth_east_west(grid: Grid) -> Grid:
    """Average along longitude so that every row has the ground resolution of the equator.

    Pixels narrow towards the poles; without this a tile at 70 degrees would see finer detail east-west
    than north-south and measure as rougher than the same ground at the equator.
    """
    rows = grid.values.shape[0]
    lat = 90.0 - (np.arange(rows) + 0.5) / grid.pixels_per_degree
    width = np.minimum(np.rint(1.0 / np.cos(np.radians(lat))), MAX_SMOOTHING_WIDTH).astype(int) | 1
    smoothed = grid.values.copy()
    for size in np.unique(width[width > 1]):
        band = width == size
        averaged = uniform_filter1d(grid.values[band].astype(np.float32), size=int(size), axis=1, mode="wrap")
        smoothed[band] = np.rint(averaged)
    return Grid(smoothed, grid.pixels_per_degree, grid.lon_west)
