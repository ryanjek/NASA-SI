"""Export compact per-cell data for the interactive Leaflet map in ``web/``.

The browser recomputes scores, requirements, regions, and explanations from these
per-cell summaries, so sliders respond instantly without a server. Ice, dust, and
elevation are stored as medians because their scores are monotonic transforms of the
median. Flatness depends on every pixel's slope, so it is stored as a table of flat
fractions for each selectable slope threshold.
"""

from __future__ import annotations

import json
import time
import warnings
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from .grid import StudyGrid, cell_blocks
from .model import ScreeningProfile, score_cells
from .screening import CAVEATS, MARS_RADIUS_KM, InputPaths, Layers, build_layers

FloatArray = NDArray[np.float64]

FLATNESS_THRESHOLDS_DEG = tuple(round(0.25 * step, 2) for step in range(1, 41))
FLATNESS_SCALE = 200
FLATNESS_MISSING = 255

CELL_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("elevation_median_m", "m", "Median MOLA elevation relative to the areoid"),
    ("ice_median", "", "Median SWIM ice consistency, -1 inconsistent to +1 consistent with ice"),
    ("dci_median", "", "Median TES Dust Cover Index emissivity; higher means less surface dust"),
    ("coverage", "", "Fraction of the cell's pixels with valid data in every layer"),
    ("slope_median_deg", "deg", "Median slope on ~1 km MOLA pixels"),
    ("slope_p95_deg", "deg", "95th-percentile slope on ~1 km MOLA pixels"),
    ("relief_m", "m", "Maximum minus minimum elevation within the cell"),
    ("python_default_score", "", "Reference score from nasa_si.model with the default profile"),
)

SOURCES = (
    {
        "name": "MOLA MEGDR topography (64 pixels/degree)",
        "url": "https://pds-geosciences.wustl.edu/missions/mgs/megdr.html",
    },
    {
        "name": "SWIM Arcadia pilot ice consistency",
        "url": "https://swim.psi.edu/PilotStudyProducts.php",
    },
    {
        "name": "TES Dust Cover Index (Ruff and Christensen, 2002)",
        "url": "https://tes.asu.edu/~ruff/DCI/dci.html",
    },
)


