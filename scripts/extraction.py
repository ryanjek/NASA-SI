"""Single-module geospatial patch extraction and Phase 1 batch runner.

Source: consolidated legacy analog_patch/core.py, sources.py, cli.py,
and scripts/extract_all_phase1.py. Missing features remain null.
"""
from __future__ import annotations
import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd
import requests
from pyproj import CRS, Transformer
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject

# --- Geometry and numerical feature computations ---
@dataclass(frozen=True)
class Grid:
    crs: CRS
    transform: object
    shape: tuple[int, int]
    bounds_wgs84: tuple[float, float, float, float]
    resolution_m: float


def patch_grid(lat: float, lon: float, size_m: float = 1000, resolution_m: float = 30) -> Grid:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError('Invalid geographic coordinate')
    if size_m <= 0 or resolution_m <= 0:
        raise ValueError('Dimensions must be positive')
    if lat >= 84 or lat <= -80:
        epsg = 3413 if lat >= 0 else 3031
    else:
        zone = int((lon + 180) // 6) + 1
        zone = min(zone, 60)
        epsg = (32600 if lat >= 0 else 32700) + zone
    crs = CRS.from_epsg(epsg)
    to_xy = Transformer.from_crs(4326, crs, always_xy=True)
    to_ll = Transformer.from_crs(crs, 4326, always_xy=True)
    cx, cy = to_xy.transform(lon, lat)
    pixels = int(math.ceil(size_m / resolution_m))
    effective_res = size_m / pixels
    xmin, xmax = cx - size_m/2, cx + size_m/2
    ymin, ymax = cy - size_m/2, cy + size_m/2
    corners = [to_ll.transform(x,y) for x in (xmin,xmax) for y in (ymin,ymax)]
    west, east = min(p[0] for p in corners), max(p[0] for p in corners)
    south, north = min(p[1] for p in corners), max(p[1] for p in corners)
    return Grid(crs, from_origin(xmin, ymax, effective_res, effective_res), (pixels,pixels),
                (west,south,east,north), effective_res)


# --- Optional run context (metrics + batched I/O). ---
# With no context set (the default, e.g. Phase 2 direct calls) every function below
# behaves exactly as before: one STAC client per call site, one open per raster read.
import threading as _threading
import time as _time
from contextlib import contextmanager as _contextmanager

_GDAL_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR', CPL_VSIL_CURL_ALLOWED_EXTENSIONS='.tif,.TIF,.tiff')


class RunMetrics:
    """Thread-safe counters and summed elapsed seconds (summed across threads)."""

    def __init__(self):
        self._lock = _threading.Lock()
        self.counters: dict[str, int] = {}
        self.timers: dict[str, float] = {}

    def add(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + n

    def add_time(self, name: str, seconds: float) -> None:
        with self._lock:
            self.timers[name] = self.timers.get(name, 0.0) + seconds

    @_contextmanager
    def timed(self, name: str):
        t0 = _time.perf_counter()
        try:
            yield
        finally:
            self.add_time(name, _time.perf_counter() - t0)

    def snapshot(self) -> dict:
        with self._lock:
            return {'counters': dict(sorted(self.counters.items())),
                    'timers_s': {k: round(v, 3) for k, v in sorted(self.timers.items())}}


# 'metrics': RunMetrics | None; 'batch': object with lookup()/query()/client/power or None.
_RUN = {'metrics': None, 'batch': None}
_DIAG = _threading.local()


def set_run_context(metrics=None, batch=None) -> None:
    _RUN['metrics'] = metrics
    _RUN['batch'] = batch


def _count(name: str, n: int = 1) -> None:
    m = _RUN['metrics']
    if m is not None:
        m.add(name, n)


def _timed_source(name):
    """Decorator: record elapsed time of a source function into the active metrics."""
    def wrap(fn):
        import functools

        @functools.wraps(fn)
        def inner(*a, **k):
            m = _RUN['metrics']
            if m is None:
                return fn(*a, **k)
            with m.timed(name):
                return fn(*a, **k)
        return inner
    return wrap


def _diag_put(key, value) -> None:
    """Record benchmark diagnostics for the current thread's cell, when enabled."""
    store = getattr(_DIAG, 'store', None)
    if store is not None:
        store[key] = value


def asset_identity(href: str) -> str:
    """Stable asset identity: the URL without its (expiring) signature query string."""
    return href.split('?', 1)[0]


def grid_key(grid: Grid) -> tuple:
    return (grid.crs.to_wkt(), tuple(grid.transform)[:6], tuple(grid.shape))


def warp_read(src, grid: Grid, band: int = 1, resampling=Resampling.bilinear) -> np.ndarray:
    """The exact per-child WarpedVRT read used by read_reprojected, on an open dataset."""
    from rasterio.vrt import WarpedVRT
    with WarpedVRT(src, crs=grid.crs.to_wkt(), transform=grid.transform,
                   width=grid.shape[1], height=grid.shape[0],
                   resampling=resampling, nodata=np.nan) as vrt:
        data = vrt.read(band, masked=True).astype('float64')
    return np.asarray(data.filled(np.nan), dtype=np.float64)


def read_reprojected(href: str, grid: Grid, band: int = 1, resampling=Resampling.bilinear) -> np.ndarray:
    """Windowed VRT read, no full raster download; returns NaN for invalid pixels."""
    batch = _RUN['batch']
    if batch is not None:
        cached = batch.lookup(href, grid, band, resampling)
        if cached is not None:
            _count('raster_cache_hits')
            return cached
    import rasterio
    from rasterio.env import Env
    # HTTP ranges for remote GeoTIFF/COG access, where supported by provider.
    _count('raster_opens')
    try:
        with Env(**_GDAL_ENV):
            with rasterio.open(href) as src:
                return warp_read(src, grid, band, resampling)
    except Exception:
        _count('raster_failed_reads')
        raise


def _mean_neighbor_tri(arr: np.ndarray, valid: np.ndarray) -> float | None:
    """Mean absolute elevation difference to 8 immediate neighbors across valid cells."""
    offsets = [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]
    differences = []
    for y, x in offsets:
        shifted = np.roll(np.roll(arr, y, axis=0), x, axis=1)
        d = np.abs(arr - shifted)
        mask = valid & np.roll(np.roll(valid, y, axis=0), x, axis=1)
        if y > 0: mask[:y, :] = False
        if y < 0: mask[y:, :] = False
        if x > 0: mask[:, :x] = False
        if x < 0: mask[:, x:] = False
        differences.append(np.where(mask, d, np.nan))
    with np.errstate(invalid='ignore'):
        tri = np.nanmean(np.stack(differences), axis=0)
    return float(np.nanmean(tri)) if np.isfinite(tri).any() else None


def dem_features(dem: np.ndarray, pixel_m: float) -> dict:
    valid = np.isfinite(dem)
    coverage = float(valid.mean())
    result = dict(elev_m=None, slope_median_deg=None, slope_p95_deg=None,
                  relief_1km_m=None, tri_100m=None, dem_valid_fraction=coverage)
    if valid.sum() < 9 or coverage < 0.8:
        return result
    arr = dem.astype(float).copy()
    # Prevent gradients over no-data holes from propagating into valid terrain.
    if not valid.all():
        from scipy.ndimage import distance_transform_edt
        idx = distance_transform_edt(~valid, return_distances=False, return_indices=True)
        arr[~valid] = arr[tuple(idx[:,~valid])]
    gy, gx = np.gradient(arr, pixel_m, pixel_m)
    slope = np.degrees(np.arctan(np.hypot(gx,gy)))
    result['elev_m'] = float(np.median(dem[valid]))
    result['slope_median_deg'] = float(np.median(slope[valid]))
    result['slope_p95_deg'] = float(np.percentile(slope[valid],95))
    result['relief_1km_m'] = float(np.percentile(dem[valid],98)-np.percentile(dem[valid],2))
    result['tri_neighbor_m'] = _mean_neighbor_tri(arr, valid)

    # Standardized 100 m TRI: resample DEM to a 100 m grid via area-averaging, then compute 8-neighbor TRI.
    h, w = dem.shape
    h100 = max(3, int(round(h * pixel_m / 100.0)))
    w100 = max(3, int(round(w * pixel_m / 100.0)))
    src_transform = from_origin(0.0, h * pixel_m, pixel_m, pixel_m)
    dst_transform = from_origin(0.0, h * pixel_m, 100.0, 100.0)
    dem_100 = np.full((h100, w100), np.nan, dtype=np.float64)
    reproject(
        source=dem.astype(np.float64),
        destination=dem_100,
        src_transform=src_transform,
        src_crs='EPSG:3857',
        dst_transform=dst_transform,
        dst_crs='EPSG:3857',
        src_nodata=np.nan,
        dst_nodata=np.nan,
        resampling=Resampling.average,
    )
    valid_100 = np.isfinite(dem_100)
    result['tri_100m'] = _mean_neighbor_tri(dem_100, valid_100) if valid_100.sum() >= 9 else None
    return result


def _ndvi_summary(ndvi: np.ndarray, bare_ndvi_threshold: float = 0.2) -> dict:
    valid = np.isfinite(ndvi)
    return dict(ndvi_summer_median=float(np.nanmedian(ndvi[valid])) if valid.any() else None,
                bare_fraction=float((ndvi[valid] < bare_ndvi_threshold).mean()) if valid.any() else None,
                optical_valid_fraction=float(valid.mean()),
                bare_ndvi_threshold=bare_ndvi_threshold)


def optical_features(red: np.ndarray, nir: np.ndarray, valid_mask: np.ndarray | None = None,
                     bare_ndvi_threshold: float = 0.2) -> dict:
    valid = np.isfinite(red) & np.isfinite(nir) & ((red+nir)>0)
    if valid_mask is not None: valid &= valid_mask.astype(bool)
    ndvi=np.full(red.shape,np.nan,dtype=float)
    ndvi[valid]=(nir[valid]-red[valid])/(nir[valid]+red[valid])
    return _ndvi_summary(ndvi, bare_ndvi_threshold=bare_ndvi_threshold)

# --- STAC raster sources ---
"""Catalog search and cloud-window reads. Real I/O requires network access."""

STAC_URL='https://planetarycomputer.microsoft.com/api/stac/v1'

def catalog():
    batch = _RUN['batch']
    if batch is not None and getattr(batch, 'client', None) is not None:
        _count('stac_catalog_reuse')
        return batch.client
    import planetary_computer
    from pystac_client import Client
    _count('stac_catalog_opens')
    return Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)

def query_items_live(client, collection, grid, date_range=None, limit=12):
    """The original per-cell STAC search (bbox intersects, server order, max_items cap)."""
    args=dict(collections=[collection],bbox=list(grid.bounds_wgs84),max_items=limit)
    if date_range: args['datetime']=date_range
    _count('stac_searches')
    return list(client.search(**args).items())

def query_items(client, collection, grid, date_range=None, limit=12):
    batch = _RUN['batch']
    if batch is not None:
        return batch.query(client, collection, grid, date_range, limit, query_items_live)
    return query_items_live(client, collection, grid, date_range, limit)

@_timed_source('dem')
def dem_from_stac(client, grid):
    """Mosaic DEM tiles where necessary, recording scene IDs and invalid coverage."""
    from rasterio.enums import Resampling
    items=query_items(client,'cop-dem-glo-30',grid,limit=12)
    if not items: raise RuntimeError('No Copernicus GLO-30 tiles found')
    combined=np.full(grid.shape,np.nan)
    ids=[]
    errors=[]
    for item in items:
        asset=item.assets.get('data')
        if not asset:
            errors.append(f'{item.id}: no data asset')
            continue
        try:
            arr=read_reprojected(asset.href,grid,resampling=Resampling.bilinear)
            good=np.isfinite(arr) & ~np.isfinite(combined)
            combined[good]=arr[good]
            if good.any(): ids.append(item.id)
        except Exception as exc:
            errors.append(f'{item.id}: {exc}')
    if not np.isfinite(combined).any():
        raise RuntimeError('DEM read failed: '+'; '.join(errors[:3]))
    return combined,dict(collection='cop-dem-glo-30',item_ids=ids,errors=errors,
                         native_resolution='~30m')

def _safe_cloud(item):
    return float(item.properties.get('eo:cloud_cover',100) or 100)

# Asset key and resampling for each optical read; shared by the reader and batch prefetch.
OPTICAL_ASSETS = {
    'sentinel-2-l2a': (('B04', Resampling.bilinear), ('B08', Resampling.bilinear), ('SCL', Resampling.nearest)),
    'landsat-c2-l2': (('red', Resampling.bilinear), ('nir08', Resampling.bilinear), ('qa_pixel', Resampling.nearest)),
}

def _read_optical_ndvi(collection: str, item, grid: Grid) -> np.ndarray | None:
    specs = OPTICAL_ASSETS['sentinel-2-l2a' if collection == 'sentinel-2-l2a' else 'landsat-c2-l2']
    if any(k not in item.assets for k, _ in specs):
        return None
    (k_red, r_red), (k_nir, r_nir), (k_qa, r_qa) = specs
    if collection == 'sentinel-2-l2a':
        red = read_reprojected(item.assets[k_red].href, grid, resampling=r_red)
        nir = read_reprojected(item.assets[k_nir].href, grid, resampling=r_nir)
        scl = read_reprojected(item.assets[k_qa].href, grid, resampling=r_qa)
        # Cloud shadow, cloud medium/high, cirrus, snow, no data, saturation; retain vegetation and bare.
        mask = np.isin(scl, [4, 5, 6, 7]) & (red >= 0) & (nir >= 0) & ((red + nir) > 0)
    else:
        red = read_reprojected(item.assets[k_red].href, grid, resampling=r_red) * 0.0000275 - 0.2
        nir = read_reprojected(item.assets[k_nir].href, grid, resampling=r_nir) * 0.0000275 - 0.2
        qa = read_reprojected(item.assets[k_qa].href, grid, resampling=r_qa)
        qi = np.nan_to_num(qa, nan=65535).astype(np.uint16)
        # bits 0 fill, 1 dilated cloud, 2 cirrus, 3 cloud, 4 shadow, 5 snow
        mask = ((qi & 0b111111) == 0) & (red >= 0) & (nir >= 0) & ((red + nir) > 0)
    ndvi = np.full(grid.shape, np.nan, dtype=float)
    ndvi[mask] = (nir[mask] - red[mask]) / (nir[mask] + red[mask])
    return ndvi


def optical_query_summer(client, grid, lat, y0, y1, limit, attempts):
    """Hemisphere-specific summer candidates: S2 then Landsat, 80 returned, month filter, cloud sort, cap."""
    season = {12, 1, 2} if lat < 0 else {6, 7, 8}
    start, end = (f'{y0}-12-01', f'{y1}-02-28') if lat < 0 else (f'{y0}-06-01', f'{y1}-08-31')
    found = []
    for collection in ('sentinel-2-l2a', 'landsat-c2-l2'):
        try:
            items = query_items(client, collection, grid, f'{start}/{end}', limit=80)
            items = [i for i in items if i.datetime and i.datetime.month in season]
            items = sorted(items, key=_safe_cloud)[:limit]
            found.extend((collection, i) for i in items)
        except Exception as exc:
            attempts.append(f'{collection}: STAC query {exc}')
    return found


OPTICAL_SINGLE_LIMIT, OPTICAL_COMPOSITE_LIMIT = 16, 30
OPTICAL_SINGLE_MIN, OPTICAL_COMPOSITE_MIN = 0.60, 0.25


@_timed_source('optical')
def optical_from_stac(client, grid, lat, year_start=2022, year_end=2025, max_items=OPTICAL_SINGLE_LIMIT):
    """Choose a good-quality summer scene, falling back to a multi-year per-pixel composite."""
    attempts = []

    # Pass 1: single good scene in requested window.
    for collection, item in optical_query_summer(client, grid, lat, year_start, year_end, max_items, attempts):
        try:
            ndvi = _read_optical_ndvi(collection, item, grid)
            if ndvi is None:
                continue
            f = _ndvi_summary(ndvi)
            if f['optical_valid_fraction'] >= OPTICAL_SINGLE_MIN:
                f['ndvi_method'] = 'single_valid_summer_scene'
                return f, dict(collection=collection, item_ids=[item.id],
                               acquisition_date=item.datetime.date().isoformat(),
                               scene_cloud_pct=_safe_cloud(item),
                               attempted_errors=attempts)
            attempts.append(f'{item.id}: valid coverage {f["optical_valid_fraction"]:.2f}')
        except Exception as exc:
            attempts.append(f'{item.id}: {exc}')

    # Pass 2: widen to 2016..year_end and build per-pixel valid-pixel median composite.
    wide_start = min(year_start, 2016)
    stack, used_ids, collections_used = [], [], set()
    for collection, item in optical_query_summer(client, grid, lat, wide_start, year_end, OPTICAL_COMPOSITE_LIMIT, attempts):
        try:
            ndvi = _read_optical_ndvi(collection, item, grid)
            if ndvi is not None and np.isfinite(ndvi).any():
                stack.append(ndvi)
                used_ids.append(item.id)
                collections_used.add(collection)
        except Exception as exc:
            attempts.append(f'{item.id}: {exc}')
    if stack:
        with np.errstate(invalid='ignore'):
            composite = np.nanmedian(np.stack(stack), axis=0)
        f = _ndvi_summary(composite)
        if f['optical_valid_fraction'] >= OPTICAL_COMPOSITE_MIN:
            f['ndvi_method'] = 'multi_scene_summer_composite'
            return f, dict(collections=sorted(collections_used), item_ids=used_ids,
                           period=f'{wide_start}-{year_end}',
                           scene_count=len(used_ids),
                           attempted_errors=attempts[-8:])
        attempts.append(f'composite valid coverage {f["optical_valid_fraction"]:.2f}')

    raise RuntimeError('No usable optical scene; ' + '; '.join(attempts[-8:]))

# --- Original per-coordinate extractor (legacy optical pilot) ---
# Provenance and feature gaps recorded explicitly; no invented values.
FEATURES=('elev_m','slope_median_deg','slope_p95_deg','relief_1km_m','tri_neighbor_m',
          'tri_100m','ndvi_summer_median','bare_fraction','dem_valid_fraction',
          'optical_valid_fraction')

def extract_base(lat,lon,site_id,output_dir,patch_size_m=1000,resolution_m=30,
            optical=True, year_start=2022,year_end=2025):
    out=Path(output_dir);out.mkdir(parents=True,exist_ok=True)
    grid=patch_grid(lat,lon,patch_size_m,resolution_m)
    row=dict(site_id=site_id,latitude=lat,longitude=lon,patch_size_m=patch_size_m,
             crs=grid.crs.to_string(),grid_resolution_m=grid.resolution_m,
             extracted_utc=datetime.now(timezone.utc).isoformat(),
             label_status='unverified_reference',confirmed_analog=False)
    row.update({f:None for f in FEATURES})
    provenance={};errors={}
    try:
        client=catalog()
    except Exception as exc:
        errors['catalog']=str(exc);client=None
    if client is not None:
        try:
            dem,meta=dem_from_stac(client,grid)
            row.update(dem_features(dem,grid.resolution_m))
            provenance['dem']=meta
        except Exception as exc: errors['dem']=str(exc)
        if optical:
            try:
                vals,meta=optical_from_stac(client,grid,lat,year_start,year_end)
                row.update(vals);provenance['optical']=meta
            except Exception as exc: errors['optical']=str(exc)
    # Features that lack dedicated adapters remain null, not zero.
    known=[k for k in FEATURES if row.get(k) is not None]
    row['features_computed']=json.dumps(known)
    row['features_unavailable']=json.dumps([k for k in FEATURES if row.get(k) is None])
    row['status']='complete' if 'elev_m' in known and ('ndvi_summer_median' in known or not optical) else ('partial' if known else 'failed')
    props=dict(row)
    props['provenance']=provenance
    props['errors']=errors
    # Polygon saved in projected coordinates reprojected to WGS84; avoids a fake degree-square.
    from pyproj import Transformer
    from rasterio.transform import xy
    to_ll=Transformer.from_crs(grid.crs,4326,always_xy=True)
    t=grid.transform; h,w=grid.shape
    corners_xy=[t*(0,0),t*(w,0),t*(w,h),t*(0,h)]
    ring=[list(to_ll.transform(x,y)) for x,y in corners_xy]
    ring.append(ring[0])
    feature={'type':'Feature','geometry':{'type':'Polygon','coordinates':[ring]},'properties':props}
    (out/f'{site_id}.geojson').write_text(json.dumps({'type':'FeatureCollection','features':[feature]},indent=2,default=str))
    (out/f'{site_id}_report.json').write_text(json.dumps(props,indent=2,default=str))
    pd.DataFrame([{k:v for k,v in row.items()}]).to_parquet(out/f'{site_id}_features.parquet',index=False)
    return props

# --- Phase 1 dataset adapters and batch orchestration ---
#!/usr/bin/env python3
"""One command to extract Phase 1 global-search features for geolocated study sites.

Reads data/phase1/{sites,features,datasets,feature_datasets}.csv; uses the existing
analog_patch DEM + optical engine; Google Earth Engine MODIS; NASA POWER;
optional locally-downloaded GLiM map and impact catalogue; NASA EMIT discovery.
All missing measurements stay null and are reported. No fake positive labels.
"""




def load_phase1(directory):
    tables = {}
    requirements = {
        'sites': {'site_id', 'latitude', 'longitude', 'site_role', 'coordinate_status', 'transferable_to_similarity_search'},
        'features': {'feature_id', 'role', 'recommended_for_phase2'},
        'datasets': {'dataset_id', 'access_url'},
        'feature_datasets': {'feature_id', 'dataset_id', 'role_in_feature'},
    }
    for name, fields in requirements.items():
        path = directory / (name + '.csv')
        if not path.exists():
            raise FileNotFoundError(f'Required Phase 1 input missing: {path}')
        table = pd.read_csv(path)
        missing = fields - set(table.columns)
        if missing:
            raise ValueError(f'{name}.csv missing columns: {missing}')
        tables[name] = table
    if not set(tables['feature_datasets'].feature_id).issubset(set(tables['features'].feature_id)):
        raise ValueError('feature_datasets.csv references unknown feature_id')
    if not set(tables['feature_datasets'].dataset_id).issubset(set(tables['datasets'].dataset_id)):
        raise ValueError('feature_datasets.csv references unknown dataset_id')
    return tables


def phase1_features(tables):
    f = tables['features']
    return f.loc[f.role.eq('global_search') & f.recommended_for_phase2.astype(str).str.lower().eq('yes'), 'feature_id'].tolist()


@_timed_source('power')
def power_features(lat, lon, first, last):
    batch = _RUN['batch']
    memo = getattr(batch, 'power', None) if batch is not None else None
    if memo is not None and (lat, lon, first, last) in memo:
        _count('power_prefetch_hits')
        return memo[(lat, lon, first, last)]
    _count('power_requests')
    r = requests.get('https://power.larc.nasa.gov/api/temporal/daily/point', params={
        'latitude': lat, 'longitude': lon, 'start': f'{first}0101', 'end': f'{last}1231',
        'parameters': 'T2M,PRECTOTCORR,RH2M', 'community': 'AG', 'format': 'JSON',
    }, timeout=120)
    r.raise_for_status()
    data = r.json()['properties']['parameter']
    def series(key):
        s = pd.Series(data[key], dtype=float)
        s.index = pd.to_datetime(s.index, format='%Y%m%d')
        return s.where(s > -900).dropna()
    temp, precip, humid = (series(x) for x in ('T2M', 'PRECTOTCORR', 'RH2M'))
    def annual(s, what):
        by_year = s.groupby(s.index.year)
        complete = [y for y, values in by_year if len(values) >= 350]
        if not complete:
            return None
        selected = s[s.index.year.isin(complete)]
        if what == 'thaw':
            return float(selected.gt(0).groupby(selected.index.year).sum().mean())
        return float(selected.groupby(selected.index.year).sum().mean())
    return {
        'air_temp_days_above_0c': annual(temp, 'thaw'),
        'precip_mm_yr': annual(precip, 'precip'),
        'humidity_mean_pct': float(humid.mean()) if len(humid) else None,
    }, {'dataset_id': 'nasa_power', 'url': r.url, 'period': f'{first}-{last}', 'native_support': 'coarse model climate grid'}


def make_ee_patch(lat, lon):
    import ee
    from pyproj import Transformer
    grid = patch_grid(lat, lon, 1000, 30)
    to_wgs = Transformer.from_crs(grid.crs, 4326, always_xy=True)
    t = grid.transform
    h, w = grid.shape
    xy = [t * (0, 0), t * (w, 0), t * (w, h), t * (0, h)]
    ring = [list(to_wgs.transform(x, y)) for x, y in xy]
    ring.append(ring[0])
    return ee.Geometry.Polygon([ring])


def _item_datetime(item):
    if item.datetime is not None:
        return item.datetime
    dt_str = item.properties.get('datetime') or item.properties.get('start_datetime')
    if not dt_str:
        return None
    return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))


