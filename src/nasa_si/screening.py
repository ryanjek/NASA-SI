"""Phase 1 Arcadia screening: align layers, score candidate cells, and write outputs."""

from __future__ import annotations

import csv
import json
import math
import time
import warnings
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import rasterio
from affine import Affine
from numpy.typing import NDArray
from rasterio.transform import from_origin

from .grid import (
    MARS_GEOGRAPHIC,
    StudyGrid,
    cell_blocks,
    load_dci,
    load_mola,
    load_swim,
    raster_bounds_deg,
    study_grid,
)
from .model import (
    ScreeningProfile,
    dust_score_from_dci,
    gate_cells,
    score_cells,
    slope_degrees,
)

FloatArray = NDArray[np.float64]
MARS_RADIUS_KM = 3396.19

CAVEATS = (
    "Regional screening only; this does not certify a landing site.",
    "Slopes come from ~1 km MOLA pixels and miss metre-scale slopes, rocks, and small craters.",
    "SWIM ice consistency combines 3 km orbital evidence; it is not a measured ice depth.",
    "The Dust Cover Index is ~3.7 km per pixel and indicates surface dust, not dust thickness.",
    "Shortlisted cells need CTX, HiRISE, and THEMIS review before being used as reference zones.",
)

CELL_COLUMNS = (
    "rank",
    "cell_id",
    "latitude_deg",
    "longitude_east_deg",
    "cell_ns_km",
    "cell_ew_km",
    "passes_gates",
    "gate_failures",
    "score",
    "flatness_score",
    "ice_score",
    "dust_score",
    "elevation_score",
    "confidence",
    "elevation_median_m",
    "slope_median_deg",
    "slope_p95_deg",
    "relief_m",
    "swim_ice_consistency_median",
    "tes_dci_median",
)


@dataclass(frozen=True)
class InputPaths:
    mola_labels: tuple[Path, ...]
    swim: Path
    dci: Path

    @classmethod
    def from_raw_dir(cls, raw_dir: Path) -> InputPaths:
        paths = cls(
            mola_labels=tuple(sorted((raw_dir / "mola").glob("megt*.xml"))),
            swim=raw_dir / "swim" / "swim_arcadia_ice_consistency.tif",
            dci=raw_dir / "dust" / "tes_dust_cover_index.pds",
        )
        missing = [str(path) for path in (paths.swim, paths.dci) if not path.exists()]
        if not paths.mola_labels:
            missing.append(str(raw_dir / "mola" / "megt*.xml"))
        if missing:
            raise FileNotFoundError(
                "Missing input data: " + ", ".join(missing) + ". Run `nasa-si download core`."
            )
        return paths


@dataclass(frozen=True)
class Layers:
    grid: StudyGrid
    elevation_m: FloatArray
    slope_deg: FloatArray
    ice_consistency: FloatArray
    dci: FloatArray
    dust_score: FloatArray


def build_layers(
    paths: InputPaths, profile: ScreeningProfile, cell_pixels: int
) -> Layers:
    west, east, south, north = raster_bounds_deg(paths.swim)
    grid = study_grid(west=west, east=east, south=south, north=north, cell_pixels=cell_pixels)
    elevation, metres_per_degree = load_mola(list(paths.mola_labels), grid)
    pixel_m = metres_per_degree * grid.pixel_deg
    slope = slope_degrees(
        elevation,
        x_resolution_m=pixel_m * np.cos(np.radians(grid.row_latitudes())),
        y_resolution_m=pixel_m,
    )
    dci = load_dci(paths.dci, grid)
    return Layers(
        grid=grid,
        elevation_m=elevation,
        slope_deg=slope,
        ice_consistency=load_swim(paths.swim, grid),
        dci=dci,
        dust_score=dust_score_from_dci(dci, profile),
    )


