import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import box

ROOT = Path(__file__).resolve().parents[1]
PIPELINE_PATH = ROOT / "tools" / "prepare-neighborhood-boundaries-multi-source.py"
CONVERTER_PATH = ROOT / "tools" / "convert-multi-source-to-site-format.py"


def load_pipeline_module():
    spec = importlib.util.spec_from_file_location("neighborhood_pipeline", PIPELINE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class NeighborhoodPipelineTests(unittest.TestCase):
    def test_voronoi_estimation_uses_shapely_2_extend_to_api(self):
        module = load_pipeline_module()
        commune = box(0, 0, 10, 10)
        seeds = [
            module.Seed(name="Nord", lon=1, lat=1, source="test"),
            module.Seed(name="Centre", lon=5, lat=5, source="test"),
            module.Seed(name="Sud", lon=9, lat=9, source="test"),
        ]

        polygons = module.estimate_boundaries(commune, seeds)

        self.assertEqual(len(polygons), 3)
        self.assertTrue(all(not polygon.is_empty for polygon in polygons))
        self.assertTrue(all(commune.covers(polygon) for polygon in polygons))
        self.assertAlmostEqual(sum(polygon.area for polygon in polygons), commune.area, places=4)

    def test_converter_refuses_empty_geojson_without_creating_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / "empty.geojson"
            output_dir = directory / "regions"
            index_path = directory / "index.json"
            source.write_text(json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(CONVERTER_PATH),
                    "--input", str(source),
                    "--output-dir", str(output_dir),
                    "--index", str(index_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("entrée GeoJSON vide", result.stdout + result.stderr)
            self.assertFalse(index_path.exists())


class PublishedDataIntegrityTests(unittest.TestCase):
    def test_neighborhood_index_matches_catalog_and_compressed_regions(self):
        import gzip

        index_path = ROOT / "assets" / "neighborhood-boundaries-index.json"
        catalog_path = ROOT / "assets" / "data-catalog.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        layer = catalog["layers"]["neighborhoodBoundaries"]
        regions = index["regions"]

        self.assertEqual(len(regions), 12, "Les 12 régions administratives doivent être présentes.")
        self.assertGreater(index["features"], 0)
        self.assertEqual(sum(region["count"] for region in regions), index["features"])
        self.assertEqual(sum(region["mapped"] for region in regions), index["mappedFeatures"])
        self.assertEqual(sum(region["estimated"] for region in regions), index["estimatedFeatures"])
        self.assertEqual(sum(region["bytesCompressed"] for region in regions), layer["bytesCompressed"])
        self.assertEqual(layer["features"], index["features"])
        self.assertEqual(layer["mappedFeatures"], index["mappedFeatures"])
        self.assertEqual(layer["estimatedFeatures"], index["estimatedFeatures"])
        self.assertEqual(layer["communesWithFeatures"], index["communesWithFeatures"])

        for region in regions:
            with self.subTest(region=region["code"]):
                path = ROOT / region["url"]
                self.assertTrue(path.is_file(), f"Fichier régional manquant : {path}")
                with gzip.open(path, "rt", encoding="utf-8") as stream:
                    geojson = json.load(stream)
                self.assertEqual(geojson.get("type"), "FeatureCollection")
                self.assertEqual(len(geojson.get("features", [])), region["count"])
                self.assertTrue(geojson.get("features"), "Une région publiée ne doit pas être vide.")

    def test_frontend_cache_busting_versions_are_synchronized(self):
        import re

        html = (ROOT / "carte-interactive-maroc.html").read_text(encoding="utf-8")
        boot = (ROOT / "assets" / "map-boot.js").read_text(encoding="utf-8")
        app = (ROOT / "assets" / "map-app.js").read_text(encoding="utf-8")
        service_worker = (ROOT / "sw.js").read_text(encoding="utf-8")
        catalog = json.loads((ROOT / "assets" / "data-catalog.json").read_text(encoding="utf-8"))
        expected = catalog["version"]

        self.assertRegex(expected, r"^maroc-v\d+-\d{8}$")
        self.assertIn(expected, html)
        self.assertIn(expected, boot)
        self.assertIn(expected, app)
        self.assertIn(expected, service_worker)
        self.assertIn(expected, service_worker.splitlines()[0])

    def test_pwa_manifest_icons_and_service_worker_precache_exist(self):
        import re

        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        for icon in manifest.get("icons", []):
            with self.subTest(icon=icon.get("src")):
                self.assertTrue((ROOT / icon["src"]).is_file(), f"Icône PWA manquante : {icon['src']}")

        worker = (ROOT / "sw.js").read_text(encoding="utf-8")
        start = worker.find("const STATIC_ASSETS = [")
        end = worker.find("];", start)
        self.assertGreaterEqual(start, 0, "Liste de précache du service worker introuvable.")
        self.assertGreater(end, start, "Liste de précache du service worker incomplète.")
        assets = re.findall(r"['\\"]([^'\\"]+)['\\"]", worker[start:end])
        for asset in assets:
            with self.subTest(asset=asset):
                if asset.startswith("./"):
                    asset = asset[2:]
                if asset:
                    self.assertTrue((ROOT / asset).is_file(), f"Ressource PWA en précache absente : {asset}")


if __name__ == "__main__":
    unittest.main()