MODIS_LST_DAY = ('LST_Day_1km', 'QC_Day')
MODIS_LST_NIGHT = ('LST_Night_1km', 'QC_Night')
MODIS_SNOW = ('NDSI_Snow_Cover', 'NDSI_Snow_Cover_Basic_QA')


def _evenly_sample(items, n=45):
    if len(items) <= n:
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def modis_grid(lat, lon):
    return patch_grid(lat, lon, 1000, 500)


def modis_sample_items(client, grid, lat, first, last):
    """Ordered MODIS samples per cell: 45 summer / 45 winter LST items, 60 snow items."""
    begin, end = f'{first}-01-01T00:00:00Z', f'{last}-12-31T23:59:59Z'
    summer_months = {12, 1, 2} if lat < 0 else {6, 7, 8}
    winter_months = {6, 7, 8} if lat < 0 else {12, 1, 2}
    lst_items = query_items(client, 'modis-11A1-061', grid, f'{begin}/{end}', limit=3000)
    lst_dated = []
    for i in lst_items:
        if i.id.startswith('MOD11A1'):
            dt = _item_datetime(i)
            if dt is not None:
                lst_dated.append((dt, i))
    lst_dated.sort(key=lambda x: x[0])
    summer_items = _evenly_sample([i for dt, i in lst_dated if dt.month in summer_months], 45)
    winter_items = _evenly_sample([i for dt, i in lst_dated if dt.month in winter_months], 45)
    snow_all = query_items(client, 'modis-10A1-061', grid, f'{begin}/{end}', limit=3000)
    snow_dated = []
    for i in snow_all:
        if i.id.startswith('MOD10A1'):
            dt = _item_datetime(i)
            if dt is not None:
                snow_dated.append((dt, i))
    snow_dated.sort(key=lambda x: x[0])
    snow_items = _evenly_sample([i for _, i in snow_dated], 60)
    return summer_items, winter_items, snow_items


