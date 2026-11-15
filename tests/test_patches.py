import math

import numpy as np
import pytest

from nasa_si.equal_area import EqualAreaGrid, aggregate
from nasa_si.patches import PatchAccumulator, PatchGrid

# A small hierarchy keeps the tests fast; the properties do not depend on the nside.
SMALL = PatchGrid(coarse_nside=8, fine_nside=64)
DEFAULT = PatchGrid()


def test_default_hierarchy_matches_the_plan():
    assert (DEFAULT.coarse_nside, DEFAULT.fine_nside) == (128, 8192)
    assert DEFAULT.children_per_patch == 4096
    assert DEFAULT.npix_coarse == 196_608
    assert DEFAULT.patch_area_km2 == pytest.approx(2594, rel=0.01)
    assert DEFAULT.patch_side_km == pytest.approx(50.9, abs=0.2)
    assert DEFAULT.fine_cell_side_km == pytest.approx(0.80, abs=0.02)


def test_children_partition_the_fine_grid():
    seen = np.concatenate([SMALL.children(p) for p in range(SMALL.npix_coarse)])
    assert np.array_equal(np.sort(seen), np.arange(SMALL.npix_fine))


def test_parent_child_round_trip():
    rng = np.random.default_rng(0)
    lon, lat = rng.uniform(-180, 180, 500), np.degrees(np.arcsin(rng.uniform(-1, 1, 500)))
    fine = SMALL.fine_cell_of(lon, lat)
    assert np.array_equal(SMALL.parent_of_fine(fine), SMALL.patch_of(lon, lat))
    for f, p in zip(fine[:20], SMALL.patch_of(lon, lat)[:20]):
        lo, hi = SMALL.child_range(int(p))
        assert lo <= f < hi


def test_centres_land_in_their_own_patch():
    lon, lat = SMALL.centers()
    assert np.array_equal(SMALL.patch_of(lon, lat), np.arange(SMALL.npix_coarse))


def test_bad_nside_rejected():
    with pytest.raises(ValueError):
        PatchGrid(coarse_nside=100, fine_nside=64)
    with pytest.raises(ValueError):
        PatchGrid(coarse_nside=64, fine_nside=8)


def test_accumulator_matches_one_shot_aggregate():
    rng = np.random.default_rng(1)
    n = 5000
    ids = rng.integers(0, 40, n)
    vals = rng.normal(size=n)
    vals[rng.random(n) < 0.2] = np.nan
    acc = PatchAccumulator(40)
    acc.add(ids, vals)
    s = acc.result()
    for how, got in (("mean", s.mean), ("min", s.minimum), ("max", s.maximum)):
        cells, ref = aggregate(ids, vals, how)
        assert np.allclose(got[cells], ref, equal_nan=True), how


def test_block_size_does_not_change_the_result():
    rng = np.random.default_rng(2)
    ids = rng.integers(0, 30, 3000)
    vals = rng.normal(size=3000)
    one = PatchAccumulator(30)
    one.add(ids, vals)
    many = PatchAccumulator(30)
    for chunk in np.array_split(np.arange(3000), 17):
        many.add(ids[chunk], vals[chunk])
    a, b = one.result(), many.result()
    assert np.array_equal(a.minimum, b.minimum, equal_nan=True)
    assert np.array_equal(a.maximum, b.maximum, equal_nan=True)
    assert np.allclose(a.mean, b.mean, equal_nan=True)


def test_extent_widens_envelope_without_counting_values():
    acc = PatchAccumulator(3)
    acc.add([1], [5.0])
    acc.add_extent([1, 2], [9.0, 4.0])
    s = acc.result()
    assert (s.minimum[1], s.maximum[1]) == (5.0, 9.0)
    assert s.valid[1] == 1
    assert s.observed[2] and s.valid[2] == 0
