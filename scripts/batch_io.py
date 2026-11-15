"""Parent-scoped batched STAC discovery and shared raster reads for sim_search.py.

Design (see sim_search.py --stac-mode batched):

* One BatchContext per parent patch. It is installed into extraction.py via
  ``set_run_context(batch=ctx)`` so the *unchanged* extraction helpers run as usual,
  but their ``catalog()``, ``query_items()``, ``read_reprojected()`` and
  ``power_features()`` calls are served from parent-level caches when possible.
* Discovery: one union-bbox search per (collection, exact datetime window). Each child
  then gets the items whose geometry intersects its own bbox (the STAC ``bbox``
  relation), in server order, truncated to the original per-cell limit. If the union
  search could be incomplete (hit its cap), crosses the antimeridian, or fails, that
  group falls back to the original per-cell search.
* Reads: ``warm_*`` functions replay the per-child selection logic to learn which
  (asset, child grid, band, resampling) reads will be needed, then read them
  asset-first: each asset is opened once by one worker, every requested child window
  is read through the exact same per-child WarpedVRT, and the arrays are cached for
  the parent. Failed reads are never cached; the extractor then retries live.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import extraction as ex

FALLBACK = object()
UNION_PAGE = 1000          # items per STAC page
MAX_UNION_LON_SPAN = 10.0  # degrees; wider unions (antimeridian/polar artefacts) use per-cell search


def _resign(href: str) -> str:
    import planetary_computer
    return planetary_computer.sign(ex.asset_identity(href))


class BatchContext:
    def __init__(self, client, child_grids: list, metrics, asset_workers: int = 8):
        self.client = client
        self.metrics = metrics
        self.asset_workers = max(1, int(asset_workers))
        self.child_bounds = {tuple(g.bounds_wgs84) for g in child_grids}
        self.all_bounds = [tuple(g.bounds_wgs84) for g in child_grids]
        self.queries: dict = {}
        self.arrays: dict = {}
        self.power: dict = {}
        self._lock = threading.Lock()
        self.inventory: dict = {}  # (collection, window) -> {'mode', 'n_items', ...} for diagnostics

    # ------------------------------------------------------------------ discovery
    def _union(self, collection, date_range, limit):
        key = (collection, date_range)
        b = np.array(self.all_bounds, dtype=float)
        w, s, e, n = b[:, 0].min(), b[:, 1].min(), b[:, 2].max(), b[:, 3].max()
        if (b[:, 2] - b[:, 0]).max() > MAX_UNION_LON_SPAN or (e - w) > MAX_UNION_LON_SPAN:
            self.inventory[key] = {'mode': 'per-cell', 'reason': 'longitude span / antimeridian'}
            return FALLBACK
        # Never fetch more than the per-cell path would in total.
        cap = int(limit) * len(self.all_bounds)
        args = dict(collections=[collection], bbox=[w, s, e, n], max_items=cap, limit=min(UNION_PAGE, cap))
        if date_range:
            args['datetime'] = date_range
        try:
            self.metrics.add('stac_union_searches')
            search = self.client.search(**args)
            matched = None
            try:
                matched = search.matched()
                self.metrics.add('stac_matched_requests')
            except Exception:
                matched = None
            if matched is not None and matched >= cap:
                self.metrics.add('stac_union_capped')
                self.inventory[key] = {'mode': 'per-cell', 'reason': f'matched {matched} >= cap {cap}'}
                return FALLBACK
            items = list(search.items())
        except Exception as exc:  # transient: do not record as empty coverage
            self.metrics.add('stac_union_failures')
            self.inventory[key] = {'mode': 'per-cell', 'reason': f'union search failed: {exc}'}
            return FALLBACK
        if len(items) >= cap:
            self.metrics.add('stac_union_capped')
            self.inventory[key] = {'mode': 'per-cell', 'reason': f'union reached cap {cap}'}
            return FALLBACK
        from shapely.geometry import box, shape
        entries = []
        for it in items:
            try:
                geom = shape(it.geometry) if it.geometry else box(*it.bbox)
            except Exception:
                self.inventory[key] = {'mode': 'per-cell', 'reason': f'unreadable footprint {it.id}'}
                return FALLBACK
            entries.append((it, geom))
        self.inventory[key] = {'mode': 'batched', 'n_items': len(entries), 'cap': cap}
        return entries

    def query(self, client, collection, grid, date_range, limit, live):
        bounds = tuple(grid.bounds_wgs84)
        if bounds not in self.child_bounds:
            return live(client, collection, grid, date_range, limit)
        key = (collection, date_range)
        with self._lock:
            entry = self.queries.get(key)
            if entry is None or (entry is not FALLBACK and entry[0] < limit):
                result = self._union(collection, date_range, limit)
                entry = FALLBACK if result is FALLBACK else (int(limit), result)
                self.queries[key] = entry
        if entry is FALLBACK:
            self.metrics.add('stac_percell_fallback_searches')
            return live(client, collection, grid, date_range, limit)
        from shapely.geometry import box
        cell = box(*bounds)
        out = [it for it, geom in entry[1] if geom.intersects(cell)][:int(limit)]
        self.metrics.add('stac_query_cache_hits')
        return out

    # ------------------------------------------------------------------ reads
    def _key(self, href, grid, band, resampling):
        return (ex.asset_identity(href), ex.grid_key(grid), int(band), int(resampling))

    def lookup(self, href, grid, band, resampling):
        arr = self.arrays.get(self._key(href, grid, band, resampling))
        return None if arr is None else arr.copy()

    def prefetch(self, requests) -> None:
        """requests: iterable of (href, grid, band, resampling). Asset-first, one owner per dataset."""
        by_asset: dict = {}
        for href, grid, band, resampling in requests:
            k = self._key(href, grid, band, resampling)
            if k in self.arrays:
                continue
            slot = by_asset.setdefault(k[0], [href, {}])
            slot[1][k] = (grid, band, resampling)
        if not by_asset:
            return
        self.metrics.add('prefetch_assets', len(by_asset))
        self.metrics.add('prefetch_windows', sum(len(v[1]) for v in by_asset.values()))

        def task(slot):
            href, wanted = slot
            import rasterio
            from rasterio.env import Env
            for attempt in (0, 1):
                try:
                    url = href if attempt == 0 else _resign(href)
                    self.metrics.add('raster_opens')
                    with Env(**ex._GDAL_ENV):
                        with rasterio.open(url) as src:
                            for k, (grid, band, resampling) in wanted.items():
                                arr = ex.warp_read(src, grid, band, resampling)
                                with self._lock:
                                    self.arrays[k] = arr
                    return
                except Exception:
                    self.metrics.add('raster_failed_reads')
                    if attempt == 1:
                        return  # not cached; extractor will retry live and record the error

        with ThreadPoolExecutor(max_workers=self.asset_workers) as pool:
            list(pool.map(task, by_asset.values()))


# ---------------------------------------------------------------------- warm-up stages
def warm_dem(ctx: BatchContext, cells: list) -> None:
    reqs = []
    for c in cells:
        items = ex.query_items(ctx.client, 'cop-dem-glo-30', c['grid30'], limit=12)
        for it in items:
            a = it.assets.get('data')
            if a:
                reqs.append((a.href, c['grid30'], 1, ex.Resampling.bilinear))
    ctx.prefetch(reqs)


def _optical_reqs(collection, item, grid):
    specs = ex.OPTICAL_ASSETS['sentinel-2-l2a' if collection == 'sentinel-2-l2a' else 'landsat-c2-l2']
    if any(k not in item.assets for k, _ in specs):
        return []
    return [(item.assets[k].href, grid, 1, r) for k, r in specs]


def warm_optical(ctx: BatchContext, cells: list, year_start: int, year_end: int) -> None:
    """Round-based: share reads of each active child's next candidate; stop children that pass."""
    cands = {}
    for c in cells:
        cands[c['id']] = ex.optical_query_summer(ctx.client, c['grid30'], c['lat'], year_start, year_end,
                                                 ex.OPTICAL_SINGLE_LIMIT, [])
    by_id = {c['id']: c for c in cells}
    active = [c['id'] for c in cells]
    passed = set()
    r = 0
    while active:
        still = [cid for cid in active if r < len(cands[cid])]
        if not still:
            break
        reqs = []
        for cid in still:
            col, it = cands[cid][r]
            reqs.extend(_optical_reqs(col, it, by_id[cid]['grid30']))
        ctx.prefetch(reqs)
        nxt = []
        for cid in still:
            col, it = cands[cid][r]
            try:
                ndvi = ex._read_optical_ndvi(col, it, by_id[cid]['grid30'])
            except Exception:
                ndvi = None
            if ndvi is not None and ex._ndvi_summary(ndvi)['optical_valid_fraction'] >= ex.OPTICAL_SINGLE_MIN:
                passed.add(cid)
            else:
                nxt.append(cid)
        active = nxt
        r += 1
    need_comp = [by_id[cid] for cid in by_id if cid not in passed]
    ctx.metrics.add('optical_single_passed', len(passed))
    ctx.metrics.add('optical_composite_needed', len(need_comp))
    if need_comp:
        wide = min(year_start, 2016)
        reqs = []
        for c in need_comp:
            for col, it in ex.optical_query_summer(ctx.client, c['grid30'], c['lat'], wide, year_end,
                                                   ex.OPTICAL_COMPOSITE_LIMIT, []):
                reqs.extend(_optical_reqs(col, it, c['grid30']))
        ctx.prefetch(reqs)


