"""Step 5: write the static viewer's data and maps from the search result.

    python -m twin.export --data <folder> --out twin/viewer

The viewer works from a 1 degree Mars grid (about 59 km, the tile size): each grid cell takes the
twins of the nearest searched Mars place. Everything is written into one ``data.js`` file so the page
opens from disk with a double click, with no server.

Code written with Claude (Anthropic); see twin/README.md.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

import astropy.units as u
import matplotlib.pyplot as plt
import numpy as np
from astropy_healpix import HEALPix
from numpy.typing import NDArray

from .build import MOLA_HEIGHTS
from .planets import PLACE_NSIDE, read_mola
from .tiles import sample

VIEWER_TWINS = 10
MAP_WIDTH = 1440
FACTOR_COLUMNS = {  # viewer label -> (key in places_*.npz, column)
    "Albedo": ("albedo", 0),
    "Mean temperature (K)": ("thermal", 0),
    "Warmest month (K)": ("thermal", 1),
    "Coldest month (K)": ("thermal", 2),
    "Ground-ice indicator": ("ice", 0),
    "Peak NDVI": ("vegetation", 0),
    "Relief (m)": ("shape", 1),
    "Flat share": ("shape", 10),
    "Day-night range (K)": ("day_night_range", 0),
}


def _encode(values: NDArray) -> str:
    return base64.b64encode(np.ascontiguousarray(values).tobytes()).decode("ascii")


def _factor_table(places: dict) -> NDArray:
    columns = [places[key][:, column] for key, column in FACTOR_COLUMNS.values()]
    table = np.stack(columns, axis=1).astype(np.float32)
    table[:, 6] = 10 ** table[:, 6] - 1.0  # relief is stored as log10(metres + 1)
    return table


def _map_lonlat(width: int) -> tuple[NDArray, NDArray]:
    height = width // 2
    lon = (np.arange(width) + 0.5) * 360.0 / width - 180.0
    lat = 90.0 - (np.arange(height) + 0.5) * 180.0 / height
    return np.meshgrid(lon, lat)


def _rows_for(lon: NDArray, lat: NDArray, nside: int, place: NDArray) -> NDArray:
    """Row in ``place`` of the HEALPix cell holding each point, or -1 where that cell was not kept."""
    cell = HEALPix(nside=nside, order="nested").lonlat_to_healpix(lon.ravel() * u.deg, lat.ravel() * u.deg)
    row = np.searchsorted(place, cell).clip(max=place.size - 1)
    return np.where(place[row] == cell, row, -1).reshape(lon.shape)


def _hillshade(heights: NDArray) -> NDArray:
    north, east = np.gradient(heights.astype(np.float32))
    return np.clip(0.75 + (east - north) / 4000.0, 0.35, 1.0)


def write_maps(data: Path, out: Path, mars: dict, earth: dict, twins: dict) -> None:
    lon, lat = _map_lonlat(MAP_WIDTH)
    gap_row = _rows_for(lon, lat, PLACE_NSIDE["mars"], twins["mars_place"])
    gap = np.where(gap_row >= 0, 1.0 - twins["best_similarity"][gap_row], np.nan)
    shade = _hillshade(sample(read_mola(data / MOLA_HEIGHTS), lon, lat))
    gap_rgb = plt.get_cmap("magma_r")(np.clip((gap - 0.2) / 0.25, 0, 1))[..., :3]
    gap_rgb[np.isnan(gap)] = 0.5
    plt.imsave(out / "mars_gap.png", gap_rgb * shade[..., None])

    earth_row = _rows_for(lon, lat, PLACE_NSIDE["earth"], twins["earth_place"])
    likeness = np.where(earth_row >= 0, twins["earth_likeness"][earth_row], np.nan)
    like_rgb = plt.get_cmap("viridis")(np.clip((likeness - 0.45) / 0.35, 0, 1))[..., :3]
    like_rgb[np.isnan(likeness)] = (0.12, 0.14, 0.18)
    plt.imsave(out / "earth_likeness.png", like_rgb)


def viewer_data(mars: dict, earth: dict, twins: dict) -> dict:
    lon, lat = np.meshgrid(np.arange(360) - 179.5, 89.5 - np.arange(180))
    # Every grid cell takes the searched Mars place at its centre, or the nearest one along the row.
    row = _rows_for(lon, lat, PLACE_NSIDE["mars"], twins["mars_place"]).ravel()
    present = row >= 0
    row = np.where(present, row, row[np.maximum.accumulate(np.where(present, np.arange(row.size), 0))])

    twin = twins["twin"][row, :VIEWER_TWINS]
    used, earth_index = np.unique(twin, return_inverse=True)
    mars_rows = np.searchsorted(mars["place"], twins["mars_place"][row])
    return {
        "families": [str(name) for name in twins["families"]],
        "factors": list(FACTOR_COLUMNS),
        "twins": VIEWER_TWINS,
        "marsFactors": _encode(_factor_table(mars)[mars_rows]),
        "marsHasData": _encode(present.astype(np.uint8)),
        "twinIndex": _encode(earth_index.reshape(twin.shape).astype(np.int32)),
        "similarity": _encode(np.rint(twins["similarity"][row, :VIEWER_TWINS] * 1000).astype(np.uint16)),
        "familySimilarity": _encode(np.rint(twins["family_similarity"][row, :VIEWER_TWINS] * 100).astype(np.uint8)),
        "earthLon": _encode(((earth["lon"][used] + 180.0) % 360.0 - 180.0).astype(np.float32)),
        "earthLat": _encode(earth["lat"][used].astype(np.float32)),
        "earthFactors": _encode(_factor_table(earth)[used]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    mars, earth, twins = (dict(np.load(args.data / name)) for name in ("places_mars.npz", "places_earth.npz", "twins.npz"))
    args.out.mkdir(parents=True, exist_ok=True)
    write_maps(args.data, args.out, mars, earth, twins)
    payload = json.dumps(viewer_data(mars, earth, twins))
    (args.out / "data.js").write_text(f"window.TWIN_DATA = {payload};\n", encoding="ascii")
    print(f"wrote {args.out / 'data.js'} ({len(payload) / 1e6:.1f} MB) and two maps")


if __name__ == "__main__":
    main()
