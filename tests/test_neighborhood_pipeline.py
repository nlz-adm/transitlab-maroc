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


if __name__ == "__main__":
    unittest.main()
