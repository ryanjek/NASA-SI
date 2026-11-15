"""Compute the five rubric values for each front-end Mars site from the real Mars rasters.

    PYTHONPATH=../NASA-SI/src ../NASA-SI/.venv/bin/python scripts/compute_mars_values.py

Method (same as the repo's Arcadia screening, src/nasa_si/screening.py + model.py):
  * Cell: 10 x 10 MOLA pixels (1/64 deg, ~9 km) centred on the site; the repo's default --cell-pixels 10.
  * Elevation: median MOLA MEGDR topography (metres above the areoid) in the cell.
  * Flatness: share of the cell's pixels with slope <= 5 deg; slope from nasa_si.model.slope_degrees.
  * Dust: TES numeric Dust Cover Index, bilinear at each MOLA pixel centre, median; fill values (<= 0.85) dropped.
  * Ice: SWIM ice consistency, only where the SWIM Arcadia pilot raster covers the site; otherwise null.
  * Coverage: share of pixels valid in ALL layers (as in model.score_cells); null when a layer is missing entirely.

Only the bytes for each cell are fetched with HTTP range requests (MOLA tiles are 133 MB each, the DCI 66 MB).
Nothing is estimated or filled: a value that cannot be computed stays null and the UI shows "Unavailable".
Writes web/src/data/mars_values.json.
"""
from __future__ import annotations

import json
import math
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from nasa_si.model import ScreeningProfile, slope_degrees
from nasa_si.raster import DCI_FILL_VALUE, DCI_HEIGHT, DCI_LABEL_BYTES, DCI_WIDTH

MOLA_BASE = "https://pds-geosciences.wustl.edu/mgs/urn-nasa-pds-mgs_mola_topography_derived/meg064"
DCI_URL = "http://www.mars.asu.edu/~ruff/DCI/dci_lo_ice_dust_16ppd_shifted.pds"
SWIM_URL = "https://swim.psi.edu/output/PilotStudy/Arcadia/Composite/Ci.tif"
PPD = 64
TILE_W, TILE_H = 11520, 5760
MOLA_RADIUS_M = 3_396_000.0  # from the tile labels (a_axis_radius 3396.0 km)
CELL = 10
PROFILE = ScreeningProfile()
UA = "NASA-SI/0.1 (NASA Space Apps educational project)"

# Same ids and coordinates as web/src/data/marsSites.ts (planetocentric N, degrees east 0-360).
SITES = {
    "G-04": (68.2188, 234.2508),
    "N-01": (-5.4, 137.8),
    "N-02": (-14.57, 175.47),
    "N-03": (-1.95, 354.47),
    "N-04": (-8.3, 239.9),
}


def fetch(url: str, start: int, end: int) -> bytes:
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        if r.status != 206:
            raise RuntimeError(f"{url}: expected 206 Partial Content, got {r.status}")
        return r.read()


def mola_cell(lat: float, lon: float):
    """Elevation block of (CELL+2) x (CELL+2) pixels around the site, plus the pixel-centre latitudes/longitudes."""
    tile = ("megt90n" if lat >= 0 else "megt00n") + ("000" if lon < 180 else "180") + "gb.img"
    top = 90.0 if lat >= 0 else 0.0
    west = 0.0 if lon < 180 else 180.0
    row = int(math.floor((top - lat) * PPD))
    col = int(math.floor((lon - west) * PPD))
    r0, c0 = row - CELL // 2 - 1, col - CELL // 2 - 1
    n = CELL + 2
    if r0 < 0 or c0 < 0 or r0 + n > TILE_H or c0 + n > TILE_W:
        raise ValueError(f"cell at {lat},{lon} crosses a MOLA tile edge; not handled")
    raw = fetch(f"{MOLA_BASE}/{tile}", r0 * TILE_W * 2, (r0 + n) * TILE_W * 2 - 1)
    rows = np.frombuffer(raw, dtype=">i2").reshape(n, TILE_W)
    elev = rows[:, c0 : c0 + n].astype(np.float64)
    lats = top - (np.arange(r0, r0 + n) + 0.5) / PPD
    lons = west + (np.arange(c0, c0 + n) + 0.5) / PPD
    return tile, elev, lats, lons


