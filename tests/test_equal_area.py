import math

import numpy as np
import pytest

from nasa_si.equal_area import PLANET_RADIUS_KM, EqualAreaGrid, aggregate, quantile, raster_cells
from nasa_si.screening import MARS_RADIUS_KM

MARS = EqualAreaGrid.for_planet("mars")
EARTH = EqualAreaGrid.for_planet("earth")


def great_circle_km(grid: EqualAreaGrid, a: int, b: int) -> float:
    lon1, lat1 = grid.centers([a])
    lon2, lat2 = grid.centers([b])
    p1, p2 = np.radians(lat1[0]), np.radians(lat2[0])
    dl = np.radians(lon2[0] - lon1[0])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * grid.radius_km * math.asin(math.sqrt(h))


# ---------------------------------------------------------------- grid size and area
def test_mars_radius_matches_screening_constant():
    assert PLANET_RADIUS_KM["mars"] == MARS_RADIUS_KM


def test_both_planets_get_the_same_cell_size():
    assert (MARS.nside, EARTH.nside) == (375, 704)
    for grid in (MARS, EARTH):
        assert grid.cell_side_km == pytest.approx(9.26, abs=0.02)
    assert MARS.cell_area_km2 == pytest.approx(EARTH.cell_area_km2, rel=0.005)


def test_cells_tile_the_whole_sphere():
    for grid in (MARS, EARTH):
        assert grid.npix * grid.cell_area_km2 == pytest.approx(4 * math.pi * grid.radius_km**2, rel=1e-12)


def test_unknown_planet_and_bad_size_rejected():
    with pytest.raises(ValueError):
        EqualAreaGrid.for_planet("venus")
    with pytest.raises(ValueError):
        EqualAreaGrid.for_planet("mars", cell_side_km=0)


def test_random_points_fall_in_cells_uniformly():
    """Equal area means every cell is equally likely to catch a uniformly random point."""
    grid = EqualAreaGrid.for_planet("mars", cell_side_km=300.0)  # coarse so the test is quick
    n = 400_000
    rng = np.random.default_rng(0)
    v = rng.normal(size=(n, 3))
    v /= np.linalg.norm(v, axis=1)[:, None]
    lon = np.degrees(np.arctan2(v[:, 1], v[:, 0]))
    lat = np.degrees(np.arcsin(v[:, 2]))
    counts = np.bincount(grid.cell_of(lon, lat), minlength=grid.npix)
    expected_cv = 1 / math.sqrt(n / grid.npix)  # Poisson noise only
    assert counts.std() / counts.mean() == pytest.approx(expected_cv, rel=0.25)


# ---------------------------------------------------------------- lookup
def test_longitude_conventions_are_the_same_meridian():
    assert MARS.cell_of(190.0, 45.0) == MARS.cell_of(-170.0, 45.0)


