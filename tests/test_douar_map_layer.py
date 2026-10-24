import gzip
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "build-douar-map-layer.py"
SPEC = importlib.util.spec_from_file_location("build_douar_map_layer", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class DouarMapLayerTests(unittest.TestCase):
    def test_builds_national_index_and_never_invents_missing_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            output_dir = root / "output"
            source_dir.mkdir()
            rows = []
            for number in range(1, 13):
                region = f"{number:02d}"
                rows.append({
                    "code": f"{number:02d}00101010001",
                    "communeCode": f"{region}.001.01.01",
                    "fraction": f"{number:02d}0010101",
                    "name": {"ar": f"دوار {region}"},
                    "type": "grouped",
                    "households": 12 + number,
                    "population": 50 + number,
                    "topics": None if number == 1 else {"age": {"under15": 20}},
                })
                (source_dir / f"{region}.json").write_text(
                    json.dumps([rows[-1]], ensure_ascii=False), encoding="utf-8"
                )
            places_path = root / "places.json"
            places_path.write_text(json.dumps({
                "places": [[rows[0]["code"], "Douar Atlas", "geonames", 33.2, -6.8, "12345", "name"]]
            }), encoding="utf-8")
            index_path = root / "index.json"
            report_path = root / "coverage.md"

            index = MODULE.build_dataset(
                source_dir, places_path, output_dir, index_path, report_path,
                source_commit="123456789abcdef",
                min_features=12,
                min_points=1,
            )

            self.assertEqual(index["features"], 12)
            self.assertEqual(index["locatedPoints"], 1)
            self.assertEqual(index["unlocatedFeatures"], 11)
            self.assertEqual(index["communes"], 12)
            self.assertEqual(sum(row["count"] for row in index["regions"]), 12)
            self.assertEqual(sum(row["pointCount"] for row in index["regions"]), 1)
            self.assertEqual(index["pointSources"]["geonames"]["count"], 1)
            self.assertIn("not the limit", index["limitation"])

            with gzip.open(output_dir / "01.geojson.gz", "rt", encoding="utf-8") as stream:
                first = json.load(stream)["features"][0]
            self.assertEqual(first["geometry"], {"type": "Point", "coordinates": [-6.8, 33.2]})
            self.assertEqual(first["properties"]["name_fr"], "Douar Atlas")
            self.assertEqual(first["properties"]["population"], 51)
            self.assertEqual(first["properties"]["geometryStatus"], "mapped_point")
            self.assertEqual(first["properties"]["locationSource"], "geonames")
            self.assertEqual(first["properties"]["sourceLicense"], "CC BY 4.0")

            with gzip.open(output_dir / "02.geojson.gz", "rt", encoding="utf-8") as stream:
                second = json.load(stream)["features"][0]
            self.assertIsNone(second["geometry"])
            self.assertEqual(second["properties"]["geometryStatus"], "unlocated")
            self.assertEqual(second["properties"]["name_ar"], "دوار 02")
            self.assertEqual(second["properties"]["population"], 52)
            self.assertEqual(second["properties"]["locationSource"], "")
            self.assertIn("geometry=null", report_path.read_text(encoding="utf-8"))

    def test_rejects_duplicate_or_invalid_place_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "places.json"
            path.write_text(json.dumps({
                "places": [
                    ["0100101010001", "A", "osm", 33.0, -6.0, "n1", "name"],
                    ["0100101010001", "B", "osm", 33.1, -6.1, "n2", "name"],
                ]
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate place match"):
                MODULE.load_places(path)

            path.write_text(json.dumps({
                "places": [["0100101010001", "A", "osm", 133.0, -6.0, "n1", "name"]]
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid matching rows"):
                MODULE.load_places(path)

    def test_rejects_place_code_not_present_in_hcp_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            source_dir.mkdir()
            for number in range(1, 13):
                region = f"{number:02d}"
                row = {
                    "code": f"{number:02d}00101010001",
                    "communeCode": f"{region}.001.01.01",
                    "fraction": f"{number:02d}0010101",
                    "name": {"ar": "دوار"},
                    "type": "grouped",
                    "households": 2,
                    "population": 6,
                    "topics": None,
                }
                (source_dir / f"{region}.json").write_text(json.dumps([row]), encoding="utf-8")
            places_path = root / "places.json"
            places_path.write_text(json.dumps({
                "places": [["9999999999999", "Missing", "osm", 33.0, -6.0, "n1", "name"]]
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown HCP douar codes"):
                MODULE.build_dataset(
                    source_dir, places_path, root / "out", root / "index.json", root / "report.md",
                    source_commit="123456789abcdef",
                    min_features=1,
                    min_points=1,
                )


if __name__ == "__main__":
    unittest.main()
