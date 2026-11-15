"""Stage 1 of the Earth analog search: a recall-preserving 50 km pre-filter.

Question answered: *which ~50 km patches of Earth could possibly contain a ~1 km window that matches
a studied analog seed?* Stage 2 (the fine 1 km comparison) then runs only inside the survivors.

How it stays honest
-------------------
* **Seeds and gates come from the Phase 1 catalog, mechanically.** A seed is a Phase 1 site with
  ``transferable_to_similarity_search = true`` and a published coordinate. Its candidate gates are
  the ``global_search`` features whose ``supported_process_ids`` belong to the transferable sites of
  the same analog family. No gate is chosen by looking at results, and nothing is tuned to recover a
  seed (Phase 1 methods rule).
* **Only what stage 1 can compute becomes a gate.** ``config/prefilter_features.csv`` marks every
  Phase 1 feature as ``gate``, ``informative`` or ``deferred`` with a reason. Elevation is never a
  gate. A seed whose candidate gates are all deferred prunes nothing, which is the correct,
  recall-preserving outcome.
* **Recall-preserving test.** A patch fails a gate only when its [min, max] envelope for that feature
  does not overlap the seed's acceptance interval [seed value - tolerance, seed value + tolerance].
  Missing data is *unknown* and passes. A patch is dropped only if some gate fails, so every patch
  that holds a fine cell with data inside every gate survives (tested as a property). A fine cell
  with missing data cannot be compared at stage 2 and is not a candidate; a patch with no data at all
  is unknown and survives.
* **Same scale on both sides.** The seed's interval is read from the same coarse layer as the
  patches, never compared against a finer or coarser measurement.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from .layers import GlobalRaster, InputFile, load_stage1_rasters, sha256_of
from .patches import PatchAccumulator, PatchGrid, PatchStats

BoolArray = NDArray[np.bool_]

_ROOT = Path(__file__).resolve().parents[2]
PHASE1_DIR = _ROOT / "config" / "phase1"
FEATURES_CONFIG = _ROOT / "config" / "prefilter_features.csv"

PASS, FAIL, UNKNOWN = 1, 0, -1
ROLES = ("gate", "informative", "deferred")


# ---------------------------------------------------------------------------------------------
# Phase 1 tables
# ---------------------------------------------------------------------------------------------


def read_table(path: Path | str) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _flag(text: str) -> bool:
    return (text or "").strip().lower() == "true"


@dataclass(frozen=True)
class Seed:
    site_id: str
    name: str
    family: str
    lon: float
    lat: float
    buffer_km: float
    coordinate_status: str


def load_seeds(phase1_dir: Path | str = PHASE1_DIR) -> list[Seed]:
    """Phase 1 sites that can seed a coordinate-based search (transferable and located)."""
    seeds = []
    for row in read_table(Path(phase1_dir) / "sites.csv"):
        if not _flag(row["transferable_to_similarity_search"]):
            continue
        if not row["latitude"].strip() or not row["longitude"].strip():
            continue
        buffer_m = float(row["buffer_m"]) if row["buffer_m"].strip() else 0.0
        seeds.append(
            Seed(
                site_id=row["site_id"],
                name=row["name"],
                family=row["analog_family"],
                lon=float(row["longitude"]),
                lat=float(row["latitude"]),
                buffer_km=buffer_m / 1000.0,
                coordinate_status=row["coordinate_status"],
            )
        )
    return seeds


BENCHMARK_FAMILY = "benchmark"


def load_benchmark_seeds(path: Path | str) -> list[Seed]:
    """Every row of ``config/benchmarks.csv`` as a seed (a team decision: the benchmarks are no longer held out).

    A benchmark has no Phase 1 processes, so it is gated on every global-search feature that has a computable
    value at its site (see ``gate_candidates``). Its acceptance interval comes from the whole site disc
    (``radius_km``). Ids are prefixed ``bm_`` so they cannot clash with Phase 1 site ids.
    """
    from .benchmarks import load_benchmarks

    return [
        Seed(site_id=f"bm_{b.id}", name=b.name, family=BENCHMARK_FAMILY, lon=b.lon, lat=b.lat, buffer_km=float(b.radius_km),
             coordinate_status=f"benchmark:{b.coord_confidence}:{'verified' if b.verified else 'unverified'}")
        for b in load_benchmarks(Path(path))
    ]


def seed_process_ids(seed: Seed, sites: list[dict], processes: list[dict]) -> set[str]:
    """Processes of every transferable site in the seed's analog family (contrast sites excluded)."""
    family_sites = {r["site_id"] for r in sites if r["analog_family"] == seed.family and _flag(r["transferable_to_similarity_search"])}
    return {p["process_id"] for p in processes if p["site_id"] in family_sites}