def modis_lst_value(item, lst_asset, qc_asset, grid):
    if lst_asset not in item.assets:
        return None
    try:
        arr = read_reprojected(item.assets[lst_asset].href, grid, resampling=Resampling.nearest)
        valid = np.isfinite(arr) & (arr > 0)
        if qc_asset in item.assets:
            qc = read_reprojected(item.assets[qc_asset].href, grid, resampling=Resampling.nearest)
            qc_int = np.nan_to_num(qc, nan=255).astype(np.uint16)
            valid &= ((qc_int & 3) <= 1)
        if valid.any():
            temp_c = arr[valid] * 0.02 - 273.15
            return float(np.mean(temp_c))
    except Exception:
        return None
    return None


def modis_lst_values(items, lst_asset, qc_asset, grid):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=12) as pool:
        res = list(pool.map(lambda it: modis_lst_value(it, lst_asset, qc_asset, grid), items))
    return [v for v in res if v is not None]


def modis_snow_value(item, grid):
    if MODIS_SNOW[0] not in item.assets:
        return None
    try:
        band = read_reprojected(item.assets[MODIS_SNOW[0]].href, grid, resampling=Resampling.nearest)
        valid = np.isfinite(band) & (band >= 0) & (band <= 100)
        if MODIS_SNOW[1] in item.assets:
            qa = read_reprojected(item.assets[MODIS_SNOW[1]].href, grid, resampling=Resampling.nearest)
            valid &= np.isfinite(qa) & (qa <= 2)
        if valid.any():
            return float((band[valid] >= 40).mean())
    except Exception:
        return None
    return None


