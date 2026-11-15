"""Join stage-2 ranked candidates with their measured features and write web/src/data/candidates.json.

    uv run python scripts/build_candidates_json.py candidate_features.parquet all_15_sites_ranked_candidates.csv

The front end reads this file; nothing is invented here. Longitudes are wrapped to -180..180. Missing
feature values stay null (the features parquet may cover only some cells). Ranks are within each seed
(1 = best for that seed): `site_rank` when the ranking file has it, else `rank`.
"""
import json
import sys
from pathlib import Path

import pandas as pd

FEATURES = ["elev_m", "slope_median_deg", "slope_p95_deg", "relief_1km_m", "tri_100m", "ndvi_summer_median", "bare_fraction",
            "air_temp_days_above_0c", "precip_mm_yr", "humidity_mean_pct", "lst_summer_max_c", "lst_winter_min_c", "snow_persistence", "impact_distance_km"]


def num(v):
    return None if pd.isna(v) else round(float(v), 4)


def feature_error(r) -> str | None:
    """Why a cell has no feature values: not in the features file, or the extractor's own error text."""
    if "features_computed" not in r.index or pd.isna(r.get("features_computed")):
        return "not in the features file"
    errs = r.get("errors")
    if isinstance(errs, str) and errs.strip():
        try:
            msgs = json.loads(errs)
        except ValueError:
            return errs[:120]
        if not msgs:
            return None
        if any("Invalid geographic coordinate" in str(m) for m in msgs.values()):
            return "feature extraction failed: invalid geographic coordinate (longitude sent as 0-360 east)"
        return "feature extraction failed: " + "; ".join(f"{k}: {str(v)[:60]}" for k, v in msgs.items())
    return None


ADMIN_KEYS = ("town", "village", "municipality", "city", "county", "region", "state_district", "state")


def place_for(cache: dict, lat: float, lon: float):
    """Name from scripts/geocode_candidates.py's cache (OpenStreetMap): island/natural area first, else the most
    specific admin name; `area` is the enclosing state and country. None when the cell was not looked up."""
    hit = cache.get(f"{round(lat / 0.05) * 0.05:.2f},{round(lon / 0.05) * 0.05:.2f}")
    if not hit:
        return None
    addr = hit.get("address") or {}
    nat = hit.get("natural")
    name = nat["name"] if nat else next((addr[k] for k in ADMIN_KEYS if addr.get(k)), None)
    area = ", ".join(dict.fromkeys(v for v in (addr.get("state"), addr.get("country")) if v and v != name))
    if not name and not area:
        return None
    return {"name": name or area, "area": area if name else None}


def feature_notes(path: str) -> dict:
    """Per-feature definition and 'why it matters' from the Phase 1 catalog (data/phase1/features.csv)."""
    if not Path(path).exists():
        return {}
    cat = pd.read_csv(path)
    return {r["feature_id"]: {"definition": r.get("definition"), "why": r.get("why_it_matters"), "limits": r.get("proxy_limitations")}
            for _, r in cat.iterrows() if r["feature_id"] in FEATURES}


def main(features_path: str, ranked_path: str, out: str = "web/src/data/candidates.json", geocache: str = "data/processed/geocode_cache.json",
         catalog: str = "../NASA-SI/data/phase1/features.csv") -> None:
    cache = json.loads(Path(geocache).read_text()) if Path(geocache).exists() else {}
    feats = pd.read_parquet(features_path)
    ranked = pd.read_csv(ranked_path)
    merged = ranked.merge(feats, on="fine_cell_id", suffixes=("", "_f"), how="left")
    rank_col = "site_rank" if "site_rank" in merged.columns else "rank"
    rows = []
    for _, r in merged.iterrows():
        lon = float(r["longitude"])
        litho = r.get("lithology_match")
        rows.append({
            "seed": r["nearest_reference_site"], "rank": int(r[rank_col]), "fineCellId": int(r["fine_cell_id"]), "patchId": int(r["parent_patch_id"]),
            "lithologyMatch": None if pd.isna(litho) else str(litho).strip().lower() == "true",
            "sourceBatch": None if pd.isna(r.get("source_batch")) else str(r["source_batch"]),
            "featureError": feature_error(r),
            "lithology": None if pd.isna(r.get("glim_lithology")) else str(r["glim_lithology"]),
            "lat": round(float(r["latitude"]), 5), "lon": round(lon - 360 if lon > 180 else lon, 5),
            "place": place_for(cache, float(r["latitude"]), lon - 360 if lon > 180 else lon),
            "distance": num(r["distance"]), "similarity": num(r["similarity_score"]),
            "featuresMatched": int(r["features_matched"]), "matched": str(r["matched_features"]).split("|"),
            "features": (feats_row := {k: num(r[k]) if k in merged.columns else None for k in FEATURES}),
            "hasFeatures": any(v is not None for v in feats_row.values()),
            "opticalError": "optical" in json.loads(r["errors"] or "{}") if isinstance(r.get("errors"), str) else False,
        })
    source = {"features": Path(features_path).name, "ranked": Path(ranked_path).name, "places": "OpenStreetMap contributors (ODbL), via Overpass and Nominatim"}
    notes = {k: {kk: (None if pd.isna(vv) else vv) for kk, vv in v.items()} for k, v in feature_notes(catalog).items()}
    Path(out).write_text(json.dumps({"source": source, "featureNotes": notes, "candidates": rows}, indent=1))
    print(f"wrote {len(rows)} candidates to {out}")


if __name__ == "__main__":
    main(*sys.argv[1:])
