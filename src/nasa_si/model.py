"""Transparent terrain features and Phase 1 Arcadia screening scores."""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class ScreeningProfile:
    """Exploratory thresholds; these are not flight-certification requirements.

    ``flat_slope_deg`` is evaluated on ~1 km MOLA pixels, which hide metre-scale
    slopes. DCI emissivity of ~0.94 and below indicates dust-mantled surfaces and
    ~0.96 and above relatively dust-free surfaces (Ruff and Christensen, 2002).
    The latitude gate follows NASA's human landing-site workshop criterion of +/-50.
    """

    flat_slope_deg: float = 5.0
    preferred_max_elevation_m: float = -2_000.0
    elevation_soft_margin_m: float = 2_000.0
    dusty_dci: float = 0.94
    clear_dci: float = 0.96
    max_abs_latitude_deg: float = 50.0
    min_confidence: float = 0.8
    flatness_weight: float = 0.30
    ice_weight: float = 0.30
    dust_weight: float = 0.20
    elevation_weight: float = 0.10
    confidence_weight: float = 0.10

    def __post_init__(self) -> None:
        total = (
            self.flatness_weight
            + self.ice_weight
            + self.dust_weight
            + self.elevation_weight
            + self.confidence_weight
        )
        if not np.isclose(total, 1.0):
            raise ValueError(f"Profile weights must total 1.0, got {total}")
        if self.clear_dci <= self.dusty_dci:
            raise ValueError("clear_dci must be greater than dusty_dci")


def slope_degrees(
    elevation_m: FloatArray,
    *,
    x_resolution_m: float | FloatArray,
    y_resolution_m: float,
) -> FloatArray:
    """Calculate first-order slope from an elevation grid."""
    if elevation_m.ndim != 2:
        raise ValueError("elevation_m must be a 2-D array")
    if y_resolution_m <= 0:
        raise ValueError("y_resolution_m must be positive")

    dz_dy = np.gradient(elevation_m, axis=0) / y_resolution_m
    dz_dx = np.gradient(elevation_m, axis=1)
    x_resolution = np.asarray(x_resolution_m, dtype=np.float64)
    if np.any(x_resolution <= 0):
        raise ValueError("x_resolution_m must be positive")
    if x_resolution.ndim == 1:
        if x_resolution.shape[0] != elevation_m.shape[0]:
            raise ValueError("row-wise x_resolution_m must match the raster height")
        x_resolution = x_resolution[:, None]
    dz_dx = dz_dx / x_resolution
    return np.degrees(np.arctan(np.hypot(dz_dx, dz_dy)))


def local_relief(elevation_m: FloatArray, radius: int = 1) -> FloatArray:
    """Return local maximum minus minimum without requiring SciPy."""
    if radius < 1:
        raise ValueError("radius must be at least 1")
    padded = np.pad(elevation_m, radius, mode="edge")
    local_min = np.full_like(elevation_m, np.nan)
    local_max = np.full_like(elevation_m, np.nan)
    size = 2 * radius + 1
    for row_offset in range(size):
        for col_offset in range(size):
            view = padded[
                row_offset : row_offset + elevation_m.shape[0],
                col_offset : col_offset + elevation_m.shape[1],
            ]
            local_min = np.fmin(local_min, view)
            local_max = np.fmax(local_max, view)
    return local_max - local_min


def robust_unit_scale(
    values: FloatArray,
    *,
    lower_percentile: float = 5.0,
    upper_percentile: float = 95.0,
) -> FloatArray:
    """Scale finite values to 0..1 while reducing sensitivity to outliers."""
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return np.full_like(values, np.nan)
    low, high = np.percentile(finite, (lower_percentile, upper_percentile))
    if np.isclose(low, high):
        result = np.full_like(values, 0.5)
        result[~np.isfinite(values)] = np.nan
        return result
    return np.clip((values - low) / (high - low), 0.0, 1.0)


def dust_score_from_dci(dci: FloatArray, profile: ScreeningProfile) -> FloatArray:
    """Map Dust Cover Index emissivity to 0 (dust-mantled) .. 1 (relatively dust-free)."""
    return np.clip(
        (dci - profile.dusty_dci) / (profile.clear_dci - profile.dusty_dci), 0.0, 1.0
    )