@_timed_source('modis')
def modis_features_pc(lat, lon, first, last, client=None):
    """Fetch MODIS LST (MOD11A1 v061) and Snow (MOD10A1 v061) via Planetary Computer STAC."""
    if client is None:
        client = catalog()
    grid = modis_grid(lat, lon)
    summer_items, winter_items, snow_items = modis_sample_items(client, grid, lat, first, last)

    s_vals = modis_lst_values(summer_items, *MODIS_LST_DAY, grid)
    w_vals = modis_lst_values(winter_items, *MODIS_LST_NIGHT, grid)
    winter_fallback = not w_vals
    if winter_fallback:
        w_vals = modis_lst_values(winter_items, *MODIS_LST_DAY, grid)
    smax = max(s_vals) if s_vals else None
    wmin = min(w_vals) if w_vals else None

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=12) as pool:
        snow_fracs = [v for v in pool.map(lambda it: modis_snow_value(it, grid), snow_items) if v is not None]
    snow_persistence = float(np.mean(snow_fracs)) if snow_fracs else None
    _diag_put('modis', {'summer_ids': [i.id for i in summer_items], 'winter_ids': [i.id for i in winter_items],
                        'snow_ids': [i.id for i in snow_items], 'winter_day_fallback': winter_fallback,
                        'summer_valid': len(s_vals), 'winter_valid': len(w_vals), 'snow_valid': len(snow_fracs)})

    return {
        'lst_summer_max_c': smax,
        'lst_winter_min_c': wmin,
        'snow_persistence': snow_persistence,
    }, {
        'provider': 'planetary_computer',
        'modis_lst': 'modis-11A1-061',
        'modis_snow': 'modis-10A1-061',
        'summer_valid_days': len(s_vals),
        'winter_valid_days': len(w_vals),
        'snow_valid_days': len(snow_fracs),
        'snow_threshold_ndsi': 40,
        'temporal_period': f'{first}-{last}',
    }


