"""Step 2: compare every place on Mars with every land place on Earth, on every factor family.

    python -m twin.search --data <folder>

A family is one kind of evidence (terrain shape, albedo, temperature, ...) stored under the same key in
``places_mars.npz`` and ``places_earth.npz``. Each family is put into tolerance units, so a distance of
1.0 means "one tolerance apart", and turned into a similarity ``exp(-distance^2 / 2)`` that saturates at
zero. A family in which Earth cannot match Mars anywhere therefore stops counting, instead of deciding
every match. The fused similarity is the weighted mean over families.

Code written with Claude (Anthropic); see twin/README.md.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from numpy.typing import NDArray

# Tolerance per column in native units, or None for "whiten, then use the neighbour rule".
TOLERANCES: dict[str, tuple[float, ...] | None] = {
    "shape": None,
    "albedo": (0.03,),
    "thermal": (5.0, 5.0, 5.0),  # kelvin: annual mean, warmest season, coldest season
    "ice": (0.5,),
    "vegetation": (0.15,),
}
NEIGHBOUR_FACTOR = 2.0  # shape tolerance = this many times the typical distance between adjacent places
KEPT_VARIANCE = 0.99
MIN_MARS_QUALITY = 0.5  # share of tile pixels holding a real laser shot
CANDIDATES = 64
TWINS = 20
HUB_NEIGHBOURS = 10
CHUNK = 512
GEOMETRIC_FLOOR = 1e-3  # keeps log finite; a factor at zero still pulls the match to about 0.001 ** share
DEVICE = torch.device("mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu")


@dataclass(frozen=True)
class Family:
    name: str
    mars: torch.Tensor  # (places, dims) in tolerance units; missing rows are zero
    earth: torch.Tensor
    mars_valid: torch.Tensor  # (places,) 1.0 where the family is measured
    earth_valid: torch.Tensor
    weight: float = 1.0


def _neighbour_distance(vectors: NDArray, place: NDArray) -> NDArray:
    """Distance from each place to its HEALPix sibling (ids differing in the last bit), where both exist."""
    sibling = np.searchsorted(place, place ^ 1)
    found = (sibling < place.size) & (place[np.minimum(sibling, place.size - 1)] == (place ^ 1))
    return np.linalg.norm(vectors[found] - vectors[sibling[found]], axis=1)


def _whiten(mars: NDArray, earth: NDArray) -> tuple[NDArray, NDArray]:
    """Robustly standardise both planets together, then decorrelate, keeping most of the variance."""
    pooled = np.concatenate([mars, earth])
    centre = np.nanmedian(pooled, axis=0)
    low, high = np.nanpercentile(pooled, (25, 75), axis=0)
    standard = np.clip((pooled - centre) / ((high - low) / 1.349), -6, 6)
    values, vectors = np.linalg.eigh(np.cov(standard[np.isfinite(standard).all(axis=1)], rowvar=False))
    order = np.argsort(values)[::-1]
    kept = order[: int(np.searchsorted(np.cumsum(values[order]) / values.sum(), KEPT_VARIANCE)) + 1]
    white = (standard - np.nanmean(standard, axis=0)) @ vectors[:, kept] / np.sqrt(values[kept])
    return white[: len(mars)], white[len(mars) :]


def make_family(name: str, mars: dict, earth: dict, weight: float = 1.0) -> Family:
    mars_values, earth_values = (np.asarray(p[name], dtype=np.float64).reshape(len(p["place"]), -1) for p in (mars, earth))
    tolerance = TOLERANCES[name]
    if tolerance is None:
        mars_values, earth_values = _whiten(mars_values, earth_values)
        gaps = np.concatenate([_neighbour_distance(mars_values, mars["place"]), _neighbour_distance(earth_values, earth["place"])])
        tolerance = NEIGHBOUR_FACTOR * np.nanmedian(gaps)
    tensors = []
    for values in (mars_values, earth_values):
        valid = np.isfinite(values).all(axis=1)
        scaled = np.where(valid[:, None], values / tolerance, 0.0)
        tensors += [torch.tensor(scaled, dtype=torch.float32, device=DEVICE), torch.tensor(valid, dtype=torch.float32, device=DEVICE)]
    return Family(name, tensors[0], tensors[2], tensors[1], tensors[3], weight)


def _family_similarity(family: Family, rows: slice) -> torch.Tensor:
    mars = family.mars[rows]
    squared = (mars**2).sum(1, keepdim=True) + (family.earth**2).sum(1) - 2 * mars @ family.earth.T
    return torch.exp(-0.5 * squared.clamp_min(0)) * family.earth_valid


def fused_similarity(families: list[Family], rows: slice, combine: str = "mean") -> torch.Tensor:
    """Weighted mean similarity. Missing on Earth scores zero; missing on Mars drops the family for that row."""
    weight = sum(f.weight * f.mars_valid[rows, None] for f in families)
    if combine == "geometric":  # any factor near zero vetoes the match
        logs = sum(f.weight * f.mars_valid[rows, None] * torch.log(_family_similarity(f, rows) + GEOMETRIC_FLOOR) for f in families)
        return torch.exp(logs / weight.clamp_min(1e-6))
    total = sum(f.weight * f.mars_valid[rows, None] * _family_similarity(f, rows) for f in families)
    return total / weight.clamp_min(1e-6)


def compare(families: list[Family], combine: str = "mean") -> dict[str, NDArray]:
    """All Mars places against all Earth places; keeps the best candidates in both directions."""
    mars_count, earth_count = families[0].mars.shape[0], families[0].earth.shape[0]
    similarity = np.empty((mars_count, CANDIDATES), np.float32)
    candidate = np.empty((mars_count, CANDIDATES), np.int64)
    earth_top = torch.full((HUB_NEIGHBOURS, earth_count), -1.0, device=DEVICE)
    earth_best_mars = torch.zeros(earth_count, dtype=torch.long, device=DEVICE)
    for start in range(0, mars_count, CHUNK):
        rows = slice(start, start + CHUNK)
        block = fused_similarity(families, rows, combine)
        best, index = torch.topk(block, CANDIDATES, dim=1)
        similarity[rows], candidate[rows] = best.cpu().numpy(), index.cpu().numpy()
        column_max, column_arg = block.max(dim=0)
        improved = column_max > earth_top[0]
        earth_best_mars[improved] = column_arg[improved] + start
        merged = torch.cat([earth_top, torch.topk(block, min(HUB_NEIGHBOURS, block.shape[0]), dim=0).values])
        earth_top = torch.topk(merged, HUB_NEIGHBOURS, dim=0).values
    earth_top = earth_top.cpu().numpy()

    # CSLS (Conneau et al. 2018): discount places that are close to everything on the other planet.
    score = 2 * similarity - similarity[:, :HUB_NEIGHBOURS].mean(axis=1, keepdims=True) - earth_top.mean(axis=0)[candidate]
    order = np.argsort(-score, axis=1)[:, :TWINS]
    return {
        "twin": np.take_along_axis(candidate, order, axis=1),
        "score": np.take_along_axis(score, order, axis=1),
        "similarity": np.take_along_axis(similarity, order, axis=1),
        "best_similarity": similarity[:, 0],
        "earth_likeness": earth_top[0],
        "earth_best_mars": earth_best_mars.cpu().numpy(),
    }


def twin_family_similarity(families: list[Family], twin: NDArray) -> NDArray:
    """Similarity of each kept pair in each family, shape (mars places, twins, families)."""
    index = torch.as_tensor(twin, device=DEVICE)
    per_family = [
        torch.exp(-0.5 * ((f.mars[:, None, :] - f.earth[index]) ** 2).sum(-1)) * f.earth_valid[index] * f.mars_valid[:, None]
        for f in families
    ]
    return torch.stack(per_family, dim=-1).cpu().numpy()


def load_places(data: Path) -> tuple[dict, dict]:
    """Both planets' place tables, with Mars places that have too few laser shots removed."""
    mars, earth = (dict(np.load(data / f"places_{planet}.npz")) for planet in ("mars", "earth"))
    observed = mars["quality"] >= MIN_MARS_QUALITY
    return {key: value[observed] if len(value) == observed.size else value for key, value in mars.items()}, earth


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--families", nargs="+", default=sorted(TOLERANCES), choices=sorted(TOLERANCES))
    parser.add_argument("--out", default="twins.npz")
    parser.add_argument("--combine", choices=("mean", "geometric"), default="mean", help="geometric: second version with a veto")
    args = parser.parse_args()

    mars, earth = load_places(args.data)
    families = [make_family(name, mars, earth) for name in args.families]
    result = compare(families, args.combine)
    np.savez_compressed(
        args.data / args.out,
        families=np.array(args.families),
        mars_place=mars["place"],
        earth_place=earth["place"],
        family_similarity=twin_family_similarity(families, result["twin"]),
        **result,
    )
    print(f"{len(mars['place'])} Mars places x {len(earth['place'])} Earth places on {DEVICE}; families: {args.families}")
    print("best similarity per Mars place, percentiles 5/50/95:", np.percentile(result["best_similarity"], (5, 50, 95)).round(3))


if __name__ == "__main__":
    main()