def summarize_cells(
    *,
    elevation_m: FloatArray,
    slope_deg: FloatArray,
    ice_consistency: FloatArray,
    dci: FloatArray,
    cell_pixels: int,
    thresholds_deg: tuple[float, ...] = FLATNESS_THRESHOLDS_DEG,
) -> dict[str, np.ndarray]:
    """Summarise jointly valid pixels per cell; flatness has a trailing threshold axis."""
    elevation = cell_blocks(elevation_m, cell_pixels)
    slope = cell_blocks(slope_deg, cell_pixels)
    ice = cell_blocks(ice_consistency, cell_pixels)
    dust = cell_blocks(dci, cell_pixels)
    valid = np.isfinite(elevation) & np.isfinite(slope) & np.isfinite(ice) & np.isfinite(dust)
    valid_count = valid.sum(axis=-1)

    def valid_only(values: np.ndarray) -> np.ndarray:
        return np.where(valid, values, np.nan)

    with warnings.catch_warnings(), np.errstate(invalid="ignore", divide="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        elevation_valid = valid_only(elevation)
        slope_valid = valid_only(slope)
        flat_counts = np.stack(
            [(valid & (slope <= threshold)).sum(axis=-1) for threshold in thresholds_deg],
            axis=-1,
        )
        return {
            "elevation_median_m": np.nanmedian(elevation_valid, axis=-1),
            "ice_median": np.nanmedian(valid_only(ice), axis=-1),
            "dci_median": np.nanmedian(valid_only(dust), axis=-1),
            "coverage": valid_count / valid.shape[-1],
            "slope_median_deg": np.nanmedian(slope_valid, axis=-1),
            "slope_p95_deg": np.nanpercentile(slope_valid, 95, axis=-1),
            "relief_m": np.nanmax(elevation_valid, axis=-1) - np.nanmin(elevation_valid, axis=-1),
            "flatness": np.where(
                valid_count[..., None] > 0, flat_counts / valid_count[..., None], np.nan
            ),
        }


def encode_flatness(flatness: np.ndarray) -> np.ndarray:
    encoded = np.full(flatness.shape, FLATNESS_MISSING, dtype=np.uint8)
    finite = np.isfinite(flatness)
    encoded[finite] = np.rint(np.clip(flatness[finite], 0.0, 1.0) * FLATNESS_SCALE).astype(np.uint8)
    return encoded


def default_settings(profile: ScreeningProfile) -> dict[str, dict[str, float | bool]]:
    """Initial slider state; requirement values at their limits do not exclude anything."""
    return {
        "latitude": {"enabled": True, "max": profile.max_abs_latitude_deg},
        "coverage": {
            "enabled": True,
            "min": profile.min_confidence,
            "weight": profile.confidence_weight,
        },
        "flatness": {
            "enabled": True,
            "slopeDeg": profile.flat_slope_deg,
            "minFraction": 0.0,
            "weight": profile.flatness_weight,
        },
        "ice": {"enabled": True, "min": -1.0, "weight": profile.ice_weight},
        "dust": {
            "enabled": True,
            "dusty": profile.dusty_dci,
            "clear": profile.clear_dci,
            "min": 0.85,
            "weight": profile.dust_weight,
        },
        "elevation": {
            "enabled": True,
            "preferredMax": profile.preferred_max_elevation_m,
            "margin": profile.elevation_soft_margin_m,
            "maxAllowed": 2_000.0,
            "weight": profile.elevation_weight,
        },
        "regions": {"topPercent": 5.0, "minAreaKm2": 100.0, "count": 8, "rankBy": "mean"},
        "stability": {"trials": 60, "perturbation": 0.2},
    }


def write_hillshade(path: Path, layers: Layers, metres_per_pixel: float, stride: int = 2) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LightSource, Normalize

    grid = layers.grid
    mid_latitude = np.radians((grid.north + grid.south) / 2)
    elevation = layers.elevation_m[::stride, ::stride]
    light = LightSource(azdeg=315, altdeg=40)
    low, high = np.percentile(elevation, (1, 99))
    rgb = light.shade(
        elevation,
        cmap=plt.get_cmap("copper"),
        norm=Normalize(vmin=low, vmax=high),
        blend_mode="soft",
        vert_exag=15,
        dx=metres_per_pixel * stride * np.cos(mid_latitude),
        dy=metres_per_pixel * stride,
    )
    plt.imsave(path, rgb[..., :3])


def export_web(
    raw_dir: Path,
    out_dir: Path,
    *,
    cell_pixels: int = 10,
    progress: Callable[[str], None] = print,
) -> dict[str, Path]:
    started = time.monotonic()
    profile = ScreeningProfile()
    paths = InputPaths.from_raw_dir(raw_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    progress("aligning MOLA, SWIM, and TES DCI onto the 1/64 degree study grid")
    layers = build_layers(paths, profile, cell_pixels)
    grid: StudyGrid = layers.grid

    progress(f"summarising {cell_pixels}x{cell_pixels}-pixel cells")
    summaries = summarize_cells(
        elevation_m=layers.elevation_m,
        slope_deg=layers.slope_deg,
        ice_consistency=layers.ice_consistency,
        dci=layers.dci,
        cell_pixels=cell_pixels,
    )
    summaries["python_default_score"] = score_cells(
        elevation_m=cell_blocks(layers.elevation_m, cell_pixels),
        slope_deg=cell_blocks(layers.slope_deg, cell_pixels),
        ice_consistency=cell_blocks(layers.ice_consistency, cell_pixels),
        dust_score=cell_blocks(layers.dust_score, cell_pixels),
        profile=profile,
    )["score"]

    rows, cols = summaries["coverage"].shape
    cell_count = rows * cols
    outputs = {
        "manifest": out_dir / "arcadia.json",
        "cells": out_dir / "arcadia_cells.bin",
        "hillshade": out_dir / "arcadia_hillshade.png",
    }

    fields: dict[str, dict[str, object]] = {}
    offset = 0
    with outputs["cells"].open("wb") as stream:
        for name, units, description in CELL_FIELDS:
            values = summaries[name].astype("<f4").reshape(-1)
            stream.write(values.tobytes())
            fields[name] = {
                "dtype": "float32",
                "offset": offset,
                "units": units,
                "description": description,
            }
            offset += values.nbytes
        flatness = encode_flatness(summaries["flatness"]).reshape(cell_count, -1)
        stream.write(flatness.tobytes())

    metres_per_degree = np.pi * MARS_RADIUS_KM * 1000 / 180
    write_hillshade(outputs["hillshade"], layers, metres_per_degree * grid.pixel_deg)

    cell_deg = grid.pixel_deg * cell_pixels
    manifest = {
        "version": 1,
        "generated_at_unix": int(time.time()),
        "study_area": "Arcadia Planitia (SWIM pilot swath)",
        "mars_radius_km": MARS_RADIUS_KM,
        "grid": {
            "west": grid.west,
            "north": grid.north,
            "east": grid.west + cols * cell_deg,
            "south": grid.north - rows * cell_deg,
            "cell_deg": cell_deg,
            "rows": rows,
            "cols": cols,
            "cell_pixels": cell_pixels,
            "pixel_deg": grid.pixel_deg,
            "longitude_convention": "degrees east, 0-360",
            "order": "row-major from the north-west corner",
        },
        "cell_count": cell_count,
        "cells_file": outputs["cells"].name,
        "fields": fields,
        "flatness": {
            "dtype": "uint8",
            "offset": offset,
            "thresholds_deg": list(FLATNESS_THRESHOLDS_DEG),
            "scale": FLATNESS_SCALE,
            "missing": FLATNESS_MISSING,
        },
        "hillshade": {
            "file": outputs["hillshade"].name,
            "west": grid.west,
            "east": grid.east,
            "south": grid.south,
            "north": grid.north,
        },
        "profile": asdict(profile),
        "defaults": default_settings(profile),
        "sources": list(SOURCES),
        "caveats": list(CAVEATS),
    }
    outputs["manifest"].write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    for path in outputs.values():
        progress(f"wrote    {path} ({path.stat().st_size / 1_048_576:.2f} MiB)")
    progress(f"done in {time.monotonic() - started:.1f}s")
    return outputs
