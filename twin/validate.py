"""Step 3: checks that could fail, with the pairs and the pass rule fixed before the first run.

    python -m twin.validate --data <folder>

For each published Mars-Earth analogy, the Mars place is used as a query against every Earth land place,
and we record where the best Earth place inside the known region ranks (as a percentile; 0% is the top).
A pair passes if that place is in the top 5% and ranks better than under the trivial baseline.

Code written with Claude (Anthropic); see twin/README.md.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import astropy.units as u
import numpy as np
import torch
from astropy_healpix import HEALPix
from numpy.typing import NDArray

from .planets import PLACE_NSIDE, RADIUS_M
from .search import TOLERANCES, fused_similarity, load_places, make_family

PASS_PERCENTILE = 5.0
# name: (Mars lon, Mars lat, Earth lon, Earth lat, Earth radius km)
PAIRS = {
    "Phoenix -> McMurdo Dry Valleys": (-125.75, 68.22, 161.5, -77.6, 150),
    "Phoenix -> Elephant Head (held out)": (-125.75, 68.22, -83.31, -79.82, 100),
    "Utopia -> Central Yakutia thermokarst": (110.0, 45.0, 130.0, 62.0, 300),
    "Athabasca Valles -> Icelandic highland lavas": (157.0, 8.6, -16.7, 64.9, 150),
    "Ares Vallis -> Channeled Scablands": (-33.2, 19.1, -119.0, 47.2, 150),
    "Pavonis Mons flank -> Mauna Loa": (-113.0, 1.5, -155.6, 19.5, 150),
    "Olympia Undae -> Namib Sand Sea": (180.0, 81.0, 15.2, -24.5, 200),
}
NEGATIVE_CONTROLS = {"Amazon": (-60.0, -3.0), "Great Plains": (-100.0, 40.0), "Hudson Bay Lowlands": (-85.0, 55.0), "Congo": (22.0, 0.0)}
MATCHABLE = [n for n in sorted(TOLERANCES) if n != "thermal"]  # Mars is colder than almost all of Earth
VARIANTS = {"all factors": sorted(TOLERANCES), "four matchable factors": MATCHABLE, "shape only": ["shape"], "all but shape": [n for n in sorted(TOLERANCES) if n != "shape"]}


def _distance_km(lon: NDArray, lat: NDArray, lon0: float, lat0: float) -> NDArray:
    lam, phi, lam0, phi0 = map(np.radians, (lon, lat, lon0, lat0))
    cosine = np.sin(phi) * np.sin(phi0) + np.cos(phi) * np.cos(phi0) * np.cos(lam - lam0)
    return np.arccos(np.clip(cosine, -1.0, 1.0)) * RADIUS_M["earth"] / 1000.0


def _row(places: dict, planet: str, lon: float, lat: float) -> int:
    cell = int(HEALPix(PLACE_NSIDE[planet], order="nested").lonlat_to_healpix(lon * u.deg, lat * u.deg))
    return int(np.argmin(np.abs(places["place"] - cell)))


def _percentile_of_best(scores: NDArray, region: NDArray) -> float:
    best = scores[region].max()
    return float((scores > best).mean() * 100.0)


def trivial_baseline(earth: dict) -> NDArray:
    """Query-independent: colder and barer is more Mars-like. Missing temperature ranks last."""
    score = -earth["thermal"][:, 0] / 5.0 - earth["vegetation"][:, 0] / 0.15
    return np.nan_to_num(score, nan=-np.inf)


def run(data: Path, combine: str = "mean", twins_file: str = "twins.npz") -> dict:
    mars, earth = load_places(data)
    variants = {name: [make_family(f, mars, earth) for f in families] for name, families in VARIANTS.items()}
    baseline = trivial_baseline(earth)
    results = {}
    for name, (mars_lon, mars_lat, lon, lat, radius) in PAIRS.items():
        row = _row(mars, "mars", mars_lon, mars_lat)
        region = _distance_km(earth["lon"], earth["lat"], lon, lat) <= radius
        entry = {"places_in_region": int(region.sum()), "chance_percentile": round(100.0 / (region.sum() + 1), 3)}
        entry["trivial baseline"] = round(_percentile_of_best(baseline, region), 3)
        for variant, families in variants.items():
            with torch.no_grad():
                scores = fused_similarity(families, slice(row, row + 1), combine)[0].cpu().numpy()
            entry[variant] = round(_percentile_of_best(scores, region), 3)
        primary = entry["all factors" if combine == "mean" else "four matchable factors"]
        entry["passes"] = primary <= PASS_PERCENTILE and primary < entry["trivial baseline"]
        results[name] = entry
        print(f"{name:46s} {entry}")

    twins = np.load(data / twins_file)
    likeness = twins["earth_likeness"]
    controls = {}
    for name, (lon, lat) in NEGATIVE_CONTROLS.items():
        row = _row(earth, "earth", lon, lat)
        controls[name] = round(float((likeness < likeness[row]).mean() * 100.0), 1)
    print("negative controls, Mars-likeness percentile (must be below 50):", controls)
    return {"pairs": results, "negative_controls_percentile": controls, "pass_percentile": PASS_PERCENTILE}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", default="validation.json")
    parser.add_argument("--combine", choices=("mean", "geometric"), default="mean")
    parser.add_argument("--twins", default="twins.npz", help="search result that supplies the Mars-likeness map")
    args = parser.parse_args()
    (args.data / args.out).write_text(json.dumps(run(args.data, args.combine, args.twins), indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
