#!/usr/bin/env python3
"""Check joins and evidence rules for the Phase 1 analog catalog."""

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "phase1"

PROCESS_GRADES = {
    "peer_reviewed",
    "conference_abstract",
    "project_website",
    "report",
    "contested",
}
SOURCE_TYPES = PROCESS_GRADES | {"gazetteer", "dataset"}
OPENED = {"full_text", "landing_page", "not_opened"}
SITE_ROLES = {
    "context_region",
    "science_subregion",
    "operations_facility",
    "contrast",
    "disturbance",
    "historical",
}
COORD_STATUS = {"unlocated", "published_point", "published_conflict", "approximate_buffer"}
FEATURE_ROLES = {"global_search", "reference_only"}
COVERAGE = {"global", "latitude_limited", "multi_region", "reference_only"}
BROADER = {"global", "latitude_limited", "multi_region"}
JOIN_ROLES = {"primary", "alternate", "context_not_measurement"}
BOOL = {"true", "false"}
YES_NO = {"yes", "no"}
YES_NO_PARTIAL = {"yes", "no", "partial"}
REMOTE = {"yes", "partial", "no"}


def read_csv(name, data=None):
    folder = DATA if data is None else Path(data)
    with (folder / name).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def split_ids(value):
    if not value or not value.strip():
        return []
    return [part.strip() for part in value.split("|") if part.strip()]


def unique(rows, key, errors):
    seen = set()
    for row in rows:
        identifier = row[key]
        if not identifier:
            errors.append(f"{key} is empty")
            continue
        if identifier in seen:
            errors.append(f"duplicate {key}: {identifier}")
        seen.add(identifier)
    return seen


