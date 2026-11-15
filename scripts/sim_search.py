#!/usr/bin/env python3
"""Phase 3: expand surviving NESTED HEALPix parents and rank 1 km analog windows.

Run from repository root; imports scripts/extraction.py without modifying it.
Uses 1,000 m centred windows (the same footprint as Phase 2), not exact
~0.80 km HEALPix child polygons. CSVs are checkpointed after each candidate.
Per-cell report files are off by default; use --write-reports only for debugging.
Use --workers 2–4 to overlap Planetary Computer / POWER network waits across cells.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import healpy as hp
import numpy as np
import pandas as pd

# scripts/extraction.py is the existing Phase 2 extractor.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from extraction import (  # noqa: E402
    extract_base, power_features, modis_features_pc, glim_feature,
    impact_feature, emit_feature,
)

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
            tasks.append({
                'fine_cell_id': child, 'parent_patch_id': int(p.patch_id),
                'latitude': float(lat), 'longitude': float(lon),
                'seeds': str(p.seeds) if pd.notna(p.seeds) else '',
                'fine_nside': fine,
            })
            if max_cells and len(tasks) >= max_cells:
                return pd.DataFrame(tasks)
    return pd.DataFrame(tasks)


def extract_candidate(task: dict, args) -> dict:
    lat, lon = task['latitude'], task['longitude']
    site_id = f"fine_{task['fine_cell_id']}"
    row = dict(task)
    errors = {}
    provenance = {}

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
            report_dir = (args.out / 'reports' / site_id) if args.write_reports else None
            result = extract_base(
                lat, lon, site_id, report_dir,
                patch_size_m=1000, year_start=args.start, year_end=args.end,
                write_reports=args.write_reports,
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
    return row


def run_extractions(tasks: list[dict], done: dict[int, dict], args, checkpoint: Path) -> dict[int, dict]:
    """Extract remaining cells, optionally in parallel. Checkpoint after each success."""
    pending = [t for t in tasks if int(t['fine_cell_id']) not in done]
    total = len(tasks)
    already = total - len(pending)
    if already:
        print(f'Skipping {already} checkpointed cells; {len(pending)} remaining', flush=True)
    if not pending:
        return done

    lock = threading.Lock()
    workers = max(1, int(args.workers))

    def _one(task: dict) -> tuple[int, dict]:
        cell_id = int(task['fine_cell_id'])
        print(f'extracting fine cell {cell_id}', flush=True)
        return cell_id, extract_candidate(task, args)

    def _store(cell_id: int, row: dict, finished: int) -> None:
        with lock:
            done[cell_id] = row
            pd.DataFrame(list(done.values())).to_parquet(checkpoint, index=False)
            print(f'[{already + finished}/{total}] saved {cell_id} (workers={workers})', flush=True)

    if workers == 1:
        for i, task in enumerate(pending, 1):
            cell_id, row = _one(task)
            _store(cell_id, row, i)
        return done

    # I/O-bound Planetary Computer / POWER calls: threads overlap network waits.
    # Nested MODIS pools still use up to 12 threads each; keep workers modest (2–4).
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, task): int(task['fine_cell_id']) for task in pending}
        finished = 0
        for fut in as_completed(futures):
            cell_id = futures[fut]
            try:
                cell_id, row = fut.result()
            except Exception as exc:
                print(f'ERROR fine cell {cell_id}: {exc}', flush=True)
                continue
            finished += 1
            _store(cell_id, row, finished)
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
        help='Parallel cell extractions',
    )
    args = ap.parse_args()
    if args.start > args.end:
        ap.error('--start must not exceed --end')
    if args.workers < 1:
        ap.error('--workers must be >= 1')
    requested = set(args.sources.split(','))
    invalid = requested - {'base', 'power', 'modis', 'glim', 'impact', 'emit'}
    if invalid:
        ap.error(f'Unknown sources: {sorted(invalid)}')
    args.out.mkdir(parents=True, exist_ok=True)
    tasks = load_tasks(args.survivors, args.max_parents, args.children_per_parent, args.max_cells)
    if tasks.empty:
        raise SystemExit('No child cells in survivors CSV')
    tasks.to_csv(args.out / 'fine_cell_tasks.csv', index=False)
    refs = pd.read_parquet(args.references)
    checkpoint = args.out / 'candidate_features.parquet'
    if checkpoint.exists():
        done_df = pd.read_parquet(checkpoint)
        done = {int(x): r for x, r in zip(done_df.fine_cell_id, done_df.to_dict('records'))}
    else:
        done = {}
    done = run_extractions(tasks.to_dict('records'), done, args, checkpoint)
    candidates = pd.DataFrame(list(done.values()))
    ranked = rank_candidates(candidates, refs, args.min_common)
    ranked.to_csv(args.out / 'ranked_candidates.csv', index=False)
    print(f'Extracted {len(candidates)} candidates; ranked {len(ranked)} candidate/reference pairs')


if __name__ == '__main__':
    main()
