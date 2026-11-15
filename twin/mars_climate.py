"""Mars surface temperature from the Mars Climate Database web interface (LMD, v6.2).

    python -m twin.mars_climate --data <folder>

Temperatures are requested at 13:30 and 01:30 local time, the overpass times of the Aqua satellite,
so the Mars numbers are built the same way as the MODIS numbers for Earth. The database grid is
coarse (about 5.6 by 3.8 degrees); it is a climate model, not a measurement.

Code written with Claude (Anthropic); see twin/README.md.
"""

from __future__ import annotations

import argparse
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from .planets import Grid

SERVER = "http://www-mars.lmd.jussieu.fr/mcd_python"
LOCAL_TIMES = (13.5, 1.5)
SEASONS = tuple(range(15, 360, 30))  # middle of each of the twelve Mars months, in degrees of solar longitude
MONTH_SOLS = (61.2, 65.4, 66.7, 64.5, 59.7, 54.4, 49.7, 46.9, 46.1, 47.4, 50.9, 55.7)  # lengths of those months
FIXED_QUERY = {
    "var1": "tsurf", "var2": "none", "var3": "none", "var4": "none", "datekeyhtml": "1", "year": "2026", "month": "11",
    "day": "15", "hours": "9", "minutes": "0", "seconds": "0", "julian": "2461359.875", "martianyear": "38", "sol": "1",
    "latitude": "all", "longitude": "all", "altitude": "10.", "zkey": "3", "spacecraft": "none", "isfixedlt": "on",
    "dust": "1", "hrkey": "1", "averaging": "off", "zonmean": "off", "diumean": "off", "animation": "off",
    "animframes": "12", "dpi": "80", "islog": "off", "colorm": "jet", "minval": "", "maxval": "", "proj": "cyl",
    "palt": "", "plon": "", "plat": "", "trans": "", "iswind": "off", "latpoint": "", "lonpoint": "",
}  # fmt: skip


def _download(url: str) -> str:
    with urllib.request.urlopen(url, timeout=180) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch(folder: Path) -> None:
    """Save one text map per season and local time; maps already on disk are skipped."""
    folder.mkdir(parents=True, exist_ok=True)
    for season in SEASONS:
        for local_time in LOCAL_TIMES:
            target = folder / f"tsurf_ls{season:03d}_lt{local_time:04.1f}.txt"
            if target.exists():
                continue
            query = urllib.parse.urlencode(FIXED_QUERY | {"ls": season, "localtime": local_time})
            page = _download(f"{SERVER}/cgi-bin/mcdcgi.py?{query}")
            link = re.search(r"href='\.\./(txt/[0-9a-f]+\.txt)'", page)
            if link is None:
                raise RuntimeError(f"no data link in the reply for season {season}, local time {local_time}")
            target.write_text(_download(f"{SERVER}/{link.group(1)}"), encoding="utf-8")
            time.sleep(1.0)


def read_map(path: Path) -> tuple[NDArray, NDArray, NDArray]:
    """Latitudes, longitudes and temperatures (latitude x longitude) of one saved map."""
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]
    lat = np.array(lines[0].split("||")[1].split(), dtype=float)
    rows = np.array([line.replace("||", " ").split() for line in lines[2:] if line.strip()], dtype=float)
    return lat, rows[:, 0], rows[:, 1:].T


def thermal_grids(folder: Path) -> dict[str, Grid]:
    """Annual mean, warmest-season and coldest-season surface temperature, and the day-night range."""
    day, night = (
        np.stack([read_map(folder / f"tsurf_ls{season:03d}_lt{local_time:04.1f}.txt")[2] for season in SEASONS])
        for local_time in LOCAL_TIMES
    )
    lat, lon, _ = read_map(folder / f"tsurf_ls{SEASONS[0]:03d}_lt{LOCAL_TIMES[0]:04.1f}.txt")
    weight = np.array(MONTH_SOLS)[:, None, None] / sum(MONTH_SOLS)
    middle = (day + night) / 2
    layers = {
        "mean": (middle * weight).sum(axis=0),
        "warmest": middle.max(axis=0),
        "coldest": middle.min(axis=0),
        "range": ((day - night) * weight).sum(axis=0),
    }
    # Points run from -180 to 180 and from -90 to 90 inclusive; the printed longitudes are rounded,
    # so the steps come from the counts. The last longitude repeats the first and is dropped.
    north_first = slice(None, None, -1) if lat[0] < lat[-1] else slice(None)
    lon_step, lat_step = 360.0 / (lon.size - 1), 180.0 / (lat.size - 1)
    return {
        name: Grid(values[north_first, :-1].astype(np.float32), 1.0 / lon_step, -180.0 - lon_step / 2, 90.0 + lat_step / 2, 1.0 / lat_step)
        for name, values in layers.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    fetch(args.data / "raw/mars/mcd")
    print("saved", len(list((args.data / "raw/mars/mcd").glob("tsurf_*.txt"))), "maps")


if __name__ == "__main__":
    main()