def gate_candidates(seed: Seed, sites: list[dict], processes: list[dict], features: list[dict]) -> list[str]:
    """``global_search`` features supporting any process of the seed's family, in catalog order."""
    if seed.family == BENCHMARK_FAMILY:
        return [f["feature_id"] for f in features if f["role"] == "global_search"]
    wanted = seed_process_ids(seed, sites, processes)
    out = []
    for feat in features:
        if feat["role"] != "global_search":
            continue
        if wanted & set(feat["supported_process_ids"].split("|")):
            out.append(feat["feature_id"])
    return out


# ---------------------------------------------------------------------------------------------
# Feature configuration
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureSpec:
    feature_id: str
    dataset_id: str  # the Drive dataset this feature is built from (datasets.csv)
    role: str
    layer: str
    unit: str
    tolerance_abs: float | None
    hard_lo: float | None
    hard_hi: float | None
    deferred_reason: str
    proxy_note: str
    source_product: str = ""


def _opt_float(text: str) -> float | None:
    return float(text) if (text or "").strip() else None


def load_feature_specs(
    path: Path | str = FEATURES_CONFIG,
    phase1_features: list[dict] | None = None,
    phase1_dir: Path | str | None = None,
) -> dict[str, FeatureSpec]:
    specs: dict[str, FeatureSpec] = {}
    for row in read_table(path):
        role = row["role"].strip()
        if role not in ROLES:
            raise ValueError(f"{row['feature_id']}: role must be one of {ROLES}, got {role!r}")
        spec = FeatureSpec(
            feature_id=row["feature_id"].strip(),
            dataset_id=row["dataset_id"].strip(),
            role=role,
            layer=row["layer"].strip(),
            unit=row["unit"].strip(),
            tolerance_abs=_opt_float(row["tolerance_abs"]),
            hard_lo=_opt_float(row["hard_lo"]),
            hard_hi=_opt_float(row["hard_hi"]),
            deferred_reason=row["deferred_reason"].strip(),
            proxy_note=row["proxy_note"].strip(),
            source_product=row.get("source_product", "").strip(),
        )
        if spec.feature_id in specs:
            raise ValueError(f"duplicate feature_id {spec.feature_id}")
        if role == "gate" and (not spec.layer or spec.tolerance_abs is None or spec.tolerance_abs < 0):
            raise ValueError(f"{spec.feature_id}: a gate needs a layer and a non-negative tolerance_abs")
        if role == "deferred" and not spec.deferred_reason:
            raise ValueError(f"{spec.feature_id}: a deferred feature needs a reason")
        if spec.feature_id == "elev_m" and role == "gate":
            raise ValueError("elev_m must never be a gate (Phase 1 methods rule)")
        specs[spec.feature_id] = spec
    if phase1_dir is not None:
        # Every dataset must be in datasets.csv and linked to the feature. Local stand-ins are rows appended
        # to our copy (see config/phase1/README.md), never silent substitutions.
        listed = {r["dataset_id"] for r in read_table(Path(phase1_dir) / "datasets.csv")}
        linked = {(r["feature_id"], r["dataset_id"]) for r in read_table(Path(phase1_dir) / "feature_datasets.csv")}
        for spec in specs.values():
            if spec.dataset_id not in listed:
                raise ValueError(f"{spec.feature_id}: dataset {spec.dataset_id!r} is not in the Drive datasets.csv")
            if (spec.feature_id, spec.dataset_id) not in linked:
                raise ValueError(f"{spec.feature_id}: dataset {spec.dataset_id!r} is not linked to it in feature_datasets.csv")
    if phase1_features is not None:
        catalog = {f["feature_id"] for f in phase1_features if f["role"] == "global_search"}
        missing = catalog - set(specs)
        extra = set(specs) - catalog
        if missing or extra:
            raise ValueError(f"prefilter_features.csv is out of step with Phase 1 features: missing={sorted(missing)} extra={sorted(extra)}")
    return specs


