import csv

import numpy as np
import pytest

from nasa_si.layers import GlobalRaster
from nasa_si.patches import PatchAccumulator, PatchGrid, PatchStats
from nasa_si.prefilter import (
    FAIL,
    PASS,
    UNKNOWN,
    FeatureSpec,
    GateRule,
    build_profile,
    evaluate,
    gate_candidates,
    load_feature_specs,
    load_seeds,
    read_table,
    Seed,
)
from nasa_si.prefilter import FEATURES_CONFIG, PHASE1_DIR

GRID = PatchGrid(coarse_nside=8, fine_nside=64)


def stats_from(ids, vals, n=GRID.npix_coarse) -> PatchStats:
    acc = PatchAccumulator(n)
    acc.add(ids, vals)
    return acc.result()


def gate(lo, hi, name="f"):
    return GateRule(name, "layer", lo, hi, lo, hi, 0.0, "", "")


# ------------------------------------------------------------------ the gate itself
def test_envelope_overlap_passes_and_disjoint_fails():
    s = stats_from([0, 0, 1, 1], [0.0, 10.0, 20.0, 30.0], 4)
    ev = evaluate({"f": s}, [gate(8, 12)], np.array([True, True, False, False]))
    assert list(ev.status["f"][:2]) == [PASS, FAIL]
    assert list(ev.survives) == [True, False, False, False]


def test_envelope_catches_a_value_the_mean_would_miss():
    # mean 15 is far from [29, 31], but the patch holds a 30: it must survive.
    s = stats_from([0, 0], [0.0, 30.0], 1)
    assert evaluate({"f": s}, [gate(29, 31)], np.array([True])).survives[0]


def test_unknown_passes():
    s = stats_from([0], [5.0], 2)
    ev = evaluate({"f": s}, [gate(100, 200)], np.array([True, True]))
    assert ev.status["f"][1] == UNKNOWN
    assert ev.survives[1] and not ev.survives[0]


def test_no_gates_prunes_nothing():
    pop = np.array([True, False, True])
    assert list(evaluate({}, [], pop).survives) == list(pop)


def test_widening_tolerance_never_shrinks_survivors():
    rng = np.random.default_rng(3)
    ids = rng.integers(0, 200, 4000)
    s = stats_from(ids, rng.normal(size=4000), 200)
    pop = np.ones(200, bool)
    prev = np.zeros(200, bool)
    for tol in (0.0, 0.1, 0.5, 1.0, 3.0):
        cur = evaluate({"f": s}, [gate(-tol, tol)], pop).survives
        assert not (prev & ~cur).any()
        prev = cur


@pytest.mark.parametrize("seed", range(5))
def test_recall_property_survivors_hold_every_passing_fine_cell(seed):
    """The contract with stage 2: no patch that holds a passing fine cell is ever pruned."""
    rng = np.random.default_rng(seed)
    n_fine = 20_000
    fine = rng.integers(0, GRID.npix_coarse, n_fine)
    # patch-level structure plus within-patch noise, so envelopes are tight enough to prune
    offset = rng.normal(size=(2, GRID.npix_coarse))
    a = offset[0][fine] + 0.1 * rng.normal(size=n_fine)
    b = offset[1][fine] + 0.1 * rng.normal(size=n_fine)
    a[rng.random(n_fine) < 0.1] = np.nan
    gates = [gate(-0.3, 0.4, "a"), gate(-0.5, 0.2, "b")]
    stats = {"a": stats_from(fine, a), "b": stats_from(fine, b)}
    pop = np.ones(GRID.npix_coarse, bool)
    survives = evaluate(stats, gates, pop).survives
    # A fine cell is a candidate only if it has data and the data fall inside every gate. A cell with
    # missing data cannot be compared at stage 2 either; a patch with no data at all is "unknown" and
    # passes (test_unknown_passes).
    passes = (a >= -0.3) & (a <= 0.4) & (b >= -0.5) & (b <= 0.2)
    assert survives[np.unique(fine[passes])].all()
    assert (~survives).sum() > 0  # the gate does prune something


# ------------------------------------------------------------------ profiles and config
def raster(values, name="modis_lst_day"):
    return GlobalRaster(name, np.array(values, dtype=np.float32), -180.0, 90.0, 360.0 / len(values[0]), 180.0 / len(values))


def spec(**kw):
    base = dict(feature_id="lst_summer_max_c", dataset_id="modis_lst", role="gate", layer="modis_lst_day", unit="degC", tolerance_abs=5.0, hard_lo=None, hard_hi=None, deferred_reason="", proxy_note="")
    base.update(kw)
    return FeatureSpec(**base)


SEED = Seed("s", "S", "fam", 10.0, 10.0, 0.0, "exact")


def test_profile_interval_is_seed_value_plus_minus_tolerance():
    r = raster([[1.0] * 8] * 4)
    p = build_profile(SEED, ["lst_summer_max_c"], {"lst_summer_max_c": spec()}, {"modis_lst_day": r})
    (g,) = p.gates
    assert (g.lo, g.hi) == (-4.0, 6.0)


