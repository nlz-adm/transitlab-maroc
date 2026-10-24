import gzip
import importlib.util
import json
import tempfile
import unittest
from collections import Counter, defaultdict
from pathlib import Path

from openpyxl import Workbook
from shapely.geometry import Point, box, mapping

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "build-postal-neighborhood-registry.py"
SPEC = importlib.util.spec_from_file_location("postal_neighborhood_registry", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PostalNeighborhoodRegistryTests(unittest.TestCase):
    def test_loads_city_neighborhood_and_postcode_after_title_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "postal.xlsx"
            book = Workbook()
            sheet = book.active
            sheet.append(["Codes postaux des quartiers au Maroc"])
            sheet.append(["Ville", "Quartier", "Nouveau code postal"])
            sheet.append(["RABAT", "QUARTIER HASSAN", 10020])
            sheet.append(["CASABLANCA", "HAY AL AMAL", "20250"])
            book.save(path)
            records = MODULE.load_postal_rows(path)

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["city"], "RABAT")
        self.assertEqual(records[0]["name"], "QUARTIER HASSAN")
        self.assertEqual(records[0]["postalCode"], "10020")
        self.assertEqual(records[0]["nameKey"], "hassan")
        self.assertEqual(records[1]["nameKey"], "amal")

    def test_geojsonseq_accepts_named_neighborhood_point_and_polygon(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "places.geojsonseq"
            features = [
                {"type": "Feature", "id": "node/1", "properties": {
                    "name": "Hay Al Amal", "place": "neighbourhood", "addr:city": "Casablanca",
                }, "geometry": mapping(Point(-7.6, 33.6))},
                {"type": "Feature", "id": "way/2", "properties": {
                    "name": "Hassan", "place": "suburb", "addr:city": "Rabat",
                }, "geometry": mapping(box(-6.85, 34.0, -6.84, 34.01))},
                {"type": "Feature", "id": "node/3", "properties": {
                    "name": "Not a neighborhood", "amenity": "school",
                }, "geometry": mapping(Point(-7.6, 33.6))},
            ]
            path.write_text("\n".join(json.dumps(f) for f in features), encoding="utf-8")
            candidates = MODULE.read_geojsonseq(path)

        self.assertEqual(len(candidates), 2)
        self.assertEqual({item["name"] for item in candidates}, {"Hay Al Amal", "Hassan"})

    def test_registry_prefers_real_polygon_and_does_not_guess_ambiguous_city(self):
        records = [
            {"sourceId": "1", "sourceRow": "sheet:2", "city": "Rabat", "cityKey": "rabat",
             "name": "QUARTIER HASSAN", "nameKey": "hassan", "postalCode": "10020"},
            {"sourceId": "2", "sourceRow": "sheet:3", "city": "Marrakech", "cityKey": "marrakech",
             "name": "Hay Al Amal", "nameKey": "amal", "postalCode": "40000"},
            {"sourceId": "3", "sourceRow": "sheet:4", "city": "Agadir", "cityKey": "agadir",
             "name": "Al Waha", "nameKey": "waha", "postalCode": "80000"},
        ]
        candidates = [
            {"name": "Hassan", "nameKey": "hassan", "city": "Rabat", "cityKey": "rabat",
             "sourceId": "way/2", "geometry": box(-6.85, 34.0, -6.84, 34.01)},
            {"name": "Hassan", "nameKey": "hassan", "city": "Rabat", "cityKey": "rabat",
             "sourceId": "node/1", "geometry": Point(-6.845, 34.005)},
            {"name": "Hay Al Amal", "nameKey": "amal", "city": "", "cityKey": "",
             "sourceId": "node/3", "geometry": Point(-7.6, 31.6)},
            {"name": "Hay Al Amal", "nameKey": "amal", "city": "Safi", "cityKey": "safi",
             "sourceId": "node/6", "geometry": Point(-9.2, 32.3)},
            {"name": "Al Waha", "nameKey": "waha", "city": "", "cityKey": "",
             "sourceId": "node/4", "geometry": Point(-9.6, 30.4)},
            {"name": "Al Waha", "nameKey": "waha", "city": "", "cityKey": "",
             "sourceId": "node/5", "geometry": Point(-9.5, 30.5)},
        ]
        features, totals, city_stats, duplicates = MODULE.build_registry(records, candidates)

        self.assertEqual(len(features), 3)
        self.assertEqual(features[0]["properties"]["geometryStatus"], "mapped_boundary")
        self.assertEqual(features[0]["geometry"]["type"], "Polygon")
        self.assertEqual(features[1]["properties"]["geometryStatus"], "unlocated")
        self.assertIsNone(features[1]["geometry"])
        self.assertEqual(features[2]["properties"]["geometryStatus"], "unlocated")
        self.assertEqual(totals["mapped_boundary"], 1)
        self.assertEqual(totals["unlocated"], 2)

    def test_output_requires_at_least_ten_thousand_source_rows(self):
        with self.assertRaisesRegex(ValueError, "10000 required"):
            MODULE.write_outputs([], Counter(), defaultdict(Counter), 0,
                                 Path("/tmp/postal.gz"), Path("/tmp/postal-index.json"),
                                 Path("/tmp/postal-report.md"))

    def test_output_writes_compressed_registry_and_audit_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            features = [
                {"type": "Feature", "id": str(i), "properties": {"geometryStatus": "unlocated"},
                 "geometry": None}
                for i in range(10000)
            ]
            totals = Counter({"mapped_boundary": 0, "mapped_point": 0, "unlocated": 10000})
            city_stats = defaultdict(Counter, {"RABAT": Counter({"unlocated": 10000})})
            index = MODULE.write_outputs(
                features, totals, city_stats, 0, root / "postal.gz",
                root / "index.json", root / "report.md",
            )
            with gzip.open(root / "postal.gz", "rt", encoding="utf-8") as stream:
                parsed = json.load(stream)
            parsed_index = json.loads((root / "index.json").read_text(encoding="utf-8"))
            report_text = (root / "report.md").read_text(encoding="utf-8")

        self.assertEqual(len(parsed["features"]), 10000)
        self.assertEqual(parsed_index["features"], 10000)
        self.assertEqual(index["unlocatedFeatures"], 10000)
        self.assertIn("pas des frontières", report_text)


if __name__ == "__main__":
    unittest.main()
