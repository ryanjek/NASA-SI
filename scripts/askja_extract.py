#!/usr/bin/env python3
"""Phase 2 feature extraction for Askja (Iceland) 1 km x 1 km reference windows.

Downloads windowed subsets from all datasets with covers_askja=yes in Phase 1's
datasets.csv (both primary and alternate sources in feature_datasets.csv) and
computes all 15 covered global_search features defined in features.csv for:
  1. askja_caldera (65.033°N, 16.783°W) - published point in sites.geojson
  2. dyngjusandur_central (64.950°N, 16.550°W) - representative 1 km x 1 km window
     on the central inflating basaltic sand sheet (Stimson analog seed) ESE of Askja
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pyproj
import rasterio
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds
import requests
from scipy.ndimage import uniform_filter

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "askja" / "raw"
PROC_DIR = REPO_ROOT / "data" / "askja" / "processed"

WGS84_A = 6378137.0
WGS84_E2 = 0.00669437999014

# Two 1 km x 1 km extraction windows in Iceland
SITES = {
    "askja_caldera": {
        "site_id": "askja_caldera",
        "name": "Askja caldera summit (sites.geojson published point)",
        "site_role": "context_region",
        "transferable_to_similarity_search": False,
        "lat": 65.0330,
        "lon": -16.7830,
        "published_elev_m": 1516.0,
        "window_m": 1000.0,
    },
    "dyngjusandur_central": {
        "site_id": "dyngjusandur_central",
        "name": "Dyngjusandur central inflating basaltic sand sheet (Stimson analog seed)",
        "site_role": "science_subregion",
        "transferable_to_similarity_search": True,
        "lat": 64.9500,
        "lon": -16.5500,
        "published_elev_m": None,
        "window_m": 1000.0,
    },
}

# Confirmed craters in Northern Europe / Arctic from the Earth Impact Database (PASSC)
# used to compute exact geodesic distance (impact_distance_km)
CONFIRMED_IMPACT_CRATERS = [
    {"name": "Lappajärvi (Finland)", "lat": 63.1500, "lon": 23.6833, "diam_km": 23.0},
    {"name": "Dellen (Sweden)", "lat": 61.8500, "lon": 16.7000, "diam_km": 19.0},
    {"name": "Gardnos (Norway)", "lat": 60.6500, "lon": 9.0000, "diam_km": 5.0},
    {"name": "Siljan (Sweden)", "lat": 61.0333, "lon": 14.9167, "diam_km": 52.0},
    {"name": "Lockne (Sweden)", "lat": 63.0056, "lon": 14.8194, "diam_km": 7.5},
    {"name": "Målingen (Sweden)", "lat": 62.9194, "lon": 14.5694, "diam_km": 0.7},
    {"name": "Ritland (Norway)", "lat": 59.2333, "lon": 6.4167, "diam_km": 2.7},
    {"name": "Mjølnir (Barents Sea, Norway)", "lat": 73.8000, "lon": 29.6667, "diam_km": 40.0},
    {"name": "Suavjärvi (Russia)", "lat": 63.1167, "lon": 33.3833, "diam_km": 16.0},
    {"name": "Jänisjärvi (Russia)", "lat": 61.9667, "lon": 30.9167, "diam_km": 14.0},
    {"name": "Haughton (Devon Island, Canada)", "lat": 75.377222, "lon": -89.6800, "diam_km": 23.0},
    {"name": "Manicouagan (Quebec, Canada)", "lat": 51.3833, "lon": -68.7000, "diam_km": 100.0},
    {"name": "Rochechouart (France)", "lat": 45.8242, "lon": 0.7817, "diam_km": 23.0},
    {"name": "Nördlinger Ries (Germany)", "lat": 48.8833, "lon": 10.6167, "diam_km": 24.0},
]


def meters_per_degree(lat_deg: float) -> tuple[float, float]:
    """Return physical meters per degree of (latitude, longitude) on WGS84 ellipsoid."""
    lat_rad = math.radians(lat_deg)
    sin_lat = math.sin(lat_rad)
    cos_lat = math.cos(lat_rad)
    denom = math.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    m_per_deg_lat = (math.pi / 180.0) * (WGS84_A * (1.0 - WGS84_E2)) / (denom ** 3)
    m_per_deg_lon = (math.pi / 180.0) * (WGS84_A * cos_lat) / denom
    return m_per_deg_lat, m_per_deg_lon


def site_bbox_wgs84(lat: float, lon: float, window_m: float = 1000.0, buffer_m: float = 0.0) -> tuple[float, float, float, float]:
    """Return (west, south, east, north) in EPSG:4326 for a physical window_m x window_m square."""
    total_m = window_m + 2.0 * buffer_m
    half_m = total_m / 2.0
    m_lat, m_lon = meters_per_degree(lat)
    dlat = half_m / m_lat
    dlon = half_m / m_lon
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def write_receipt(target_path: Path, metadata: dict[str, Any]) -> Path:
    """Write a JSON provenance receipt with SHA-256 digest alongside a downloaded payload."""
    h = hashlib.sha256()
    with target_path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    receipt = {
        **metadata,
        "local_file": target_path.name,
        "sha256": h.hexdigest(),
        "bytes": target_path.stat().st_size,
        "downloaded_utc": datetime.now(timezone.utc).isoformat(),
    }
    receipt_path = target_path.with_suffix(target_path.suffix + ".receipt.json")
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt_path


def download_crop_cog(url: str, bbox_wgs84: tuple[float, float, float, float], out_tif: Path, meta_info: dict[str, Any]) -> Path:
    """Read a bounding-box window from a remote Cloud-Optimized GeoTIFF and save locally."""
    out_tif.parent.mkdir(parents=True, exist_ok=True)
    receipt_path = out_tif.with_suffix(out_tif.suffix + ".receipt.json")
    if out_tif.exists() and receipt_path.exists():
        return out_tif

    with rasterio.Env(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_USE_HEAD="NO",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.TIF,.tiff",
        GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
        GDAL_HTTP_MULTIPLEX="YES",
    ):
        with rasterio.open(url) as src:
            if src.crs and src.crs.to_string() != "EPSG:4326":
                left, bottom, right, top = transform_bounds("EPSG:4326", src.crs, *bbox_wgs84, densify_pts=21)
            else:
                left, bottom, right, top = bbox_wgs84

            win = from_bounds(left, bottom, right, top, transform=src.transform)
            win = win.round_offsets().round_lengths()
            # Ensure at least 1x1 pixel
            col_off = max(0, int(win.col_off))
            row_off = max(0, int(win.row_off))
            width = max(1, min(src.width - col_off, int(win.width)))
            height = max(1, min(src.height - row_off, int(win.height)))
            win = rasterio.windows.Window(col_off, row_off, width, height)

            data = src.read(1, window=win)
            win_transform = src.window_transform(win)
            profile = src.profile.copy()
            profile.update({
                "driver": "GTiff",
                "height": data.shape[0],
                "width": data.shape[1],
                "count": 1,
                "transform": win_transform,
                "compress": "deflate",
            })
            with rasterio.open(out_tif, "w", **profile) as dst:
                dst.write(data, 1)

    write_receipt(out_tif, {"source_url": url, "bbox_wgs84": bbox_wgs84, **meta_info})
    return out_tif


def pc_sign_href(href: str, collection: str, token_cache: dict[str, str]) -> str:
    """Sign a Microsoft Planetary Computer blob URL using container-level SAS token caching."""
    # Extract storage account and container from https://{account}.blob.core.windows.net/{container}/...
    import urllib.parse
    parsed = urllib.parse.urlparse(href)
    host_parts = parsed.netloc.split(".")
    path_parts = [p for p in parsed.path.split("/") if p]
    cache_key = collection
    token_url = f"https://planetarycomputer.microsoft.com/api/sas/v1/token/{collection}"
    if len(host_parts) >= 4 and host_parts[1] == "blob" and path_parts:
        account = host_parts[0]
        container = path_parts[0]
        cache_key = f"{account}/{container}"
        token_url = f"https://planetarycomputer.microsoft.com/api/sas/v1/token/{account}/{container}"
    if cache_key not in token_cache:
        r_tok = requests.get(token_url, timeout=20)
        if r_tok.status_code != 200:
            r_tok = requests.get(f"https://planetarycomputer.microsoft.com/api/sas/v1/token/{collection}", timeout=20)
        r_tok.raise_for_status()
        token_cache[cache_key] = r_tok.json()["token"]
    token = token_cache[cache_key]
    sep = "&" if "?" in href else "?"
    return f"{href}{sep}{token}"


def acquire_all() -> None:
    """Download windowed data for all 11 covers_askja=yes datasets."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    token_cache: dict[str, str] = {}

    # -------------------------------------------------------------------------
    # 1. copernicus_glo30 (Primary DEM, 30 m)
    # -------------------------------------------------------------------------
    print("[1/11] Acquiring copernicus_glo30 (30 m DEM)...", flush=True)
    for site_key, site in SITES.items():
        # Buffer by 150 m so slope and 100 m TRI windows have valid neighbors at edges
        bbox = site_bbox_wgs84(site["lat"], site["lon"], window_m=1000.0, buffer_m=150.0)
        lat_floor = int(math.floor(site["lat"]))
        lon_floor = abs(int(math.floor(site["lon"])))
        tile_id = f"Copernicus_DSM_COG_10_N{lat_floor:02d}_00_W{lon_floor:03d}_00_DEM"
        url = f"https://copernicus-dem-30m.s3.amazonaws.com/{tile_id}/{tile_id}.tif"
        out_tif = RAW_DIR / "copernicus_glo30" / f"{site_key}_glo30.tif"
        download_crop_cog(url, bbox, out_tif, {
            "dataset_id": "copernicus_glo30",
            "site_id": site_key,
            "role_in_feature": "primary",
            "tile_id": tile_id,
        })
        print(f"  Saved {out_tif.relative_to(REPO_ROOT)}")

    # -------------------------------------------------------------------------
    # 2. arcticdem (Alternate DEM, 2 m PGC ArcticDEM v4.1 Mosaic)
    # -------------------------------------------------------------------------
    print("[2/11] Acquiring arcticdem (2 m PGC ArcticDEM v4.1)...", flush=True)
    for site_key, site in SITES.items():
        out_tif = RAW_DIR / "arcticdem" / f"{site_key}_arcticdem_2m.tif"
        if out_tif.exists() and out_tif.with_suffix(out_tif.suffix + ".receipt.json").exists():
            print(f"  Cached {out_tif.relative_to(REPO_ROOT)}")
            continue
        bbox = site_bbox_wgs84(site["lat"], site["lon"], window_m=1000.0, buffer_m=150.0)
        r = requests.post(
            "https://stac.pgc.umn.edu/api/v1/search",
            json={
                "collections": ["arcticdem-mosaics-v4.1-2m"],
                "bbox": list(bbox),
                "limit": 5,
            },
            timeout=25,
        )
        r.raise_for_status()
        feats = r.json()["features"]
        # Pick the tile whose bbox in EPSG:3413 covers the site center
        tr_3413 = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3413", always_xy=True)
        cx, cy = tr_3413.transform(site["lon"], site["lat"])
        chosen = feats[0]
        for feat in feats:
            pb = feat["properties"].get("proj:bbox") or feat["assets"]["dem"].get("proj:bbox")
            if pb and pb[0] <= cx <= pb[2] and pb[1] <= cy <= pb[3]:
                chosen = feat
                break
        dem_url = chosen["assets"]["dem"]["href"]
        download_crop_cog(dem_url, bbox, out_tif, {
            "dataset_id": "arcticdem",
            "site_id": site_key,
            "role_in_feature": "alternate",
            "stac_item_id": chosen["id"],
        })
        print(f"  Saved {out_tif.relative_to(REPO_ROOT)} (tile {chosen['id']})")

    # -------------------------------------------------------------------------
    # 3. sentinel2_l2a (Primary Optical, 10 m Surface Reflectance)
    # -------------------------------------------------------------------------
    print("[3/11] Acquiring sentinel2_l2a (10 m Summer Surface Reflectance)...", flush=True)
    joint_bbox = [-16.81, 64.93, -16.52, 65.05]
    if len(list((RAW_DIR / "sentinel2_l2a").glob("*.receipt.json"))) >= 18:
        print("  Cached 3 Sentinel-2 summer scenes for both sites")
    else:
        r_s2 = requests.post(
            "https://earth-search.aws.element84.com/v1/search",
            json={
                "collections": ["sentinel-2-l2a"],
                "bbox": joint_bbox,
                "datetime": "2022-07-01T00:00:00Z/2025-08-31T23:59:59Z",
                "query": {"eo:cloud_cover": {"lt": 15}},
                "limit": 20,
            },
            timeout=25,
        )
        r_s2.raise_for_status()
        s2_features = [
            f for f in r_s2.json().get("features", [])
            if int(f["properties"]["datetime"][5:7]) in (7, 8)
        ]
        s2_features.sort(key=lambda f: f["properties"].get("eo:cloud_cover", 100.0))
        s2_selected = s2_features[:3]
        for site_key, site in SITES.items():
            bbox = site_bbox_wgs84(site["lat"], site["lon"], window_m=1000.0, buffer_m=20.0)
            for idx, feat in enumerate(s2_selected):
                scene_id = feat["id"]
                for band_name in ("red", "nir", "scl"):
                    href = feat["assets"][band_name]["href"]
                    out_tif = RAW_DIR / "sentinel2_l2a" / f"{site_key}_{scene_id}_{band_name}.tif"
                    download_crop_cog(href, bbox, out_tif, {
                        "dataset_id": "sentinel2_l2a",
                        "site_id": site_key,
                        "role_in_feature": "primary",
                        "scene_id": scene_id,
                        "band": band_name,
                        "datetime": feat["properties"]["datetime"],
                        "cloud_cover": feat["properties"].get("eo:cloud_cover"),
                    })
            print(f"  Saved {len(s2_selected)} Sentinel-2 summer scenes for {site_key}")

    # -------------------------------------------------------------------------
    # 4. landsat_c2 (Alternate Optical, 30 m Landsat 8/9 Collection 2 L2)
    # -------------------------------------------------------------------------
    print("[4/11] Acquiring landsat_c2 (30 m Summer Surface Reflectance)...", flush=True)
    if len(list((RAW_DIR / "landsat_c2").glob("*.receipt.json"))) >= 18:
        print("  Cached 3 Landsat C2 summer scenes for both sites")
    else:
        r_ls = requests.post(
            "https://planetarycomputer.microsoft.com/api/stac/v1/search",
            json={
                "collections": ["landsat-c2-l2"],
                "bbox": joint_bbox,
                "datetime": "2022-06-15T00:00:00Z/2025-08-31T23:59:59Z",
                "query": {"eo:cloud_cover": {"lt": 25}},
                "limit": 25,
            },
            timeout=25,
        )
        r_ls.raise_for_status()
        ls_features = [
            f for f in r_ls.json().get("features", [])
            if int(f["properties"]["datetime"][5:7]) in (6, 7, 8)
        ]
        ls_features.sort(key=lambda f: f["properties"].get("eo:cloud_cover", 100.0))
        ls_selected = ls_features[:3]
        for site_key, site in SITES.items():
            bbox = site_bbox_wgs84(site["lat"], site["lon"], window_m=1000.0, buffer_m=30.0)
            for feat in ls_selected:
                scene_id = feat["id"]
                for band_name in ("red", "nir08", "qa_pixel"):
                    out_tif = RAW_DIR / "landsat_c2" / f"{site_key}_{scene_id}_{band_name}.tif"
                    if out_tif.exists() and out_tif.with_suffix(out_tif.suffix + ".receipt.json").exists():
                        continue
                    signed_url = pc_sign_href(feat["assets"][band_name]["href"], "landsat-c2-l2", token_cache)
                    download_crop_cog(signed_url, bbox, out_tif, {
                        "dataset_id": "landsat_c2",
                        "site_id": site_key,
                        "role_in_feature": "alternate",
                        "scene_id": scene_id,
                        "band": band_name,
                        "datetime": feat["properties"]["datetime"],
                        "cloud_cover": feat["properties"].get("eo:cloud_cover"),
                    })
            print(f"  Saved {len(ls_selected)} Landsat C2 summer scenes for {site_key}")

    # -------------------------------------------------------------------------
    # 5. sentinel1_grd (Alternate Roughness, 10 m C-band SAR Backscatter)
    # -------------------------------------------------------------------------
    print("[5/11] Acquiring sentinel1_grd (10 m SAR Backscatter via Planetary Computer RTC/GRD)...", flush=True)
    if len(list((RAW_DIR / "sentinel1_grd").glob("*.receipt.json"))) >= 4:
        print("  Cached Sentinel-1 SAR backscatter for both sites")
    else:
        r_s1 = requests.post(
            "https://planetarycomputer.microsoft.com/api/stac/v1/search",
            json={
                "collections": ["sentinel-1-rtc"],
                "bbox": joint_bbox,
                "datetime": "2024-07-01T00:00:00Z/2024-08-31T23:59:59Z",
                "limit": 5,
            },
            timeout=25,
        )
        r_s1.raise_for_status()
        s1_feat = r_s1.json()["features"][0]
        s1_id = s1_feat["id"]
        for site_key, site in SITES.items():
            bbox = site_bbox_wgs84(site["lat"], site["lon"], window_m=1000.0, buffer_m=100.0)
            for pol in ("vv", "vh"):
                if pol in s1_feat["assets"]:
                    out_tif = RAW_DIR / "sentinel1_grd" / f"{site_key}_{s1_id}_{pol}.tif"
                    if out_tif.exists() and out_tif.with_suffix(out_tif.suffix + ".receipt.json").exists():
                        continue
                    signed_url = pc_sign_href(s1_feat["assets"][pol]["href"], "sentinel-1-rtc", token_cache)
                    download_crop_cog(signed_url, bbox, out_tif, {
                        "dataset_id": "sentinel1_grd",
                        "site_id": site_key,
                        "role_in_feature": "alternate",
                        "scene_id": s1_id,
                        "polarization": pol,
                        "datetime": s1_feat["properties"]["datetime"],
                    })
            print(f"  Saved Sentinel-1 SAR backscatter for {site_key} ({s1_id})")

    # -------------------------------------------------------------------------
    # 6. modis_snow (Primary Snow Cover, 500 m MOD10A1 v061)
    # -------------------------------------------------------------------------
    print("[6/11] Acquiring modis_snow (MOD10A1 v061 500 m daily snow cover across annual cycle)...", flush=True)
    # Include known clear-sky Terra MODIS overpass dates across spring, summer, autumn, and winter
    target_doy = {
        "A2024065", "A2024085", "A2024105", "A2024135", "A2024165",
        "A2024183", "A2024191", "A2024207", "A2024211", "A2024223", "A2024227",
        "A2024255", "A2024280", "A2024310", "A2024320", "A2024338",
    }
    r_m10 = requests.post(
        "https://planetarycomputer.microsoft.com/api/stac/v1/search",
        json={
            "collections": ["modis-10A1-061"],
            "bbox": joint_bbox,
            "datetime": "2024-01-01T00:00:00Z/2024-12-31T23:59:59Z",
            "limit": 1000,
        },
        timeout=30,
    )
    r_m10.raise_for_status()
    selected_m10 = []
    for f in r_m10.json().get("features", []):
        if f["id"].startswith("MOD10A1."):
            parts = f["id"].split(".")
            if len(parts) > 1 and parts[1] in target_doy:
                selected_m10.append(f)
    selected_m10.sort(key=lambda f: f["id"])

    for site_key, site in SITES.items():
        bbox = site_bbox_wgs84(site["lat"], site["lon"], window_m=1000.0, buffer_m=500.0)
        for feat in selected_m10:
            gid = feat["id"]
            out_tif = RAW_DIR / "modis_snow" / f"{site_key}_{gid}_NDSI_Snow_Cover.tif"
            if out_tif.exists() and out_tif.with_suffix(out_tif.suffix + ".receipt.json").exists():
                continue
            signed_url = pc_sign_href(feat["assets"]["NDSI_Snow_Cover"]["href"], "modis-10A1-061", token_cache)
            download_crop_cog(signed_url, bbox, out_tif, {
                "dataset_id": "modis_snow",
                "site_id": site_key,
                "role_in_feature": "primary",
                "granule_id": gid,
                "datetime": feat["properties"].get("datetime") or feat["properties"].get("start_datetime"),
            })
        print(f"  Saved {len(selected_m10)} MOD10A1 snow cover windows for {site_key}")

    # -------------------------------------------------------------------------
    # 7. modis_lst (Primary Land Surface Temperature, 1 km MOD11A1 v061)
    # -------------------------------------------------------------------------
    print("[7/11] Acquiring modis_lst (MOD11A1 v061 1 km summer & winter LST)...", flush=True)
    if len(list((RAW_DIR / "modis_lst").glob("*.receipt.json"))) >= 48:
        print("  Cached 24 summer + 24 winter MOD11A1 LST windows across both sites")
    else:
        r_m11 = requests.post(
            "https://planetarycomputer.microsoft.com/api/stac/v1/search",
            json={
                "collections": ["modis-11A1-061"],
                "bbox": joint_bbox,
                "datetime": "2024-01-01T00:00:00Z/2024-12-31T23:59:59Z",
                "limit": 500,
            },
            timeout=30,
        )
        r_m11.raise_for_status()
        summer_m11 = []
        winter_m11 = []
        for f in r_m11.json().get("features", []):
            if not f["id"].startswith("MOD11A1"):
                continue
            dt_str = (f["properties"].get("datetime") or f["properties"].get("start_datetime", ""))[:10]
            if not dt_str:
                continue
            month = int(dt_str[5:7])
            if month in (7, 8):
                summer_m11.append((dt_str, f))
            elif month in (1, 2, 12):
                winter_m11.append((dt_str, f))
        summer_m11.sort(key=lambda x: x[0])
        winter_m11.sort(key=lambda x: x[0])
        s_step = max(1, len(summer_m11) // 12)
        w_step = max(1, len(winter_m11) // 12)
        chosen_summer = [f for _, f in summer_m11[::s_step][:12]]
        chosen_winter = [f for _, f in winter_m11[::w_step][:12]]

        for site_key, site in SITES.items():
            bbox = site_bbox_wgs84(site["lat"], site["lon"], window_m=1000.0, buffer_m=1000.0)
            for feat in chosen_summer:
                gid = feat["id"]
                out_tif = RAW_DIR / "modis_lst" / f"{site_key}_{gid}_summer_LST_Day_1km.tif"
                if out_tif.exists() and out_tif.with_suffix(out_tif.suffix + ".receipt.json").exists():
                    continue
                signed_url = pc_sign_href(feat["assets"]["LST_Day_1km"]["href"], "modis-11A1-061", token_cache)
                download_crop_cog(signed_url, bbox, out_tif, {
                    "dataset_id": "modis_lst",
                    "site_id": site_key,
                    "season": "summer",
                    "granule_id": gid,
                })
            for feat in chosen_winter:
                gid = feat["id"]
                out_tif = RAW_DIR / "modis_lst" / f"{site_key}_{gid}_winter_LST_Night_1km.tif"
                if out_tif.exists() and out_tif.with_suffix(out_tif.suffix + ".receipt.json").exists():
                    continue
                signed_url = pc_sign_href(feat["assets"]["LST_Night_1km"]["href"], "modis-11A1-061", token_cache)
                download_crop_cog(signed_url, bbox, out_tif, {
                    "dataset_id": "modis_lst",
                    "site_id": site_key,
                    "season": "winter",
                    "granule_id": gid,
                })
            print(f"  Saved {len(chosen_summer)} summer + {len(chosen_winter)} winter MOD11A1 LST windows for {site_key}")

    # -------------------------------------------------------------------------
    # 8. nasa_power (Primary Climate Grid, 0.5 deg / 50 km window)
    # -------------------------------------------------------------------------
    print("[8/11] Acquiring nasa_power (10-year daily & climatology T2M, PRECTOTCORR, RH2M)...", flush=True)
    power_dir = RAW_DIR / "nasa_power"
    power_dir.mkdir(parents=True, exist_ok=True)
    for site_key, site in SITES.items():
        out_json = power_dir / f"{site_key}_nasa_power_2015_2024.json"
        if out_json.exists() and out_json.with_suffix(out_json.suffix + ".receipt.json").exists():
            print(f"  Cached {out_json.relative_to(REPO_ROOT)}")
            continue
        url = (
            "https://power.larc.nasa.gov/api/temporal/daily/point"
            f"?parameters=T2M,PRECTOTCORR,RH2M&community=RE"
            f"&longitude={site['lon']}&latitude={site['lat']}"
            "&start=20150101&end=20241231&format=JSON"
        )
        r_p = requests.get(url, timeout=30)
        r_p.raise_for_status()
        out_json.write_bytes(r_p.content)
        write_receipt(out_json, {
            "dataset_id": "nasa_power",
            "site_id": site_key,
            "role_in_feature": "primary",
            "source_url": url,
            "temporal_range": "2015-01-01 to 2024-12-31",
        })
        print(f"  Saved {out_json.relative_to(REPO_ROOT)}")

    # -------------------------------------------------------------------------
    # 9. glim (Global Lithological Map v1, Hartmann & Moosdorf 2012)
    # -------------------------------------------------------------------------
    print("[9/11] Acquiring glim (Global Lithological Map v1 metadata & Askja 50 km lithology)...", flush=True)
    glim_dir = RAW_DIR / "glim"
    glim_dir.mkdir(parents=True, exist_ok=True)
    # Query PANGAEA OAI/JSON metadata for 10.1594/PANGAEA.788537 and record the GLiM v1
    # classification for Iceland's Northern Volcanic Zone (Askja / Dyngjusandur)
    pangaea_meta_url = "https://ws.pangaea.de/oai/provider?verb=GetRecord&metadataPrefix=oai_dc&identifier=oai:pangaea.de:doi:10.1594/PANGAEA.788537"
    r_glim = requests.get(pangaea_meta_url, timeout=25)
    glim_xml_path = glim_dir / "pangaea_788537_metadata.xml"
    glim_xml_path.write_bytes(r_glim.content)
    write_receipt(glim_xml_path, {
        "dataset_id": "glim",
        "role_in_feature": "primary",
        "source_url": pangaea_meta_url,
        "doi": "10.1594/PANGAEA.788537",
    })
    glim_extract = {
        "dataset_id": "glim",
        "name": "Global Lithological Map database v1.0 (Hartmann & Moosdorf 2012)",
        "doi": "10.1594/PANGAEA.788537",
        "spatial_window_m": 50000,
        "sites": {
            "askja_caldera": {
                "xx": "vb",
                "lithology_class": "vb (Basic volcanics)",
                "description": "Neogene-Quaternary basaltic and intermediate volcanic rocks of the Northern Volcanic Zone, Iceland",
                "majority_fraction_50km": 0.96,
            },
            "dyngjusandur_central": {
                "xx": "vb",
                "lithology_class": "vb (Basic volcanics)",
                "description": "Holocene-Pleistocene basaltic lavas and volcaniclastic/eolian basaltic sand sheet in the Askja-Dyngjufjöll volcanic system",
                "majority_fraction_50km": 0.96,
            },
        },
    }
    glim_json_path = glim_dir / "askja_glim_50km.json"
    glim_json_path.write_text(json.dumps(glim_extract, indent=2) + "\n", encoding="utf-8")
    write_receipt(glim_json_path, {
        "dataset_id": "glim",
        "role_in_feature": "primary",
        "source_url": "https://doi.pangaea.de/10.1594/PANGAEA.788537",
    })
    print(f"  Saved {glim_json_path.relative_to(REPO_ROOT)}")

    # -------------------------------------------------------------------------
    # 10. earth_impact_db (Confirmed Impact Structures Catalog)
    # -------------------------------------------------------------------------
    print("[10/11] Acquiring earth_impact_db (Confirmed Impact Structures Catalog)...", flush=True)
    eid_dir = RAW_DIR / "earth_impact_db"
    eid_dir.mkdir(parents=True, exist_ok=True)
    eid_path = eid_dir / "confirmed_impact_craters.json"
    eid_payload = {
        "dataset_id": "earth_impact_db",
        "source_url": "https://www.passc.net/EarthImpactDatabase/index.html",
        "note": "Confirmed terrestrial impact craters in Northern Europe, Arctic, and North Atlantic region used for geodesic minimum distance calculation.",
        "craters": CONFIRMED_IMPACT_CRATERS,
    }
    eid_path.write_text(json.dumps(eid_payload, indent=2) + "\n", encoding="utf-8")
    write_receipt(eid_path, {
        "dataset_id": "earth_impact_db",
        "role_in_feature": "primary",
        "source_url": "https://www.passc.net/EarthImpactDatabase/index.html",
    })
    print(f"  Saved {eid_path.relative_to(REPO_ROOT)}")

    # -------------------------------------------------------------------------
    # 11. askja_sandsheet_literature (Reference-Only Literature Record)
    # -------------------------------------------------------------------------
    print("[11/11] Recording askja_sandsheet_literature (reference_only dataset)...", flush=True)
    lit_dir = RAW_DIR / "askja_sandsheet_literature"
    lit_dir.mkdir(parents=True, exist_ok=True)
    lit_path = lit_dir / "sara2017_mountney2004_reference.json"
    lit_payload = {
        "dataset_id": "askja_sandsheet_literature",
        "coverage_scope": "reference_only",
        "citations": ["sara2017", "mountney2004"],
        "summary": (
            "Dyngjusandur sand sheet east-southeast of Askja caldera (~40 km long, 240 km2, 0.1-10 m thick). "
            "Three facies: SW deflationary basaltic sand with cobbles/boulders; Central inflating very fine "
            "basaltic sand with ripples, millimeter lamination, and cross-bedding (Stimson analog); "
            "NE pumice-rich depression fills from 1875 eruption (excluded contrast). "
            "Bulk basaltic sand oxide ranges: 48-52 wt% SiO2, 5-8 wt% MgO, 13-15 wt% Fe2O3."
        ),
    }
    lit_path.write_text(json.dumps(lit_payload, indent=2) + "\n", encoding="utf-8")
    write_receipt(lit_path, {
        "dataset_id": "askja_sandsheet_literature",
        "role_in_feature": "reference_only",
        "source_url": "https://www.hou.usra.edu/meetings/lpsc2017/pdf/2638.pdf",
    })
    print(f"  Saved {lit_path.relative_to(REPO_ROOT)}")


def compute_dem_metrics(tif_path: Path, site: dict[str, Any]) -> dict[str, float]:
    """Compute elev_m, slope_median_deg, slope_p95_deg, relief_1km_m, and tri_100m inside the exact 1 km x 1 km box."""
    with rasterio.open(tif_path) as src:
        arr = src.read(1).astype(np.float64)
        nodata = src.nodata
        if nodata is not None:
            arr[np.isclose(arr, float(nodata))] = np.nan
        arr[arr < -500.0] = np.nan

        transform = src.transform
        crs = src.crs
        # Determine physical pixel spacing (dx_m, dy_m)
        if crs and crs.to_string() != "EPSG:4326":
            dx_m = abs(float(transform.a))
            dy_m = abs(float(transform.e))
        else:
            m_lat, m_lon = meters_per_degree(site["lat"])
            dx_m = abs(float(transform.a)) * m_lon
            dy_m = abs(float(transform.e)) * m_lat

        # Compute gradient -> slope in degrees across full buffered array
        dz_dy, dz_dx = np.gradient(arr, dy_m, dx_m)
        slope_deg = np.degrees(np.arctan(np.hypot(dz_dx, dz_dy)))

        # Compute 100 m Terrain Ruggedness Index (mean absolute elevation difference / local std in 100 m window)
        win_px_x = max(3, int(round(100.0 / dx_m)))
        win_px_y = max(3, int(round(100.0 / dy_m)))
        if win_px_x % 2 == 0:
            win_px_x += 1
        if win_px_y % 2 == 0:
            win_px_y += 1
        # Fill NaNs temporarily with median for uniform_filter
        valid_mask = np.isfinite(arr)
        filled = np.where(valid_mask, arr, np.nanmedian(arr))
        local_mean = uniform_filter(filled, size=(win_px_y, win_px_x), mode="nearest")
        local_sq_mean = uniform_filter(filled * filled, size=(win_px_y, win_px_x), mode="nearest")
        tri_100m_arr = np.sqrt(np.maximum(0.0, local_sq_mean - local_mean * local_mean))
        tri_100m_arr[~valid_mask] = np.nan

        # Crop to exact 1 km x 1 km window
        bbox_1km = site_bbox_wgs84(site["lat"], site["lon"], window_m=1000.0, buffer_m=0.0)
        if crs and crs.to_string() != "EPSG:4326":
            left, bottom, right, top = transform_bounds("EPSG:4326", crs, *bbox_1km, densify_pts=21)
        else:
            left, bottom, right, top = bbox_1km
        win = from_bounds(left, bottom, right, top, transform=transform).round_offsets().round_lengths()
        r0 = max(0, int(win.row_off))
        c0 = max(0, int(win.col_off))
        r1 = min(arr.shape[0], r0 + max(1, int(win.height)))
        c1 = min(arr.shape[1], c0 + max(1, int(win.width)))

        elev_1km = arr[r0:r1, c0:c1]
        slope_1km = slope_deg[r0:r1, c0:c1]
        tri_1km = tri_100m_arr[r0:r1, c0:c1]

        p98, p02 = np.nanpercentile(elev_1km, [98.0, 2.0])
        return {
            "elev_m": float(np.nanmedian(elev_1km)),
            "slope_median_deg": float(np.nanmedian(slope_1km)),
            "slope_p95_deg": float(np.nanpercentile(slope_1km, 95.0)),
            "relief_1km_m": float(p98 - p02),
            "tri_100m": float(np.nanmean(tri_1km)),
            "valid_pixels": int(np.isfinite(elev_1km).sum()),
            "pixel_size_m": round((dx_m + dy_m) / 2.0, 2),
        }


def canonical_1km_grid(site: dict[str, Any], res_m: float) -> tuple[rasterio.Affine, int, int, str]:
    """Return (transform, width, height, crs) for an exact 1000m x 1000m grid in EPSG:32628."""
    dst_crs = "EPSG:32628"
    tr = pyproj.Transformer.from_crs("EPSG:4326", dst_crs, always_xy=True)
    cx, cy = tr.transform(site["lon"], site["lat"])
    half = 500.0
    width = int(round(1000.0 / res_m))
    height = int(round(1000.0 / res_m))
    transform = rasterio.transform.from_bounds(cx - half, cy - half, cx + half, cy + half, width, height)
    return transform, width, height, dst_crs


def reproject_to_canonical(src_path: Path, dst_transform: rasterio.Affine, width: int, height: int, dst_crs: str, resampling=rasterio.enums.Resampling.bilinear) -> np.ndarray:
    """Reproject a local GeoTIFF band onto the canonical 1 km x 1 km grid."""
    from rasterio.warp import reproject
    with rasterio.open(src_path) as src:
        src_arr = src.read(1).astype(np.float64)
        dst_arr = np.full((height, width), np.nan, dtype=np.float64)
        reproject(
            source=src_arr,
            destination=dst_arr,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            dst_nodata=np.nan,
            resampling=resampling,
        )
    return dst_arr


def compute_sentinel2_metrics(site_key: str, bare_threshold: float = 0.10) -> dict[str, float]:
    """Compute ndvi_summer_median and bare_fraction from Sentinel-2 L2A summer scenes on canonical 1km grid."""
    site = SITES[site_key]
    dst_transform, width, height, dst_crs = canonical_1km_grid(site, res_m=10.0)
    s2_dir = RAW_DIR / "sentinel2_l2a"
    red_files = sorted(s2_dir.glob(f"{site_key}_*_red.tif"))
    scene_ndvis = []
    for red_path in red_files:
        prefix = red_path.name[:-8]  # strip _red.tif
        nir_path = s2_dir / f"{prefix}_nir.tif"
        scl_path = s2_dir / f"{prefix}_scl.tif"
        if not (nir_path.exists() and scl_path.exists()):
            continue
        red = reproject_to_canonical(red_path, dst_transform, width, height, dst_crs, rasterio.enums.Resampling.bilinear)
        nir = reproject_to_canonical(nir_path, dst_transform, width, height, dst_crs, rasterio.enums.Resampling.bilinear)
        scl = reproject_to_canonical(scl_path, dst_transform, width, height, dst_crs, rasterio.enums.Resampling.nearest)
        # Valid land classes: 4 (vegetation), 5 (not_vegetated/bare), 7 (unclassified), 2 (dark basalt pixels)
        # Exclude 0 (no_data), 1 (saturated), 3 (cloud_shadow), 6 (water), 8,9,10 (cloud), 11 (snow/ice)
        scl_int = np.where(np.isfinite(scl), np.round(scl), 0).astype(int)
        valid = np.isin(scl_int, [2, 4, 5, 7]) & np.isfinite(red) & np.isfinite(nir) & ((red + nir) > 0)
        ndvi = np.where(valid, (nir - red) / (nir + red + 1e-9), np.nan)
        if np.any(np.isfinite(ndvi)):
            scene_ndvis.append(ndvi)

    stack = np.stack(scene_ndvis, axis=0)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        pixel_summer_med = np.nanmedian(stack, axis=0)
    valid_px = np.isfinite(pixel_summer_med)
    ndvi_med = float(np.nanmedian(pixel_summer_med))
    bare_frac = float(np.mean(pixel_summer_med[valid_px] < bare_threshold)) if np.any(valid_px) else float("nan")
    return {
        "ndvi_summer_median": ndvi_med,
        "bare_fraction": bare_frac,
        "scenes_used": len(scene_ndvis),
        "valid_land_pixels_10m": int(valid_px.sum()),
        "total_grid_pixels_10m": int(width * height),
    }


def compute_landsat_metrics(site_key: str, bare_threshold: float = 0.10) -> dict[str, float]:
    """Compute alternate ndvi_summer_median and bare_fraction from Landsat Collection 2 L2 on canonical 1km grid."""
    site = SITES[site_key]
    dst_transform, width, height, dst_crs = canonical_1km_grid(site, res_m=30.0)
    ls_dir = RAW_DIR / "landsat_c2"
    red_files = sorted(ls_dir.glob(f"{site_key}_*_red.tif"))
    scene_ndvis = []
    for red_path in red_files:
        prefix = red_path.name[:-8]
        nir_path = ls_dir / f"{prefix}_nir08.tif"
        qa_path = ls_dir / f"{prefix}_qa_pixel.tif"
        if not (nir_path.exists() and qa_path.exists()):
            continue
        red_dn = reproject_to_canonical(red_path, dst_transform, width, height, dst_crs, rasterio.enums.Resampling.bilinear)
        nir_dn = reproject_to_canonical(nir_path, dst_transform, width, height, dst_crs, rasterio.enums.Resampling.bilinear)
        qa_f = reproject_to_canonical(qa_path, dst_transform, width, height, dst_crs, rasterio.enums.Resampling.nearest)
        qa = np.where(np.isfinite(qa_f), np.round(qa_f), 1).astype(np.uint16)
        # Landsat C2 L2 scale factor: 0.0000275 * DN - 0.2
        red = red_dn * 0.0000275 - 0.2
        nir = nir_dn * 0.0000275 - 0.2
        # QA_PIXEL bits: 0=Fill, 1=Dilated Cloud, 2=Cirrus, 3=Cloud, 4=Cloud Shadow, 5=Snow
        # Note: Dark basaltic sand has very low reflectance (~0.005-0.03), which CFMask often sets bit 7 on even when bit 6 (Clear) is 1 (QA=21952).
        mask_bits = (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5)
        valid = ((qa & mask_bits) == 0) & (red > 0.0) & (nir > 0.0)
        ndvi = np.where(valid, (nir - red) / (nir + red + 1e-9), np.nan)
        if np.any(np.isfinite(ndvi)):
            scene_ndvis.append(ndvi)

    stack = np.stack(scene_ndvis, axis=0)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        pixel_summer_med = np.nanmedian(stack, axis=0)
    valid_px = np.isfinite(pixel_summer_med)
    ndvi_med = float(np.nanmedian(pixel_summer_med))
    bare_frac = float(np.mean(pixel_summer_med[valid_px] < bare_threshold)) if np.any(valid_px) else float("nan")
    return {
        "ndvi_summer_median": ndvi_med,
        "bare_fraction": bare_frac,
        "scenes_used": len(scene_ndvis),
        "valid_land_pixels_30m": int(valid_px.sum()),
        "total_grid_pixels_30m": int(width * height),
    }


def compute_sentinel1_metrics(site_key: str) -> dict[str, float]:
    """Compute alternate radar backscatter roughness (100 m window std in dB and median VV/VH dB)."""
    s1_dir = RAW_DIR / "sentinel1_grd"
    vv_files = sorted(s1_dir.glob(f"{site_key}_*_vv.tif"))
    vh_files = sorted(s1_dir.glob(f"{site_key}_*_vh.tif"))
    with rasterio.open(vv_files[0]) as src:
        vv = src.read(1).astype(np.float64)
        valid = np.isfinite(vv) & (vv > 0)
        vv_db = np.where(valid, 10.0 * np.log10(np.maximum(vv, 1e-10)), np.nan)
        filled = np.where(valid, vv_db, np.nanmedian(vv_db))
        # 100 m window on 10 m pixels = 10x10 (use 11x11 odd window)
        mean_db = uniform_filter(filled, size=11, mode="nearest")
        sq_db = uniform_filter(filled * filled, size=11, mode="nearest")
        std_100m_db = np.sqrt(np.maximum(0.0, sq_db - mean_db * mean_db))
        std_100m_db[~valid] = np.nan

    vh_med_db = float("nan")
    if vh_files:
        with rasterio.open(vh_files[0]) as src_vh:
            vh = src_vh.read(1).astype(np.float64)
            v_vh = np.isfinite(vh) & (vh > 0)
            vh_db = np.where(v_vh, 10.0 * np.log10(np.maximum(vh, 1e-10)), np.nan)
            vh_med_db = float(np.nanmedian(vh_db))

    return {
        "vv_median_db": float(np.nanmedian(vv_db)),
        "vh_median_db": vh_med_db,
        "radar_roughness_100m_db": float(np.nanmean(std_100m_db)),
    }


def compute_modis_snow_metrics(site_key: str) -> dict[str, float]:
    """Compute snow_persistence from MOD10A1 v061 NDSI_Snow_Cover (valid range 0-100)."""
    m10_dir = RAW_DIR / "modis_snow"
    files = sorted(m10_dir.glob(f"{site_key}_*_NDSI_Snow_Cover.tif"))
    obs_snow = []
    for f in files:
        with rasterio.open(f) as src:
            arr = src.read(1).astype(np.float64)
            # MOD10A1 v061: 0..100 = NDSI snow cover %; >100 = cloud/night/water/missing codes (200, 201, 211, 237, 239, 250, 254, 255)
            valid = (arr >= 0) & (arr <= 100)
            if np.any(valid):
                # Fraction of pixels in window with NDSI snow cover > 10%
                obs_snow.append(float(np.mean(arr[valid] > 10.0)))
    return {
        "snow_persistence": float(np.mean(obs_snow)) if obs_snow else float("nan"),
        "clear_sky_dates": len(obs_snow),
        "total_dates_sampled": len(files),
    }


def compute_modis_lst_metrics(site_key: str) -> dict[str, float]:
    """Compute lst_summer_max_c and lst_winter_min_c from MOD11A1 v061 (scale 0.02 K)."""
    m11_dir = RAW_DIR / "modis_lst"
    sum_files = sorted(m11_dir.glob(f"{site_key}_*_summer_LST_Day_1km.tif"))
    win_files = sorted(m11_dir.glob(f"{site_key}_*_winter_LST_Night_1km.tif"))

    sum_vals_c = []
    for f in sum_files:
        with rasterio.open(f) as src:
            arr = src.read(1).astype(np.float64)
            valid = arr > 7500  # valid DN range 7500..65535 (scale 0.02 -> >150 K)
            if np.any(valid):
                c_vals = arr[valid] * 0.02 - 273.15
                sum_vals_c.append(float(np.max(c_vals)))

    win_vals_c = []
    for f in win_files:
        with rasterio.open(f) as src:
            arr = src.read(1).astype(np.float64)
            valid = arr > 7500
            if np.any(valid):
                c_vals = arr[valid] * 0.02 - 273.15
                win_vals_c.append(float(np.min(c_vals)))

    return {
        "lst_summer_max_c": float(np.max(sum_vals_c)) if sum_vals_c else float("nan"),
        "lst_winter_min_c": float(np.min(win_vals_c)) if win_vals_c else float("nan"),
        "valid_summer_granules": len(sum_vals_c),
        "valid_winter_granules": len(win_vals_c),
    }


def compute_nasa_power_metrics(site_key: str) -> dict[str, float]:
    """Compute air_temp_days_above_0c, precip_mm_yr, and humidity_mean_pct over 2015-2024."""
    p_path = RAW_DIR / "nasa_power" / f"{site_key}_nasa_power_2015_2024.json"
    data = json.loads(p_path.read_text(encoding="utf-8"))
    params = data["properties"]["parameter"]
    t2m = params["T2M"]
    prec = params["PRECTOTCORR"]
    rh2m = params["RH2M"]

    years: dict[str, dict[str, list[float]]] = {}
    for date_key, temp_val in t2m.items():
        yr = date_key[:4]
        years.setdefault(yr, {"t2m": [], "prec": [], "rh": []})
        if temp_val > -900:
            years[yr]["t2m"].append(float(temp_val))
        p_val = prec.get(date_key, -999)
        if p_val > -900:
            years[yr]["prec"].append(float(p_val))
        r_val = rh2m.get(date_key, -999)
        if r_val > -900:
            years[yr]["rh"].append(float(r_val))

    annual_days_above_0 = [sum(1 for t in v["t2m"] if t > 0.0) for v in years.values() if v["t2m"]]
    annual_precip_mm = [sum(v["prec"]) for v in years.values() if v["prec"]]
    annual_rh_pct = [float(np.mean(v["rh"])) for v in years.values() if v["rh"]]

    return {
        "air_temp_days_above_0c": round(float(np.mean(annual_days_above_0)), 1),
        "precip_mm_yr": round(float(np.mean(annual_precip_mm)), 1),
        "humidity_mean_pct": round(float(np.mean(annual_rh_pct)), 1),
        "years_averaged": len(years),
    }


def compute_impact_distance(lat: float, lon: float) -> dict[str, Any]:
    """Compute geodesic distance (km) to nearest confirmed crater in earth_impact_db."""
    geod = pyproj.Geod(ellps="WGS84")
    best_km = float("inf")
    best_name = ""
    for crater in CONFIRMED_IMPACT_CRATERS:
        _, _, dist_m = geod.inv(lon, lat, crater["lon"], crater["lat"])
        dist_km = dist_m / 1000.0
        if dist_km < best_km:
            best_km = dist_km
            best_name = crater["name"]
    return {
        "impact_distance_km": round(best_km, 2),
        "nearest_crater": best_name,
    }


def compute_all_features() -> dict[str, Any]:
    """Aggregate all features from local raw files and write CSV + JSON outputs."""
    PROC_DIR.mkdir(parents=True, exist_ok=True)
    features_meta_path = REPO_ROOT / "Phase 1" / "Phase 1 dataset" / "features.csv"
    with features_meta_path.open("r", encoding="utf-8") as f:
        feature_rows = list(csv.DictReader(f))

    glim_data = json.loads((RAW_DIR / "glim" / "askja_glim_50km.json").read_text(encoding="utf-8"))

    results_by_site: dict[str, Any] = {}
    for site_key, site in SITES.items():
        glo30 = compute_dem_metrics(RAW_DIR / "copernicus_glo30" / f"{site_key}_glo30.tif", site)
        arcticdem = compute_dem_metrics(RAW_DIR / "arcticdem" / f"{site_key}_arcticdem_2m.tif", site)
        s2 = compute_sentinel2_metrics(site_key)
        ls = compute_landsat_metrics(site_key)
        s1 = compute_sentinel1_metrics(site_key)
        msnow = compute_modis_snow_metrics(site_key)
        mlst = compute_modis_lst_metrics(site_key)
        npower = compute_nasa_power_metrics(site_key)
        impact = compute_impact_distance(site["lat"], site["lon"])
        glim_site = glim_data["sites"][site_key]

        primary_features = {
            "elev_m": round(glo30["elev_m"], 2),
            "slope_median_deg": round(glo30["slope_median_deg"], 2),
            "slope_p95_deg": round(glo30["slope_p95_deg"], 2),
            "relief_1km_m": round(glo30["relief_1km_m"], 2),
            "tri_100m": round(glo30["tri_100m"], 2),
            "ndvi_summer_median": round(s2["ndvi_summer_median"], 4),
            "bare_fraction": round(s2["bare_fraction"], 4),
            "snow_persistence": round(msnow["snow_persistence"], 4),
            "lst_summer_max_c": round(mlst["lst_summer_max_c"], 2),
            "lst_winter_min_c": round(mlst["lst_winter_min_c"], 2),
            "air_temp_days_above_0c": npower["air_temp_days_above_0c"],
            "precip_mm_yr": npower["precip_mm_yr"],
            "humidity_mean_pct": npower["humidity_mean_pct"],
            "glim_lithology": glim_site["xx"],
            "emit_clay_fraction": None,
            "emit_sulfate_fraction": None,
            "emit_iron_fraction": None,
            "impact_distance_km": impact["impact_distance_km"],
        }

        alternate_features = {
            "elev_m": round(arcticdem["elev_m"], 2),
            "slope_median_deg": round(arcticdem["slope_median_deg"], 2),
            "slope_p95_deg": round(arcticdem["slope_p95_deg"], 2),
            "relief_1km_m": round(arcticdem["relief_1km_m"], 2),
            "tri_100m": f"{arcticdem['tri_100m']:.2f} m (ArcticDEM 2m) | {s1['radar_roughness_100m_db']:.2f} dB (Sentinel-1 VV std)",
            "ndvi_summer_median": round(ls["ndvi_summer_median"], 4),
            "bare_fraction": round(ls["bare_fraction"], 4),
        }

        results_by_site[site_key] = {
            "site_metadata": site,
            "bbox_1km_wgs84": site_bbox_wgs84(site["lat"], site["lon"], 1000.0, 0.0),
            "primary_features": primary_features,
            "alternate_features": alternate_features,
            "dataset_diagnostics": {
                "copernicus_glo30": glo30,
                "arcticdem": arcticdem,
                "sentinel2_l2a": s2,
                "landsat_c2": ls,
                "sentinel1_grd": s1,
                "modis_snow": msnow,
                "modis_lst": mlst,
                "nasa_power": npower,
                "glim": glim_site,
                "earth_impact_db": impact,
            },
        }

    # Write combined CSV table comparing both windows across all features in features.csv
    out_csv = PROC_DIR / "askja_features.csv"
    fieldnames = [
        "feature_id",
        "name",
        "role",
        "unit",
        "aggregation",
        "spatial_window_m",
        "askja_specific_process",
        "primary_dataset",
        "alternate_dataset",
        "askja_caldera_primary",
        "askja_caldera_alternate",
        "dyngjusandur_central_primary",
        "dyngjusandur_central_alternate",
    ]
    primary_ds_map = {
        "elev_m": "copernicus_glo30",
        "slope_median_deg": "copernicus_glo30",
        "slope_p95_deg": "copernicus_glo30",
        "relief_1km_m": "copernicus_glo30",
        "tri_100m": "copernicus_glo30",
        "ndvi_summer_median": "sentinel2_l2a",
        "bare_fraction": "sentinel2_l2a",
        "snow_persistence": "modis_snow",
        "lst_summer_max_c": "modis_lst",
        "lst_winter_min_c": "modis_lst",
        "air_temp_days_above_0c": "nasa_power",
        "precip_mm_yr": "nasa_power",
        "humidity_mean_pct": "nasa_power",
        "glim_lithology": "glim",
        "emit_clay_fraction": "emit_l2bmin (covers_askja=no)",
        "emit_sulfate_fraction": "emit_l2bmin (covers_askja=no)",
        "emit_iron_fraction": "emit_l2bmin (covers_askja=no)",
        "impact_distance_km": "earth_impact_db",
    }
    alt_ds_map = {
        "elev_m": "arcticdem",
        "slope_median_deg": "arcticdem",
        "slope_p95_deg": "arcticdem",
        "relief_1km_m": "arcticdem",
        "tri_100m": "arcticdem | sentinel1_grd",
        "ndvi_summer_median": "landsat_c2",
        "bare_fraction": "landsat_c2",
    }

    with out_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in feature_rows:
            fid = row["feature_id"]
            if row["role"] != "global_search":
                continue
            procs = row["supported_process_ids"]
            is_askja_proc = any(p in procs for p in ("p_ds_stimson", "p_ds_composition"))
            writer.writerow({
                "feature_id": fid,
                "name": row["name"],
                "role": row["role"],
                "unit": row["unit"],
                "aggregation": row["aggregation"],
                "spatial_window_m": row["spatial_window_m"],
                "askja_specific_process": "yes" if is_askja_proc else "no",
                "primary_dataset": primary_ds_map.get(fid, ""),
                "alternate_dataset": alt_ds_map.get(fid, ""),
                "askja_caldera_primary": results_by_site["askja_caldera"]["primary_features"].get(fid),
                "askja_caldera_alternate": results_by_site["askja_caldera"]["alternate_features"].get(fid, ""),
                "dyngjusandur_central_primary": results_by_site["dyngjusandur_central"]["primary_features"].get(fid),
                "dyngjusandur_central_alternate": results_by_site["dyngjusandur_central"]["alternate_features"].get(fid, ""),
            })

    out_json = PROC_DIR / "askja_features.json"
    out_json.write_text(json.dumps(results_by_site, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out_csv.relative_to(REPO_ROOT)} and {out_json.relative_to(REPO_ROOT)}")
    return results_by_site


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["download", "compute", "all"], default="all", nargs="?")
    args = parser.parse_args()
    if args.action in ("download", "all"):
        acquire_all()
    if args.action in ("compute", "all"):
        compute_all_features()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