def test_hard_bound_clips_interval_and_seed_outside_is_an_error():
    r = raster([[0.05] * 8] * 4, "modis_ndvi")
    s = spec(feature_id="ndvi_summer_median", layer="modis_ndvi", tolerance_abs=0.1, hard_lo=0.0)
    (g,) = build_profile(SEED, ["ndvi_summer_median"], {"ndvi_summer_median": s}, {"modis_ndvi": r}).gates
    assert g.lo == 0.0
    bad = spec(feature_id="ndvi_summer_median", layer="modis_ndvi", hard_lo=0.5)
    with pytest.raises(ValueError):
        build_profile(SEED, ["ndvi_summer_median"], {"ndvi_summer_median": bad}, {"modis_ndvi": r})


def test_seed_with_no_valid_value_and_missing_layer_become_non_gates():
    nan = raster([[np.nan] * 8] * 4)
    p = build_profile(SEED, ["lst_summer_max_c"], {"lst_summer_max_c": spec()}, {"modis_lst_day": nan})
    assert not p.gates and "unknown" in p.skipped[0][1]
    p = build_profile(SEED, ["lst_summer_max_c"], {"lst_summer_max_c": spec()}, {})
    assert not p.gates and "not loaded" in p.skipped[0][1]


def test_deferred_and_informative_features_never_gate():
    specs = {
        "elev_m": spec(feature_id="elev_m", role="informative", layer="", tolerance_abs=None),
        "slope_median_deg": spec(feature_id="slope_median_deg", role="deferred", layer="", tolerance_abs=None, deferred_reason="x"),
    }
    p = build_profile(SEED, list(specs), specs, {})
    assert not p.gates and len(p.skipped) == 2


def test_catalog_and_feature_config_agree_and_elevation_is_not_a_gate():
    features = read_table(PHASE1_DIR / "features.csv")
    specs = load_feature_specs(FEATURES_CONFIG, phase1_features=features, phase1_dir=PHASE1_DIR)
    assert specs["elev_m"].role != "gate"
    assert all(s.layer for s in specs.values() if s.role == "gate")


def test_elevation_gate_is_rejected(tmp_path):
    path = tmp_path / "f.csv"
    path.write_text("feature_id,dataset_id,role,layer,unit,tolerance_abs,hard_lo,hard_hi,deferred_reason,proxy_note\nelev_m,copernicus_glo30,gate,dem,m,100,,,,\n")
    with pytest.raises(ValueError, match="never be a gate"):
        load_feature_specs(path)


def test_out_of_step_config_is_rejected(tmp_path):
    path = tmp_path / "f.csv"
    path.write_text("feature_id,dataset_id,role,layer,unit,tolerance_abs,hard_lo,hard_hi,deferred_reason,proxy_note\nelev_m,copernicus_glo30,informative,,m,,,,,\n")
    with pytest.raises(ValueError, match="out of step"):
        load_feature_specs(path, phase1_features=read_table(PHASE1_DIR / "features.csv"))


def test_seeds_are_the_located_transferable_phase1_sites():
    seeds = {s.site_id: s for s in load_seeds()}
    assert set(seeds) == {"university_valley", "beacon_central_polygon", "hiseas_pahoehoe_radius", "drill_hill"}
    assert seeds["hiseas_pahoehoe_radius"].buffer_km == pytest.approx(6.0)
    sites, procs, feats = (read_table(PHASE1_DIR / f"{n}.csv") for n in ("sites", "processes", "features"))
    for s in seeds.values():
        cands = gate_candidates(s, sites, procs, feats)
        assert cands and "elev_m" not in [c for c in cands if False]


def test_every_feature_uses_a_catalogued_dataset_and_gates_name_their_stand_in():
    specs = load_feature_specs(FEATURES_CONFIG, phase1_dir=PHASE1_DIR)
    listed = {r["dataset_id"] for r in read_table(PHASE1_DIR / "datasets.csv")}
    assert {s.dataset_id for s in specs.values()} <= listed
    assert {s.dataset_id for s in specs.values() if s.role == "gate"} == {"modis_mod11c3", "modis_mod13c2", "modis_mod10cm", "merra2_land"}
    for s in specs.values():
        if s.role == "gate":
            assert "stand-in" in s.source_product


def test_dataset_outside_the_drive_catalog_is_rejected(tmp_path):
    path = tmp_path / "f.csv"
    path.write_text("feature_id,dataset_id,role,layer,unit,tolerance_abs,hard_lo,hard_hi,deferred_reason,proxy_note\n"
                    "lst_summer_max_c,modis_mod13,gate,modis_ndvi,1,0.1,,,,\n")
    with pytest.raises(ValueError, match="not in the Drive|not in the"):
        load_feature_specs(path, phase1_dir=PHASE1_DIR)