# ---------------------------------------------------------------------------------------------
# Profiles and the recall-preserving test
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GateRule:
    feature: str
    layer: str
    lo: float  # acceptance interval after tolerance
    hi: float
    seed_lo: float  # what the seed itself shows in this layer
    seed_hi: float
    tolerance: float
    unit: str
    note: str


@dataclass(frozen=True)
class Profile:
    seed: Seed
    gates: tuple[GateRule, ...]
    skipped: tuple[tuple[str, str], ...]  # (feature, reason)


def build_profile(seed: Seed, candidates: list[str], specs: dict[str, FeatureSpec], rasters: dict[str, GlobalRaster]) -> Profile:
    gates: list[GateRule] = []
    skipped: list[tuple[str, str]] = []
    for feature in candidates:
        spec = specs.get(feature)
        if spec is None:
            skipped.append((feature, "not listed in prefilter_features.csv"))
            continue
        if spec.role == "informative":
            skipped.append((feature, f"informative only: {spec.proxy_note}"))
            continue
        if spec.role == "deferred":
            skipped.append((feature, f"deferred: {spec.deferred_reason}"))
            continue
        raster = rasters.get(spec.layer)
        if raster is None:
            skipped.append((feature, f"layer {spec.layer} is not loaded"))
            continue
        window = raster.window_values(seed.lon, seed.lat, seed.buffer_km)
        if spec.layer + "@hi" in rasters:  # a layer stored as a minimum and a maximum raster
            window = np.concatenate([window, rasters[spec.layer + "@hi"].window_values(seed.lon, seed.lat, seed.buffer_km)])
        if window.size == 0:
            skipped.append((feature, "no valid value at the seed, so it is unknown and cannot prune"))
            continue
        seed_lo, seed_hi = float(window.min()), float(window.max())
        tol = float(spec.tolerance_abs or 0.0)
        lo, hi = seed_lo - tol, seed_hi + tol
        if spec.hard_lo is not None:
            if seed_lo < spec.hard_lo:
                raise ValueError(f"{feature}: seed value {seed_lo} is below hard_lo {spec.hard_lo}")
            lo = max(lo, spec.hard_lo)
        if spec.hard_hi is not None:
            if seed_hi > spec.hard_hi:
                raise ValueError(f"{feature}: seed value {seed_hi} is above hard_hi {spec.hard_hi}")
            hi = min(hi, spec.hard_hi)
        gates.append(GateRule(feature, spec.layer, lo, hi, seed_lo, seed_hi, tol, spec.unit, spec.proxy_note))
    return Profile(seed, tuple(gates), tuple(skipped))


@dataclass(frozen=True)
class Evaluation:
    population: BoolArray  # patches with any valid input (the land patches the filter is judged over)
    status: dict[str, NDArray[np.int8]]  # per gate: PASS, FAIL or UNKNOWN
    survives: BoolArray


