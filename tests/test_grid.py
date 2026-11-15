import math

import numpy as np
import pytest
from affine import Affine
from rasterio.crs import CRS
from rasterio.transform import from_origin

from nasa_si.grid import cell_blocks, degree_transform, resample_to_grid, study_grid


def eqc_crs(lon_0: float, radius: float) -> CRS:
    return CRS.from_proj4(f"+proj=eqc +lat_ts=0 +lat_0=0 +lon_0={lon_0} +R={radius} +units=m")


def test_degree_transform_for_mola_tile_centred_on_180():
    radius = 3_396_000.0
    metres_per_pixel = math.pi * radius / 180 / 64
    transform = Affine(metres_per_pixel, 0, 0.0, 0, -metres_per_pixel, math.pi * radius / 2)

    degrees, found_radius = degree_transform(transform, eqc_crs(180, radius))

    assert found_radius == radius
    assert degrees.a == pytest.approx(1 / 64)
    assert degrees.c == pytest.approx(180.0)
    assert degrees.f == pytest.approx(90.0)


def test_degree_transform_wraps_negative_longitudes_to_0_360():
    radius = 3_396_190.0
    metres_per_degree = math.pi * radius / 180
    transform = Affine(3000.0, 0, -172.0 * metres_per_degree, 0, -3000.0, 60 * metres_per_degree)

    degrees, _ = degree_transform(transform, eqc_crs(0, radius))

    assert degrees.c == pytest.approx(188.0)
    assert degrees.f == pytest.approx(60.0)


def test_degree_transform_rejects_non_equatorial_standard_parallel():
    crs = CRS.from_proj4("+proj=eqc +lat_ts=30 +lon_0=0 +R=3396190 +units=m")

    with pytest.raises(ValueError):
        degree_transform(Affine(1, 0, 0, 0, -1, 0), crs)


def test_study_grid_snaps_inward_and_trims_to_whole_cells():
    grid = study_grid(west=188.0, east=208.0589, south=-1e-14, north=59.975, cell_pixels=10)

    assert grid.west == 188.0
    assert grid.north == pytest.approx(59.96875)
    assert grid.width % 10 == 0 and grid.height % 10 == 0
    assert grid.east <= 208.0589 and grid.south >= 0.0


def test_resample_beyond_180_east_is_bilinear():
    source = np.add.outer(np.arange(10.0), np.arange(20.0) * 100)
    grid = study_grid(west=188.0, east=208.0, south=50.0, north=60.0, cell_pixels=1,
                      pixels_per_degree=4)

    result = resample_to_grid(source, from_origin(188.0, 60.0, 1.0, 1.0), grid)

    row, col = 20, 40
    source_row = (60.0 - grid.row_latitudes()[row]) - 0.5
    source_col = (grid.col_longitudes()[col] - 188.0) - 0.5
    assert result[row, col] == pytest.approx(source_row + 100 * source_col)


def test_cell_blocks_groups_each_cell_pixels():
    values = np.arange(16).reshape(4, 4)

    blocks = cell_blocks(values, 2)

    assert blocks.shape == (2, 2, 4)
    assert sorted(blocks[0, 1].tolist()) == [2, 3, 6, 7]
    assert sorted(blocks[1, 0].tolist()) == [8, 9, 12, 13]
