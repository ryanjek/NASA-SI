"""Step 6, on the cloud machine: fetch Copernicus GLO-90 terrain for each hand-off region and measure it at 1 km.

    python fine_terrain.py <regions.csv> <tile folder> <out.csv>

Needs only numpy and rasterio. Tiles come from the public Copernicus DEM bucket on AWS (no account).
A tile that does not exist (open sea) is skipped and counted. For each region, a 59 km square around
the centre is averaged to about 1 km pixels (area averaging, never nearest neighbour), and slope and relief
are measured there: the inputs the team's 1 km stage expects.

Code written with Claude (Anthropic); see twin/README.md.
"""

from __future__ import annotations

import csv
import math
import sys
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import rasterio
from rasterio.merge import merge

BUCKET = "https://copernicus-dem-90m.s3.amazonaws.com"
HALF_WINDOW_KM = 29.5
TARGET_PIXEL_M = 1000.0
EARTH_RADIUS_M = 6_371_008.8


def tile_name(lat: int, lon: int) -> str:
    ns, ew = ("N" if lat >= 0 else "S"), ("E" if lon >= 0 else "W")
    return f"Copernicus_DSM_COG_30_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


def fetch(lat: int, lon: int, folder: Path) -> Path | None:
    name = tile_name(lat, lon)
    target = folder / f"{name}.tif"
    if target.exists():
        return target
    try:
        urllib.request.urlretrieve(f"{BUCKET}/{name}/{name}.tif", target.with_suffix(".part"))
    except urllib.error.HTTPError as error:
        if error.code in (403, 404):
            return None
        raise
    target.with_suffix(".part").rename(target)
    return target


def window_tiles(lat: float, lon: float) -> list[tuple[int, int]]:
    dlat = math.degrees(HALF_WINDOW_KM * 1000 / EARTH_RADIUS_M)
    dlon = dlat / max(math.cos(math.radians(lat)), 0.05)
    lats = range(math.floor(lat - dlat), math.floor(lat + dlat) + 1)
    lons = range(math.floor(lon - dlon), math.floor(lon + dlon) + 1)
    return [(a, ((b + 180) % 360) - 180) for a in lats for b in lons]


def measure(paths: list[Path], lat: float, lon: float) -> dict:
    dlat = math.degrees(HALF_WINDOW_KM * 1000 / EARTH_RADIUS_M)
    dlon = dlat / max(math.cos(math.radians(lat)), 0.05)
    sources = [rasterio.open(p) for p in paths]
    try:
        mosaic, transform = merge(sources, bounds=(lon - dlon, lat - dlat, lon + dlon, lat + dlat))
    finally:
        for source in sources:
            source.close()
    heights = mosaic[0].astype(np.float64)
    sea_share = float((heights == 0).mean())  # Copernicus DEM stores the sea as exactly 0 m, with no nodata
    pixel_north_m = abs(transform.e) * math.pi / 180 * EARTH_RADIUS_M
    pixel_east_m = abs(transform.a) * math.pi / 180 * EARTH_RADIUS_M * math.cos(math.radians(lat))
    rows_per, cols_per = max(1, round(TARGET_PIXEL_M / pixel_north_m)), max(1, round(TARGET_PIXEL_M / pixel_east_m))
    r, c = heights.shape[0] // rows_per * rows_per, heights.shape[1] // cols_per * cols_per
    km = heights[:r, :c].reshape(r // rows_per, rows_per, c // cols_per, cols_per).mean(axis=(1, 3))
    north, east = np.gradient(km, rows_per * pixel_north_m, cols_per * pixel_east_m)
    slope = np.degrees(np.arctan(np.hypot(north, east)))
    return {
        "fine_pixels": km.size, "sea_share": round(sea_share, 3), "slope_median_deg": round(float(np.median(slope)), 2),
        "slope_p95_deg": round(float(np.percentile(slope, 95)), 2), "flat_share_lt2deg": round(float((slope < 2).mean()), 3),
        "relief_p2_p98_m": round(float(np.percentile(km, 98) - np.percentile(km, 2)), 0), "mean_height_m": round(float(km.mean()), 0),
    }  # fmt: skip


def main() -> None:
    regions, folder, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    folder.mkdir(parents=True, exist_ok=True)
    with regions.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    missing = 0
    for row in rows:
        lat, lon = float(row["lat"]), float(row["lon"])
        paths = [p for p in (fetch(a, b, folder) for a, b in window_tiles(lat, lon)) if p is not None]
        missing += len(window_tiles(lat, lon)) - len(paths)
        row.update(measure(paths, lat, lon) if paths else {"fine_pixels": 0})
        print(row["reason"], row["mars_target"], lat, lon, {k: row.get(k) for k in ("slope_median_deg", "relief_p2_p98_m")}, flush=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"done: {len(rows)} regions; {missing} tiles absent from the bucket (open sea)")


if __name__ == "__main__":
    main()