def test_cell_centres_round_trip():
    ids = np.array([0, 1, 12345, MARS.npix // 2, MARS.npix - 1])
    lon, lat = MARS.centers(ids)
    assert np.array_equal(MARS.cell_of(lon, lat), ids)
    assert np.all((lon >= 0) & (lon < 360))


def test_invalid_inputs_rejected():
    with pytest.raises(ValueError):
        MARS.cell_of(0.0, 91.0)
    with pytest.raises(ValueError):
        MARS.centers([MARS.npix])
    with pytest.raises(ValueError):
        MARS.centers([-1])


def test_neighbors_are_symmetric_and_adjacent():
    ids = np.array([5000, 123456, 700000, 1_600_000])
    nb = MARS.neighbors(ids)
    assert nb.shape == (4, 8)
    for a, row in zip(ids, nb):
        for b in row[row >= 0]:
            assert a in MARS.neighbors([int(b)])[0]
            assert great_circle_km(MARS, int(a), int(b)) < 2.2 * MARS.cell_side_km


# ---------------------------------------------------------------- regions (benchmarks)
def test_site_radius_selects_about_the_right_number_of_cells():
    cells = MARS.cells_within(190.0, 45.0, 50.0)
    expected = math.pi * 50.0**2 / MARS.cell_area_km2
    assert 0.75 * expected < cells.size < 1.25 * expected
    assert MARS.cell_of(190.0, 45.0) in cells


def test_site_region_size_does_not_depend_on_latitude():
    """The point of an equal-area grid: a 40 km radius covers the same number of cells everywhere."""
    counts = [EARTH.cells_within(10.0, lat, 40.0).size for lat in (0.0, 30.0, 60.0, 75.0)]
    assert max(counts) <= 1.15 * min(counts)


def test_radius_smaller_than_a_cell_returns_the_containing_cell():
    cells = MARS.cells_within(190.0, 45.0, 0.5)
    assert cells.tolist() == [int(MARS.cell_of(190.0, 45.0))]


def test_box_area_matches_spherical_area():
    west, east, south, north = 185.0, 195.0, 42.0, 48.0
    cells = MARS.cells_in_box(west, east, south, north)
    area = MARS.radius_km**2 * math.radians(east - west) * (math.sin(math.radians(north)) - math.sin(math.radians(south)))
    assert cells.size * MARS.cell_area_km2 == pytest.approx(area, rel=0.03)
    with pytest.raises(ValueError):
        MARS.cells_in_box(10.0, 5.0, 0.0, 1.0)


def test_cell_boundary_is_closed_and_never_wraps():
    for lon_c in (0.2, 359.8, 180.0, 90.0):
        cell = int(MARS.cell_of(lon_c, 10.0))
        lon, lat = MARS.cell_boundary(cell)
        assert lon[0] == lon[-1] and lat[0] == lat[-1]
        assert np.ptp(lon) < 1.0  # no 360-degree jump at the prime meridian or antimeridian
        centre_lon, centre_lat = MARS.centers([cell])
        assert lon.min() < centre_lon[0] < lon.max()
        assert lat.min() < centre_lat[0] < lat.max()


# ---------------------------------------------------------------- aggregation
def test_mean_count_and_valid_fraction():
    cells = np.array([3, 3, 3, 7, 7, 9])
    vals = np.array([1.0, 2.0, np.nan, 10.0, 20.0, 5.0])
    c, mean = aggregate(cells, vals, "mean")
    assert c.tolist() == [3, 7, 9]
    assert mean.tolist() == [1.5, 15.0, 5.0]
    assert aggregate(cells, vals, "count")[1].tolist() == [3, 2, 1]
    assert aggregate(cells, vals, "valid_fraction")[1] == pytest.approx([2 / 3, 1.0, 1.0])


def test_weighted_mean():
    _, mean = aggregate([1, 1], [0.0, 10.0], "mean", weights=[1.0, 3.0])
    assert mean[0] == pytest.approx(7.5)


def test_median_min_max_and_quantile():
    cells = np.array([2, 2, 2, 2, 5])
    vals = np.array([4.0, 1.0, 3.0, 2.0, 9.0])
    assert aggregate(cells, vals, "median")[1].tolist() == [2.5, 9.0]
    assert aggregate(cells, vals, "min")[1].tolist() == [1.0, 9.0]
    assert aggregate(cells, vals, "max")[1].tolist() == [4.0, 9.0]
    _, q95 = quantile(np.zeros(11, dtype=int), np.arange(11.0), 0.95)
    assert q95[0] == pytest.approx(9.5)
    with pytest.raises(ValueError):
        quantile([0], [1.0], 1.5)


def test_cell_with_no_valid_pixels_is_nan_but_reports_zero_coverage():
    cells = np.array([1, 1, 2])
    vals = np.array([np.nan, np.nan, 3.0])
    c, mean = aggregate(cells, vals, "mean")
    assert np.isnan(mean[0]) and mean[1] == 3.0
    assert aggregate(cells, vals, "valid_fraction")[1].tolist() == [0.0, 1.0]
    assert np.isnan(aggregate(cells, vals, "median")[1][0])


def test_flat_fraction_recipe_ignores_invalid_pixels():
    """Rubric: fraction of *valid* pixels with slope <= 5 deg."""
    slope = np.array([1.0, 2.0, 6.0, np.nan])
    flat = np.where(np.isfinite(slope), (slope <= 5).astype(float), np.nan)
    _, frac = aggregate(np.zeros(4, dtype=int), flat, "mean")
    assert frac[0] == pytest.approx(2 / 3)


def test_mismatched_inputs_rejected():
    with pytest.raises(ValueError):
        aggregate([1, 2], [1.0], "mean")
    with pytest.raises(ValueError):
        aggregate([1, 2], [1.0, 2.0], "mean", weights=[1.0])
    with pytest.raises(ValueError):
        aggregate([1], [1.0], "mode")  # type: ignore[arg-type]


def test_raster_cells_matches_pointwise_lookup_and_constant_raster_aggregates_to_constant():
    lons = np.linspace(188.0, 193.0, 60)
    lats = np.linspace(47.5, 42.5, 50)
    ids = raster_cells(MARS, lons, lats)
    assert ids.shape == (50, 60)
    assert ids[3, 7] == MARS.cell_of(lons[7], lats[3])
    _, mean = aggregate(ids, np.full(ids.shape, 7.0), "mean", weights=np.cos(np.radians(lats))[:, None] * np.ones(ids.shape))
    assert np.allclose(mean, 7.0)
