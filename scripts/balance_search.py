#!/usr/bin/env python3
"""Build a stratified survivors CSV for an MVP Phase 3 run.

Prefer parents that list exactly one seed. If a seed has too few exclusive
parents (e.g. beacon_central_polygon), backfill from multi-seed parents that
include it, then rewrite ``seeds`` to that single id so each fine cell is
compared to one analog only.

Writes data/prefilter_vm/union/survivors_mvp_stratified.csv.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SURVIVORS = ROOT / "data/prefilter_vm/union/survivors_union.csv"
OUT = ROOT / "data/prefilter_vm/union/survivors_mvp_stratified.csv"
SEEDS = ("university_valley", "beacon_central_polygon", "drill_hill")
N_PER_SEED = 5


def _contains_seed(series: pd.Series, seed: str) -> pd.Series:
    return series.fillna("").str.split("|").apply(lambda xs: seed in xs)


def sample_for_seed(
    surv: pd.DataFrame, seed: str, n: int, rng: int, used: set[int]
) -> pd.DataFrame:
    available = surv[~surv["patch_id"].isin(used)]
    exclusive = available[available["seeds"] == seed]
    take = exclusive.sample(n=min(n, len(exclusive)), random_state=rng).copy()
    if len(take) < n:
        need = n - len(take)
        pool = available[_contains_seed(available["seeds"], seed) & (available["seeds"] != seed)]
        pool = pool[~pool["patch_id"].isin(take["patch_id"])]
        extra = pool.sample(n=min(need, len(pool)), random_state=rng).copy()
        take = pd.concat([take, extra], ignore_index=True)
    # Force single-seed comparisons for the MVP ranking story.
    take = take.copy()
    take["seeds"] = seed
    return take


def main() -> None:
    surv = pd.read_csv(SURVIVORS)
    parts: list[pd.DataFrame] = []
    used: set[int] = set()
    for i, seed in enumerate(SEEDS):
        part = sample_for_seed(surv, seed, N_PER_SEED, rng=42 + i, used=used)
        if part.empty:
            raise SystemExit(f"No parents for {seed!r} in {SURVIVORS}")
        used.update(int(x) for x in part["patch_id"])
        parts.append(part)

    strat = pd.concat(parts, ignore_index=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    strat.to_csv(OUT, index=False)
    print(f"Wrote {len(strat)} parents -> {OUT}")
    print(strat["seeds"].value_counts().to_string())
    print(f"unique patch_id: {strat['patch_id'].nunique()}")


if __name__ == "__main__":
    main()
