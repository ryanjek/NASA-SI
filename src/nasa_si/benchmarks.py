"""Known planetary-analog sites ("benchmarks") and the equal-area grid cells they cover.

The benchmarks are used three ways: as positive controls (do known analogs rank high for the
Mars targets they were used for?), to calibrate what "analog-like" looks like in feature space,
and as anchors for clustering Earth cells. They are NOT training labels.

``config/benchmarks.csv`` is the single source of truth. Every row records how well its claim
is supported (``evidence``), how sure the coordinates are (``coord_confidence``) and whether a
person has checked it (``verified``). Nothing in the file should be quoted to judges while its
``verified`` flag is false.

Run ``python -m nasa_si.benchmarks`` for a one-screen summary.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from .equal_area import EqualAreaGrid

IntArray = NDArray[np.int64]

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "benchmarks.csv"

BODIES = ("mars", "moon", "both")
ROLES = ("canonical", "hypothesis")  # canonical = named in the challenge; hypothesis = proposed, needs testing
EVIDENCE = ("nasa_or_agency", "peer_reviewed", "secondary", "report_only")
CONFIDENCE = ("high", "medium", "low")

Body = Literal["mars", "moon", "both"]

_REQUIRED = (
    "id", "name", "lon", "lat", "radius_km", "body", "role", "category", "named_in_challenge",
    "why_used", "evidence", "rationale_source", "coords_source", "coord_confidence", "verified", "notes",
)


@dataclass(frozen=True)
class Benchmark:
    id: str
    name: str
    lon: float  # degrees east, -180..180
    lat: float
    radius_km: float
    body: str
    role: str
    category: str
    named_in_challenge: bool
    why_used: str
    evidence: str
    rationale_source: str
    coords_source: str
    coord_confidence: str
    verified: bool
    notes: str

    def cells(self, grid: EqualAreaGrid) -> IntArray:
        """Grid cells whose centre lies within ``radius_km`` of the site."""
        return grid.cells_within(self.lon, self.lat, self.radius_km)


def _flag(row_id: str, field: str, text: str) -> bool:
    value = text.strip().lower()
    if value not in ("true", "false"):
        raise ValueError(f"{row_id}: {field} must be true or false, got {text!r}")
    return value == "true"


def _number(row_id: str, field: str, text: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"{row_id}: {field} must be a number, got {text!r}") from None


def _choice(row_id: str, field: str, text: str, allowed: tuple[str, ...]) -> str:
    value = text.strip()
    if value not in allowed:
        raise ValueError(f"{row_id}: {field} must be one of {allowed}, got {text!r}")
    return value


def load_benchmarks(path: Path | str = DEFAULT_PATH) -> list[Benchmark]:
    """Read and strictly validate the benchmark file."""
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in _REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing columns {missing}")
        rows = list(reader)

    out: list[Benchmark] = []
    seen: set[str] = set()
    for line, row in enumerate(rows, start=2):
        rid = (row["id"] or "").strip()
        if not rid:
            raise ValueError(f"{path}:{line}: empty id")
        if rid in seen:
            raise ValueError(f"{rid}: duplicate id")
        seen.add(rid)

        lon = _number(rid, "lon", row["lon"])
        lat = _number(rid, "lat", row["lat"])
        radius = _number(rid, "radius_km", row["radius_km"])
        if not -180.0 <= lon <= 180.0:
            raise ValueError(f"{rid}: lon {lon} outside -180..180")
        if not -90.0 <= lat <= 90.0:
            raise ValueError(f"{rid}: lat {lat} outside -90..90")
        if not 1.0 <= radius <= 100.0:
            raise ValueError(f"{rid}: radius_km {radius} outside 1..100")
        for field in ("name", "why_used", "rationale_source", "coords_source"):
            if not (row[field] or "").strip():
                raise ValueError(f"{rid}: {field} is empty")

        out.append(
            Benchmark(
                id=rid,
                name=row["name"].strip(),
                lon=lon,
                lat=lat,
                radius_km=radius,
                body=_choice(rid, "body", row["body"], BODIES),
                role=_choice(rid, "role", row["role"], ROLES),
                category=row["category"].strip(),
                named_in_challenge=_flag(rid, "named_in_challenge", row["named_in_challenge"]),
                why_used=row["why_used"].strip(),
                evidence=_choice(rid, "evidence", row["evidence"], EVIDENCE),
                rationale_source=row["rationale_source"].strip(),
                coords_source=row["coords_source"].strip(),
                coord_confidence=_choice(rid, "coord_confidence", row["coord_confidence"], CONFIDENCE),
                verified=_flag(rid, "verified", row["verified"]),
                notes=row["notes"].strip(),
            )
        )
    return out


def for_body(benchmarks: list[Benchmark], body: str) -> list[Benchmark]:
    """Benchmarks relevant to a target body ('both' sites count for either)."""
    if body not in ("mars", "moon"):
        raise ValueError("body must be 'mars' or 'moon'")
    return [b for b in benchmarks if b.body in (body, "both")]


def cell_table(benchmarks: list[Benchmark], grid: EqualAreaGrid) -> dict[str, IntArray]:
    """Cells covered by each benchmark."""
    return {b.id: b.cells(grid) for b in benchmarks}


def overlapping_pairs(table: dict[str, IntArray]) -> list[tuple[str, str, int]]:
    """Benchmark pairs that share cells (their features would not be independent)."""
    ids = list(table)
    pairs = []
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            shared = np.intersect1d(table[a], table[b]).size
            if shared:
                pairs.append((a, b, int(shared)))
    return pairs


def main() -> None:  # pragma: no cover - thin CLI
    earth = EqualAreaGrid.for_planet("earth")
    benchmarks = load_benchmarks()
    table = cell_table(benchmarks, earth)
    print(f"{len(benchmarks)} benchmarks on the Earth grid (nside {earth.nside}, {earth.cell_area_km2:.1f} km2 cells)\n")
    print(f"{'id':<20}{'role':<11}{'body':<6}{'cells':>6}  {'evidence':<15}{'coords':<7}{'verified'}")
    for b in benchmarks:
        print(f"{b.id:<20}{b.role:<11}{b.body:<6}{table[b.id].size:>6}  {b.evidence:<15}{b.coord_confidence:<7}{b.verified}")
    unverified = sum(not b.verified for b in benchmarks)
    low = sum(b.coord_confidence == "low" for b in benchmarks)
    print(f"\n{unverified}/{len(benchmarks)} not human-verified; {low} with low-confidence coordinates.")
    overlaps = overlapping_pairs(table)
    if overlaps:
        print("Overlapping sites:", overlaps)


if __name__ == "__main__":  # pragma: no cover
    main()
