"""Hand-made terrain-shape measurements for a tile: the baseline any learned model must beat.

Every number is in physical units (metres, metres per metre) and is computed the same way on both
planets. Nothing is rescaled per tile, because the height of the relief is itself a real property.

Code written with Claude (Anthropic); see twin/README.md.
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

LAGS = (1, 2, 4, 8)  # pixel separations for slope: 1.9, 3.7, 7.4 and 14.8 km
FLAT_SLOPE = math.tan(math.radians(0.5))
SLOPE_FLOOR = 2e-4  # a one-metre height step over one pixel is 5e-4; below this is storage noise
MIN_SPREAD_M = 1.0
NAMES = ("tilt", "relief", "roughness", *(f"slope_{lag}" for lag in LAGS), "skewness", "kurtosis", "grain", "flat_share")


def _rms_slope(tiles: NDArray, lag: int, spacing_m: float) -> NDArray:
    east = tiles[:, :, lag:] - tiles[:, :, :-lag]
    north = tiles[:, lag:, :] - tiles[:, :-lag, :]
    mean_square = ((east**2).mean(axis=(1, 2)) + (north**2).mean(axis=(1, 2))) / 2
    return np.sqrt(mean_square) / (lag * spacing_m)


def describe(tiles: NDArray, spacing_m: float) -> NDArray:
    """Shape measurements, one row per tile and one column per entry of ``NAMES``."""
    count, size, _ = tiles.shape
    axis = np.arange(size) - (size - 1) / 2
    east_axis, north_axis = np.meshgrid(axis, -axis)
    design = np.stack([np.ones(size * size), east_axis.ravel(), north_axis.ravel()], axis=1)
    heights = tiles.reshape(count, -1).astype(np.float64)
    plane = heights @ np.linalg.pinv(design).T
    residual = heights - plane @ design.T

    low, high = np.percentile(heights, (2, 98), axis=1)
    spread = residual.std(axis=1)
    scaled = residual / np.maximum(spread, MIN_SPREAD_M)[:, None]
    textured = spread >= MIN_SPREAD_M
    skewness = np.where(textured, (scaled**3).mean(axis=1), 0.0).clip(-3, 3)
    kurtosis = np.where(textured, (scaled**4).mean(axis=1) - 3, 0.0).clip(-2, 10)

    east = (tiles[:, 1:-1, 2:] - tiles[:, 1:-1, :-2]) / (2 * spacing_m)
    north = (tiles[:, :-2, 1:-1] - tiles[:, 2:, 1:-1]) / (2 * spacing_m)
    east_power, north_power = (east**2).mean(axis=(1, 2)), (north**2).mean(axis=(1, 2))
    cross = (east * north).mean(axis=(1, 2))
    grain = np.hypot(east_power - north_power, 2 * cross) / (east_power + north_power + SLOPE_FLOOR**2)

    columns = [
        np.log10(np.hypot(plane[:, 1], plane[:, 2]) / spacing_m + SLOPE_FLOOR),
        np.log10(high - low + 1.0),
        np.log10(spread + 1.0),
        *(np.log10(_rms_slope(tiles, lag, spacing_m) + SLOPE_FLOOR) for lag in LAGS),
        skewness,
        kurtosis,
        grain,
        (np.hypot(east, north) < FLAT_SLOPE).mean(axis=(1, 2)),
    ]
    return np.stack(columns, axis=1).astype(np.float32)