def score_layers(
    layers: Layers, profile: ScreeningProfile, cell_pixels: int
) -> dict[str, np.ndarray]:
    """Return per-cell 2-D arrays of metrics, gates, coordinates, and ranks."""
    metrics = score_cells(
        elevation_m=cell_blocks(layers.elevation_m, cell_pixels),
        slope_deg=cell_blocks(layers.slope_deg, cell_pixels),
        ice_consistency=cell_blocks(layers.ice_consistency, cell_pixels),
        dust_score=cell_blocks(layers.dust_score, cell_pixels),
        profile=profile,
    )
    grid = layers.grid
    cell_deg = grid.pixel_deg * cell_pixels
    rows = grid.height // cell_pixels
    cols = grid.width // cell_pixels
    latitude = np.repeat((grid.north - (np.arange(rows) + 0.5) * cell_deg)[:, None], cols, axis=1)
    longitude = np.repeat((grid.west + (np.arange(cols) + 0.5) * cell_deg)[None, :], rows, axis=0)
    passes, failures = gate_cells(
        latitude_deg=latitude, confidence=metrics["confidence"], profile=profile
    )
    passes &= np.isfinite(metrics["score"])

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        ice_raw = np.nanmedian(cell_blocks(layers.ice_consistency, cell_pixels), axis=-1)
        dci_raw = np.nanmedian(cell_blocks(layers.dci, cell_pixels), axis=-1)

    rank = np.zeros((rows, cols), dtype=np.int64)
    order = np.argsort(np.where(passes, -metrics["score"], np.inf), axis=None, kind="stable")
    passing = int(passes.sum())
    rank.flat[order[:passing]] = np.arange(1, passing + 1)

    km_per_degree = math.pi * MARS_RADIUS_KM / 180.0
    return {
        **metrics,
        "rank": rank,
        "latitude_deg": latitude,
        "longitude_east_deg": longitude,
        "cell_ns_km": np.full((rows, cols), cell_deg * km_per_degree),
        "cell_ew_km": cell_deg * km_per_degree * np.cos(np.radians(latitude)),
        "passes_gates": passes,
        "gate_failures": failures,
        "swim_ice_consistency_median": ice_raw,
        "tes_dci_median": dci_raw,
    }


def great_circle_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * MARS_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


def distinct_candidates(
    cells: dict[str, np.ndarray], top: int, min_separation_km: float
) -> list[tuple[int, int]]:
    """Greedily pick top-ranked cells that are at least ``min_separation_km`` apart."""
    ranked = np.argwhere(cells["rank"] > 0)
    ranked = ranked[np.argsort(cells["rank"][ranked[:, 0], ranked[:, 1]])]
    chosen: list[tuple[int, int]] = []
    for row, col in ranked:
        lat = float(cells["latitude_deg"][row, col])
        lon = float(cells["longitude_east_deg"][row, col])
        if all(
            great_circle_km(
                lat,
                lon,
                float(cells["latitude_deg"][r, c]),
                float(cells["longitude_east_deg"][r, c]),
            )
            >= min_separation_km
            for r, c in chosen
        ):
            chosen.append((int(row), int(col)))
            if len(chosen) == top:
                break
    return chosen


def _cell_record(cells: dict[str, np.ndarray], row: int, col: int) -> dict[str, object]:
    record: dict[str, object] = {}
    for name in CELL_COLUMNS:
        if name == "cell_id":
            record[name] = f"r{row:03d}_c{col:03d}"
            continue
        value = cells[name][row, col]
        if name == "rank":
            record[name] = int(value) if value > 0 else ""
        elif name == "passes_gates":
            record[name] = bool(value)
        elif name == "gate_failures":
            record[name] = str(value)
        else:
            record[name] = "" if not np.isfinite(value) else round(float(value), 5)
    return record


def write_csv(path: Path, records: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=CELL_COLUMNS)
        writer.writeheader()
        writer.writerows(records)


def write_raster(
    path: Path, bands: dict[str, np.ndarray], transform: Affine
) -> None:
    first = next(iter(bands.values()))
    profile = {
        "driver": "GTiff",
        "width": first.shape[1],
        "height": first.shape[0],
        "count": len(bands),
        "dtype": "float32",
        "crs": MARS_GEOGRAPHIC,
        "transform": transform,
        "nodata": np.nan,
        "compress": "deflate",
        "predictor": 3,
        "tiled": True,
    }
    with rasterio.open(path, "w", **profile) as destination:
        for index, (name, values) in enumerate(bands.items(), start=1):
            destination.write(values.astype(np.float32), index)
            destination.set_band_description(index, name)


