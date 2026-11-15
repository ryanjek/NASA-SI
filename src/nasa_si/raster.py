"""Raster metadata helpers used before any scientific processing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin

DCI_WIDTH = 5760
DCI_HEIGHT = 2880
DCI_LABEL_BYTES = 1024
DCI_TRANSFORM = from_origin(-180.0, 90.0, 1 / 16, 1 / 16)
# The numeric DCI fills unobserved pixels with its colour-scale floor rather than NaN.
DCI_FILL_VALUE = 0.85


def dci_valid_mask(values: np.ndarray) -> np.ndarray:
    return np.isfinite(values) & (values > DCI_FILL_VALUE + 1e-6)


def read_dci_pds(path: Path, *, memory_map: bool = True) -> np.ndarray:
    """Read the numeric TES Dust Cover Index's embedded PDS3 QUBE."""
    expected = DCI_LABEL_BYTES + DCI_WIDTH * DCI_HEIGHT * 4
    if path.stat().st_size != expected:
        raise ValueError(
            f"Unexpected DCI file size: {path.stat().st_size:,}; expected {expected:,}"
        )
    array_type = np.memmap if memory_map else np.fromfile
    values = array_type(
        path,
        dtype="<f4",
        mode="r" if memory_map else None,
        offset=DCI_LABEL_BYTES,
        shape=(DCI_HEIGHT, DCI_WIDTH),
    )
    return np.asarray(values)


def raster_summary(path: Path, sample_size: int = 512) -> dict[str, Any]:
    """Return metadata and sampled statistics without loading a full raster."""
    if path.suffix.lower() == ".pds":
        values = read_dci_pds(path)
        row_step = max(1, values.shape[0] // sample_size)
        col_step = max(1, values.shape[1] // sample_size)
        sample = np.asarray(values[::row_step, ::col_step], dtype=np.float64)
        finite = sample[dci_valid_mask(sample)]
        return {
            "path": str(path),
            "driver": "PDS3 QUBE (custom reader)",
            "width": DCI_WIDTH,
            "height": DCI_HEIGHT,
            "bands": 1,
            "dtypes": ["float32"],
            "crs": "Mars geographic sphere; assigned by the pipeline",
            "transform": list(DCI_TRANSFORM)[:6],
            "bounds": {
                "left": -180.0,
                "bottom": -90.0,
                "right": 180.0,
                "top": 90.0,
            },
            "nodata": DCI_FILL_VALUE,
            "sample_count": int(finite.size),
            "sample_min": float(np.min(finite)),
            "sample_median": float(np.median(finite)),
            "sample_max": float(np.max(finite)),
        }
    with rasterio.open(path) as source:
        out_height = min(sample_size, source.height)
        out_width = min(sample_size, source.width)
        band = source.read(
            1,
            out_shape=(out_height, out_width),
            masked=True,
            resampling=Resampling.nearest,
        )
        finite = np.asarray(band.compressed(), dtype=np.float64)
        stats: dict[str, float | int | None]
        if finite.size:
            stats = {
                "sample_count": int(finite.size),
                "sample_min": float(np.min(finite)),
                "sample_median": float(np.median(finite)),
                "sample_max": float(np.max(finite)),
            }
        else:
            stats = {
                "sample_count": 0,
                "sample_min": None,
                "sample_median": None,
                "sample_max": None,
            }
        return {
            "path": str(path),
            "driver": source.driver,
            "width": source.width,
            "height": source.height,
            "bands": source.count,
            "dtypes": list(source.dtypes),
            "crs": source.crs.to_string() if source.crs else None,
            "transform": list(source.transform)[:6],
            "bounds": {
                "left": source.bounds.left,
                "bottom": source.bounds.bottom,
                "right": source.bounds.right,
                "top": source.bounds.top,
            },
            "nodata": source.nodata,
            **stats,
        }
