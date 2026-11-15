"""Step 4: a terrain-shape network shared by both planets, trained without labels (VICReg).

    python -m twin.network train --data <folder> --earth-raw <folder>
    python -m twin.network check --data <folder> --earth-raw <folder>

The network sees only elevation, in metres, at the same 1.85 km pixel on both planets. Earth tiles are
also shown through the gaps of a real Mars laser-shot mask, so the gaps cannot tell the planets apart.
It is kept only if it beats the hand-made shape numbers on the frozen checks (see validate.py).

Code written with Claude (Anthropic); see twin/README.md.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from numpy.typing import NDArray
from scipy.ndimage import distance_transform_edt
from torch import nn
from torchvision.models import resnet18

from . import shape
from .build import ETOPO_SURFACE, MOLA_COUNTS, MOLA_HEIGHTS
from .planets import RADIUS_M, TILE_SIZE, TILE_SPACING_M, Grid, read_etopo, read_mola, read_water_mask, smooth_east_west
from .search import DEVICE, load_places
from .tiles import sample, tile_lonlat
from .validate import PAIRS, _distance_km, _percentile_of_best, _row

VERSION = "terrain-vicreg-v1"
CUT = 40  # tiles are cut larger and cropped, which shifts the view by up to 4 pixels
EMBED, PROJECT = 128, 512
BATCH = 256
STEPS = 3000
SEED = 0


def load_grids(data: Path, earth_raw: Path) -> dict[str, tuple[Grid, Grid]]:
    counts = read_mola(data / MOLA_COUNTS)
    water = read_water_mask(next((earth_raw / "land-cover").glob("*.hdf")), data / ETOPO_SURFACE)
    return {
        "mars": (smooth_east_west(read_mola(data / MOLA_HEIGHTS)), Grid(counts.values > 0, counts.pixels_per_degree, counts.lon_west)),
        "earth": (smooth_east_west(read_etopo(data / ETOPO_SURFACE, water)), None),
    }


def cut(grids: dict, planet: str, lon: NDArray, lat: NDArray, size: int) -> tuple[NDArray, NDArray | None]:
    tile_lon, tile_lat = tile_lonlat(lon, lat, RADIUS_M[planet], size)
    heights, shots = grids[planet]
    mask = sample(shots, tile_lon, tile_lat, order=0) > 0.5 if shots is not None else None
    return sample(heights, tile_lon, tile_lat), mask


def through_mask(tiles: NDArray, masks: NDArray) -> NDArray:
    """Keep heights only where a Mars laser shot exists and fill the gaps from the nearest shot."""
    out = tiles.copy()
    for k, mask in enumerate(masks):
        if mask.any() and not mask.all():
            _, (rows, cols) = distance_transform_edt(~mask, return_indices=True)
            out[k] = tiles[k][rows, cols]
    return out


def to_input(tiles: NDArray) -> torch.Tensor:
    """Height relative to the tile median, through a signed log: relief size is kept, not normalised away."""
    relative = tiles - np.median(tiles, axis=(1, 2), keepdims=True)
    return torch.tensor(np.sign(relative) * np.log1p(np.abs(relative) / 10.0), dtype=torch.float32)[:, None]


def augment(tiles: NDArray, rng: np.random.Generator) -> NDArray:
    size = tiles.shape[-1]
    out = np.empty((len(tiles), TILE_SIZE, TILE_SIZE), np.float32)
    for k, tile in enumerate(tiles):
        r, c = rng.integers(0, size - TILE_SIZE + 1, 2)
        view = np.rot90(tile[r : r + TILE_SIZE, c : c + TILE_SIZE], rng.integers(4))
        out[k] = view[:, ::-1] if rng.random() < 0.5 else view
    return out + rng.normal(0.0, 1.0, out.shape).astype(np.float32)


class Encoder(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        backbone = resnet18(num_classes=EMBED)
        backbone.conv1 = nn.Conv2d(1, 64, 3, 1, 1, bias=False)  # 32 x 32 input: no early downsampling
        backbone.maxpool = nn.Identity()
        self.backbone = backbone
        self.projector = nn.Sequential(nn.Linear(EMBED, PROJECT), nn.BatchNorm1d(PROJECT), nn.ReLU(), nn.Linear(PROJECT, PROJECT))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.backbone(x)


def vicreg(a: torch.Tensor, b: torch.Tensor) -> torch.Tensor:
    """Bardes et al. 2022: invariance + variance + covariance, with their default weights 25, 25, 1."""
    invariance = F.mse_loss(a, b)
    variance = sum(F.relu(1.0 - torch.sqrt(z.var(dim=0) + 1e-4)).mean() for z in (a, b))
    covariance = 0.0
    for z in (a, b):
        z = z - z.mean(dim=0)
        cov = (z.T @ z) / (len(z) - 1)
        covariance = covariance + (cov.pow(2).sum() - cov.diagonal().pow(2).sum()) / z.shape[1]
    return 25.0 * invariance + 25.0 * variance + covariance


def _views(grids: dict, mars: dict, earth: dict, rng: np.random.Generator) -> tuple[torch.Tensor, torch.Tensor]:
    """Two views of BATCH/2 Mars and BATCH/2 Earth tiles; each Earth view borrows a random Mars shot mask."""
    half = BATCH // 2
    m, e, donor = rng.integers(len(mars["lon"]), size=half), rng.integers(len(earth["lon"]), size=half), rng.integers(len(mars["lon"]), size=2 * half)
    mars_tiles, _ = cut(grids, "mars", mars["lon"][m], mars["lat"][m], CUT)
    earth_tiles, _ = cut(grids, "earth", earth["lon"][e], earth["lat"][e], CUT)
    _, donor_masks = cut(grids, "mars", mars["lon"][donor], mars["lat"][donor], CUT)
    views = []
    for k in range(2):
        masked = through_mask(earth_tiles, donor_masks[k * half : (k + 1) * half])
        views.append(to_input(augment(np.concatenate([mars_tiles, masked]), rng)).to(DEVICE))
    return views[0], views[1]


def train(grids: dict, mars: dict, earth: dict, out: Path, seed: int = SEED) -> None:
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    model = Encoder().to(DEVICE)
    optimiser = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
    schedule = torch.optim.lr_scheduler.OneCycleLR(optimiser, max_lr=1e-3, total_steps=STEPS)
    start = time.time()
    for step in range(STEPS):
        a, b = _views(grids, mars, earth, rng)
        loss = vicreg(model.projector(model(a)), model.projector(model(b)))
        optimiser.zero_grad()
        loss.backward()
        optimiser.step()
        schedule.step()
        if step % 100 == 0:
            print(f"step {step} loss {loss.item():.3f} ({time.time() - start:.0f} s)", flush=True)
    torch.save({"version": VERSION, "state": model.state_dict()}, out)


@torch.no_grad()
def embed(model: Encoder, grids: dict, planet: str, places: dict, chunk: int = 4096) -> NDArray:
    model.eval()
    parts = []
    for start in range(0, len(places["lon"]), chunk):
        tiles, _ = cut(grids, planet, places["lon"][start : start + chunk], places["lat"][start : start + chunk], TILE_SIZE)
        parts.append(F.normalize(model(to_input(tiles).to(DEVICE)), dim=1).cpu().numpy().astype(np.float16))
    return np.concatenate(parts)


def _sensor_test(model: Encoder, grids: dict, mars: dict, earth: dict, rng: np.random.Generator, count: int = 2000) -> dict:
    """Test 1 (retrieval through the mask) and test 2 (does a planet classifier flip on masked Earth tiles)."""
    e, donor = rng.choice(len(earth["lon"]), count, replace=False), rng.integers(len(mars["lon"]), size=count)
    tiles, _ = cut(grids, "earth", earth["lon"][e], earth["lat"][e], TILE_SIZE)
    _, masks = cut(grids, "mars", mars["lon"][donor], mars["lat"][donor], TILE_SIZE)
    masked = through_mask(tiles, masks)
    result = {}
    for name, encode in {
        "network": lambda t: F.normalize(model(to_input(t).to(DEVICE)), dim=1).cpu().numpy(),
        "hand-made": lambda t: shape.describe(t, TILE_SPACING_M),
    }.items():
        clean, seen = encode(tiles), encode(masked)
        if name == "hand-made":
            centre, spread = clean.mean(0), clean.std(0) + 1e-6
            clean, seen = (clean - centre) / spread, (seen - centre) / spread
        distance = ((seen[:, None, :] - clean[None, :, :]) ** 2).sum(-1)
        rank = (distance < distance.diagonal()[:, None]).sum(1)
        result[name] = {"recall_at_1": float((rank == 0).mean()), "recall_at_10": float((rank < 10).mean())}
    return result


def _planet_probe(mars_vec: NDArray, earth_vec: NDArray, masked_earth: NDArray) -> dict:
    """Train a linear Mars-vs-Earth classifier; return the share of masked Earth tiles it still calls Earth."""
    x = torch.tensor(np.concatenate([mars_vec, earth_vec]), dtype=torch.float32)
    y = torch.tensor(np.r_[np.ones(len(mars_vec)), np.zeros(len(earth_vec))], dtype=torch.float32)
    probe = nn.Linear(x.shape[1], 1)
    optimiser = torch.optim.Adam(probe.parameters(), lr=1e-2)
    for _ in range(300):
        optimiser.zero_grad()
        F.binary_cross_entropy_with_logits(probe(x)[:, 0], y).backward()
        optimiser.step()
    with torch.no_grad():
        accuracy = float(((probe(x)[:, 0] > 0).float() == y).float().mean())
        still_earth = float((probe(torch.tensor(masked_earth, dtype=torch.float32))[:, 0] < 0).float().mean())
    return {"planet_accuracy": accuracy, "masked_earth_called_earth": still_earth}


def check(grids: dict, mars: dict, earth: dict, weights: Path, embeddings: Path) -> dict:
    model = Encoder().to(DEVICE)
    saved = torch.load(weights, map_location=DEVICE)
    model.load_state_dict(saved["state"])
    model.eval()
    mars_vec, earth_vec = embed(model, grids, "mars", mars), embed(model, grids, "earth", earth)
    np.savez(embeddings, version=saved["version"], mars=mars_vec, earth=earth_vec)

    pairs = {}
    for name, (mars_lon, mars_lat, lon, lat, radius) in PAIRS.items():
        scores = earth_vec.astype(np.float32) @ mars_vec[_row(mars, "mars", mars_lon, mars_lat)].astype(np.float32)
        pairs[name] = round(_percentile_of_best(scores, _distance_km(earth["lon"], earth["lat"], lon, lat) <= radius), 3)

    rng = np.random.default_rng(SEED + 1)
    with torch.no_grad():
        sensor = _sensor_test(model, grids, mars, earth, rng)
        e, donor = rng.choice(len(earth["lon"]), 2000, replace=False), rng.integers(len(mars["lon"]), size=2000)
        tiles, _ = cut(grids, "earth", earth["lon"][e], earth["lat"][e], TILE_SIZE)
        _, masks = cut(grids, "mars", mars["lon"][donor], mars["lat"][donor], TILE_SIZE)
        masked_vec = F.normalize(model(to_input(through_mask(tiles, masks)).to(DEVICE)), dim=1).cpu().numpy()
    pick = rng.choice(len(mars_vec), 20000, replace=False), rng.choice(len(earth_vec), 20000, replace=False)
    probe = _planet_probe(mars_vec[pick[0]].astype(np.float32), earth_vec[pick[1]].astype(np.float32), masked_vec)
    return {"version": saved["version"], "pairs_percentile": pairs, "sensor_test": sensor, "artefact_test": probe}


def stability(data: Path, other_seed: int, queries: int = 1000, top: int = 50) -> dict:
    """0b test 6: overlap of the top Earth places per Mars query between two training seeds."""
    first, second = np.load(data / "embeddings.npz"), np.load(data / f"embeddings-seed{other_seed}.npz")
    rows = np.random.default_rng(SEED + 2).choice(len(first["mars"]), queries, replace=False)
    overlaps = []
    for row in rows:
        best = [set(np.argpartition(-(e["earth"] @ e["mars"][row]).astype(np.float32), top)[:top]) for e in (first, second)]
        overlaps.append(len(best[0] & best[1]) / top)
    return {"queries": queries, "top": top, "mean_overlap": round(float(np.mean(overlaps)), 3), "median_overlap": round(float(np.median(overlaps)), 3)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("train", "check", "stability"))
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--earth-raw", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=SEED, help="training seed; a second seed tests stability")
    args = parser.parse_args()
    if args.action == "stability":
        result = stability(args.data, args.seed)
        (args.data / "network_stability.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(result)
        return
    mars, earth = load_places(args.data)
    grids = load_grids(args.data, args.earth_raw)
    suffix = f"-seed{args.seed}" if args.seed != SEED else ""
    weights = args.data / f"{VERSION}{suffix}.pt"
    if args.action == "train":
        train(grids, mars, earth, weights, args.seed)
        return
    result = check(grids, mars, earth, weights, args.data / f"embeddings{suffix}.npz")
    (args.data / f"network_check{suffix}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
