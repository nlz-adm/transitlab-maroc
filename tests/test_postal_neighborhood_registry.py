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

    def test_loads_parquet_mirror_rows(self):
        import pyarrow as pa
        import pyarrow.parquet as pq

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "postal.parquet"
            table = pa.table({
                "source_record_id": ["poste:0", "poste:1"],
                "source_sheet": ["Codes postal Géographiques", "Codes postal Géographiques"],
                "ville": ["RABAT", "CASABLANCA"],
                "quartier": ["QUARTIER HASSAN", "HAY AL AMAL"],
                "nouveau_code_postal": [10020, 20250],
            })
            pq.write_table(table, path)
            records = MODULE.load_postal_rows(path)

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["city"], "RABAT")
        self.assertEqual(records[0]["name"], "QUARTIER HASSAN")
        self.assertEqual(records[0]["postalCode"], "10020")
        self.assertEqual(records[0]["nameKey"], "hassan")
        self.assertEqual(records[0]["sourceRow"], "poste:0")
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

    def test_unmatched_named_osm_neighborhoods_are_added_without_faking_boundaries(self):
        records = [
            {"sourceId": "postal:1", "sourceRow": "sheet:2", "city": "Rabat", "cityKey": "rabat",
             "name": "Quartier Known", "nameKey": "known", "postalCode": "10020"},
            {"sourceId": "postal:2", "sourceRow": "sheet:3", "city": "Rabat", "cityKey": "rabat",
             "name": "Quartier Without Geometry", "nameKey": "without geometry", "postalCode": "10030"},
        ]
        candidates = [
            {"name": "Known", "nameKey": "known", "city": "Rabat", "cityKey": "rabat",
             "sourceId": "way/1", "placeType": "neighbourhood", "geometry": box(-6.85, 34.0, -6.84, 34.01)},
            {"name": "Hay Al Wifaq", "nameFr": "Hay Al Wifaq", "nameAr": "حي الوفاق",
             "nameKey": "wifaq", "city": "Casablanca", "cityKey": "casablanca",
             "sourceId": "way/2", "placeType": "suburb", "geometry": box(-7.61, 33.59, -7.60, 33.60)},
            {"name": "Hay Al Wifaq", "nameKey": "wifaq", "city": "Casablanca", "cityKey": "casablanca",
             "sourceId": "node/3", "placeType": "neighbourhood", "geometry": Point(-7.605, 33.595)},
            {"name": "Sidi New", "nameKey": "sidi new", "city": "Marrakech", "cityKey": "marrakech",
             "sourceId": "node/4", "placeType": "neighbourhood", "geometry": Point(-8.0, 31.6)},
            {"name": "Quartier Without Geometry", "nameKey": "without geometry", "city": "", "cityKey": "",
             "sourceId": "node/5", "placeType": "neighbourhood", "geometry": Point(-6.84, 34.02)},
            {"name": "Quartier Without Geometry", "nameKey": "without geometry", "city": "", "cityKey": "",
             "sourceId": "node/6", "placeType": "neighbourhood", "geometry": Point(-6.83, 34.03)},
        ]

        features, totals, city_stats, duplicates = MODULE.build_registry(
            records, candidates, include_unmatched_mapped=True
        )

        self.assertEqual(len(features), 4)
        self.assertEqual(features[0]["properties"]["source"], "poste_maroc_2018")
        self.assertEqual(features[0]["properties"]["geometryStatus"], "mapped_boundary")
        self.assertEqual(features[1]["properties"]["geometryStatus"], "unlocated")
        self.assertIsNone(features[1]["geometry"])
        osm_features = [f for f in features if f["properties"].get("source") == "openstreetmap"]
        self.assertEqual(len(osm_features), 2)
        by_name = {f["properties"]["name"]: f for f in osm_features}
        self.assertEqual(by_name["Hay Al Wifaq"]["geometry"]["type"], "Polygon")
        self.assertEqual(by_name["Hay Al Wifaq"]["properties"]["name_ar"], "حي الوفاق")
        self.assertEqual(by_name["Hay Al Wifaq"]["properties"]["geometryStatus"], "mapped_boundary")
        self.assertEqual(by_name["Sidi New"]["geometry"]["type"], "Point")
        self.assertEqual(by_name["Sidi New"]["properties"]["postalCode"], "", "Postal metadata must not be fabricated")
        self.assertEqual(totals["mapped_boundary"], 2)
        self.assertEqual(totals["mapped_point"], 1)
        self.assertEqual(totals["unlocated"], 1)

    def test_untagged_osm_features_get_their_commune_from_real_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            commune_path = Path(tmp) / "communes.geojson"
            commune_path.write_text(json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "properties": {"code": "MA001", "name_fr": "Rabat"},
                    "geometry": mapping(box(-7.0, 33.0, -6.0, 35.0)),
                }],
            }), encoding="utf-8")
            communes, geometries, tree = MODULE.prepare_commune_areas(commune_path)
            candidates = [{
                "name": "Hay Untagged", "nameKey": "untagged", "city": "", "cityKey": "",
                "sourceId": "node/9", "geometry": Point(-6.84, 34.02),
            }]
            MODULE.enrich_candidates_with_communes(candidates, communes, geometries, tree)

        self.assertEqual(candidates[0]["city"], "Rabat")
        self.assertEqual(candidates[0]["cityKey"], "rabat")
        self.assertEqual(candidates[0]["communeCode"], "MA001")

    def test_mapped_overture_polygons_are_read_with_source_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapped.geojson"
            path.write_text(json.dumps({
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": "ov:area:matched",
                        "properties": {
                            "name": "Gauthier",
                            "name_fr": "Gauthier",
                            "name_ar": "غوتييه",
                            "source": "overture",
                            "sourceDetail": "Overture Maps division_area",
                            "sourceUrl": "https://docs.overturemaps.org/guides/divisions/",
                            "sourceLicense": "CC-BY-4.0",
                            "sourceId": "ov:area:matched",
                            "sourceSubtype": "neighborhood",
                            "commune_name": "CASABLANCA",
                            "commune_id": "06.141.01.0",
                            "boundaryKind": "mapped",
                            "estimated": False,
                        },
                        "geometry": mapping(box(-7.63, 33.58, -7.62, 33.59)),
                    },
                    {
                        "type": "Feature",
                        "id": "ov:area:new",
                        "properties": {
                            "name": "Quartier Overture Inédit",
                            "name_fr": "Quartier Overture Inédit",
                            "source": "overture",
                            "sourceDetail": "Overture Maps division_area",
                            "sourceUrl": "https://docs.overturemaps.org/guides/divisions/",
                            "sourceLicense": "CC-BY-4.0",
                            "sourceId": "ov:area:new",
                            "sourceSubtype": "neighborhood",
                            "commune_name": "CASABLANCA",
                            "commune_id": "06.141.01.0",
                            "boundaryKind": "mapped",
                            "estimated": False,
                        },
                        "geometry": mapping(box(-7.61, 33.58, -7.60, 33.59)),
                    },
                ],
            }), encoding="utf-8")
            candidates = MODULE.read_mapped_boundary_candidates(path)

        self.assertEqual(len(candidates), 2)
        self.assertTrue(all(candidate["source"] == "overture" for candidate in candidates))
        self.assertTrue(all(candidate["geometry"].geom_type == "Polygon" for candidate in candidates))
        records = [{
            "sourceId": "postal:1", "sourceRow": "sheet:2",
            "city": "CASABLANCA", "cityKey": "casablanca",
            "name": "HAY GAUTHIER", "nameKey": "gauthier", "postalCode": "20000",
        }]
        features, totals, city_stats, duplicates = MODULE.build_registry(
            records, candidates, include_unmatched_mapped=True
        )
        postal = features[0]
        overtime = [feature for feature in features if feature["properties"].get("source") == "overture"]
        self.assertEqual(postal["properties"]["geometryStatus"], "mapped_boundary")
        self.assertEqual(postal["properties"]["geometrySource"], "overture")
        self.assertEqual(postal["geometry"]["type"], "Polygon")
        self.assertEqual(len(overtime), 1)
        self.assertEqual(overtime[0]["properties"]["source"], "overture")
        self.assertEqual(overtime[0]["properties"]["geometrySource"], "overture")
        self.assertEqual(overtime[0]["properties"]["sourceId"], "ov:area:new")
        self.assertEqual(overtime[0]["properties"]["sourceLicense"], "CC-BY-4.0")
        self.assertEqual(totals["mapped_boundary"], 2)

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