def evaluate(stats: dict[str, PatchStats], gates: tuple[GateRule, ...] | list[GateRule], population: BoolArray) -> Evaluation:
    """Apply gates. A patch fails a gate only if its envelope cannot overlap the acceptance interval."""
    survives = population.copy()
    status: dict[str, NDArray[np.int8]] = {}
    for gate in gates:
        s = stats[gate.feature]
        observed = s.observed
        with np.errstate(invalid="ignore"):
            overlap = (s.maximum >= gate.lo) & (s.minimum <= gate.hi)
        st = np.where(~observed, UNKNOWN, np.where(overlap, PASS, FAIL)).astype(np.int8)
        status[gate.feature] = st
        survives &= st != FAIL
    return Evaluation(population=population, status=status, survives=survives)


def compute_feature_stats(rasters: dict[str, GlobalRaster], specs: dict[str, FeatureSpec], grid: PatchGrid) -> dict[str, PatchStats]:
    """Per-patch statistics for every gate or informative feature whose layer is loaded."""
    by_layer: dict[str, PatchStats] = {}
    out: dict[str, PatchStats] = {}
    for feature, spec in specs.items():
        if spec.role == "deferred" or spec.layer not in rasters:
            continue
        if spec.layer not in by_layer:
            acc = PatchAccumulator(grid.npix_coarse)
            rasters[spec.layer].accumulate(acc, grid)
            if spec.layer + "@hi" in rasters:
                rasters[spec.layer + "@hi"].accumulate(acc, grid)
            by_layer[spec.layer] = acc.result()
        out[feature] = by_layer[spec.layer]
    return out


def population_mask(stats: dict[str, PatchStats], npix: int) -> BoolArray:
    mask = np.zeros(npix, dtype=bool)
    for s in stats.values():
        mask |= s.observed
    return mask


# ---------------------------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------------------------

_LABEL = {PASS: "pass", FAIL: "fail", UNKNOWN: "unknown"}


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=_ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001 - a missing git must not stop a run
        return "unknown"


def _fmt(value: float) -> str:
    return "" if not np.isfinite(value) else f"{value:.6g}"


def seed_recall(seed: Seed, grid: PatchGrid, evaluation: Evaluation) -> dict:
    """Does the patch holding the seed (and every patch its buffer touches) survive its own profile?"""
    own = int(grid.patch_of(np.array([seed.lon]), np.array([seed.lat]))[0])
    near = grid.patches_within(seed.lon, seed.lat, seed.buffer_km) if seed.buffer_km > 0 else np.array([own])
    near = np.union1d(near, [own])
    return {
        "seed_patch": own,
        "seed_patch_in_population": bool(evaluation.population[own]),
        "seed_patch_survives": bool(evaluation.survives[own]),
        "buffer_patches": int(near.size),
        "buffer_patches_surviving": int(evaluation.survives[near].sum()),
    }


