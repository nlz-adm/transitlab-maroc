import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import Point, box, mapping, shape

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "extract-osm-neighborhood-polygons.py"
SPEC = importlib.util.spec_from_file_location("osm_neighborhood_polygons", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def feature(name, geometry, place="neighbourhood", source_id="way/1"):
    return {
        "type": "Feature",
        "id": source_id,
        "properties": {"name": name, "place": place, "@id": source_id},
        "geometry": mapping(geometry),
    }


class OSMNeighbourhoodPolygonTests(unittest.TestCase):
    def test_accepts_named_mapped_neighbourhood_polygon_with_provenance(self):
        candidate = feature("Hay Al Amal", box(-7.61, 33.59, -7.60, 33.60))
        stats = MODULE.Counter()
        result = MODULE.osm_feature_to_neighbourhood(candidate, stats)

        self.assertIsNotNone(result)
        props = result["properties"]
        self.assertEqual(result["geometry"]["type"], "Polygon")
        self.assertEqual(props["name"], "Hay Al Amal")
        self.assertEqual(props["source"], "osm")
        self.assertEqual(props["sourceId"], "way/1")
        self.assertEqual(props["sourceLicense"], "ODbL-1.0")
        self.assertEqual(props["boundaryKind"], "mapped")
        self.assertFalse(props["estimated"])

    def test_rejects_points_unnamed_objects_and_generic_city_boundaries(self):
        stats = MODULE.Counter()
        point = feature("Hay Al Amal", Point(-7.6, 33.6))
        unnamed = feature("", box(-7.61, 33.59, -7.60, 33.60))
        city = feature("Casablanca", box(-8.0, 33.0, -7.0, 34.0), place="city")

        self.assertIsNone(MODULE.osm_feature_to_neighbourhood(point, stats))
        self.assertIsNone(MODULE.osm_feature_to_neighbourhood(unnamed, stats))
        self.assertIsNone(MODULE.osm_feature_to_neighbourhood(city, stats))
        self.assertEqual(stats["skip_non_polygon"], 1)
        self.assertEqual(stats["skip_name"], 1)
        self.assertEqual(stats["skip_settlement_or_admin"], 1)

    def test_accepts_named_boundary_place_polygon(self):
        candidate = {
            "type": "Feature", "id": "relation/5",
            "properties": {"name:fr": "Quartier Hassan", "boundary": "place", "@id": "relation/5"},
            "geometry": mapping(box(-6.85, 34.0, -6.84, 34.01)),
        }
        result = MODULE.osm_feature_to_neighbourhood(candidate, MODULE.Counter())
        self.assertIsNotNone(result)
        self.assertEqual(result["properties"]["sourceSubtype"], "boundary=place")

    def test_deduplicates_same_polygon_but_keeps_same_name_in_another_location(self):
        existing_geometry = box(-6.85, 34.0, -6.84, 34.01)
        existing = {
            "type": "Feature", "id": "existing-hassan",
            "properties": {"name": "Quartier Hassan", "name_fr": "Quartier Hassan",
                           "boundaryKind": "mapped", "estimated": False, "sourceId": "overture/123"},
            "geometry": mapping(existing_geometry),
        }
        same_geometry = feature("Quartier Hassan", existing_geometry, source_id="way/10")
        other_location = feature("Quartier Hassan", box(-7.61, 33.59, -7.60, 33.60), source_id="way/11")

        result, stats = MODULE.merge_osm_polygons([existing], [same_geometry, other_location])
        self.assertEqual(len(result["features"]), 2)
        self.assertEqual(stats["deduplicated_osm_polygons"], 1)
        self.assertEqual(stats["new_osm_polygons"], 1)
        self.assertTrue(box(-7.61, 33.59, -7.60, 33.60).equals(shape(result["features"][1]["geometry"])))

    def test_geojsonseq_reader_handles_record_separator(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "places.geojsonseq"
            path.write_text(
                "\\x1e" + json.dumps(feature("Hay Al Amal", box(-7.61, 33.59, -7.60, 33.60))) + "\\n",
                encoding="utf-8",
            )
            rows = list(MODULE.read_geojsonseq(path))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["properties"]["name"], "Hay Al Amal")


if __name__ == "__main__":
    unittest.main()
