#!/usr/bin/env python3
"""Phase 3: expand surviving NESTED HEALPix parents and rank 1 km analog windows.

Run from repository root. Uses scripts/extraction.py (Phase 2 extractor) for every
measurement. Uses 1,000 m centred windows (the same footprint as Phase 2), not exact
~0.80 km HEALPix child polygons. Results are checkpointed after each candidate.

Modes (--stac-mode):
  per-cell  original path: every cell opens its own STAC client, runs its own searches
            and opens every raster itself. --workers N overlaps cells with threads.
  batched   one parent at a time: union STAC searches per (collection, window), per-child
            filtering in server order with the original limits, and asset-first shared
            raster reads (scripts/batch_io.py). Use --workers 1; reads are parallelised
            per asset with --asset-workers.
Per-cell report files are off by default; use --write-reports only for debugging.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import healpy as hp
import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
# scripts/extraction.py is the existing Phase 2 extractor.
sys.path.insert(0, str(_HERE))
import extraction as ex  # noqa: E402
from extraction import (  # noqa: E402
    extract_base, power_features, modis_features_pc, glim_feature,
    impact_feature, emit_feature,
)

try:  # same longitude convention as the prefilter's patch grid
    sys.path.insert(0, str(_ROOT / 'src'))
    from nasa_si.patches import _wrap_lon  # noqa: E402
except Exception:  # pragma: no cover
    def _wrap_lon(lon):
        return (np.asarray(lon, dtype=np.float64) + 180.0) % 360.0 - 180.0

# Continuous features that both reference_features.parquet and extraction.py expose.
CONTINUOUS = (
    'elev_m', 'slope_median_deg', 'slope_p95_deg', 'relief_1km_m',
    'tri_100m', 'ndvi_summer_median', 'bare_fraction',
    'snow_persistence', 'lst_summer_max_c', 'lst_winter_min_c',
    'air_temp_days_above_0c', 'precip_mm_yr', 'humidity_mean_pct',
    'impact_distance_km', 'emit_clay_fraction', 'emit_sulfate_fraction',
    'emit_iron_fraction',
)
CATEGORICAL = ('glim_lithology',)
CODE_FILES = ('scripts/balance_search.py', 'scripts/sim_search.py', 'scripts/extraction.py', 'scripts/batch_io.py')


def wrap_lon(lon: float) -> float:
    return float(_wrap_lon(lon))


def load_tasks(path: Path, max_parents: int, children_per_parent: int, max_cells: int) -> pd.DataFrame:
    """Use the exact child hierarchy output by prefilter.write_union()."""
    parents = pd.read_csv(path)
    required = {'patch_id', 'child_start', 'n_children', 'coarse_nside', 'fine_nside', 'seeds'}
    missing = required - set(parents.columns)
    if missing:
        raise ValueError(f'{path} missing columns: {sorted(missing)}')
    if max_parents:
        parents = parents.head(max_parents)
    tasks = []
    seen = set()
    for p in parents.itertuples(index=False):
        coarse, fine = int(p.coarse_nside), int(p.fine_nside)
        if not (hp.isnsideok(coarse, nest=True) and hp.isnsideok(fine, nest=True)):
            raise ValueError(f'Invalid HEALPix nside: {coarse}, {fine}')
        factor = (fine // coarse) ** 2
        if fine % coarse or factor != int(p.n_children) or int(p.child_start) != int(p.patch_id) * factor:
            raise ValueError(f'Invalid NESTED child range for parent {p.patch_id}')
        count = min(int(p.n_children), children_per_parent) if children_per_parent else int(p.n_children)
        # Spread pilot cells throughout the parent instead of testing its first corner only.
        offsets = np.unique(np.linspace(0, int(p.n_children) - 1, count, dtype=np.int64))
        for offset in offsets:
            child = int(p.child_start) + int(offset)
            if child in seen:
                continue
            seen.add(child)
            lon, lat = hp.pix2ang(fine, child, nest=True, lonlat=True)
            lon = wrap_lon(lon)  # healpy returns 0..360; every source expects [-180, 180)
            tasks.append({
                'fine_cell_id': child, 'parent_patch_id': int(p.patch_id),
                'latitude': float(lat), 'longitude': lon,
                'seeds': str(p.seeds) if pd.notna(p.seeds) else '',
                'fine_nside': fine,
            })
            if max_cells and len(tasks) >= max_cells:
                return pd.DataFrame(tasks)
    return pd.DataFrame(tasks)


def validate_tasks(tasks: pd.DataFrame) -> dict:
    lon, lat = tasks['longitude'], tasks['latitude']
    bad = int(((lon < -180) | (lon >= 180) | (lat < -90) | (lat > 90)).sum())
    if bad:
        raise ValueError(f'{bad} task coordinates out of range after wrapping')
    if tasks['fine_cell_id'].duplicated().any():
        raise ValueError('duplicate fine_cell_id in task list')
    return {'tasks': int(len(tasks)), 'unique_ids': int(tasks['fine_cell_id'].nunique()),
            'per_seed': {str(k): int(v) for k, v in tasks['seeds'].value_counts().items()},
            'out_of_range_coords': bad}


def extract_candidate(task: dict, args) -> dict:
    lat, lon = task['latitude'], task['longitude']
    site_id = f"fine_{task['fine_cell_id']}"
    row = dict(task)
    errors = {}
    provenance = {}
    if args.diagnostics:
        ex._DIAG.store = {}

    def attempt(source, fn):
        try:
            values, meta = fn()
            row.update(values)
            provenance[source] = meta
        except Exception as exc:
            errors[source] = str(exc)

    sources = set(args.sources.split(','))
    if 'base' in sources:
        def base():
            # Exactly the same 1,000 m window and time interval as the reference runner.
            if args.write_reports:
                result = extract_base(
                    lat, lon, site_id, args.out / 'reports' / site_id,
                    patch_size_m=1000, year_start=args.start, year_end=args.end,
                )
            else:
                with tempfile.TemporaryDirectory() as tmpdir:
                    result = extract_base(
                        lat, lon, site_id, tmpdir,
                        patch_size_m=1000, year_start=args.start, year_end=args.end,
                    )
            errors.update(result.get('errors', {}))
            # Match the Phase 2 reference extractor's base feature set.
            keys = ('elev_m', 'slope_median_deg', 'slope_p95_deg', 'relief_1km_m',
                    'tri_100m', 'ndvi_summer_median', 'bare_fraction')
            return {k: result.get(k) for k in keys}, result.get('provenance', {})
        attempt('base', base)
    if 'power' in sources:
        attempt('power', lambda: power_features(lat, lon, args.start, args.end))
    if 'modis' in sources:
        # Phase 2 defaults to Planetary Computer MODIS; Earth Engine is not required.
        attempt('modis', lambda: modis_features_pc(lat, lon, args.start, args.end))
    if 'glim' in sources:
        attempt('glim', lambda: glim_feature(lat, lon, args.phase1))
    if 'impact' in sources:
        attempt('impact', lambda: impact_feature(lat, lon, args.phase1))
    if 'emit' in sources:
        attempt('emit', lambda: emit_feature(lat, lon, args.start, args.end, args.phase1))
    row['errors'] = json.dumps(errors)
    row['provenance'] = json.dumps(provenance, default=str)
    row['features_computed'] = sum(pd.notna(row.get(f)) for f in CONTINUOUS + CATEGORICAL)
    if args.diagnostics:
        row['diagnostics'] = json.dumps(getattr(ex._DIAG, 'store', {}), default=str)
        ex._DIAG.store = None
    return row


class Checkpointer:
    """Single writer for candidate_features.parquet."""

    def __init__(self, path: Path, done: dict, total: int, metrics):
        self.path, self.done, self.total, self.metrics = path, done, total, metrics
        self.lock = threading.Lock()
        self.t0 = time.perf_counter()
        self.start_count = len(done)

    def store(self, cell_id: int, row: dict, label: str = '') -> None:
        with self.lock:
            self.done[cell_id] = row
            with self.metrics.timed('checkpoint'):
                pd.DataFrame(list(self.done.values())).to_parquet(self.path, index=False)
            n_new = len(self.done) - self.start_count
            rate = (time.perf_counter() - self.t0) / max(n_new, 1)
            print(f'[{len(self.done)}/{self.total}] saved {cell_id} {label} '
                  f'({rate:.1f} s/cell this session)', flush=True)


def run_extractions(tasks: list[dict], done: dict[int, dict], args, checkpoint: Path, metrics) -> dict[int, dict]:
    """Per-cell mode: extract remaining cells, optionally in parallel threads."""
    pending = [t for t in tasks if int(t['fine_cell_id']) not in done]
    total = len(tasks)
    if total - len(pending):
        print(f'Skipping {total - len(pending)} checkpointed cells; {len(pending)} remaining', flush=True)
    if not pending:
        return done
    ckpt = Checkpointer(checkpoint, done, total, metrics)
    workers = max(1, int(args.workers))
    ex.set_run_context(metrics=metrics, batch=None)

    def _one(task: dict) -> tuple[int, dict]:
        cell_id = int(task['fine_cell_id'])
        print(f'extracting fine cell {cell_id}', flush=True)
        return cell_id, extract_candidate(task, args)

    if workers == 1:
        for task in pending:
            cell_id, row = _one(task)
            ckpt.store(cell_id, row, '(per-cell, workers=1)')
        return done

    # I/O-bound Planetary Computer / POWER calls: threads overlap network waits.
    # Nested MODIS pools still use up to 12 threads each; keep workers modest (2–4).
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, task): int(task['fine_cell_id']) for task in pending}
        for fut in as_completed(futures):
            cell_id = futures[fut]
            try:
                cell_id, row = fut.result()
            except Exception as exc:
                print(f'ERROR fine cell {cell_id}: {exc}', flush=True)
                continue
            ckpt.store(cell_id, row, f'(per-cell, workers={workers})')
    return done


def run_batched(tasks: list[dict], done: dict[int, dict], args, checkpoint: Path, metrics) -> dict[int, dict]:
    """Batched mode: one parent at a time; shared discovery and asset-first reads."""
    import batch_io
    pending = [t for t in tasks if int(t['fine_cell_id']) not in done]
    total = len(tasks)
    if total - len(pending):
        print(f'Skipping {total - len(pending)} checkpointed cells; {len(pending)} remaining', flush=True)
    if not pending:
        return done
    ckpt = Checkpointer(checkpoint, done, total, metrics)
    parents: dict[int, list[dict]] = {}
    for t in pending:  # stable: parents in first-appearance order, children in task order
        parents.setdefault(int(t['parent_patch_id']), []).append(t)
    ex.set_run_context(metrics=metrics, batch=None)
    client = ex.catalog()  # one run-scoped client, used only on this coordinator thread
    sources = set(args.sources.split(','))
    inventories = []
    for pi, (pid, children) in enumerate(parents.items(), 1):
        t_parent = time.perf_counter()
        cells = []
        for t in children:
            cells.append({'id': int(t['fine_cell_id']), 'lat': t['latitude'], 'lon': t['longitude'],
                          'grid30': ex.patch_grid(t['latitude'], t['longitude'], 1000, 30),
                          'grid500': ex.modis_grid(t['latitude'], t['longitude'])})
        ctx = batch_io.BatchContext(client, [c['grid30'] for c in cells], metrics, args.asset_workers)
        ex.set_run_context(metrics=metrics, batch=ctx)
        try:
            warm = batch_io.warm_parent(ctx, cells, sources, args.start, args.end)
            print(f'parent {pid} ({pi}/{len(parents)}, {len(cells)} cells) warm-up {warm}; '
                  f'{len(ctx.arrays)} cached windows', flush=True)
            for t in children:
                cell_id = int(t['fine_cell_id'])
                row = extract_candidate(t, args)
                ckpt.store(cell_id, row, f'(batched parent {pid})')
        finally:
            inventories.append({'parent_patch_id': pid, 'queries': {f'{k[0]}|{k[1]}': v for k, v in ctx.inventory.items()}})
            ex.set_run_context(metrics=metrics, batch=None)
            del ctx  # bound cache lifetime to the parent
        metrics.add_time('parent_wall', time.perf_counter() - t_parent)
    (args.out / 'query_inventory.json').write_text(json.dumps(inventories, indent=1, default=str))
    return done


def rank_candidates(candidates: pd.DataFrame, references: pd.DataFrame, min_common: int) -> pd.DataFrame:
    if 'site_id' not in references.columns:
        raise ValueError('reference_features.parquet must have a site_id column')
    refs = references.set_index('site_id', drop=False)
    common = [c for c in CONTINUOUS if c in refs.columns and c in candidates.columns]
    if not common:
        raise ValueError('No shared numerical features between candidates and references')
    # Robust scales from reference distribution. Constant features are omitted,
    # rather than being assigned an arbitrary numerical scale.
    scales = {}
    for feature in common:
        s = pd.to_numeric(refs[feature], errors='coerce').dropna()
        if len(s) < 2:
            continue
        iqr = float(s.quantile(.75) - s.quantile(.25))
        std = float(s.std(ddof=0))
        scale = iqr if iqr > 1e-9 else std
        if np.isfinite(scale) and scale > 1e-9:
            scales[feature] = scale
    if not scales:
        raise ValueError('Reference features have no variable numeric columns')
    results = []
    unknown_seeds = set()
    for candidate in candidates.to_dict('records'):
        seed_ids = str(candidate.get('seeds', '')).split('|')
        for seed_id in seed_ids:
            if not seed_id:
                continue
            if seed_id not in refs.index:
                unknown_seeds.add(seed_id)
                continue
            ref = refs.loc[seed_id]
            if isinstance(ref, pd.DataFrame):
                ref = ref.iloc[0]
            differences = []
            used = []
            for feature, scale in scales.items():
                x = pd.to_numeric(candidate.get(feature), errors='coerce')
                y = pd.to_numeric(ref.get(feature), errors='coerce')
                if pd.notna(x) and pd.notna(y):
                    differences.append(((float(x) - float(y)) / scale) ** 2)
                    used.append(feature)
            if len(used) < min_common:
                continue
            # Lithology is a separately reported hard distinction, not ordinal arithmetic.
            litho_match = None
            if 'glim_lithology' in candidates.columns and 'glim_lithology' in refs.columns:
                a, b = candidate.get('glim_lithology'), ref.get('glim_lithology')
                if pd.notna(a) and pd.notna(b):
                    litho_match = str(a).strip() == str(b).strip()
            distance = math.sqrt(float(np.mean(differences)))
            results.append({
                'fine_cell_id': candidate['fine_cell_id'],
                'parent_patch_id': candidate['parent_patch_id'],
                'latitude': candidate['latitude'], 'longitude': candidate['longitude'],
                'nearest_reference_site': seed_id,
                'distance': distance,
                'similarity_score': 1.0 / (1.0 + distance),
                'features_matched': len(used),
                'matched_features': '|'.join(used),
                'lithology_match': litho_match,
            })
    if unknown_seeds:
        print('WARNING: prefilter seeds absent from reference_features:', sorted(unknown_seeds))
    if not results:
        print('WARNING: no candidate/reference comparisons met --min-common')
    ranked = pd.DataFrame(results)
    if not ranked.empty:
        ranked = ranked.sort_values(['similarity_score', 'features_matched'], ascending=[False, False])
        ranked.insert(0, 'rank', np.arange(1, len(ranked) + 1))
    return ranked


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(args) -> dict:
    try:
        rev = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=_ROOT, capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(['git', 'status', '--short', '--', *CODE_FILES], cwd=_ROOT, capture_output=True, text=True).stdout.strip()
    except Exception:
        rev, dirty = 'unknown', ''
    run_args = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()}
    return {
        'git_revision': rev,
        'git_dirty_code_files': dirty.splitlines(),
        'code_sha256': {f: _sha256(_ROOT / f) for f in CODE_FILES},
        'inputs_sha256': {'survivors': _sha256(args.survivors), 'references': _sha256(args.references)},
        'args': run_args,
    }


RESUME_KEYS = ('code_sha256', 'inputs_sha256', 'args')


def check_manifest(out: Path, manifest: dict, checkpoint: Path, allow_mismatch: bool) -> None:
    path = out / 'run_manifest.json'
    if path.exists():
        old = json.loads(path.read_text())
        diffs = [k for k in RESUME_KEYS if old.get(k) != manifest.get(k)]
        if diffs and checkpoint.exists() and not allow_mismatch:
            raise SystemExit(f'{out} was produced with different {diffs}; use a fresh --out '
                             '(or --allow-resume-mismatch if you are sure).')
    elif checkpoint.exists() and not allow_mismatch:
        raise SystemExit(f'{checkpoint} exists without run_manifest.json; refusing to resume an unverified run. '
                         'Use a fresh --out.')
    path.write_text(json.dumps(manifest, indent=2))


def main():
    ap = argparse.ArgumentParser(description='Fine-resolution analog search from prefilter survivor union')
    ap.add_argument('--survivors', type=Path, required=True)
    ap.add_argument('--references', type=Path, default=Path('data/phase2/reference_features.parquet'))
    ap.add_argument('--phase1', type=Path, default=Path('data/phase1'))
    ap.add_argument('--out', type=Path, default=Path('data/phase3'))
    ap.add_argument('--start', type=int, default=2022)
    ap.add_argument('--end', type=int, default=2024)
    ap.add_argument('--sources', default='base,power', help='Comma-separated: base,power,modis,glim,impact,emit')
    ap.add_argument('--max-parents', type=int, default=1, help='0 means all')
    ap.add_argument('--children-per-parent', type=int, default=10, help='0 means all')
    ap.add_argument('--max-cells', type=int, default=10, help='0 means all')
    ap.add_argument('--min-common', type=int, default=3)
    ap.add_argument(
        '--write-reports', action='store_true', default=False,
        help='Write per-cell GeoJSON/JSON/Parquet under out/reports/ (off by default for bulk runs)',
    )
    ap.add_argument(
        '--workers', type=int, default=4,
        help='Parallel cell extractions (threads) in per-cell mode. Use 2–4 for I/O overlap; 1 = serial. '
             'Batched mode requires 1.',
    )
    ap.add_argument('--stac-mode', choices=('per-cell', 'batched'), default='per-cell',
                    help='per-cell = original path; batched = parent-level shared STAC discovery and reads')
    ap.add_argument('--asset-workers', type=int, default=8,
                    help='Batched mode: concurrent asset reads (each worker owns its dataset handle)')
    ap.add_argument('--diagnostics', action='store_true', default=False,
                    help='Store MODIS sample item IDs per cell in a diagnostics column (for parity checks)')
    ap.add_argument('--allow-resume-mismatch', action='store_true', default=False)
    args = ap.parse_args()
    if args.start > args.end:
        ap.error('--start must not exceed --end')
    if args.workers < 1:
        ap.error('--workers must be >= 1')
    if args.stac_mode == 'batched' and args.workers != 1:
        ap.error('--stac-mode batched runs one parent at a time; pass --workers 1')
    requested = set(args.sources.split(','))
    invalid = requested - {'base', 'power', 'modis', 'glim', 'impact', 'emit'}
    if invalid:
        ap.error(f'Unknown sources: {sorted(invalid)}')
    args.out.mkdir(parents=True, exist_ok=True)
    tasks = load_tasks(args.survivors, args.max_parents, args.children_per_parent, args.max_cells)
    if tasks.empty:
        raise SystemExit('No child cells in survivors CSV')
    summary = validate_tasks(tasks)
    print(f'Tasks: {summary}', flush=True)
    tasks.to_csv(args.out / 'fine_cell_tasks.csv', index=False)
    refs = pd.read_parquet(args.references)
    checkpoint = args.out / 'candidate_features.parquet'
    check_manifest(args.out, build_manifest(args), checkpoint, args.allow_resume_mismatch)
    if checkpoint.exists():
        done_df = pd.read_parquet(checkpoint)
        done = {int(x): r for x, r in zip(done_df.fine_cell_id, done_df.to_dict('records'))}
    else:
        done = {}
    metrics = ex.RunMetrics()
    t0 = time.perf_counter()
    n_before = len(done)
    if args.stac_mode == 'batched':
        done = run_batched(tasks.to_dict('records'), done, args, checkpoint, metrics)
    else:
        done = run_extractions(tasks.to_dict('records'), done, args, checkpoint, metrics)
    wall = time.perf_counter() - t0
    candidates = pd.DataFrame(list(done.values()))
    ranked = rank_candidates(candidates, refs, args.min_common)
    ranked.to_csv(args.out / 'ranked_candidates.csv', index=False)
    n_new = len(done) - n_before
    report = {'mode': args.stac_mode, 'workers': args.workers, 'asset_workers': args.asset_workers,
              'wall_s': round(wall, 2), 'cells_extracted_this_session': n_new,
              's_per_cell': round(wall / n_new, 2) if n_new else None, **metrics.snapshot()}
    (args.out / 'run_metrics.json').write_text(json.dumps(report, indent=2))
    print(f'Metrics: {json.dumps(report)}')
    print(f'Extracted {len(candidates)} candidates; ranked {len(ranked)} candidate/reference pairs')
    print(f'Outputs: {args.out}')


if __name__ == '__main__':
    main()
