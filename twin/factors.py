"""The non-shape factors, as global grids in the same quantity and units on both planets.

albedo      Mars: TES bolometric albedo (the mosaic is clipped to 0.06-0.32).
            Earth: MODIS MCD43C3 white-sky shortwave albedo, the lowest of the dates held (least snow).
thermal     Surface temperature in kelvin, the mean of the 13:30 and 01:30 local-time values:
            annual mean, warmest month, coldest month.
            Mars: Mars Climate Database v6.2 (a climate model). Earth: MODIS Aqua MYD11C3.
ice         An indicator of shallow ground ice: 0 = evidence against, 0.5 = no evidence, 1 = evidence for.
            Mars: SWIM 2.0 ice consistency, the larger of the 0-1 m and 1-5 m maps (60S-60N only).
            Earth: ESA CCI permafrost fraction (north of 25N); permanent ice cover counts as 1.
vegetation  Peak monthly NDVI (MODIS MOD13C2), floored at zero. Mars is zero everywhere.

Code written with Claude (Anthropic); see twin/README.md.
"""

from __future__ import annotations

from pathlib import Path

import netCDF4
import numpy as np
import rasterio
from numpy.typing import NDArray
from pyhdf.SD import SD, SDC

from .mars_climate import thermal_grids
from .planets import Grid

TES_ALBEDO = "raw/mars/Mars_MGS_TES_Albedo_mosaic_global_7410m.tif"
SWIM_MAPS = ("raw/mars/SWIM2_c0_1.tif", "raw/mars/SWIM2_c1_5.tif")
SWIM_NORTH_EDGE = 60.0
SWIM_SATURATION = 0.5  # consistency beyond +-0.5 counts as full evidence
MARS_CLIMATE = "raw/mars/mcd"

CMG_PIXELS_PER_DEGREE = 20.0  # the MODIS 0.05 degree climate-modelling grid: 3600 x 7200, row 0 at 90N
LAND_COVER = "Majority_Land_Cover_Type_1"
WATER, PERMANENT_ICE, BARREN = 0, 15, 16
MIN_MONTHS = 9
PERMAFROST_FILL = 255  # water, and land outside the modelled permafrost domain
PERMAFROST_BLOCK = 5  # 0.01 degree pixels per 0.05 degree pixel
ANTARCTIC_ROW = 3000  # first row south of 60S


def _cmg(values: NDArray) -> Grid:
    return Grid(values.astype(np.float32), CMG_PIXELS_PER_DEGREE, -180.0)


def _modis(path: Path, name: str, scale: float, lowest: int, highest: int) -> NDArray:
    """One MODIS layer in physical units, with everything outside the valid range as NaN."""
    raw = SD(str(path), SDC.READ).select(name).get()
    return np.where((raw >= lowest) & (raw <= highest), raw * np.float32(scale), np.float32(np.nan))


def _files(folder: Path, pattern: str = "*.hdf") -> list[Path]:
    found = sorted(folder.glob(pattern))
    if not found:
        raise FileNotFoundError(f"no {pattern} in {folder}")
    return found


def read_land_cover(raw: Path) -> NDArray:
    return SD(str(_files(raw / "land-cover")[0]), SDC.READ).select(LAND_COVER).get()


def earth_albedo(raw: Path) -> Grid:
    dates = np.stack([_modis(path, "Albedo_WSA_shortwave", 0.001, 0, 32766) for path in _files(raw / "albedo")])
    return _cmg(np.fmin.reduce(dates))


def earth_thermal(raw: Path) -> dict[str, Grid]:
    """Aqua crosses at about 13:30 and 01:30 local time; a month counts only if both passes have a value."""
    day, night = (
        np.stack([_modis(path, name, 0.02, 7500, 65535) for path in _files(raw / "temperature-aqua")])
        for name in ("LST_Day_CMG", "LST_Night_CMG")
    )
    enough = np.isfinite(day + night).sum(axis=0) >= MIN_MONTHS
    day, night = day[:, enough], night[:, enough]
    middle = (day + night) / 2
    layers = {
        "mean": np.nanmean(middle, axis=0),
        "warmest": np.nanmax(middle, axis=0),
        "coldest": np.nanmin(middle, axis=0),
        "range": np.nanmean(day - night, axis=0),
    }
    grids = {}
    for name, values in layers.items():
        full = np.full(enough.shape, np.nan, np.float32)
        full[enough] = values
        grids[name] = _cmg(full)
    return grids