def dci_at(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Bilinear TES DCI at each (lat, lon) pixel centre. Grid: 16 ppd, origin 180 W / 90 N, little-endian float32."""
    lon_w = np.where(lons > 180, lons - 360, lons)
    fr = (90.0 - lats) * 16 - 0.5  # fractional row of pixel centres
    fc = (lon_w + 180.0) * 16 - 0.5
    r0, r1 = int(np.floor(fr.min())), int(np.floor(fr.max())) + 1
    block = np.frombuffer(fetch(DCI_URL, DCI_LABEL_BYTES + r0 * DCI_WIDTH * 4, DCI_LABEL_BYTES + (r1 + 1) * DCI_WIDTH * 4 - 1), dtype="<f4")
    block = block.reshape(r1 - r0 + 1, DCI_WIDTH).astype(np.float64)
    block[~(np.isfinite(block) & (block > DCI_FILL_VALUE + 1e-6))] = np.nan
    out = np.full((len(lats), len(lons)), np.nan)
    for i, y in enumerate(fr):
        for j, x in enumerate(fc):
            y0, x0 = int(np.floor(y)) - r0, int(np.floor(x)) % DCI_WIDTH
            x1 = (x0 + 1) % DCI_WIDTH
            dy, dx = y - np.floor(y), x - np.floor(x)
            q = [block[y0, x0], block[y0, x1], block[y0 + 1, x0], block[y0 + 1, x1]]
            if not any(np.isnan(q)):  # a fill neighbour makes the pixel missing, like NaN nodata in the repo resample
                out[i, j] = q[0] * (1 - dx) * (1 - dy) + q[1] * dx * (1 - dy) + q[2] * (1 - dx) * dy + q[3] * dx * dy
    return out


def swim_at(lats: np.ndarray, lons: np.ndarray):
    """SWIM ice consistency at pixel centres if the Arcadia pilot raster covers them; else None."""
    import rasterio
    from nasa_si.grid import degree_transform

    with rasterio.open(f"/vsicurl/{SWIM_URL}") as src:
        t, _ = degree_transform(src.transform, src.crs)
        west, north = t.c, t.f
        east, south = west + src.width * t.a, north + src.height * t.e
        lon_e = np.where(lons < 0, lons + 360, lons)
        if not (south <= lats.min() and lats.max() <= north and west <= lon_e.min() and lon_e.max() <= east):
            return None, (west, east, south, north)
        pts = [((lo - west) / t.a, (north - la) / -t.e) for la in lats for lo in lon_e]
        rows = [int(p[1]) for p in pts]
        cols = [int(p[0]) for p in pts]
        win = rasterio.windows.Window(min(cols), min(rows), max(cols) - min(cols) + 1, max(rows) - min(rows) + 1)
        data = src.read(1, window=win, masked=True).astype(np.float64).filled(np.nan)
        vals = np.array([data[r - min(rows), c - min(cols)] for r, c in zip(rows, cols)]).reshape(len(lats), len(lons))
        return vals, (west, east, south, north)


def r(v, nd=4):
    return None if v is None or not np.isfinite(v) else round(float(v), nd)


def main(out: str = "web/src/data/mars_values.json") -> None:
    mpd = math.pi * MOLA_RADIUS_M / 180.0
    sites = {}
    for sid, (lat, lon) in SITES.items():
        tile, elev, lats, lons = mola_cell(lat, lon)
        slope = slope_degrees(elev, x_resolution_m=mpd / PPD * np.cos(np.radians(lats)), y_resolution_m=mpd / PPD)
        inner = (slice(1, -1), slice(1, -1))  # drop the 1-pixel margin used for the gradient
        e, s = elev[inner], slope[inner]
        clats, clons = lats[1:-1], lons[1:-1]
        dci = dci_at(clats, clons)
        swim, swim_bounds = swim_at(clats, clons)
        valid = np.isfinite(e) & np.isfinite(s) & np.isfinite(dci)
        layers_present = swim is not None
        if layers_present:
            valid &= np.isfinite(swim)
        sites[sid] = {
            "elevationM": r(np.nanmedian(e), 1),
            "flatFraction": r(np.mean(s <= PROFILE.flat_slope_deg)),
            "dci": r(np.nanmedian(dci)) if np.isfinite(dci).any() else None,
            "swimMedian": r(np.nanmedian(swim)) if swim is not None and np.isfinite(swim).any() else None,
            "coverage": r(valid.mean()) if layers_present else None,
            "slopeMedianDeg": r(np.median(s), 2),
            "slopeP95Deg": r(np.percentile(s, 95), 2),
            "dciValidFraction": r(np.isfinite(dci).mean(), 2),
            "molaTile": tile,
        }
        # Why each null is null, shown in the UI next to "Unavailable".
        missing = {}
        if swim is None:
            missing["swimMedian"] = f"outside SWIM coverage ({round(swim_bounds[2]) + 0}–{round(swim_bounds[3])}°N, {round(swim_bounds[0])}–{round(swim_bounds[1])}°E)"
            missing["coverage"] = "needs every layer; SWIM has no data here"
        if sites[sid]["dci"] is None:
            missing["dci"] = "no valid TES DCI pixels in this cell"
        sites[sid]["missing"] = missing
        print(sid, sites[sid])
    doc = {
        "computed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "10x10 MOLA pixel cell (1/64 deg, ~9 km) centred on each site; repo slope_degrees; flat = slope <= 5 deg; DCI bilinear at pixel centres, median; SWIM only where the Arcadia pilot covers the site.",
        "sources": {"mola": MOLA_BASE, "dci": DCI_URL, "swim": SWIM_URL},
        "sites": sites,
    }
    Path(out).write_text(json.dumps(doc, indent=1))
    print("wrote", out)


if __name__ == "__main__":
    main()
