import importlib.util
import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "earth_data", Path(__file__).parents[1] / "scripts" / "earth_data.py"
)
earth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(earth)


class EarthDownloadTests(unittest.TestCase):
    def test_missing_month_is_incomplete_and_pages_are_read(self):
        entries = [{"id": f"G{month}", "time_start": f"2025-{month:02d}-01T00:00:00Z",
                    "links": [{"rel": "http://esipfed.org/ns/fedsearch/1.1/data#",
                               "href": f"https://example.org/data{month}.hdf"}]}
                   for month in range(1, 12)]
        pages = [json.dumps({"feed": {"entry": entries}}).encode(),
                 json.dumps({"feed": {"entry": []}}).encode()]
        with patch.object(earth, "request_bytes", side_effect=pages) as request:
            assets, coverage = earth.cmr_assets("snow", 2025, earth.DEFAULT_BBOX)
        self.assertEqual(len(assets), 11)
        self.assertEqual(coverage["missing_months"], ["2025-12"])
        self.assertFalse(coverage["complete"])
        self.assertEqual(request.call_count, 2)
        self.assertIn("version=61&", request.call_args_list[0].args[0])

    def test_partial_discovery_preserves_other_datasets(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            manifest = Path(folder) / "inventory.json"
            previous = {"year": 2025, "permafrost_year": 2023,
                        "download_bbox": list(earth.DEFAULT_BBOX),
                        "datasets": {"terrain": {"complete": True}, "snow": {"complete": False}},
                        "assets": [{"dataset": "terrain", "filename": "saved.tif"}]}
            earth.write_json(manifest, previous)
            args = argparse.Namespace(year=2025, permafrost_year=2023,
                                      bbox=earth.DEFAULT_BBOX, datasets=["snow"], manifest=manifest)
            with patch.object(earth, "cmr_assets", return_value=(
                    [{"dataset": "snow", "filename": "snow.hdf"}], {"complete": True})):
                self.assertEqual(earth.discover(args), 0)
            updated = json.loads(manifest.read_text())
            self.assertEqual(len(updated["assets"]), 2)
            self.assertIn("terrain", updated["datasets"])
            args.year = 2024
            before = manifest.read_bytes()
            with self.assertRaises(ValueError):
                earth.discover(args)
            self.assertEqual(manifest.read_bytes(), before)

    def test_failed_download_preserves_previous_file(self):
        class FakeNASA:
            def download(self, urls, local_path, **kwargs):
                path = Path(local_path) / "test.nc"
                path.write_bytes(b"<html>Login</html>")
                return [path]

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            destination = Path(folder)
            target = destination / "snow" / "test.nc"
            target.parent.mkdir()
            target.write_bytes(b"CDF\x01original")
            asset = {"dataset": "snow", "filename": "test.nc",
                     "url": "https://example.org/test.nc", "authentication": "earthdata"}
            with self.assertRaises(ValueError):
                earth.download_one(asset, destination, FakeNASA())
            self.assertEqual(target.read_bytes(), b"CDF\x01original")
            self.assertEqual(list(target.parent.glob("staging-*")), [])

    def test_verified_file_is_reused_without_network(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            target = Path(folder) / "snow" / "test.nc"
            target.parent.mkdir()
            target.write_bytes(b"CDF\x01verified")
            asset = {"dataset": "snow", "filename": "test.nc",
                     "url": "https://example.org/test.nc", "authentication": "none"}
            earth.write_json(target.with_suffix(".nc.json"),
                             {"url": asset["url"], "sha256": earth.digest(target)})
            with patch.object(earth.urllib.request, "urlopen", side_effect=AssertionError):
                self.assertEqual(earth.download_one(asset, Path(folder)), target)

    def test_rejects_path_traversal(self):
        for filename in ("../secret", "C:\\secret", "..", ""):
            with self.assertRaises(ValueError):
                earth.safe_name(filename)

    def test_download_writes_provenance_and_verified_data(self):
        class FakeNASA:
            def download(self, urls, local_path, **kwargs):
                path = Path(local_path) / "test.nc"
                path.write_bytes(b"CDF\x01sample")
                return [path]

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            asset = {"dataset": "snow", "filename": "test.nc", "year": 2025,
                     "url": "https://example.org/test.nc", "authentication": "earthdata"}
            path = earth.download_one(asset, Path(folder), FakeNASA())
            receipt = json.loads(path.with_suffix(".nc.json").read_text())
            self.assertEqual(receipt["sha256"], earth.digest(path))
            self.assertEqual(receipt["year"], 2025)

    def test_dem_intersection_excludes_outside_tiles(self):
        keys = ["Copernicus_DSM_COG_30_N60_00_W150_00_DEM.tif",
                "Copernicus_DSM_COG_30_N80_00_W150_00_DEM.tif",
                "Copernicus_DSM_COG_30_N60_00_E050_00_DEM.tif"]
        xml = ('<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
               + ''.join(f'<Contents><Key>{k}</Key><Size>100</Size></Contents>' for k in keys)
               + '</ListBucketResult>')
        with patch.object(earth, "request_bytes", return_value=xml.encode()):
            assets, coverage = earth.terrain_assets(earth.DEFAULT_BBOX)
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["filename"], keys[0])
        self.assertTrue(coverage["complete"])


if __name__ == "__main__":
    unittest.main()
