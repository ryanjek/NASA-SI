import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "earth_map.py"
spec = importlib.util.spec_from_file_location("earth_map", SCRIPT_PATH)
earth_map = importlib.util.module_from_spec(spec)
sys.modules["earth_map"] = earth_map
spec.loader.exec_module(earth_map)


class EarthMapUnitTests(unittest.TestCase):
    def test_level_surface_qualifies_as_flat(self):
        elev = np.full((120, 60), 450.0, dtype=np.float64)
        slope = earth_map.compute_tile_slope_degrees(
            elev,
            south_lat_deg=62.0,
            west_lon_deg=-148.0,
            north_row=np.full(60, 450.0),
            south_row=np.full(60, 450.0),
            west_col=np.full(120, 450.0),
            east_col=np.full(120, 450.0),
        )
        np.testing.assert_allclose(slope, 0.0, atol=1e-12)

        config = earth_map.EarthScreeningConfig()
        metrics = earth_map.score_cell_subpixels(
            elevation_m=elev.reshape(1, 1, -1),
            slope_deg=slope.reshape(1, 1, -1),
            permafrost_fraction=np.full((1, 1, elev.size), 0.8),
            land_mask=np.ones((1, 1, elev.size), dtype=bool),
            config=config,
        )
        self.assertAlmostEqual(float(metrics["flatness_fraction"][0, 0]), 1.0)
        self.assertTrue(bool(metrics["eligible"][0, 0]))
        self.assertAlmostEqual(float(metrics["score"][0, 0]), 0.9)

    def test_steep_terrain_does_not_qualify_as_flat(self):
        height, width = 120, 60
        dlat_deg = 1.0 / height
        row_lats = 63.0 - np.arange(height, dtype=np.float64) * dlat_deg
        _, dy_m = earth_map.wgs84_pixel_spacings_m(row_lats, dlon_deg=1.0 / width, dlat_deg=dlat_deg)
        # Construct a 15-degree north-south incline (> 5 deg threshold)
        tan_15 = np.tan(np.radians(15.0))
        y_dist = np.cumsum(np.BroadcastTo(dy_m, (height,)) if False else dy_m)
        elev = (y_dist[:, None] * tan_15) * np.ones((1, width))

        slope = earth_map.compute_tile_slope_degrees(
            elev,
            south_lat_deg=62.0,
            west_lon_deg=-148.0,
        )
        interior_slope = slope[1:-1, 1:-1]
        self.assertTrue(np.all(interior_slope > 14.0))

        config = earth_map.EarthScreeningConfig()
        metrics = earth_map.score_cell_subpixels(
            elevation_m=elev[1:-1, 1:-1].reshape(1, 1, -1),
            slope_deg=interior_slope.reshape(1, 1, -1),
            permafrost_fraction=np.full((1, 1, interior_slope.size), 1.0),
            land_mask=np.ones((1, 1, interior_slope.size), dtype=bool),
            config=config,
        )
        self.assertAlmostEqual(float(metrics["flatness_fraction"][0, 0]), 0.0)
        self.assertAlmostEqual(float(metrics["score"][0, 0]), 0.5)

    def test_tile_seams_do_not_create_artificial_slopes_and_unresolved_edges_are_invalid(self):
        height, width = 120, 60
        dlat = 1.0 / height
        dlon = 1.0 / width

        # Continuous planar ramp in latitude and longitude across two adjacent tiles:
        # Tile A: [62, 63] x [-148, -147], Tile B (North): [63, 64] x [-148, -147],
        # Tile C (East): [62, 63] x [-147, -146]
        def make_tile(south_lat: float, west_lon: float) -> np.ndarray:
            lats = (south_lat + 1.0) - np.arange(height, dtype=np.float64)[:, None] * dlat
            lons = west_lon + np.arange(width, dtype=np.float64)[None, :] * dlon
            # Gentle ramp (< 1 degree everywhere)
            return 500.0 + 100.0 * (lats - 62.0) + 50.0 * (lons - (-148.0))

        tile_center = make_tile(62.0, -148.0)
        tile_north = make_tile(63.0, -148.0)
        tile_south = make_tile(61.0, -148.0)
        tile_west = make_tile(62.0, -149.0)
        tile_east = make_tile(62.0, -147.0)

        # With neighbor halo supplied, edge slope matches interior slope smoothly with no seam spike
        slope_with_halo = earth_map.compute_tile_slope_degrees(
            tile_center,
            south_lat_deg=62.0,
            west_lon_deg=-148.0,
            north_row=tile_north[-1, :],
            south_row=tile_south[0, :],
            west_col=tile_west[:, -1],
            east_col=tile_east[:, 0],
        )
        self.assertTrue(np.all(np.isfinite(slope_with_halo)))
        # Top edge row (index 0) and second row (index 1) should differ by < 0.001 deg
        np.testing.assert_allclose(slope_with_halo[0, :], slope_with_halo[1, :], atol=1e-3)
        # Right edge col (index -1) and second-to-last col (index -2) should match identically
        np.testing.assert_allclose(slope_with_halo[:, -1], slope_with_halo[:, -2], atol=1e-6)

        # Without neighbor halo, unresolved outer edges must be marked NaN (invalid)
        slope_unresolved = earth_map.compute_tile_slope_degrees(
            tile_center,
            south_lat_deg=62.0,
            west_lon_deg=-148.0,
        )
        self.assertTrue(np.all(np.isnan(slope_unresolved[0, :])))
        self.assertTrue(np.all(np.isnan(slope_unresolved[-1, :])))
        self.assertTrue(np.all(np.isnan(slope_unresolved[:, 0])))
        self.assertTrue(np.all(np.isnan(slope_unresolved[:, -1])))
        self.assertTrue(np.all(np.isfinite(slope_unresolved[1:-1, 1:-1])))

    def test_valid_zero_permafrost_differs_from_missing_data(self):
        raw = np.array([[0, 50, 100, 255]], dtype=np.uint8)
        decoded = earth_map.decode_permafrost_fraction(raw)
        self.assertEqual(decoded[0, 0], 0.0)
        self.assertEqual(decoded[0, 1], 0.5)
        self.assertEqual(decoded[0, 2], 1.0)
        self.assertTrue(np.isnan(decoded[0, 3]))

        config = earth_map.EarthScreeningConfig()
        # Cell 0: 100% valid zero permafrost -> eligible=True, permafrost_fraction=0.0
        # Cell 1: 100% missing permafrost (255 -> NaN) -> input_coverage=0.0, eligible=False, score=NaN
        elev = np.full((2, 1, 10), 200.0)
        slope = np.zeros((2, 1, 10))
        pfr = np.array(
            [
                [np.zeros(10, dtype=np.float64)],
                [np.full(10, np.nan, dtype=np.float64)],
            ]
        )
        land = np.ones((2, 1, 10), dtype=bool)
        metrics = earth_map.score_cell_subpixels(
            elevation_m=elev,
            slope_deg=slope,
            permafrost_fraction=pfr,
            land_mask=land,
            config=config,
        )
        self.assertTrue(bool(metrics["eligible"][0, 0]))
        self.assertAlmostEqual(float(metrics["permafrost_fraction"][0, 0]), 0.0)
        self.assertAlmostEqual(float(metrics["score"][0, 0]), 0.5)

        self.assertFalse(bool(metrics["eligible"][1, 0]))
        self.assertAlmostEqual(float(metrics["input_coverage"][1, 0]), 0.0)
        self.assertTrue(np.isnan(metrics["score"][1, 0]))

    def test_incomplete_cells_rejected_under_coverage_gate(self):
        config = earth_map.EarthScreeningConfig(min_coverage=0.80)
        n = 100
        elev = np.full((2, 1, n), 300.0)
        slope = np.zeros((2, 1, n))
        land = np.ones((2, 1, n), dtype=bool)

        # Cell 0 has 79% valid permafrost (< 80% gate -> rejected)
        # Cell 1 has 80% valid permafrost (>= 80% gate -> eligible)
        pfr = np.full((2, 1, n), 1.0)
        pfr[0, 0, 79:] = np.nan
        pfr[1, 0, 80:] = np.nan

        metrics = earth_map.score_cell_subpixels(
            elevation_m=elev,
            slope_deg=slope,
            permafrost_fraction=pfr,
            land_mask=land,
            config=config,
        )
        self.assertAlmostEqual(float(metrics["input_coverage"][0, 0]), 0.79)
        self.assertFalse(bool(metrics["eligible"][0, 0]))
        self.assertEqual(metrics["gate_failure"][0, 0], "coverage<0.8")
        self.assertTrue(np.isnan(metrics["score"][0, 0]))

        self.assertAlmostEqual(float(metrics["input_coverage"][1, 0]), 0.80)
        self.assertTrue(bool(metrics["eligible"][1, 0]))
        self.assertEqual(metrics["gate_failure"][1, 0], "")
        self.assertAlmostEqual(float(metrics["score"][1, 0]), 1.0)

    def test_ignores_staging_directories_during_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_dir = root / "raw"
            (raw_dir / "terrain" / "staging-abc").mkdir(parents=True)
            (raw_dir / "permafrost").mkdir(parents=True)

            # Valid terrain tile and receipt
            tif_name = "Copernicus_DSM_COG_30_N62_00_W149_00_DEM.tif"
            tif_path = raw_dir / "terrain" / tif_name
            tif_bytes = b"II*\x00sample_tif"
            tif_path.write_bytes(tif_bytes)
            tif_hash = earth_map.file_sha256(tif_path)
            (raw_dir / "terrain" / (tif_name + ".json")).write_text(
                json.dumps(
                    {
                        "url": "https://example.org/" + tif_name,
                        "sha256": tif_hash,
                        "actual_bytes": len(tif_bytes),
                    }
                )
            )

            # Valid permafrost file and receipt
            nc_name = "ESACCI-PERMAFROST-2023.nc"
            nc_path = raw_dir / "permafrost" / nc_name
            nc_bytes = b"\x89HDF\r\n\x1a\nsample_nc"
            nc_path.write_bytes(nc_bytes)
            nc_hash = earth_map.file_sha256(nc_path)
            (raw_dir / "permafrost" / (nc_name + ".json")).write_text(
                json.dumps(
                    {
                        "url": "https://example.org/" + nc_name,
                        "sha256": nc_hash,
                        "actual_bytes": len(nc_bytes),
                    }
                )
            )

            inv_path = root / "inventory.json"
            inv_path.write_text(
                json.dumps(
                    {
                        "assets": [
                            {
                                "dataset": "terrain",
                                "filename": tif_name,
                                "url": "https://example.org/" + tif_name,
                            },
                            {
                                "dataset": "permafrost",
                                "filename": nc_name,
                                "url": "https://example.org/" + nc_name,
                            },
                        ]
                    }
                )
            )

            tiles, pf = earth_map.discover_verified_inputs(inv_path, raw_dir)
            self.assertIn((62, -149), tiles)
            self.assertEqual(pf.filename, nc_name)


if __name__ == "__main__":
    unittest.main()
