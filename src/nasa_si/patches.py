"""Nested HEALPix patch hierarchy for the coarse (~50 km) pre-filter.

Stage 1 of the Earth search cuts the globe to a small set of ~50 km *patches* that could contain a
match; stage 2 (the fine ~1 km comparison, owned by a teammate) then looks only inside the
survivors. For that hand-off to be trivial the two levels must be one hierarchy:

* coarse level, nside 128: 196,608 patches of ~2,594 km2 (~50.9 km on a side);
* fine level,   nside 8192: ~0.63 km2 cells (~0.80 km on a side), the power of two nearest 1 km;
* NESTED ordering, so the children of patch ``p`` are exactly ``p*4096 .. p*4096 + 4095``
  and the parent of a fine cell ``c`` is ``c // 4096``. No lookup table is needed.

This module is separate from ``equal_area.py``: that one uses RING ordering with nside 375/704 so
that Mars and Earth cells match at ~9.26 km, and RING ids have no parent/child structure.

Longitude is degrees east; inputs may be 0-360 or -180..180. Outputs of ``centers`` use -180..180.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import healpy as hp
import numpy as np
from numpy.typing import ArrayLike, NDArray

from .equal_area import PLANET_RADIUS_KM

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]

COARSE_NSIDE = 128
FINE_NSIDE = 8192


def _is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def _wrap_lon(lon: ArrayLike) -> FloatArray:
    return (np.asarray(lon, dtype=np.float64) + 180.0) % 360.0 - 180.0


@dataclass(frozen=True)
class PatchGrid:
    coarse_nside: int = COARSE_NSIDE
    fine_nside: int = FINE_NSIDE
    radius_km: float = PLANET_RADIUS_KM["earth"]

    def __post_init__(self) -> None:
        if not (_is_power_of_two(self.coarse_nside) and _is_power_of_two(self.fine_nside)):
            raise ValueError("nside values must be powers of two (NESTED ordering)")
        if self.fine_nside < self.coarse_nside:
            raise ValueError("fine_nside must be at least coarse_nside")
        if self.radius_km <= 0:
            raise ValueError("radius_km must be positive")

    # ------------------------------------------------------------------ sizes
    @property
    def children_per_patch(self) -> int:
        return (self.fine_nside // self.coarse_nside) ** 2

    @property
    def npix_coarse(self) -> int:
        return int(hp.nside2npix(self.coarse_nside))

    @property
    def npix_fine(self) -> int:
        return int(hp.nside2npix(self.fine_nside))

    @property
    def patch_area_km2(self) -> float:
        return 4 * math.pi * self.radius_km**2 / self.npix_coarse

    @property
    def patch_side_km(self) -> float:
        return math.sqrt(self.patch_area_km2)

    @property
    def fine_cell_area_km2(self) -> float:
        return 4 * math.pi * self.radius_km**2 / self.npix_fine

    @property
    def fine_cell_side_km(self) -> float:
        return math.sqrt(self.fine_cell_area_km2)

    # ------------------------------------------------------------------ lookup
    def patch_of(self, lon_deg: ArrayLike, lat_deg: ArrayLike) -> IntArray:
        """Coarse patch id for each (lon, lat). Shapes broadcast."""
        lat = np.asarray(lat_deg, dtype=np.float64)
        if np.any(np.abs(lat) > 90):
            raise ValueError("latitude must be within [-90, 90]")
        lon = np.asarray(lon_deg, dtype=np.float64)
        return np.asarray(hp.ang2pix(self.coarse_nside, lon, lat, nest=True, lonlat=True), dtype=np.int64)

    def fine_cell_of(self, lon_deg: ArrayLike, lat_deg: ArrayLike) -> IntArray:
        """Fine (~1 km) cell id for each (lon, lat)."""
        lat = np.asarray(lat_deg, dtype=np.float64)
        if np.any(np.abs(lat) > 90):
            raise ValueError("latitude must be within [-90, 90]")
        lon = np.asarray(lon_deg, dtype=np.float64)
        return np.asarray(hp.ang2pix(self.fine_nside, lon, lat, nest=True, lonlat=True), dtype=np.int64)

    def centers(self, patches: ArrayLike | None = None) -> tuple[FloatArray, FloatArray]:
        """(lon, lat) of patch centres; lon in -180..180. All patches when ``patches`` is None."""
        ids = np.arange(self.npix_coarse) if patches is None else np.asarray(patches, dtype=np.int64)
        self._check(ids)
        lon, lat = hp.pix2ang(self.coarse_nside, ids, nest=True, lonlat=True)
        return _wrap_lon(lon), np.asarray(lat, dtype=np.float64)

    # ------------------------------------------------------------------ hierarchy
    def child_range(self, patch: int) -> tuple[int, int]:
        """Half-open range ``[start, stop)`` of the fine-cell ids inside a patch."""
        self._check(np.asarray([patch]))
        start = int(patch) * self.children_per_patch
        return start, start + self.children_per_patch

    def children(self, patch: int) -> IntArray:
        start, stop = self.child_range(patch)
        return np.arange(start, stop, dtype=np.int64)

    def parent_of_fine(self, fine_ids: ArrayLike) -> IntArray:
        ids = np.asarray(fine_ids, dtype=np.int64)
        if ids.size and (ids.min() < 0 or ids.max() >= self.npix_fine):
            raise ValueError("fine cell id out of range")
        return ids // self.children_per_patch

    # ------------------------------------------------------------------ geometry and regions
    def neighbors(self, patches: ArrayLike) -> IntArray:
        """The 8 surrounding patches for each id, shape (n, 8); -1 where none exists."""
        ids = np.atleast_1d(np.asarray(patches, dtype=np.int64))
        self._check(ids)
        out = hp.get_all_neighbours(self.coarse_nside, ids, nest=True)
        return np.asarray(out, dtype=np.int64).T

    def patches_within(self, lon_deg: float, lat_deg: float, radius_km: float) -> IntArray:
        """Patches whose centre lies within ``radius_km`` of a point; the containing patch if none."""
        if radius_km < 0:
            raise ValueError("radius_km must be non-negative")
        vec = hp.ang2vec(lon_deg, lat_deg, lonlat=True)
        found = hp.query_disc(self.coarse_nside, vec, radius_km / self.radius_km, inclusive=False, nest=True)
        if found.size == 0:
            found = self.patch_of(np.array([lon_deg]), np.array([lat_deg]))
        return np.asarray(found, dtype=np.int64)

    def boundary(self, patch: int, step: int = 2) -> tuple[FloatArray, FloatArray]:
        """Closed (lon, lat) ring around a patch, unwrapped around its centre (GeoJSON-ready)."""
        self._check(np.asarray([patch]))
        vec = hp.boundaries(self.coarse_nside, int(patch), step=step, nest=True)
        lon, lat = hp.vec2ang(vec.T, lonlat=True)
        centre_lon = float(self.centers([patch])[0][0])
        lon = centre_lon + (np.asarray(lon, dtype=np.float64) - centre_lon + 180.0) % 360.0 - 180.0
        lat = np.asarray(lat, dtype=np.float64)
        return np.append(lon, lon[0]), np.append(lat, lat[0])

    def _check(self, ids: IntArray) -> None:
        if ids.size and (ids.min() < 0 or ids.max() >= self.npix_coarse):
            raise ValueError(f"patch id out of range for coarse_nside={self.coarse_nside}")


# ---------------------------------------------------------------------------------------------
# Streaming per-patch statistics
# ---------------------------------------------------------------------------------------------


def _group_min_max(ids: IntArray, values: FloatArray) -> tuple[IntArray, FloatArray, FloatArray]:
    """Per-id minimum and maximum, by sorting (much faster than ``ufunc.at`` for large blocks)."""
    order = np.argsort(ids, kind="stable")
    sid, sval = ids[order], values[order]
    starts = np.flatnonzero(np.r_[True, sid[1:] != sid[:-1]])
    return sid[starts], np.minimum.reduceat(sval, starts), np.maximum.reduceat(sval, starts)


@dataclass(frozen=True)
class PatchStats:
    """Per-patch statistics of one feature. Arrays are indexed by coarse patch id."""

    count: IntArray  # pixels seen, valid or not
    valid: IntArray  # pixels with a finite value
    valid_fraction: FloatArray  # NaN where nothing was seen
    mean: FloatArray  # weighted mean; NaN where nothing was valid
    minimum: FloatArray  # NaN where nothing was valid
    maximum: FloatArray

    @property
    def observed(self) -> NDArray[np.bool_]:
        """Patches that received at least one valid value (centre-assigned or footprint)."""
        return np.isfinite(self.minimum)


class PatchAccumulator:
    """Accumulate per-patch statistics from pixel blocks without holding the whole raster.

    Feed it ``(patch_id, value)`` pairs in any number of blocks; the result does not depend on the
    block size. Non-finite values count toward ``count`` but not ``valid``, so missing data stays
    distinguishable from a real zero. ``weights`` (for example ``cos(lat)`` for a lat/lon raster)
    only affect the mean. Minimum and maximum are exact, which is what recall-preserving
    pruning needs; an optional histogram gives approximate quantiles for reports.
    """

    def __init__(self, npix: int, *, hist_range: tuple[float, float] | None = None, n_bins: int = 0) -> None:
        if n_bins and (hist_range is None or not hist_range[0] < hist_range[1]):
            raise ValueError("a histogram needs hist_range=(low, high) with low < high")
        self.npix = npix
        self._count = np.zeros(npix, dtype=np.int64)
        self._valid = np.zeros(npix, dtype=np.int64)
        self._wsum = np.zeros(npix, dtype=np.float64)
        self._vsum = np.zeros(npix, dtype=np.float64)
        self._min = np.full(npix, np.inf)
        self._max = np.full(npix, -np.inf)
        self._range = hist_range
        self._bins = n_bins
        self._hist = np.zeros((npix, n_bins), dtype=np.uint32) if n_bins else None

    def add(self, patch_ids: ArrayLike, values: ArrayLike, weights: ArrayLike | None = None) -> None:
        ids = np.asarray(patch_ids, dtype=np.int64).ravel()
        vals = np.asarray(values, dtype=np.float64).ravel()
        if ids.shape != vals.shape:
            raise ValueError("patch_ids and values must have the same number of elements")
        if ids.size and (ids.min() < 0 or ids.max() >= self.npix):
            raise ValueError("patch id out of range")
        w = np.ones_like(vals) if weights is None else np.asarray(weights, dtype=np.float64).ravel()
        if w.shape != vals.shape:
            raise ValueError("weights must match values")

        self._count += np.bincount(ids, minlength=self.npix)
        good = np.isfinite(vals) & np.isfinite(w) & (w > 0)
        if not good.any():
            return
        gid, gval, gw = ids[good], vals[good], w[good]
        self._valid += np.bincount(gid, minlength=self.npix)
        self._wsum += np.bincount(gid, weights=gw, minlength=self.npix)
        self._vsum += np.bincount(gid, weights=gw * gval, minlength=self.npix)
        uid, lo, hi = _group_min_max(gid, gval)
        self._min[uid] = np.minimum(self._min[uid], lo)
        self._max[uid] = np.maximum(self._max[uid], hi)
        if self._hist is not None and self._range is not None:
            low, high = self._range
            b = np.clip(((gval - low) / (high - low) * self._bins).astype(np.int64), 0, self._bins - 1)
            np.add.at(self._hist, (gid, b), 1)

    def add_extent(self, patch_ids: ArrayLike, values: ArrayLike) -> None:
        """Widen per-patch minimum and maximum only (count, mean and histogram are untouched).

        A coarse pixel can straddle a patch boundary. ``add`` credits it to the patch holding its
        centre; ``add_extent`` additionally credits its value to every other patch its footprint
        touches, so a patch is never judged to lack a value that physically overlaps it.
        """
        ids = np.asarray(patch_ids, dtype=np.int64).ravel()
        vals = np.asarray(values, dtype=np.float64).ravel()
        if ids.shape != vals.shape:
            raise ValueError("patch_ids and values must have the same number of elements")
        good = np.isfinite(vals)
        if not good.any():
            return
        if ids.size and (ids.min() < 0 or ids.max() >= self.npix):
            raise ValueError("patch id out of range")
        uid, lo, hi = _group_min_max(ids[good], vals[good])
        self._min[uid] = np.minimum(self._min[uid], lo)
        self._max[uid] = np.maximum(self._max[uid], hi)

    def result(self) -> PatchStats:
        seen = np.isfinite(self._min)
        with np.errstate(invalid="ignore", divide="ignore"):
            frac = np.where(self._count > 0, self._valid / np.maximum(self._count, 1), np.nan)
            mean = np.where(self._wsum > 0, self._vsum / np.where(self._wsum > 0, self._wsum, 1.0), np.nan)
        return PatchStats(
            count=self._count.copy(),
            valid=self._valid.copy(),
            valid_fraction=frac,
            mean=mean,
            minimum=np.where(seen, self._min, np.nan),
            maximum=np.where(seen, self._max, np.nan),
        )

    def quantile(self, q: float) -> FloatArray:
        """Approximate per-patch quantile from the histogram (error up to one bin width)."""
        if self._hist is None or self._range is None:
            raise ValueError("this accumulator was created without a histogram")
        if not 0.0 <= q <= 1.0:
            raise ValueError("q must be within [0, 1]")
        low, high = self._range
        cum = np.cumsum(self._hist, axis=1, dtype=np.float64)
        total = cum[:, -1]
        target = q * total
        idx = np.argmax(cum >= target[:, None], axis=1)
        prev = np.where(idx > 0, cum[np.arange(self.npix), np.maximum(idx - 1, 0)], 0.0)
        in_bin = cum[np.arange(self.npix), idx] - prev
        frac = np.where(in_bin > 0, (target - prev) / np.where(in_bin > 0, in_bin, 1.0), 0.5)
        width = (high - low) / self._bins
        out = low + (idx + np.clip(frac, 0.0, 1.0)) * width
        return np.where(total > 0, out, np.nan)
