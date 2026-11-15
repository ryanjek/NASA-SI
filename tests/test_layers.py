import numpy as np

from nasa_si.layers import GlobalRaster, combine_rasters
from nasa_si.patches import PatchAccumulator, PatchGrid

GRID = PatchGrid(coarse_nside=8, fine_nside=64)


def make(values, name="x"):
    v = np.array(values, dtype=np.float32)
    return GlobalRaster(name, v, -180.0, 90.0, 360.0 / v.shape[1], 180.0 / v.shape[0])


def test_every_patch_a_pixel_touches_sees_its_value():
    """Footprint crediting: a coarse pixel straddling patches widens each of their envelopes."""
    rng = np.random.default_rng(0)
    r = make(rng.normal(size=(360, 720)))  # 0.5 deg pixels, patches ~7 deg: the real regime (pixel << patch)
    acc = PatchAccumulator(GRID.npix_coarse)
    r.accumulate(acc, GRID)
    s = acc.result()
    lon, lat = r.lons(), r.lats()
    # dense sampling inside each pixel: the patch holding any sample must have the value in its envelope
    for i in range(0, 360, 23):
        for j in range(0, 720, 31):
            fx = np.linspace(-0.49, 0.49, 5) * r.dlon
            fy = np.linspace(-0.49, 0.49, 5) * r.dlat
            gx, gy = np.meshgrid(lon[j] + fx, lat[i] + fy)
            p = np.unique(GRID.patch_of(gx.ravel(), gy.ravel()))
            assert (s.minimum[p] <= r.values[i, j] + 1e-6).all() and (s.maximum[p] >= r.values[i, j] - 1e-6).all()


def test_nan_pixels_leave_patches_unobserved():
    r = make(np.full((36, 72), np.nan))
    acc = PatchAccumulator(GRID.npix_coarse)
    r.accumulate(acc, GRID)
    assert not acc.result().observed.any()


def test_block_size_invariance():
    rng = np.random.default_rng(1)
    r = make(rng.normal(size=(36, 72)))
    a, b = PatchAccumulator(GRID.npix_coarse), PatchAccumulator(GRID.npix_coarse)
    r.accumulate(a, GRID, row_block=36)
    r.accumulate(b, GRID, row_block=5)
    ra, rb = a.result(), b.result()
    assert np.array_equal(ra.minimum, rb.minimum, equal_nan=True)
    assert np.array_equal(ra.maximum, rb.maximum, equal_nan=True)


def test_window_values_and_combine():
    r = make([[1, 2], [3, 4]])
    assert set(r.window_values(-90, 45, 0).tolist()) == {1.0}
    mx = combine_rasters([make([[1, np.nan]]), make([[5, 2]])], "max", "m")
    assert mx.values.tolist() == [[5.0, 2.0]]
    mn = combine_rasters([make([[1, np.nan]]), make([[5, 2]])], "min", "m")
    assert mn.values.tolist() == [[1.0, 2.0]]