def write_seed_outputs(
    out_dir: Path,
    profile: Profile,
    grid: PatchGrid,
    stats: dict[str, PatchStats],
    evaluation: Evaluation,
    recall: dict,
    inputs: list[InputFile],
    notes: list[str],
    config_files: dict[str, Path],
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    idx = np.flatnonzero(evaluation.population)
    lon, lat = grid.centers(idx)
    features = list(stats)

    columns = ["patch_id", "lon", "lat"]
    for f in features:
        columns += [f"{f}_min", f"{f}_mean", f"{f}_max", f"{f}_valid_fraction"]
    columns += [f"gate_{g.feature}" for g in profile.gates] + ["survives"]
    with open(out_dir / "patches.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for k, p in enumerate(idx):
            row: list = [int(p), f"{lon[k]:.4f}", f"{lat[k]:.4f}"]
            for f in features:
                s = stats[f]
                row += [_fmt(s.minimum[p]), _fmt(s.mean[p]), _fmt(s.maximum[p]), _fmt(s.valid_fraction[p])]
            row += [_LABEL[int(evaluation.status[g.feature][p])] for g in profile.gates]
            row.append(bool(evaluation.survives[p]))
            writer.writerow(row)

    survivors = np.flatnonzero(evaluation.survives)
    slon, slat = grid.centers(survivors)
    with open(out_dir / "survivors.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["patch_id", "lon", "lat", "child_start", "n_children", "coarse_nside", "fine_nside"])
        for k, p in enumerate(survivors):
            start, _ = grid.child_range(int(p))
            writer.writerow([int(p), f"{slon[k]:.4f}", f"{slat[k]:.4f}", start, grid.children_per_patch, grid.coarse_nside, grid.fine_nside])

    n_pop, n_surv = int(evaluation.population.sum()), int(survivors.size)
    summary = {
        "seed": profile.seed.site_id,
        "gates": [g.feature for g in profile.gates],
        "population_patches": n_pop,
        "surviving_patches": n_surv,
        "removed_fraction": round(1 - n_surv / n_pop, 4) if n_pop else None,
        **recall,
    }
    profile_doc = {
        "seed": profile.seed.__dict__,
        "interpretation": "A patch survives unless some gate fails. A gate fails only when the patch's [min, max] for that feature cannot overlap [lo, hi]. Missing data is unknown and passes.",
        "gates": [g.__dict__ for g in profile.gates],
        "not_gates": [{"feature": f, "reason": r} for f, r in profile.skipped],
    }
    (out_dir / "profile.json").write_text(json.dumps(profile_doc, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "grid": {"coarse_nside": grid.coarse_nside, "fine_nside": grid.fine_nside, "ordering": "NESTED", "patch_area_km2": round(grid.patch_area_km2, 1), "children_per_patch": grid.children_per_patch},
        "inputs": [i.__dict__ for i in inputs],
        "config_sha256": {name: sha256_of(path) for name, path in config_files.items()},
        "notes": notes,
        "summary": summary,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return summary


def write_union(out_dir: Path, grid: PatchGrid, survivors_by_seed: dict[str, BoolArray]) -> int:
    """One list for stage 2: every patch that survives at least one seed, with the seeds that keep it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    union = np.zeros(grid.npix_coarse, dtype=bool)
    for mask in survivors_by_seed.values():
        union |= mask
    idx = np.flatnonzero(union)
    lon, lat = grid.centers(idx)
    with open(out_dir / "survivors_union.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["patch_id", "lon", "lat", "child_start", "n_children", "coarse_nside", "fine_nside", "seeds"])
        for k, p in enumerate(idx):
            start, _ = grid.child_range(int(p))
            keepers = "|".join(sid for sid, mask in survivors_by_seed.items() if mask[p])
            writer.writerow([int(p), f"{lon[k]:.4f}", f"{lat[k]:.4f}", start, grid.children_per_patch, grid.coarse_nside, grid.fine_nside, keepers])
    return int(idx.size)


# ---------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------


def run_prefilter(
    raw_dir: Path,
    out_dir: Path,
    *,
    seed_ids: list[str] | None = None,
    months: tuple[int, ...] | None = None,
    phase1_dir: Path = PHASE1_DIR,
    features_config: Path = FEATURES_CONFIG,
    benchmarks_csv: Path | None = None,
    benchmark_seeds: Path | None = None,
    grid: PatchGrid | None = None,
) -> list[dict]:
    grid = grid or PatchGrid()
    sites = read_table(phase1_dir / "sites.csv")
    processes = read_table(phase1_dir / "processes.csv")
    features = read_table(phase1_dir / "features.csv")
    specs = load_feature_specs(features_config, phase1_features=features, phase1_dir=phase1_dir)
    seeds = load_seeds(phase1_dir)
    if benchmark_seeds:
        seeds += load_benchmark_seeds(benchmark_seeds)
    if seed_ids:
        unknown = set(seed_ids) - {s.site_id for s in seeds}
        if unknown:
            raise ValueError(f"not a located Phase 1 seed: {sorted(unknown)}; choose from {[s.site_id for s in seeds]}")
        seeds = [s for s in seeds if s.site_id in seed_ids]

    needed = {sp.layer for sp in specs.values() if sp.role == "gate"}
    rasters, inputs, notes = load_stage1_rasters(raw_dir, months=months, layers=needed)
    for sp in specs.values():
        if sp.role == "gate" and sp.source_product:
            notes.append(f"{sp.feature_id}: {sp.source_product}")
    if not rasters:
        raise FileNotFoundError(f"no input files found under {raw_dir} (looking for MODIS MOD11C3, MOD13C2, MOD10CM and MERRA-2 land monthly files)")
    stats = compute_feature_stats(rasters, specs, grid)
    population = population_mask(stats, grid.npix_coarse)

    config_files = {"prefilter_features.csv": features_config, "phase1_sites.csv": phase1_dir / "sites.csv", "phase1_features.csv": phase1_dir / "features.csv", "phase1_processes.csv": phase1_dir / "processes.csv"}
    summaries: list[dict] = []
    survivors_by_seed: dict[str, BoolArray] = {}
    for seed in seeds:
        candidates = gate_candidates(seed, sites, processes, features)
        profile = build_profile(seed, candidates, specs, rasters)
        evaluation = evaluate(stats, profile.gates, population)
        recall = seed_recall(seed, grid, evaluation)
        summary = write_seed_outputs(out_dir / seed.site_id, profile, grid, stats, evaluation, recall, inputs, notes, config_files)
        summary["candidate_features"] = candidates
        summaries.append(summary)
        survivors_by_seed[seed.site_id] = evaluation.survives

    n_union = write_union(out_dir / "union", grid, survivors_by_seed)
    for s in summaries:
        s["union_patches"] = n_union

    if benchmarks_csv and benchmarks_csv.exists():
        from .benchmarks import load_benchmarks  # informational check only

        for s, seed in zip(summaries, seeds):
            kept = []
            for b in load_benchmarks(benchmarks_csv):
                p = int(grid.patch_of(np.array([b.lon]), np.array([b.lat]))[0])
                kept.append((b.id, bool(survivors_by_seed[seed.site_id][p]), bool(population[p])))
            s["benchmarks_surviving"] = [bid for bid, keep, _ in kept if keep]
            s["benchmarks_pruned"] = [bid for bid, keep, in_pop in kept if in_pop and not keep]
            s["benchmarks_without_data"] = [bid for bid, _, in_pop in kept if not in_pop]
    return summaries


def print_summary(summaries: list[dict]) -> None:
    print(f"{'seed':<24}{'gates':>6}{'land patches':>14}{'survive':>9}{'removed':>9}  seed patch")
    for s in summaries:
        removed = "n/a" if s["removed_fraction"] is None else f"{s['removed_fraction'] * 100:.1f}%"
        verdict = "survives" if s["seed_patch_survives"] else ("NOT IN DATA" if not s["seed_patch_in_population"] else "PRUNED (bug)")
        print(f"{s['seed']:<24}{len(s['gates']):>6}{s['population_patches']:>14,}{s['surviving_patches']:>9,}{removed:>9}  {verdict}")
        if not s["gates"]:
            print(f"{'':<24}(no computable gate for this seed yet: candidates {s['candidate_features']} are deferred, nothing pruned)")
        if "benchmarks_pruned" in s:
            print(f"{'':<24}benchmark sites pruned (informational): {s['benchmarks_pruned'] or 'none'}")
    if summaries:
        print(f"\nunion of survivors across seeds: {summaries[0]['union_patches']:,} patches")


def run_cli(args) -> int:  # pragma: no cover - thin wrapper around run_prefilter
    months = tuple(int(m) for m in args.months.split(",")) if args.months else None
    bench = _ROOT / "config" / "benchmarks.csv"
    try:
        summaries = run_prefilter(
            args.raw,
            args.out,
            seed_ids=args.seed or None,
            months=months,
            benchmarks_csv=bench if bench.exists() else None,
            benchmark_seeds=bench if (getattr(args, "benchmark_seeds", False) and bench.exists()) else None,
        )
    except (FileNotFoundError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print_summary(summaries)
    return 0
