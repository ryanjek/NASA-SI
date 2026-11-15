"""Cut square tiles of true ground size out of a global lon/lat grid.

Each tile gets its own azimuthal equidistant projection centred on the place, so one pixel is the
same number of metres east-west and north-south at any latitude, on either planet.

Code written with Claude (Anthropic); see twin/README.md.
"""

from __future__ import annotations

import numpy as np
from astropy_healpix import HEALPix
from numpy.typing import NDArray
from scipy.ndimage import map_coordinates

from .planets import TILE_SIZE, TILE_SPACING_M, Grid


def place_centres(nside: int) -> tuple[NDArray, NDArray]:
    """Longitude and latitude in degrees of every HEALPix NESTED cell centre."""
    grid = HEALPix(nside=nside, order="nested")
    lon, lat = grid.healpix_to_lonlat(np.arange(grid.npix))
    return lon.deg, lat.deg


def tile_lonlat(lon0: NDArray, lat0: NDArray, radius_m: float, size: int = TILE_SIZE) -> tuple[NDArray, NDArray]:
    """Longitude and latitude of every tile pixel, shape (places, size, size)."""
    offset = (np.arange(size) - (size - 1) / 2) * TILE_SPACING_M
    east, north = offset[None, None, :], -offset[None, :, None]  # row 0 is the northern edge
    arc = np.hypot(east, north) / radius_m
    scale = np.sinc(arc / np.pi) / radius_m  # sin(arc) / distance, safe at the centre
    lam, phi = np.radians(lon0)[:, None, None], np.radians(lat0)[:, None, None]
    lat = np.arcsin(np.clip(np.cos(arc) * np.sin(phi) + north * scale * np.cos(phi), -1.0, 1.0))
    lon = lam + np.arctan2(east * scale, np.cos(phi) * np.cos(arc) - north * scale * np.sin(phi))
    return np.degrees(lon), np.degrees(lat)


def sample(grid: Grid, lon: NDArray, lat: NDArray, order: int = 1) -> NDArray:
    """Read the grid at the given points (bilinear by default), wrapping across the date line.

    Points north or south of the grid's coverage come back as NaN.
    """
    row, col = grid.pixel_position(lon, lat)
    wrapped = np.concatenate([grid.values[:, -1:], grid.values, grid.values[:, :1]], axis=1)
    values = map_coordinates(wrapped, [row, col + 1.0], order=order, mode="nearest", output=np.float32)
    values[(row < -0.5) | (row > grid.values.shape[0] - 0.5)] = np.nan
    return values
