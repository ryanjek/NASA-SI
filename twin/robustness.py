"""Extra checks, run after the frozen ones: tolerance sensitivity and the SpaceX positive control.

    python -m twin.robustness --data <folder>

Sensitivity scales every tolerance by 0.5 and 2 and reports how the known pairs and the top-10 twins
move. It describes the method; it does not change it. The SpaceX control asks whether the Earth twins
of the eight HiRISE locations imaged as SpaceX Starship candidates (chosen for shallow ground ice on flat,
low ground) are more often flat and icy than the twins of random Mars places at the same latitudes.

Code written with Claude (Anthropic); see twin/README.md.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch

from . import search
from .validate import PAIRS, _distance_km, _percentile_of_best, _row

SCALES = (0.5, 2.0)
SAMPLE_QUERIES = 1000
SPACEX_SITES = [(40.0, 203.35), (39.9, 192.0), (39.75, 195.55), (39.2, 195.6), (39.1, 196.7), (39.1, 189.8), (38.6, 190.2), (39.3, 199.3)]  # lat, lon E
ICE_MIN, FLAT_MIN = 0.5, 0.5
CONTROL_BAND = (30.0, 50.0)


def _run_at_scale(mars: dict, earth: dict, rows: np.ndarray, scale: float) -> tuple[dict, np.ndarray]:
    base, neighbour = dict(search.TOLERANCES), search.NEIGHBOUR_FACTOR
    try:
        search.TOLERANCES.update({k: None if v is None else tuple(t * scale for t in v) for k, v in base.items()})
        search.NEIGHBOUR_FACTOR = neighbour * scale
        families = [search.make_family(name, mars, earth) for name in sorted(base)]
    finally:
        search.TOLERANCES.update(base)
        search.NEIGHBOUR_FACTOR = neighbour
    with torch.no_grad():
        tops = np.stack([torch.topk(search.fused_similarity(families, slice(r, r + 1))[0], 10).indices.cpu().numpy() for r in rows])
        pairs = {}
        for name, (mars_lon, mars_lat, lon, lat, radius) in PAIRS.items():
            r = _row(mars, "mars", mars_lon, mars_lat)
            scores = search.fused_similarity(families, slice(r, r + 1))[0].cpu().numpy()
            pairs[name] = round(_percentile_of_best(scores, _distance_km(earth["lon"], earth["lat"], lon, lat) <= radius), 3)
    return pairs, tops


def sensitivity(mars: dict, earth: dict) -> dict:
    rows = np.random.default_rng(7).choice(len(mars["lon"]), SAMPLE_QUERIES, replace=False)
    default_pairs, default_tops = _run_at_scale(mars, earth, rows, 1.0)
    out = {"1.0": {"pairs": default_pairs}}
    for scale in SCALES:
        pairs, tops = _run_at_scale(mars, earth, rows, scale)
        overlap = np.mean([len(set(a) & set(b)) / 10 for a, b in zip(default_tops, tops)])
        out[str(scale)] = {"pairs": pairs, "top10_overlap_with_default": round(float(overlap), 3)}
    return out


def spacex_control(data: Path, mars: dict, earth: dict) -> dict:
    twins = np.load(data / "twins.npz")
    flat = earth["shape"][:, list(earth["names"]).index("flat_share")]
    flat_and_icy = (earth["ice"][:, 0] >= ICE_MIN) & (flat >= FLAT_MIN)
    share = lambda rows: float(flat_and_icy[twins["twin"][rows, :10]].mean())  # noqa: E731
    sites = [_row(mars, "mars", (lon + 180) % 360 - 180, lat) for lat, lon in SPACEX_SITES]
    band = np.flatnonzero((mars["lat"] >= CONTROL_BAND[0]) & (mars["lat"] <= CONTROL_BAND[1]))
    control = np.random.default_rng(3).choice(band, SAMPLE_QUERIES, replace=False)
    out = {
        "spacex_share": round(share(sites), 3),
        "per_site": [round(share([r]), 2) for r in sites],
        "control_share": round(share(control), 3),
        "earth_base_rate": round(float(flat_and_icy.mean()), 3),
    }
    out["passes"] = out["spacex_share"] > out["control_share"]
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    mars, earth = search.load_places(args.data)
    result = {"spacex_control": spacex_control(args.data, mars, earth), "sensitivity": sensitivity(mars, earth)}
    (args.data / "robustness.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
