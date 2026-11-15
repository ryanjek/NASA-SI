import math

import numpy as np
import pytest

from nasa_si.model import (
    ScreeningProfile,
    dust_score_from_dci,
    gate_cells,
    score_block,
    score_cells,
    slope_degrees,
)


def test_slope_of_tilted_plane_matches_known_angle():
    pixel_m = 1000.0
    rise_per_pixel = pixel_m * math.tan(math.radians(3.0))
    elevation = np.tile(np.arange(20) * rise_per_pixel, (10, 1))

    slope = slope_degrees(elevation, x_resolution_m=pixel_m, y_resolution_m=pixel_m)

    assert slope[5, 5] == pytest.approx(3.0)


def test_slope_uses_row_wise_longitude_spacing():
    elevation = np.tile(np.arange(5, dtype=float) * 10.0, (2, 1))
    x_resolution = np.array([1000.0, 500.0])

    slope = slope_degrees(elevation, x_resolution_m=x_resolution, y_resolution_m=1000.0)

    assert slope[0, 2] == pytest.approx(math.degrees(math.atan(10 / 1000)))
    assert slope[1, 2] == pytest.approx(math.degrees(math.atan(10 / 500)))


def test_dust_score_ramps_between_dusty_and_clear_thresholds():
    profile = ScreeningProfile()

    scores = dust_score_from_dci(np.array([0.90, 0.94, 0.95, 0.96, 0.99]), profile)

    np.testing.assert_allclose(scores, [0.0, 0.0, 0.5, 1.0, 1.0])


def test_missing_pixels_lower_confidence_but_not_flatness():
    profile = ScreeningProfile()
    flat = np.zeros(4)
    elevation = np.array([-4000.0, -4000.0, np.nan, np.nan])

    metrics = score_block(
        elevation_m=elevation,
        slope_deg=flat,
        ice_consistency=flat,
        dust_score=np.ones(4),
        profile=profile,
    )

    assert metrics["confidence"] == pytest.approx(0.5)
    assert metrics["flatness_score"] == pytest.approx(1.0)


def test_score_cells_is_weighted_sum_of_components():
    profile = ScreeningProfile()
    cells = 3
    pixels = 4
    elevation = np.full((cells, pixels), -4000.0)
    slope = np.zeros((cells, pixels))
    slope[1] = 10.0
    ice = np.zeros((cells, pixels))
    dust = np.ones((cells, pixels))

    metrics = score_cells(
        elevation_m=elevation,
        slope_deg=slope,
        ice_consistency=ice,
        dust_score=dust,
        profile=profile,
    )

    expected_flat = (
        profile.flatness_weight
        + profile.ice_weight * 0.5
        + profile.dust_weight
        + profile.elevation_weight
        + profile.confidence_weight
    )
    assert metrics["score"][0] == pytest.approx(expected_flat)
    assert metrics["score"][1] == pytest.approx(expected_flat - profile.flatness_weight)


def test_empty_cell_scores_nan():
    profile = ScreeningProfile()
    empty = np.full((1, 4), np.nan)

    metrics = score_cells(
        elevation_m=empty,
        slope_deg=empty,
        ice_consistency=empty,
        dust_score=empty,
        profile=profile,
    )

    assert metrics["confidence"][0] == 0.0
    assert np.isnan(metrics["score"][0])


def test_gates_report_each_failure_reason():
    profile = ScreeningProfile(max_abs_latitude_deg=50.0, min_confidence=0.8)

    passes, reasons = gate_cells(
        latitude_deg=np.array([45.0, 55.0, 45.0, 55.0]),
        confidence=np.array([1.0, 1.0, 0.5, 0.5]),
        profile=profile,
    )

    assert passes.tolist() == [True, False, False, False]
    assert reasons.tolist() == ["", "latitude>50", "coverage<0.8", "latitude>50;coverage<0.8"]


def test_profile_rejects_inverted_dust_thresholds():
    with pytest.raises(ValueError):
        ScreeningProfile(dusty_dci=0.96, clear_dci=0.94)