def plot_screening(
    path: Path,
    layers: Layers,
    cells: dict[str, np.ndarray],
    candidates: list[tuple[int, int]],
    profile: ScreeningProfile,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.patheffects as path_effects
    import matplotlib.pyplot as plt

    outline = [path_effects.withStroke(linewidth=2.5, foreground="black")]

    grid = layers.grid
    extent = (grid.west, grid.east, grid.south, grid.north)
    aspect = 1.0 / math.cos(math.radians((grid.north + grid.south) / 2))
    score = np.where(cells["passes_gates"], cells["score"], np.nan)
    panels = (
        ("MOLA elevation (m)", layers.elevation_m, "terrain", None, None),
        ("Slope at ~1 km (deg)", layers.slope_deg, "magma_r", 0, 2 * profile.flat_slope_deg),
        ("SWIM ice consistency", layers.ice_consistency, "RdBu", -0.7, 0.7),
        ("TES Dust Cover Index", layers.dci, "cividis", profile.dusty_dci - 0.02, profile.clear_dci + 0.01),
        ("Screening score (gated)", score, "viridis", None, None),
    )

    figure = plt.figure(figsize=(18, 12), constrained_layout=True)
    spec = figure.add_gridspec(2, len(panels), height_ratios=(3, 1.1))
    for index, (title, values, cmap, vmin, vmax) in enumerate(panels):
        axis = figure.add_subplot(spec[0, index])
        colormap = plt.get_cmap(cmap).copy()
        colormap.set_bad("0.8")
        image = axis.imshow(
            values, extent=extent, cmap=colormap, vmin=vmin, vmax=vmax,
            interpolation="nearest", aspect=aspect,
        )
        axis.axhline(profile.max_abs_latitude_deg, color="white", lw=0.8, ls="--")
        for number, (row, col) in enumerate(candidates, start=1):
            lon = cells["longitude_east_deg"][row, col]
            lat = cells["latitude_deg"][row, col]
            axis.plot(lon, lat, "o", mfc="none", mec="white", mew=1.5, ms=9,
                      path_effects=outline)
            axis.annotate(str(number), (lon, lat), xytext=(5, 3), textcoords="offset points",
                          color="white", fontsize=8, weight="bold", path_effects=outline)
        axis.set_title(title, fontsize=10)
        axis.set_xlabel("Longitude (deg E)")
        if index == 0:
            axis.set_ylabel("Latitude (deg N)")
        figure.colorbar(image, ax=axis, shrink=0.6, location="bottom")

    components = (
        ("Flatness", "flatness_score", profile.flatness_weight),
        ("Ice", "ice_score", profile.ice_weight),
        ("Dust", "dust_score", profile.dust_weight),
        ("Elevation", "elevation_score", profile.elevation_weight),
        ("Coverage", "confidence", profile.confidence_weight),
    )
    bars = figure.add_subplot(spec[1, :])
    labels = [
        f"#{number}  {cells['latitude_deg'][row, col]:.2f}N "
        f"{cells['longitude_east_deg'][row, col]:.2f}E"
        for number, (row, col) in enumerate(candidates, start=1)
    ]
    left = np.zeros(len(candidates))
    for name, key, weight in components:
        contribution = np.array([weight * cells[key][row, col] for row, col in candidates])
        bars.barh(labels, contribution, left=left, label=f"{name} (weight {weight:g})")
        left += contribution
    bars.invert_yaxis()
    bars.set_xlim(0, 1)
    bars.set_xlabel("Weighted contribution to screening score (max 1.0)")
    bars.set_title("Why each distinct candidate ranked where it did", fontsize=10)
    bars.legend(loc="upper center", bbox_to_anchor=(0.5, -0.3), fontsize=8, ncols=5)
    figure.suptitle(
        "Arcadia Planitia landing / temporary-outpost screening (exploratory, not certification)",
        fontsize=13,
    )
    figure.savefig(path, dpi=130)
    plt.close(figure)


def run_screening(
    raw_dir: Path,
    out_dir: Path,
    *,
    profile: ScreeningProfile | None = None,
    cell_pixels: int = 10,
    top: int = 10,
    min_separation_km: float = 150.0,
    progress: Callable[[str], None] = print,
) -> dict[str, Path]:
    profile = profile or ScreeningProfile()
    started = time.monotonic()
    paths = InputPaths.from_raw_dir(raw_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    progress("aligning MOLA, SWIM, and TES DCI onto the 1/64 degree study grid")
    layers = build_layers(paths, profile, cell_pixels)
    grid = layers.grid
    progress(
        f"grid     {grid.west:.3f}-{grid.east:.3f}E, {grid.south:.3f}-{grid.north:.3f}N, "
        f"{grid.width}x{grid.height} pixels"
    )

    progress(f"scoring  {cell_pixels}x{cell_pixels}-pixel candidate cells")
    cells = score_layers(layers, profile, cell_pixels)
    candidates = distinct_candidates(cells, top, min_separation_km)
    rows, cols = cells["rank"].shape

    outputs = {
        "layers": out_dir / "arcadia_layers.tif",
        "cell_scores": out_dir / "arcadia_cell_scores.tif",
        "cells": out_dir / "arcadia_cells.csv",
        "candidates": out_dir / "arcadia_candidates.csv",
        "figure": out_dir / "arcadia_screening.png",
        "run": out_dir / "arcadia_run.json",
    }
    write_raster(
        outputs["layers"],
        {
            "elevation_m": layers.elevation_m,
            "slope_deg": layers.slope_deg,
            "swim_ice_consistency": layers.ice_consistency,
            "tes_dci": layers.dci,
            "dust_score": layers.dust_score,
        },
        grid.transform,
    )
    cell_deg = grid.pixel_deg * cell_pixels
    write_raster(
        outputs["cell_scores"],
        {
            "score_gated": np.where(cells["passes_gates"], cells["score"], np.nan),
            "score": cells["score"],
            "flatness_score": cells["flatness_score"],
            "ice_score": cells["ice_score"],
            "dust_score": cells["dust_score"],
            "elevation_score": cells["elevation_score"],
            "confidence": cells["confidence"],
            "passes_gates": cells["passes_gates"].astype(np.float32),
        },
        from_origin(grid.west, grid.north, cell_deg, cell_deg),
    )
    write_csv(
        outputs["cells"],
        [_cell_record(cells, row, col) for row in range(rows) for col in range(cols)],
    )
    candidate_records = [_cell_record(cells, row, col) for row, col in candidates]
    write_csv(outputs["candidates"], candidate_records)
    plot_screening(outputs["figure"], layers, cells, candidates, profile)

    run = {
        "generated_at_unix": int(time.time()),
        "inputs": {
            "mola": [str(path) for path in paths.mola_labels],
            "swim": str(paths.swim),
            "dci": str(paths.dci),
        },
        "grid": {
            **asdict(grid),
            "east": grid.east,
            "south": grid.south,
            "crs": "Mars 2000 sphere, planetocentric, longitude 0-360 east",
        },
        "cell_pixels": cell_pixels,
        "profile": asdict(profile),
        "min_separation_km": min_separation_km,
        "cells_total": int(rows * cols),
        "cells_passing_gates": int(cells["passes_gates"].sum()),
        "candidates": candidate_records,
        "caveats": list(CAVEATS),
    }
    outputs["run"].write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")

    progress(
        f"cells    {run['cells_passing_gates']:,} of {run['cells_total']:,} pass the gates; "
        f"{len(candidates)} distinct candidates >= {min_separation_km:g} km apart"
    )
    for number, record in enumerate(candidate_records, start=1):
        progress(
            f"  #{number:<2} {record['latitude_deg']:>7}N {record['longitude_east_deg']:>8}E  "
            f"score {record['score']:.3f}  flat {record['flatness_score']:.2f}  "
            f"ice {record['ice_score']:.2f}  dust {record['dust_score']:.2f}  "
            f"elev {record['elevation_median_m']:.0f} m"
        )
    for path in outputs.values():
        progress(f"wrote    {path}")
    progress(f"done in {time.monotonic() - started:.1f}s")
    return outputs
