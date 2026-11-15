"""Step 6: choose the Earth regions to hand to the team's fine (1 km) stage.

    python -m twin.handoff --data <folder> --out <regions.csv>

Regions are (a) the most Mars-like Earth places overall, at least MIN_SEPARATION_KM apart, and (b) the
best twin of each headline Mars target. Each row carries the coarse factor values, so the fine stage can
check them against its own 1 km numbers.

Code written with Claude (Anthropic); see twin/README.md.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import numpy as np

from .search import load_places
from .validate import _distance_km, _row

TOP_REGIONS = 12
MIN_SEPARATION_KM = 500.0
MIN_LAND_SHARE = 0.9
MARS_TARGETS = {
    "Jezero": (77.5, 18.4), "Gale": (137.8, -5.4), "Arcadia Planitia": (-176.0, 47.0),
    "Utopia Planitia": (110.0, 45.0), "Valles Marineris": (-70.0, -10.0), "Olympus Mons": (-133.8, 18.6),
}  # fmt: skip


def distinct_best(score: np.ndarray, lon: np.ndarray, lat: np.ndarray, count: int) -> list[int]:
    chosen: list[int] = []
    for index in np.argsort(-score):
        if all(_distance_km(lon[index], lat[index], lon[c], lat[c]) > MIN_SEPARATION_KM for c in chosen):
            chosen.append(int(index))
        if len(chosen) == count:
            break
    return chosen


def regions(data: Path) -> list[dict]:
    mars, earth = load_places(data)
    twins = np.load(data / "twins.npz")
    likeness = twins["earth_likeness"]
    inland = np.where(earth["quality"] >= MIN_LAND_SHARE, likeness, -np.inf)  # half-sea windows look flat for the wrong reason
    picks = [("most Mars-like", index, "") for index in distinct_best(inland, earth["lon"], earth["lat"], TOP_REGIONS)]
    for target, (lon, lat) in MARS_TARGETS.items():
        picks.append(("best twin", int(twins["twin"][_row(mars, "mars", lon, lat), 0]), target))
    rows = []
    for reason, index, target in picks:
        best_mars = int(twins["earth_best_mars"][index])
        rows.append({
            "reason": reason, "mars_target": target,
            "lon": round(float((earth["lon"][index] + 180) % 360 - 180), 3), "lat": round(float(earth["lat"][index]), 3),
            "mars_likeness": round(float(likeness[index]), 3), "land_share": round(float(earth["quality"][index]), 2),
            "closest_mars_lon": round(float((mars["lon"][best_mars] + 180) % 360 - 180), 2), "closest_mars_lat": round(float(mars["lat"][best_mars]), 2),
            "albedo": round(float(earth["albedo"][index, 0]), 3), "mean_temp_k": round(float(earth["thermal"][index, 0]), 1),
            "ground_ice": round(float(earth["ice"][index, 0]), 2), "peak_ndvi": round(float(earth["vegetation"][index, 0]), 2),
            "relief_m": round(float(10 ** earth["shape"][index, 1] - 1), 0),
        })  # fmt: skip
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    rows = regions(args.data)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    for row in rows:
        print(row)


if __name__ == "__main__":
    main()
