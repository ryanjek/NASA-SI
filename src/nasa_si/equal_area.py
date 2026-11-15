"""One equal-area cell grid for Mars and Earth.

Why: the screening grid is 10 MOLA pixels (~9.26 km) in *degrees*, so a cell is 9.26 km tall
but only ~6.5 km wide at 45 N and razor thin near the poles. Feature statistics (flat-area
fraction, p95 slope, ...) and any clustering would then depend on latitude for no physical
reason. A HEALPix grid gives every cell the same area on both planets, with bounded shape
distortion (cell diagonals differ by less than ~1.8x everywhere, poles included).

Cell size: ``cell_side_km`` (default 9.26 km, the committed Mars cell height) sets the target
area ``side**2`` ~ 85.8 km2. nside is rounded to the nearest integer, so Mars (nside 375) and
Earth (nside 704) differ in cell area by about 0.15%, which is below the noise in any feature.

Conventions:
* Longitude is degrees east. Inputs may be 0-360 or -180..180 (they are the same meridian);
  outputs use 0-360, the Mars/MOLA convention.
* Cell ids use the HEALPix RING scheme, so any integer nside works (the NESTED scheme would
  force a power of two and break the cross-planet area match).
* A cell id is only meaningful together with its grid (planet + nside).

Scale matching is the caller's job and matters as much as the grid: derive per-pixel layers at
the scale the other planet was measured (for example Earth slope from a DEM aggregated to
~1 km, as MOLA slope is) and only then aggregate into cells with the functions here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import healpy as hp
import numpy as np
from numpy.typing import ArrayLike, NDArray

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]

# Keep in sync with screening.MARS_RADIUS_KM (a test enforces it).
PLANET_RADIUS_KM: dict[str, float] = {"mars": 3396.19, "earth": 6371.0088}
DEFAULT_CELL_SIDE_KM = 9.26

Stat = Literal["mean", "count", "valid_fraction", "median", "min", "max"]


@dataclass(frozen=True)
class EqualAreaGrid:
    planet: str
    radius_km: float
    nside: int

    # ------------------------------------------------------------------ construction
    @classmethod
    def for_planet(cls, planet: str, cell_side_km: float = DEFAULT_CELL_SIDE_KM) -> EqualAreaGrid:
        try:
            radius = PLANET_RADIUS_KM[planet.lower()]
        except KeyError:
            raise ValueError(f"Unknown planet {planet!r}; known: {sorted(PLANET_RADIUS_KM)}") from None
        if cell_side_km <= 0:
            raise ValueError("cell_side_km must be positive")
        ideal = math.sqrt(4 * math.pi * radius**2 / (cell_side_km**2) / 12)
        return cls(planet=planet.lower(), radius_km=radius, nside=max(1, round(ideal)))

    # ------------------------------------------------------------------ geometry
    @property
    def npix(self) -> int:
        return int(hp.nside2npix(self.nside))

    @property
    def cell_area_km2(self) -> float:
        return 4 * math.pi * self.radius_km**2 / self.npix

    @property
    def cell_side_km(self) -> float:
        return math.sqrt(self.cell_area_km2)

    # ------------------------------------------------------------------ lookup
    def cell_of(self, lon_deg: ArrayLike, lat_deg: ArrayLike) -> IntArray:
        """Cell id for each (lon, lat). Shapes broadcast; returns int64."""
        lon = np.asarray(lon_deg, dtype=np.float64)
        lat = np.asarray(lat_deg, dtype=np.float64)
        if np.any(np.abs(lat) > 90):
            raise ValueError("latitude must be within [-90, 90]")
        return np.asarray(hp.ang2pix(self.nside, lon, lat, nest=False, lonlat=True), dtype=np.int64)

    def centers(self, cells: ArrayLike | None = None) -> tuple[FloatArray, FloatArray]:
        """(lon, lat) of cell centres, lon in 0-360. All cells when ``cells`` is None."""
        ids = np.arange(self.npix) if cells is None else np.asarray(cells, dtype=np.int64)
        self._check(ids)
        lon, lat = hp.pix2ang(self.nside, ids, nest=False, lonlat=True)
        return np.asarray(lon, dtype=np.float64), np.asarray(lat, dtype=np.float64)

    def neighbors(self, cells: ArrayLike) -> IntArray:
        """The 8 surrounding cells for each id, shape (n, 8); -1 where a neighbour does not exist."""
        ids = np.atleast_1d(np.asarray(cells, dtype=np.int64))
        self._check(ids)
        out = hp.get_all_neighbours(self.nside, ids, nest=False)  # (8, n)
        return np.asarray(out, dtype=np.int64).T

    def cell_boundary(self, cell: int, step: int = 2) -> tuple[FloatArray, FloatArray]:
        """Closed ring (lon, lat) around a cell, longitudes unwrapped so the ring never jumps 360.

        Ready to drop into GeoJSON. ``step`` is points per side (2 = a smooth-ish diamond).
        """
        self._check(np.asarray([cell]))
        vec = hp.boundaries(self.nside, int(cell), step=step, nest=False)  # (3, 4*step)
        lon, lat = hp.vec2ang(vec.T, lonlat=True)
        lon = np.asarray(lon, dtype=np.float64)
        centre_lon = float(self.centers([cell])[0][0])
        lon = centre_lon + (lon - centre_lon + 180.0) % 360.0 - 180.0
        lat = np.asarray(lat, dtype=np.float64)
        return np.append(lon, lon[0]), np.append(lat, lat[0])

    # ------------------------------------------------------------------ regions
    def cells_within(self, lon_deg: float, lat_deg: float, radius_km: float) -> IntArray:
        """Cells whose centre lies within ``radius_km`` (great-circle) of a point.

        This is how a benchmark site becomes a *region*: known analog sites are often smaller than
        one 9 km cell, so use a radius of a few km up to ~30 km (Haughton Crater is ~23 km across).
        """
        if radius_km < 0:
            raise ValueError("radius_km must be non-negative")
        vec = hp.ang2vec(lon_deg, lat_deg, lonlat=True)
        found = hp.query_disc(self.nside, vec, radius_km / self.radius_km, inclusive=False, nest=False)
        if found.size == 0:  # radius smaller than a cell: fall back to the cell containing the point
            found = self.cell_of(np.array([lon_deg]), np.array([lat_deg]))
        return np.asarray(found, dtype=np.int64)

    def cells_in_box(self, west: float, east: float, south: float, north: float) -> IntArray:
        """Cells whose centre lies in a lon/lat box (west < east, degrees east; small regions only).

        Uses the box's four corners as a spherical polygon, so keep it well under a hemisphere.
        """
        if not (west < east and south < north):
            raise ValueError("expected west < east and south < north")
        corners = np.array([(west, south), (east, south), (east, north), (west, north)], dtype=np.float64)
        vec = np.array([hp.ang2vec(lo, la, lonlat=True) for lo, la in corners])
        found = hp.query_polygon(self.nside, vec, inclusive=False, nest=False)
        return np.asarray(found, dtype=np.int64)

    # ------------------------------------------------------------------ internals
    def _check(self, ids: IntArray) -> None:
        if ids.size and (ids.min() < 0 or ids.max() >= self.npix):
            raise ValueError(f"cell id out of range for nside={self.nside} (0..{self.npix - 1})")


def aggregate(
    cell_ids: ArrayLike,
    values: ArrayLike,
    how: Stat = "mean",
    weights: ArrayLike | None = None,
) -> tuple[IntArray, FloatArray]:
    """Reduce per-pixel values to one number per occupied cell.

    ``cell_ids`` and ``values`` are flat arrays of equal length (use ``.ravel()`` on rasters).
    Non-finite values are ignored by every statistic except ``count`` and ``valid_fraction``,
    which report data coverage. Returns ``(cells, stat)`` for occupied cells, sorted by id.

    * ``mean``            weighted mean (use ``weights=cos(lat)`` for a lat/lon raster)
    * ``count``           number of source pixels that landed in the cell
    * ``valid_fraction``  finite pixels / all pixels (the "data coverage" category)
    * ``median``/``min``/``max``
    For "fraction of pixels with slope <= 5 deg", aggregate ``(slope <= 5).astype(float)`` with
    ``mean`` over the valid mask (set invalid slope pixels to NaN first).
    """
    ids = np.asarray(cell_ids, dtype=np.int64).ravel()
    vals = np.asarray(values, dtype=np.float64).ravel()
    if ids.shape != vals.shape:
        raise ValueError("cell_ids and values must have the same number of elements")
    w = None if weights is None else np.asarray(weights, dtype=np.float64).ravel()
    if w is not None and w.shape != vals.shape:
        raise ValueError("weights must match values")

    cells, inverse = np.unique(ids, return_inverse=True)
    n = cells.size
    finite = np.isfinite(vals)

    if how == "count":
        return cells, np.bincount(inverse, minlength=n).astype(np.float64)
    if how == "valid_fraction":
        total = np.bincount(inverse, minlength=n)
        good = np.bincount(inverse, weights=finite.astype(np.float64), minlength=n)
        return cells, good / total
    if how == "mean":
        ww = np.where(finite, 1.0 if w is None else w, 0.0)
        num = np.bincount(inverse, weights=np.where(finite, vals, 0.0) * ww, minlength=n)
        den = np.bincount(inverse, weights=ww, minlength=n)
        with np.errstate(invalid="ignore", divide="ignore"):
            return cells, np.where(den > 0, num / den, np.nan)
    if how == "median":
        return quantile(ids, vals, 0.5)
    if how in ("min", "max"):
        return quantile(ids, vals, 0.0 if how == "min" else 1.0)
    raise ValueError(f"Unknown statistic {how!r}")


def quantile(cell_ids: ArrayLike, values: ArrayLike, q: float) -> tuple[IntArray, FloatArray]:
    """Per-cell quantile (linear interpolation), ignoring non-finite values. ``q`` in [0, 1]."""
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be within [0, 1]")
    ids = np.asarray(cell_ids, dtype=np.int64).ravel()
    vals = np.asarray(values, dtype=np.float64).ravel()
    cells, inverse = np.unique(ids, return_inverse=True)
    n = cells.size
    out = np.full(n, np.nan)
    finite = np.isfinite(vals)
    order = np.lexsort((vals[finite], inverse[finite]))
    sv, si = vals[finite][order], inverse[finite][order]
    if sv.size:
        starts = np.searchsorted(si, np.arange(n), side="left")
        ends = np.searchsorted(si, np.arange(n), side="right")
        has = ends > starts
        pos = starts[has] + q * (ends[has] - starts[has] - 1)
        lo = np.floor(pos).astype(np.int64)
        hi = np.ceil(pos).astype(np.int64)
        frac = pos - lo
        out[has] = sv[lo] * (1 - frac) + sv[hi] * frac
    return cells, out


def raster_cells(grid: EqualAreaGrid, lons: ArrayLike, lats: ArrayLike) -> IntArray:
    """Cell id for every pixel of a regular lon/lat raster, shape (len(lats), len(lons)).

    ``lons`` and ``lats`` are 1-D pixel-centre coordinates in degrees. Process large rasters in
    row blocks: the result is one int64 per pixel.
    """
    lo = np.asarray(lons, dtype=np.float64)
    la = np.asarray(lats, dtype=np.float64)
    lon2d, lat2d = np.meshgrid(lo, la)
    return grid.cell_of(lon2d, lat2d)
