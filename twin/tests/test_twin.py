"""Unit tests for the twin search on tiny synthetic arrays. No network, no real downloads.

    python -m pytest twin/tests

Code written with Claude (Anthropic); see twin/README.md.
"""

import math

import numpy as np
import pytest
import torch

from twin import search, shape
from twin.mars_climate import read_map
from twin.planets import TILE_SIZE, TILE_SPACING_M, Grid, smooth_east_west, water_mask
from twin.tiles import sample, tile_lonlat
from twin.validate import _distance_km, _percentile_of_best

MARS_RADIUS = 3_396_190.0


def _great_circle_m(lon1, lat1, lon2, lat2, radius):
    lam1, phi1, lam2, phi2 = map(np.radians, (lon1, lat1, lon2, lat2))
    cosine = np.sin(phi1) * np.sin(phi2) + np.cos(phi1) * np.cos(phi2) * np.cos(lam2 - lam1)
    return np.arccos(np.clip(cosine, -1, 1)) * radius


# Geometry


@pytest.mark.parametrize("lat0", [0.0, 60.0, 85.0])
def test_tile_pixels_are_the_same_size_east_and_north_at_any_latitude(lat0):
    lon, lat = tile_lonlat(np.array([30.0]), np.array([lat0]), MARS_RADIUS)
    centre = TILE_SIZE // 2
    east = _great_circle_m(lon[0, centre, centre], lat[0, centre, centre], lon[0, centre, centre + 1], lat[0, centre, centre + 1], MARS_RADIUS)
    north = _great_circle_m(lon[0, centre, centre], lat[0, centre, centre], lon[0, centre - 1, centre], lat[0, centre - 1, centre], MARS_RADIUS)
    assert east == pytest.approx(TILE_SPACING_M, rel=1e-3)
    assert north == pytest.approx(TILE_SPACING_M, rel=1e-3)