def score_cells(
    *,
    elevation_m: FloatArray,
    slope_deg: FloatArray,
    ice_consistency: FloatArray,
    dust_score: FloatArray,
    profile: ScreeningProfile,
) -> dict[str, FloatArray]:
    """Calculate explainable metrics; the last axis of every input holds one cell's pixels."""
    valid = (
        np.isfinite(elevation_m)
        & np.isfinite(slope_deg)
        & np.isfinite(ice_consistency)
        & np.isfinite(dust_score)
    )
    valid_count = valid.sum(axis=-1)
    confidence = valid_count / valid.shape[-1]

    def only_valid(values: FloatArray) -> FloatArray:
        return np.where(valid, values, np.nan)

    elevation_valid = only_valid(elevation_m)
    slope = only_valid(slope_deg)
    with warnings.catch_warnings(), np.errstate(invalid="ignore", divide="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        elevation = np.nanmedian(elevation_valid, axis=-1)
        flat_count = (valid & (slope_deg <= profile.flat_slope_deg)).sum(axis=-1)
        flatness = np.where(valid_count > 0, flat_count / valid_count, np.nan)
        ice = np.clip((np.nanmedian(only_valid(ice_consistency), axis=-1) + 1.0) / 2.0, 0.0, 1.0)
        dust = np.nanmedian(only_valid(dust_score), axis=-1)
        slope_median = np.nanmedian(slope, axis=-1)
        slope_p95 = np.nanpercentile(slope, 95, axis=-1)
        relief = np.nanmax(elevation_valid, axis=-1) - np.nanmin(elevation_valid, axis=-1)
    elevation_score = np.clip(
        (profile.preferred_max_elevation_m + profile.elevation_soft_margin_m - elevation)
        / profile.elevation_soft_margin_m,
        0.0,
        1.0,
    )
    total = (
        profile.flatness_weight * flatness
        + profile.ice_weight * ice
        + profile.dust_weight * dust
        + profile.elevation_weight * elevation_score
        + profile.confidence_weight * confidence
    )
    return {
        "score": total,
        "confidence": confidence,
        "flatness_score": flatness,
        "ice_score": ice,
        "dust_score": dust,
        "elevation_score": elevation_score,
        "elevation_median_m": elevation,
        "slope_median_deg": slope_median,
        "slope_p95_deg": slope_p95,
        "relief_m": relief,
    }


def score_block(
    *,
    elevation_m: FloatArray,
    slope_deg: FloatArray,
    ice_consistency: FloatArray,
    dust_score: FloatArray,
    profile: ScreeningProfile,
) -> dict[str, float]:
    """Calculate explainable metrics for one candidate cell."""
    metrics = score_cells(
        elevation_m=np.asarray(elevation_m, dtype=np.float64).reshape(-1),
        slope_deg=np.asarray(slope_deg, dtype=np.float64).reshape(-1),
        ice_consistency=np.asarray(ice_consistency, dtype=np.float64).reshape(-1),
        dust_score=np.asarray(dust_score, dtype=np.float64).reshape(-1),
        profile=profile,
    )
    return {name: float(value) for name, value in metrics.items()}


def gate_cells(
    *,
    latitude_deg: FloatArray,
    confidence: FloatArray,
    profile: ScreeningProfile,
) -> tuple[NDArray[np.bool_], NDArray[np.str_]]:
    """Apply hard eligibility gates; return pass flags and readable failure reasons."""
    too_far = np.abs(latitude_deg) > profile.max_abs_latitude_deg
    sparse = ~(confidence >= profile.min_confidence)
    reasons = np.full(np.shape(latitude_deg), "", dtype=object)
    reasons[too_far] = f"latitude>{profile.max_abs_latitude_deg:g}"
    reasons[sparse & too_far] += ";"
    reasons[sparse] += f"coverage<{profile.min_confidence:g}"
    return ~(too_far | sparse), reasons.astype(str)
