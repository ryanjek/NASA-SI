import numpy as np
import pytest

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


def _flat(values, name="x"):
    from nasa_si.layers import GlobalRaster

    return GlobalRaster(name, np.array(values, dtype=np.float32), -180.0, 90.0, 180.0, 90.0)


def test_combine_mean_and_sum_ignore_nan():
    from nasa_si.layers import combine_rasters

    a, b = _flat([[1.0, np.nan], [4.0, np.nan]]), _flat([[3.0, np.nan], [np.nan, 2.0]])
    mean = combine_rasters([a, b], "mean", "m").values
    total = combine_rasters([a, b], "sum", "s").values
    assert mean[0, 0] == 2.0 and mean[1, 0] == 4.0 and mean[1, 1] == 2.0 and np.isnan(mean[0, 1])
    assert total[0, 0] == 4.0 and total[1, 0] == 4.0 and np.isnan(total[0, 1])


def test_upsample_keeps_values_and_shrinks_pixels():
    from nasa_si.layers import upsample

    r = _flat([[1.0, 2.0], [3.0, 4.0]])
    u = upsample(r, 4)
    assert u.values.shape == (8, 8) and u.dlon == r.dlon / 4 and u.dlat == r.dlat / 4
    assert (u.values[:4, :4] == 1.0).all() and (u.values[4:, 4:] == 4.0).all()
    assert (u.lon_west, u.lat_north) == (r.lon_west, r.lat_north)


def test_snow_fraction_scales_percent_and_drops_flags():
    from nasa_si.layers import snow_fraction

    out = snow_fraction(np.array([0, 50, 100, 101, 200, 255], dtype=np.uint8))
    assert out[:3].tolist() == [0.0, 0.5, 1.0] and np.isnan(out[3:]).all()


def test_precip_converts_rate_to_mm_and_masks_fill():
    from nasa_si.layers import precip_mm_month

    rate = np.array([1e-5, 0.0, 1e15, np.nan])
    mm = precip_mm_month(rate, 2025, 2)  # 28 days
    assert mm[0] == pytest.approx(1e-5 * 86400 * 28, rel=1e-5)
    assert mm[1] == 0.0 and np.isnan(mm[2]) and np.isnan(mm[3])


def test_find_files_matches_terra_only_and_reads_merra_months(tmp_path):
    from nasa_si.layers import find_modis_files, merra_month_of

    for name in ["MOD11C3.A2025032.061.x.hdf", "MYD11C3.A2025032.061.x.hdf", "MOD10CM.A2025001.061.x.hdf", "MOD13C2.A2025060.061.x.hdf", "MERRA2_400.tavgM_2d_lnd_Nx.202503.nc4", "MERRA2_400.tavgM_2d_aer_Nx.202503.nc4"]:
        (tmp_path / name).write_bytes(b"")
    found = find_modis_files(tmp_path)
    assert [p.name for p in found["MOD11C3"]] == ["MOD11C3.A2025032.061.x.hdf"]
    assert len(found["MOD10CM"]) == len(found["MOD13C2"]) == len(found["MERRA2_lnd"]) == 1
    assert merra_month_of(found["MERRA2_lnd"][0]) == (2025, 3)


def test_pixels_centred_past_a_pole_are_accepted():
    # MERRA-2 style grid: 361 rows from +90 to -90 means the top row's pixel is centred at the pole.
    r = GlobalRaster("p", np.full((8, 4), 5.0, dtype=np.float32), -180.0, 90.25, 90.0, 0.5 / 1)
    acc = PatchAccumulator(GRID.npix_coarse)
    r.accumulate(acc, GRID)
    assert acc.result().observed.any()


def test_barren_fraction_is_a_share_and_fills_become_missing():
    from nasa_si.layers import barren_fraction

    out = barren_fraction(np.array([0, 40, 100, 255]))
    assert out[:3].tolist() == [0.0, pytest.approx(0.4), 1.0] and np.isnan(out[3])


def test_terrain_file_reads_as_a_minimum_and_a_maximum_raster_per_feature(tmp_path):
    from nasa_si.layers import HI, TERRAIN_LAYERS, read_terrain

    arrays = {f"{k}_{b}": np.full((4, 8), v, dtype=np.float32) for k in ("s50", "s95", "rel") for b, v in (("lo", 1.0), ("hi", 9.0))}
    np.savez(tmp_path / "t.npz", **arrays, lon_west=-180.0, lat_north=90.0, d=45.0)
    out = read_terrain(tmp_path / "t.npz")
    assert set(out) == set(TERRAIN_LAYERS) | {k + HI for k in TERRAIN_LAYERS}
    assert out["terrain_relief"].values.max() == 1.0 and out[("terrain_relief" + HI)].values.min() == 9.0


def test_open_water_masks_only_whole_water_pixels(monkeypatch):
    from nasa_si import layers

    pct = np.zeros((2, 2, 17))
    pct[0, 0, 0], pct[0, 1, 0] = 100, 60
    monkeypatch.setattr(layers, "_read_sds", lambda path, name: (pct, {}))
    assert layers.open_water("x").tolist() == [[True, False], [False, False]]


def test_barren_layer_is_missing_over_open_water(monkeypatch):
    from nasa_si import layers

    pct = np.zeros((2, 2, 17), dtype=np.uint8)
    pct[0, 0, 0] = 100  # all water
    pct[1, 1, 16] = 50  # half barren
    monkeypatch.setattr(layers, "_read_sds", lambda path, name: (pct, {}))
    out = layers.read_modis_barren("x").values
    assert np.isnan(out[0, 0]) and out[1, 1] == pytest.approx(0.5) and out[0, 1] == 0.0
