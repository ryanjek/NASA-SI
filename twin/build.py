"""Step 1: measure terrain shape and the other factors at every place on a planet.

    python -m twin.build mars  --data <folder>
    python -m twin.build earth --data <folder> --earth-raw <folder of the shared Earth layers>

Writes ``<data>/places_<planet>.npz`` and, with ``--tiles``, the height tiles the shape network trains on.

Code written with Claude (Anthropic); see twin/README.md.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from . import shape
from .factors import earth_factors, mars_factors
from .planets import PLACE_NSIDE, RADIUS_M, TILE_SPACING_M, Grid, read_etopo, read_mola, read_water_mask, smooth_east_west
from .tiles import place_centres, sample, tile_lonlat

CHUNK = 8192
MIN_LAND_SHARE = 0.5
FACTOR_STEP = 4  # factors are read at every fourth tile pixel: an 8 x 8 grid, 7.4 km apart
MOLA_HEIGHTS = "raw/mars/megt90n000fb.img"
MOLA_COUNTS = "raw/mars/megc90n000fb.img"
ETOPO_SURFACE = "raw/earth/ETOPO_2022_v1_60s_N90W180_surface.nc"


def load_planet(planet: str, data: Path, earth_raw: Path | None) -> tuple[Grid, Grid, dict[str, list[Grid]]]:
    """The height grid, a 0-1 quality grid (Mars: has a laser shot; Earth: is land) and the factor grids."""
    if planet == "mars":
        counts = read_mola(data / MOLA_COUNTS)
        observed = Grid(counts.values > 0, counts.pixels_per_degree, counts.lon_west)
        return smooth_east_west(read_mola(data / MOLA_HEIGHTS)), observed, mars_factors(data)
    water = read_water_mask(next((earth_raw / "land-cover").glob("*.hdf")), data / ETOPO_SURFACE)
    land = Grid(~water.values, water.pixels_per_degree, water.lon_west)
    return smooth_east_west(read_etopo(data / ETOPO_SURFACE, water)), land, earth_factors(earth_raw)


def _tile_mean(layer: Grid, lon: NDArray, lat: NDArray) -> NDArray:
    """Mean of a factor over each tile, ignoring missing pixels; NaN where the whole tile is missing."""
    values = sample(layer, lon, lat, order=1 if layer.pixels_per_degree < 1 else 0)
    present = np.isfinite(values)
    count = present.sum(axis=(1, 2))
    return np.where(count > 0, np.where(present, values, 0.0).sum(axis=(1, 2)) / np.maximum(count, 1), np.nan)


def measure(planet: str, heights: Grid, quality: Grid, factors: dict[str, list[Grid]], keep_tiles: bool) -> dict[str, NDArray]:
    lon, lat = place_centres(PLACE_NSIDE[planet])
    columns = defaultdict(list)
    for start in range(0, lon.size, CHUNK):
        tile_lon, tile_lat = tile_lonlat(lon[start : start + CHUNK], lat[start : start + CHUNK], RADIUS_M[planet])
        tiles = sample(heights, tile_lon, tile_lat)
        mask = sample(quality, tile_lon, tile_lat, order=0)
        columns["shape"].append(shape.describe(tiles, TILE_SPACING_M))
        columns["height"].append(tiles.mean(axis=(1, 2)))
        columns["quality"].append(mask.mean(axis=(1, 2)))
        coarse = tile_lon[:, FACTOR_STEP // 2 :: FACTOR_STEP, FACTOR_STEP // 2 :: FACTOR_STEP], tile_lat[:, FACTOR_STEP // 2 :: FACTOR_STEP, FACTOR_STEP // 2 :: FACTOR_STEP]
        for name, layers in factors.items():
            columns[name].append(np.stack([_tile_mean(layer, *coarse) for layer in layers], axis=1).astype(np.float32))
        if keep_tiles:
            columns["tiles"].append(np.rint(tiles).astype(np.int16))
            columns["masks"].append(np.packbits(mask > 0.5, axis=-1))
    return {"lon": lon, "lat": lat} | {name: np.concatenate(parts) for name, parts in columns.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("planet", choices=sorted(PLACE_NSIDE))
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--earth-raw", type=Path, help="folder holding land-cover/, albedo/, temperature-aqua/, vegetation/, permafrost/")
    parser.add_argument("--tiles", action="store_true", help="also save the height tiles and their quality masks")
    args = parser.parse_args()
    if args.planet == "earth" and args.earth_raw is None:
        parser.error("earth needs --earth-raw")

    places = measure(args.planet, *load_planet(args.planet, args.data, args.earth_raw), args.tiles)
    count = places["lon"].size
    if args.planet == "mars":
        places["vegetation"] = np.zeros((count, 1), np.float32)
    keep = places["quality"] >= MIN_LAND_SHARE if args.planet == "earth" else np.ones(count, bool)
    for name in ("tiles", "masks"):
        if name in places:
            np.save(args.data / f"{name}_{args.planet}.npy", places.pop(name)[keep])
    np.savez_compressed(
        args.data / f"places_{args.planet}.npz",
        place=np.flatnonzero(keep),
        names=np.array(shape.NAMES),
        **{key: value[keep] for key, value in places.items()},
    )
    print(f"{args.planet}: {int(keep.sum())} places kept of {count}")
    for name in ("albedo", "thermal", "ice", "vegetation"):
        missing = ~np.isfinite(places[name][keep]).all(axis=1)
        print(f"  {name}: missing at {int(missing.sum())} places ({missing.mean():.1%})")


if __name__ == "__main__":
    main()