def validate(data_dir=None):
    """Return catalog errors. An empty list means the Phase 1 tables are consistent."""
    errors = []
    references = read_csv("references.csv", data_dir)
    sites = read_csv("sites.csv", data_dir)
    processes = read_csv("processes.csv", data_dir)
    features = read_csv("features.csv", data_dir)
    datasets = read_csv("datasets.csv", data_dir)
    joins = read_csv("feature_datasets.csv", data_dir)
    folder = DATA if data_dir is None else Path(data_dir)
    geo = json.loads((folder / "sites.geojson").read_text(encoding="utf-8"))

    citation_ids = unique(references, "citation_id", errors)
    site_ids = unique(sites, "site_id", errors)
    process_ids = unique(processes, "process_id", errors)
    feature_ids = unique(features, "feature_id", errors)
    dataset_ids = unique(datasets, "dataset_id", errors)

    for row in references:
        if row["source_type"] not in SOURCE_TYPES:
            errors.append(f"{row['citation_id']} has source_type {row['source_type']}")
        if row["opened"] not in OPENED:
            errors.append(f"{row['citation_id']} has opened {row['opened']}")
        if row["opened"] == "not_opened" and row["doi"]:
            errors.append(f"{row['citation_id']} has a DOI but was not opened")

    for row in sites:
        if row["site_role"] not in SITE_ROLES:
            errors.append(f"{row['site_id']} has site_role {row['site_role']}")
        if row["coordinate_status"] not in COORD_STATUS:
            errors.append(f"{row['site_id']} has coordinate_status {row['coordinate_status']}")
        if row["transferable_to_similarity_search"] not in BOOL:
            errors.append(f"{row['site_id']} has a bad transferable flag")
        if row["site_role"] == "operations_facility" and row["transferable_to_similarity_search"] != "false":
            errors.append(f"{row['site_id']} is an operations site marked transferable")
        parent = row["parent_site_id"]
        if parent and parent not in site_ids:
            errors.append(f"{row['site_id']} parent {parent} is missing")
        if parent == row["site_id"]:
            errors.append(f"{row['site_id']} is its own parent")
        located = row["coordinate_status"] != "unlocated"
        has_coord = bool(row["latitude"] and row["longitude"])
        if located != has_coord:
            errors.append(f"{row['site_id']} coordinate status does not match latitude/longitude")
        if not split_ids(row["citation_ids"]):
            errors.append(f"{row['site_id']} has no citation")
        for citation_id in split_ids(row["citation_ids"]):
            if citation_id not in citation_ids:
                errors.append(f"{row['site_id']} cites missing {citation_id}")

    for row in processes:
        if row["site_id"] not in site_ids:
            errors.append(f"{row['process_id']} site {row['site_id']} is missing")
        if row["evidence_strength"] not in PROCESS_GRADES:
            errors.append(f"{row['process_id']} has evidence_strength {row['evidence_strength']}")
        if row["measurable_by_remote_sensing"] not in REMOTE:
            errors.append(f"{row['process_id']} has a bad remote-sensing flag")
        cites = split_ids(row["citation_ids"])
        if not cites:
            errors.append(f"{row['process_id']} has no citation")
        for citation_id in cites:
            if citation_id not in citation_ids:
                errors.append(f"{row['process_id']} cites missing {citation_id}")
        if row["evidence_strength"] == "contested" and not row["caveat"].strip():
            errors.append(f"{row['process_id']} is contested without a caveat")
        if row["evidence_strength"] == "project_website" and row["measurable_by_remote_sensing"] == "yes":
            errors.append(f"{row['process_id']} treats a website claim as directly measurable")

    datasets_by_id = {row["dataset_id"]: row for row in datasets}
    for row in datasets:
        if row["coverage_scope"] not in COVERAGE:
            errors.append(f"{row['dataset_id']} has coverage_scope {row['coverage_scope']}")
        if row["phase1_status"] != "cataloged_not_downloaded":
            errors.append(f"{row['dataset_id']} is not catalog-only")
        if row["auth_required"] not in YES_NO:
            errors.append(f"{row['dataset_id']} has a bad auth flag")
        for flag in ("covers_mcmurdo", "covers_askja", "covers_mauna_loa", "covers_haughton"):
            if row[flag] not in YES_NO_PARTIAL:
                errors.append(f"{row['dataset_id']} has {flag} {row[flag]}")

    joins_by_feature = {}
    for row in joins:
        if row["feature_id"] not in feature_ids:
            errors.append(f"join feature {row['feature_id']} is missing")
        if row["dataset_id"] not in dataset_ids:
            errors.append(f"join dataset {row['dataset_id']} is missing")
        if row["role_in_feature"] not in JOIN_ROLES:
            errors.append(f"join role {row['role_in_feature']} is invalid")
        joins_by_feature.setdefault(row["feature_id"], []).append(row)

    for row in features:
        if row["role"] not in FEATURE_ROLES:
            errors.append(f"{row['feature_id']} has role {row['role']}")
        if row["recommended_for_phase2"] not in YES_NO:
            errors.append(f"{row['feature_id']} has a bad phase-2 flag")
        if row["role"] == "global_search" and row["recommended_for_phase2"] != "yes":
            errors.append(f"{row['feature_id']} is global but not recommended")
        if row["role"] == "reference_only" and row["recommended_for_phase2"] != "no":
            errors.append(f"{row['feature_id']} is reference-only but recommended for search")
        for process_id in split_ids(row["supported_process_ids"]):
            if process_id not in process_ids:
                errors.append(f"{row['feature_id']} supports missing {process_id}")
        linked = joins_by_feature.get(row["feature_id"], [])
        if row["role"] == "global_search":
            broader = [
                item
                for item in linked
                if item["dataset_id"] in datasets_by_id
                and datasets_by_id[item["dataset_id"]]["coverage_scope"] in BROADER
            ]
            if not broader:
                errors.append(f"{row['feature_id']} has no dataset beyond a reference site")
        if row["role"] == "reference_only":
            for item in linked:
                dataset = datasets_by_id.get(item["dataset_id"])
                if dataset is None:
                    continue
                scope = dataset["coverage_scope"]
                if scope in BROADER and item["role_in_feature"] == "primary":
                    errors.append(f"{row['feature_id']} uses a broad dataset as a primary measurement")

    geometries = {}
    if geo.get("type") != "FeatureCollection":
        errors.append("sites.geojson is not a FeatureCollection")
    for feature in geo.get("features", []):
        site_id = feature.get("properties", {}).get("site_id")
        coordinates = feature.get("geometry", {}).get("coordinates")
        if feature.get("geometry", {}).get("type") != "Point" or len(coordinates) != 2:
            errors.append(f"{site_id} geometry is not a point")
            continue
        if site_id in geometries:
            errors.append(f"duplicate geometry for {site_id}")
        geometries[site_id] = coordinates

    for row in sites:
        site_id = row["site_id"]
        if row["coordinate_status"] == "unlocated":
            if site_id in geometries:
                errors.append(f"unlocated {site_id} has a geometry")
            continue
        if site_id not in geometries:
            errors.append(f"{site_id} is located but has no geometry")
            continue
        longitude, latitude = geometries[site_id]
        if abs(float(row["longitude"]) - longitude) > 1e-6 or abs(float(row["latitude"]) - latitude) > 1e-6:
            errors.append(f"{site_id} geometry does not match the table")

    return errors


def main():
    errors = validate()
    if errors:
        print(f"{len(errors)} error(s):")
        for error in errors:
            print(f"- {error}")
        return 1
    sites = read_csv("sites.csv")
    processes = read_csv("processes.csv")
    features = read_csv("features.csv")
    datasets = read_csv("datasets.csv")
    points = sum(1 for row in sites if row["coordinate_status"] != "unlocated")
    print(
        "phase 1 catalog ok: "
        f"{len(sites)} sites, {points} points, "
        f"{len(processes)} processes, {len(features)} features, {len(datasets)} datasets"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