def modis_features(lat, lon, first, last):
    import ee
    patch = make_ee_patch(lat, lon)
    begin, end = f'{first}-01-01', f'{last+1}-01-01'
    lst = ee.ImageCollection('MODIS/061/MOD11A1').filterDate(begin, end).filterBounds(patch)
    def good_lst(img):
        qc = img.select('QC_Day')
        return img.select('LST_Day_1km').multiply(0.02).subtract(273.15).rename('temp').updateMask(qc.bitwiseAnd(3).eq(0))
    lst = lst.map(good_lst)
    def season_filter(months):
        return ee.Filter.Or(*[ee.Filter.calendarRange(m, m, 'month') for m in months])
    def seasonal_extreme(months, reducer):
        values = lst.filter(season_filter(months))
        # Spatial average of each daily image; temporal extreme of observations.
        def annotate(img):
            return img.set('patch_value', img.reduceRegion(ee.Reducer.mean(), patch, scale=1000, maxPixels=10000).get('temp'))
        vals = values.map(annotate).aggregate_array('patch_value').getInfo()
        vals = [float(v) for v in vals if v is not None]
        return reducer(vals) if vals else None, len(vals)
    summer = [12, 1, 2] if lat < 0 else [6, 7, 8]
    winter = [6, 7, 8] if lat < 0 else [12, 1, 2]
    smax, ns = seasonal_extreme(summer, max)
    wmin, nw = seasonal_extreme(winter, min)
    snow = ee.ImageCollection('MODIS/061/MOD10A1').filterDate(begin, end).filterBounds(patch)
    def snow_mask(img):
        band, qa = img.select('NDSI_Snow_Cover'), img.select('NDSI_Snow_Cover_Basic_QA')
        valid = band.gte(0).And(band.lte(100)).And(qa.lte(2))
        return band.gte(40).rename('snow').updateMask(valid)
    prepared = snow.map(snow_mask)
    fraction = prepared.mean().reduceRegion(ee.Reducer.mean(), patch, scale=500, maxPixels=10000).get('snow').getInfo() if snow.size().getInfo() else None
    return {
        'lst_summer_max_c': smax, 'lst_winter_min_c': wmin, 'snow_persistence': fraction,
    }, {'modis_lst': 'MODIS/061/MOD11A1', 'modis_snow': 'MODIS/061/MOD10A1', 'summer_valid_days': ns,
         'winter_valid_days': nw, 'snow_threshold_ndsi': 40, 'temporal_period': f'{first}-{last}',
         'note': 'LST extremes are of daily patch-mean clear-sky daytime temperatures'}