def earth_vegetation(raw: Path, cover: NDArray) -> Grid:
    months = np.stack([_modis(path, "CMG 0.05 Deg Monthly NDVI", 1e-4, -2000, 10000) for path in _files(raw / "vegetation")])
    peak = np.fmax.reduce(months).clip(min=0.0)
    unvegetated = np.isnan(peak) & np.isin(cover, (PERMANENT_ICE, BARREN))
    print(f"vegetation: {int(unvegetated.sum())} ice or barren pixels with no NDVI set to 0")
    return _cmg(np.where(unvegetated, 0.0, peak))


def earth_ice(raw: Path, cover: NDArray) -> Grid:
    """Permafrost fraction on the 0.05 degree grid, with the choices that fill its gaps stated here.

    Land north of 60S that the permafrost product does not model is counted as having none. Permanent
    ice cover is 1. Ice-free ground south of 60S is 1: the product stops at 25N, and Antarctic ground
    is permafrost throughout (Obu et al. 2019). Open water stays missing.
    """
    with netCDF4.Dataset(_files(raw / "permafrost", "*PFR*.nc")[0]) as dataset:
        dataset.set_auto_maskandscale(False)
        percent, lat = dataset["PFR"][0], dataset["lat"][:]
    if lat[0] < lat[-1]:
        percent = percent[::-1]
    rows, cols = percent.shape[0] // PERMAFROST_BLOCK, percent.shape[1] // PERMAFROST_BLOCK
    blocks = percent.reshape(rows, PERMAFROST_BLOCK, cols, PERMAFROST_BLOCK)
    modelled = blocks != PERMAFROST_FILL
    count = modelled.sum(axis=(1, 3))
    fraction = np.where(modelled, blocks, 0).sum(axis=(1, 3), dtype=np.float32) / np.maximum(count, 1) / 100.0

    ice = np.zeros(cover.shape, np.float32)
    first_row = round((90.0 - float(lat.max())) * CMG_PIXELS_PER_DEGREE)
    ice[first_row : first_row + rows] = fraction
    ice[cover == PERMANENT_ICE] = 1.0
    ice[ANTARCTIC_ROW:] = 1.0
    ice[cover == WATER] = np.nan
    print(f"ice: {int((count > 0).sum())} modelled pixels; {int((cover == PERMANENT_ICE).sum())} permanent-ice pixels set to 1")
    return _cmg(ice)


def earth_factors(raw: Path) -> dict[str, list[Grid]]:
    cover, thermal = read_land_cover(raw), earth_thermal(raw)
    return {
        "albedo": [earth_albedo(raw)],
        "thermal": [thermal["mean"], thermal["warmest"], thermal["coldest"]],
        "day_night_range": [thermal["range"]],
        "ice": [earth_ice(raw, cover)],
        "vegetation": [earth_vegetation(raw, cover)],
    }


def _read_band(path: Path) -> NDArray:
    with rasterio.open(path) as source:
        band = source.read(1)
        return np.where(band == source.nodata, np.float32(np.nan), band)


def mars_ice(data: Path) -> Grid:
    consistency = np.fmax.reduce([_read_band(data / name) for name in SWIM_MAPS])
    indicator = (consistency.clip(-SWIM_SATURATION, SWIM_SATURATION) + SWIM_SATURATION) / (2 * SWIM_SATURATION)
    return Grid(indicator, indicator.shape[1] / 360.0, -180.0, SWIM_NORTH_EDGE)


def mars_factors(data: Path) -> dict[str, list[Grid]]:
    albedo, thermal = _read_band(data / TES_ALBEDO), thermal_grids(data / MARS_CLIMATE)
    return {
        "albedo": [Grid(albedo, albedo.shape[1] / 360.0, -180.0)],
        "thermal": [thermal["mean"], thermal["warmest"], thermal["coldest"]],
        "day_night_range": [thermal["range"]],
        "ice": [mars_ice(data)],
    }