def _modis_reqs(items, assets, grid):
    out = []
    for it in items:
        for k in assets:
            if k in it.assets:
                out.append((it.assets[k].href, grid, 1, ex.Resampling.nearest))
    return out


def warm_modis(ctx: BatchContext, cells: list, first: int, last: int) -> None:
    plans = {}
    reqs = []
    for c in cells:
        s, w, sn = ex.modis_sample_items(ctx.client, c['grid500'], c['lat'], first, last)
        plans[c['id']] = (c, w)
        reqs += _modis_reqs(s, ex.MODIS_LST_DAY, c['grid500'])
        reqs += _modis_reqs(w, ex.MODIS_LST_NIGHT, c['grid500'])
        reqs += _modis_reqs(sn, ex.MODIS_SNOW, c['grid500'])
    ctx.prefetch(reqs)
    # Daytime winter fallback only for children with no valid night value.
    fb = []
    for c, w in plans.values():
        if not ex.modis_lst_values(w, *ex.MODIS_LST_NIGHT, c['grid500']):
            fb += _modis_reqs(w, ex.MODIS_LST_DAY, c['grid500'])
    if fb:
        ctx.metrics.add('modis_winter_fallback_prefetch', 1)
        ctx.prefetch(fb)


def warm_power(ctx: BatchContext, cells: list, first: int, last: int, workers: int = 4) -> None:
    def one(c):
        key = (c['lat'], c['lon'], first, last)
        try:
            ctx.power[key] = ex.power_features(c['lat'], c['lon'], first, last)
        except Exception:
            pass  # not memoised; the extractor retries live and records the error
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, cells))


def warm_parent(ctx: BatchContext, cells: list, sources: set, start: int, end: int) -> dict:
    """Run all warm-up stages for one parent; returns elapsed seconds per stage."""
    elapsed = {}
    stages = []
    if 'base' in sources:
        stages += [('warm_dem', lambda: warm_dem(ctx, cells)),
                   ('warm_optical', lambda: warm_optical(ctx, cells, start, end))]
    if 'modis' in sources:
        stages.append(('warm_modis', lambda: warm_modis(ctx, cells, start, end)))
    if 'power' in sources:
        stages.append(('warm_power', lambda: warm_power(ctx, cells, start, end)))
    for name, fn in stages:
        t0 = time.perf_counter()
        try:
            fn()
        except Exception as exc:  # warm-up is an optimisation only
            ctx.metrics.add(f'{name}_errors')
            print(f'  {name} failed ({exc}); cells will use live reads', flush=True)
        dt = time.perf_counter() - t0
        ctx.metrics.add_time(name, dt)
        elapsed[name] = round(dt, 2)
    return elapsed