def _local_geology_file(directory):
    for pattern in ('glim*.gpkg', 'glim*.geojson', 'glim*.shp', 'GLiM*.shp', 'GLiM*.gpkg', '*GLiM*.gdb', '*LiMW*.gdb', '*.gdb'):
        matches = list(directory.rglob(pattern))
        if matches:
            return matches[0]
    return None


@_timed_source('glim')
def glim_feature(lat, lon, directory):
    import geopandas as gpd
    from shapely.geometry import Point
    file = _local_geology_file(directory)
    if file is None:
        raise FileNotFoundError('GLiM map not downloaded. Add its .shp/.gpkg/.gdb to data/phase1/source_data/glim/')
    # Phase 1 specifies 50 km spatial support for GLiM, not exact patch lithology.
    center = gpd.GeoSeries([Point(lon, lat)], crs='EPSG:4326')
    zone = max(1, min(60, int((lon + 180) // 6) + 1))
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    area = center.to_crs(epsg).buffer(25000)
    region_wgs84 = gpd.GeoDataFrame(geometry=area, crs=f'EPSG:{epsg}').to_crs('EPSG:4326')
    read_kwargs = {'layer': 'GLiM_export'} if file.suffix.lower() == '.gdb' else {}
    try:
        subset = gpd.read_file(file, mask=region_wgs84, **read_kwargs)
    except Exception:
        geology = gpd.read_file(file, **read_kwargs)
        if geology.crs is None:
            raise ValueError(f'GLiM file has no CRS: {file}')
        region = region_wgs84.to_crs(geology.crs)
        subset = geology[geology.intersects(region.geometry.iloc[0])]
    if subset.empty:
        return {'glim_lithology': None}, {'dataset_id': 'glim', 'file': str(file), 'status': 'no_coverage'}
    subset = subset.to_crs(epsg)
    field = next((c for c in ('xx', 'XX', 'lithology', 'LITHOLOGY', 'litho', 'class') if c in subset.columns), None)
    if field is None:
        raise ValueError('GLiM lithology column not identified. Columns: ' + str(list(subset.columns)))
    part = subset.geometry.intersection(area.iloc[0]).area
    majority = part.groupby(subset[field].astype(str)).sum().sort_values(ascending=False)
    return {'glim_lithology': majority.index[0]}, {'dataset_id': 'glim', 'file': str(file), 'support_m': 50000, 'class_column': field}


@_timed_source('impact')
def impact_feature(lat, lon, directory):
    from pyproj import Geod
    candidates = list(directory.rglob('*impact*.csv')) + list(directory.rglob('*crater*.csv'))
    if not candidates:
        raise FileNotFoundError('No curated impact structure coordinate CSV in data/phase1/source_data/impact/.')
    file = candidates[0]
    cat = pd.read_csv(file)
    def col(*names):
        return next((c for c in cat.columns if c.strip().lower() in names), None)
    lat_col = col('latitude', 'lat', 'latitude_deg')
    lon_col = col('longitude', 'lon', 'long', 'longitude_deg')
    if not lat_col or not lon_col:
        raise ValueError(f'Impact catalogue requires latitude and longitude columns: {file}')
    pts = cat[[lat_col,lon_col]].apply(pd.to_numeric, errors='coerce').dropna()
    pts = pts[pts[lat_col].between(-90,90) & pts[lon_col].between(-180,180)]
    if pts.empty:
        raise ValueError('Impact catalogue has no valid coordinates')
    g = Geod(ellps='WGS84')
    _,_,meters = g.inv(np.full(len(pts),lon), np.full(len(pts),lat), pts[lon_col].to_numpy(), pts[lat_col].to_numpy())
    return {'impact_distance_km': float(np.min(meters)/1000)}, {'dataset_id': 'earth_impact_db', 'file': str(file), 'record_count': len(pts), 'note':'Confirm the CSV contains only verified terrestrial impact structures'}


def emit_feature(lat, lon, first, last, directory):
    """Use NASA CMR to discover EMIT; numerical decoding requires verified L2B handling.

    The source is not a generic raster and numerical output is intentionally withheld
    unless the product-specific mineral lookup and GLT geolocation are implemented.
    """
    if abs(lat) > 52:
        return {}, {'dataset_id': 'emit_l2bmin', 'status': 'outside_latitude_coverage'}
    import earthaccess
    results = earthaccess.search_data(short_name='EMITL2BMIN', version='001',
        bounding_box=(lon-.012,lat-.012,lon+.012,lat+.012),
        temporal=(f'{first}-01-01', f'{last}-12-31'), count=10)
    if not results:
        return {}, {'dataset_id':'emit_l2bmin', 'status':'no_matching_granule'}
    # This only establishes spatial-footprint overlap; the actual sensor pixels
    # can still miss the 1km patch. Never invent a mineral fraction from a granule ID.
    return {}, {'dataset_id':'emit_l2bmin','status':'granules_discovered_decoder_required',
                'matching_granules':len(results), 'note':'Use NASA EMIT L2B geolocation and mineral-name lookup before computing clay/sulfate/iron fractions'}


def main(args):
    directory = Path(args.phase1)
    tables = load_phase1(directory)
    features = phase1_features(tables)
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    sites = tables['sites']
    if args.only:
        sites = sites[sites.site_id.isin(args.only.split(','))]
    if not args.all_points:
        sites = sites[sites.site_role.eq('science_subregion') & sites.transferable_to_similarity_search.astype(str).str.lower().eq('true')]
    ee_error = 'unused (Planetary Computer MODIS selected)'
    if args.modis_source == 'ee':
        if not args.ee_project:
            ee_error = '--ee-project is required when --modis-source=ee'
        else:
            try:
                import ee
                ee.Initialize(project=args.ee_project)
                ee_error = None
            except Exception as exc:
                ee_error = str(exc)
    outputs=[]; statuses=[]
    for site in sites.to_dict('records'):
        site_id=site['site_id']
        lat,lon=site.get('latitude'),site.get('longitude')
        if pd.isna(lat) or pd.isna(lon):
            print(f"Skipping {site_id}: no coordinates available")
            statuses.append({
                "site_id": site_id,
                "status": "missing_coordinates"
            })
            continue

        lat,lon=float(lat),float(lon)
        print('Extracting',site_id, flush=True)
        row={f:None for f in features}
        row.update({'site_id':site_id,'latitude':lat,'longitude':lon,'patch_size_m':1000,
                    'coordinate_status':site.get('coordinate_status'),'label_status':'unverified_reference'})
        provenance={};errors={}
        def attempt(name, fn):
            try:
                values, metadata=fn()
                row.update({k:v for k,v in values.items() if k in row})
                provenance[name]=metadata
            except Exception as exc:
                errors[name]=str(exc)
        def base():
            res=extract_base(lat,lon,site_id,out/site_id,year_start=args.start,year_end=args.end)
            errors.update(res.get('errors',{}))
            vals={k:res.get(k) for k in ('elev_m','slope_median_deg','slope_p95_deg','relief_1km_m','tri_100m','ndvi_summer_median','bare_fraction')}
            provenance['optical_pilot']={'ndvi':res.get('ndvi_summer_median'), 'bare_fraction':res.get('bare_fraction'),
                                         'ndvi_method':res.get('ndvi_method'),
                                         'optical_valid_fraction':res.get('optical_valid_fraction')}
            return vals, res.get('provenance',{})
        attempt('dem_optical_pilot',base)
        attempt('nasa_power',lambda:power_features(lat,lon,args.start,args.end))
        if args.modis_source == 'pc':
            attempt('modis',lambda:modis_features_pc(lat,lon,args.start,args.end))
        elif ee_error is None:
            attempt('modis',lambda:modis_features(lat,lon,args.start,args.end))
        else:
            errors['earth_engine']=ee_error
        attempt('glim',lambda:glim_feature(lat,lon,directory))
        attempt('earth_impact_db',lambda:impact_feature(lat,lon,directory))
        attempt('emit_l2bmin',lambda:emit_feature(lat,lon,args.start,args.end,directory))
        computed=[f for f in features if pd.notna(row.get(f))]
        missing=[f for f in features if f not in computed]
        row['status']='partial' if computed else 'no_data'
        row['features_computed']=json.dumps(computed)
        row['features_unavailable']=json.dumps(missing)
        folder=out/site_id;folder.mkdir(parents=True,exist_ok=True)
        (folder/f'{site_id}_all_sources_report.json').write_text(json.dumps({'site_id':site_id,'provenance':provenance,'errors':errors,
           'computed_features':computed,'missing_features':missing},indent=2,default=str))
        pd.DataFrame([row]).to_parquet(folder/f'{site_id}_phase1_features.parquet',index=False)
        outputs.append(row);statuses.append({'site_id':site_id,'status':row['status'],'computed':len(computed)})
    pd.DataFrame(statuses).to_csv(out/'extraction_status.csv',index=False)
    if outputs:
        pd.DataFrame(outputs).to_parquet(out/'reference_features.parquet',index=False)
    print(json.dumps({'processed':len(outputs),'statuses':statuses},indent=2))



if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Analog patch extractor"
    )

    parser.add_argument("--phase1", default="data/phase1")
    parser.add_argument("--out", default="data/phase2")
    parser.add_argument("--modis-source", choices=["pc", "ee"], default="pc")
    parser.add_argument("--ee-project", default=None)
    parser.add_argument("--start", type=int, default=2022)
    parser.add_argument("--end", type=int, default=2024)
    parser.add_argument("--only", help="Comma-separated site IDs")
    parser.add_argument("--include-conflicts", action="store_true")
    parser.add_argument("--all-points", action="store_true")

    args = parser.parse_args()

    if args.start > args.end:
        parser.error("--start must be <= --end")

    main(args)
