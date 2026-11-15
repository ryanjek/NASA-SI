import math
from pathlib import Path

import pytest

from nasa_si.benchmarks import (
    DEFAULT_PATH,
    cell_table,
    for_body,
    load_benchmarks,
    overlapping_pairs,
)
from nasa_si.equal_area import EqualAreaGrid

EARTH = EqualAreaGrid.for_planet("earth")
HEADER = (
    "id,name,lon,lat,radius_km,body,role,category,named_in_challenge,why_used,evidence,"
    "rationale_source,coords_source,coord_confidence,verified,notes\n"
)
GOOD_ROW = "a,Site A,10,20,15,mars,canonical,hyperarid,true,why,secondary,http://x,http://y,high,false,n\n"


def write(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "b.csv"
    path.write_text(HEADER + "".join(rows), encoding="utf-8")
    return path


# ------------------------------------------------------------------ the shipped file
def test_shipped_file_loads_and_is_consistent():
    benchmarks = load_benchmarks(DEFAULT_PATH)
    assert len(benchmarks) == 11
    assert len({b.id for b in benchmarks}) == 11
    roles = [b.role for b in benchmarks]
    assert roles.count("canonical") == 6 and roles.count("hypothesis") == 5


def test_canonical_sites_are_exactly_those_named_in_the_challenge():
    benchmarks = load_benchmarks()
    assert {b.id for b in benchmarks if b.named_in_challenge} == {b.id for b in benchmarks if b.role == "canonical"}
    for expected in ("dry_valleys", "yungay", "askja", "hi_seas", "haughton", "death_valley"):
        assert expected in {b.id for b in benchmarks}


def test_a_verified_row_must_be_traceable():
    """`verified: true` is a claim. It needs web sources, usable coordinates and no guesswork."""
    for b in load_benchmarks():
        if not b.verified:
            continue
        assert b.rationale_source.startswith("http"), b.id
        assert b.coords_source.startswith("http"), b.id
        assert b.coord_confidence != "low", b.id
        assert "memory" not in b.coords_source.lower(), b.id


def test_report_only_claims_are_never_canonical():
    """Claims that rest only on the unsourced report stay hypotheses."""
    for b in load_benchmarks():
        if b.evidence == "report_only":
            assert b.role == "hypothesis", b.id
    by_id = {b.id: b for b in load_benchmarks()}
    assert by_id["khaybar"].evidence == "report_only"
    assert by_id["haughton"].coord_confidence == "high"


def test_mars_validation_set_excludes_lunar_sites():
    mars = {b.id for b in for_body(load_benchmarks(), "mars")}
    assert "haughton" in mars and "askja" in mars  # 'both' counts
    assert "craters_of_the_moon" not in mars and "khaybar" not in mars


def test_each_site_covers_about_the_expected_number_of_cells():
    for b in load_benchmarks():
        n = b.cells(EARTH).size
        expected = math.pi * b.radius_km**2 / EARTH.cell_area_km2
        assert n >= 1
        assert abs(n - expected) <= max(3.0, 0.3 * expected), (b.id, n, expected)


def test_sites_do_not_share_cells():
    assert overlapping_pairs(cell_table(load_benchmarks(), EARTH)) == []


# ------------------------------------------------------------------ validation of bad files
@pytest.mark.parametrize(
    "row, message",
    [
        ("a,A,10,95,15,mars,canonical,x,true,w,secondary,s,c,high,false,n\n", "lat"),
        ("a,A,200,20,15,mars,canonical,x,true,w,secondary,s,c,high,false,n\n", "lon"),
        ("a,A,10,20,0.5,mars,canonical,x,true,w,secondary,s,c,high,false,n\n", "radius_km"),
        ("a,A,10,20,15,venus,canonical,x,true,w,secondary,s,c,high,false,n\n", "body"),
        ("a,A,10,20,15,mars,maybe,x,true,w,secondary,s,c,high,false,n\n", "role"),
        ("a,A,10,20,15,mars,canonical,x,true,w,guess,s,c,high,false,n\n", "evidence"),
        ("a,A,10,20,15,mars,canonical,x,true,w,secondary,s,c,sure,false,n\n", "coord_confidence"),
        ("a,A,10,20,15,mars,canonical,x,yes,w,secondary,s,c,high,false,n\n", "named_in_challenge"),
        ("a,A,10,20,15,mars,canonical,x,true,w,secondary,s,c,high,maybe,n\n", "verified"),
        ("a,A,ten,20,15,mars,canonical,x,true,w,secondary,s,c,high,false,n\n", "lon"),
        ("a,,10,20,15,mars,canonical,x,true,w,secondary,s,c,high,false,n\n", "name"),
        ("a,A,10,20,15,mars,canonical,x,true,,secondary,s,c,high,false,n\n", "why_used"),
    ],
)
def test_invalid_rows_are_rejected_with_the_field_named(tmp_path, row, message):
    with pytest.raises(ValueError, match=message):
        load_benchmarks(write(tmp_path, row))


def test_duplicate_ids_and_missing_columns_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="duplicate"):
        load_benchmarks(write(tmp_path, GOOD_ROW, GOOD_ROW))
    bad = tmp_path / "c.csv"
    bad.write_text("id,name\na,A\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing columns"):
        load_benchmarks(bad)


def test_a_valid_custom_file_round_trips(tmp_path):
    (b,) = load_benchmarks(write(tmp_path, GOOD_ROW))
    assert (b.id, b.lon, b.lat, b.radius_km, b.verified) == ("a", 10.0, 20.0, 15.0, False)
    assert b.cells(EARTH).size >= 1


def test_for_body_rejects_unknown_body():
    with pytest.raises(ValueError):
        for_body(load_benchmarks(), "venus")