def test_tile_row_zero_is_the_northern_edge():
    _, lat = tile_lonlat(np.array([0.0]), np.array([10.0]), MARS_RADIUS)
    assert lat[0, 0, TILE_SIZE // 2] > lat[0, -1, TILE_SIZE // 2]


def test_sampling_wraps_across_the_date_line():
    columns = np.tile(np.arange(360, dtype=np.float32), (180, 1))
    grid = Grid(columns, 1.0, -180.0)
    west, east = sample(grid, np.array([-179.5]), np.array([0.0])), sample(grid, np.array([179.5]), np.array([0.0]))
    assert west[0] == pytest.approx(0.0)
    assert east[0] == pytest.approx(359.0)
    across = sample(grid, np.array([180.0]), np.array([0.0]))
    assert across[0] == pytest.approx((0.0 + 359.0) / 2)


def test_sampling_outside_a_partial_grid_is_missing():
    swim_like = Grid(np.ones((120, 360), np.float32), 1.0, -180.0, 60.0)  # covers 60N to 60S only
    values = sample(swim_like, np.array([0.0, 0.0, 0.0]), np.array([0.0, 70.0, -70.0]))
    assert values[0] == 1.0
    assert np.isnan(values[1]) and np.isnan(values[2])


def test_east_west_smoothing_leaves_the_equator_and_smooths_high_latitudes():
    rng = np.random.default_rng(0)
    heights = rng.integers(-100, 100, (180, 360)).astype(np.int16)
    smoothed = smooth_east_west(Grid(heights, 1.0, -180.0)).values
    assert np.array_equal(smoothed[90], heights[90])
    assert smoothed[5].std() < heights[5].std() / 2


# Earth land mask


def test_sea_ice_over_sea_is_water_and_ice_over_land_is_not():
    cover = np.array([[0, 15], [15, 16]], np.uint8)  # water, ice over sea, ice over land, barren
    below_sea = np.zeros((6, 6), bool)
    below_sea[:3, 3:] = True
    water = water_mask(cover, below_sea)
    assert water[:3, :3].all() and water[:3, 3:].all()
    assert not water[3:].any()


def test_a_coastal_ice_pixel_is_split_per_fine_pixel():
    cover = np.array([[15]], np.uint8)
    below_sea = np.array([[True, False, False], [True, False, False], [True, True, False]])
    assert np.array_equal(water_mask(cover, below_sea), below_sea)


# Shape measurements


def test_flat_tile_is_all_flat_with_no_relief():
    features = shape.describe(np.full((1, TILE_SIZE, TILE_SIZE), 100.0), TILE_SPACING_M)[0]
    names = list(shape.NAMES)
    assert features[names.index("flat_share")] == 1.0
    assert features[names.index("relief")] == pytest.approx(0.0)


def test_tilted_plane_reports_its_slope():
    slope = 0.01
    x = np.arange(TILE_SIZE) * TILE_SPACING_M * slope
    tile = np.tile(x, (TILE_SIZE, 1))[None]
    tilt = shape.describe(tile, TILE_SPACING_M)[0][list(shape.NAMES).index("tilt")]
    assert 10**tilt == pytest.approx(slope + shape.SLOPE_FLOOR, rel=1e-3)


def test_shape_does_not_depend_on_which_way_is_north():
    rng = np.random.default_rng(1)
    tile = rng.normal(0, 50, (1, TILE_SIZE, TILE_SIZE))
    turned = np.rot90(tile, 1, axes=(1, 2))
    names = list(shape.NAMES)
    keep = [names.index(n) for n in ("relief", "roughness", "slope_1", "slope_4", "flat_share")]
    np.testing.assert_allclose(shape.describe(tile, TILE_SPACING_M)[0][keep], shape.describe(turned, TILE_SPACING_M)[0][keep], rtol=1e-5)


def test_rougher_ground_scores_rougher():
    rng = np.random.default_rng(2)
    noise = rng.normal(0, 1, (1, TILE_SIZE, TILE_SIZE))
    gentle, rough = shape.describe(noise * 5, TILE_SPACING_M)[0], shape.describe(noise * 500, TILE_SPACING_M)[0]
    assert rough[list(shape.NAMES).index("roughness")] > gentle[list(shape.NAMES).index("roughness")]


# Similarity


def _places(values):
    values = np.asarray(values, dtype=np.float64).reshape(len(values), -1)
    return {"place": np.arange(len(values)), "albedo": values}


def test_similarity_is_one_when_equal_and_falls_with_tolerance_units():
    family = search.make_family("albedo", _places([0.20]), _places([0.20, 0.23, 0.50]))
    similarity = search.fused_similarity([family], slice(0, 1))[0].cpu().numpy()
    assert similarity[0] == pytest.approx(1.0)
    assert similarity[1] == pytest.approx(math.exp(-0.5), rel=1e-4)  # one tolerance (0.03) apart
    assert similarity[2] < 1e-6


def test_missing_on_earth_scores_zero_not_neutral():
    family = search.make_family("albedo", _places([0.20]), _places([np.nan]))
    assert search.fused_similarity([family], slice(0, 1))[0, 0].item() == 0.0


def test_missing_on_mars_drops_that_factor_from_the_mean():
    albedo = search.make_family("albedo", _places([0.20]), _places([0.20]))
    ice_mars, ice_earth = {"place": np.arange(1), "ice": np.array([[np.nan]])}, {"place": np.arange(1), "ice": np.array([[0.0]])}
    ice = search.make_family("ice", ice_mars, ice_earth)
    assert search.fused_similarity([albedo, ice], slice(0, 1))[0, 0].item() == pytest.approx(1.0)


def test_neighbour_distance_pairs_healpix_siblings_only():
    vectors = np.array([[0.0], [1.0], [5.0], [9.0]])
    place = np.array([0, 1, 2, 7])  # 0-1 are siblings; 2's sibling 3 is absent; 7's sibling 6 is absent
    np.testing.assert_allclose(np.sort(search._neighbour_distance(vectors, place)), [1.0, 1.0])


# Checks


def test_percentile_is_zero_when_the_region_holds_the_best_place_and_high_when_the_worst():
    scores = np.arange(100, dtype=float)
    region = np.zeros(100, bool)
    region[99] = True
    assert _percentile_of_best(scores, region) == 0.0
    region[:] = False
    region[0] = True
    assert _percentile_of_best(scores, region) == 99.0


def test_distance_handles_the_date_line():
    assert _distance_km(np.array([179.9]), np.array([0.0]), -179.9, 0.0)[0] == pytest.approx(22.2, abs=0.1)


# Mars climate file reader


def test_climate_map_reader_returns_latitude_by_longitude(tmp_path):
    text = "# header\n lat || -90.0 0.0 90.0\n----\n -180.0 || 150 200 160\n 0.0 || 151 210 161\n 180.0 || 150 200 160\n"
    path = tmp_path / "tsurf.txt"
    path.write_text(text)
    lat, lon, values = read_map(path)
    assert lat.tolist() == [-90.0, 0.0, 90.0]
    assert lon.tolist() == [-180.0, 0.0, 180.0]
    assert values.shape == (3, 3)
    assert values[1, 1] == 210.0  # equator, 0 E


def test_geometric_combination_lets_one_failed_factor_veto_the_match():
    albedo = search.make_family("albedo", _places([0.20]), _places([0.20, 0.20]))
    veg_mars, veg_earth = {"place": np.arange(1), "vegetation": np.array([[0.0]])}, {"place": np.arange(2), "vegetation": np.array([[0.0], [0.9]])}
    vegetation = search.make_family("vegetation", veg_mars, veg_earth)
    mean = search.fused_similarity([albedo, vegetation], slice(0, 1))[0].cpu().numpy()
    geometric = search.fused_similarity([albedo, vegetation], slice(0, 1), "geometric")[0].cpu().numpy()
    assert mean[1] == pytest.approx(0.5, abs=1e-3)  # the mean still gives half credit
    assert geometric[1] < 0.05  # the veto does not
    assert geometric[0] == pytest.approx(1.0, abs=2e-3)  # the log floor adds 0.001
