import importlib.util
import unittest
from pathlib import Path

from shapely.geometry import box, mapping, shape

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "assign-mapped-neighborhood-polygons.py"


def load_assignment_module():
    spec = importlib.util.spec_from_file_location("mapped_neighborhood_assignment", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def commune(code, name, geometry, region="Marrakech-Safi", region_code="07"):
    return {
        "type": "Feature",
        "id": code,
        "properties": {
            "code": code,
            "name_fr": name,
            "name_ar": "",
            "region": region,
            "region_code": region_code,
        },
        "geometry": mapping(geometry),
    }


class MappedNeighborhoodAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.module = load_assignment_module()

    def test_polygon_is_assigned_to_commune_with_maximum_overlap_and_clipped(self):
        west = commune("W", "West", box(0, 0, 1, 1))
        east = commune("E", "East", box(1, 0, 2, 1))
        feature = {
            "type": "Feature",
            "id": "mapped-1",
            "properties": {
                "name": "Quartier connu",
                "name_fr": "Quartier connu",
                "source": "osm_overpass",
                "sourceId": "relation/123",
                "sourceLicense": "ODbL-1.0",
                "estimated": False,
                "boundaryKind": "mapped",
            },
            "geometry": mapping(box(0.1, 0.1, 1.3, 0.9)),
        }

        output, stats = self.module.assign_mapped_polygons([feature], [west, east])
        self.assertEqual(stats["accepted"], 1)
        self.assertEqual(stats["clipped"], 1)
        result = output["features"][0]
        props = result["properties"]
        geometry = shape(result["geometry"])
        self.assertEqual(props["commune_id"], "W")
        self.assertEqual(props["commune_name"], "West")
        self.assertEqual(props["boundaryKind"], "mapped")
        self.assertFalse(props["estimated"])
        self.assertEqual(props["sourceId"], "relation/123")
        self.assertAlmostEqual(geometry.bounds[2], 1.0)
        self.assertTrue(box(0, 0, 1, 1).covers(geometry))

    def test_estimated_voronoi_cells_are_never_promoted_to_mapped_boundaries(self):
        boundary = commune("W", "West", box(0, 0, 1, 1))
        synthetic = {
            "type": "Feature",
            "properties": {
                "name": "Cellule Voronoï",
                "source": "geonames",
                "estimated": True,
            },
            "geometry": mapping(box(0.1, 0.1, 0.3, 0.3)),
        }
        result, stats = self.module.assign_mapped_polygons([synthetic], [boundary])
        self.assertEqual(result["features"], [])
        self.assertEqual(stats["skip_estimated"], 1)

    def test_invalid_non_polygon_and_unnamed_features_are_rejected(self):
        boundary = commune("W", "West", box(0, 0, 1, 1))
        unnamed = {
            "type": "Feature",
            "properties": {"estimated": False, "source": "osm"},
            "geometry": mapping(box(0.1, 0.1, 0.3, 0.3)),
        }
        point = {
            "type": "Feature",
            "properties": {"name": "Point only", "estimated": False},
            "geometry": {"type": "Point", "coordinates": [0.2, 0.2]},
        }
        result, stats = self.module.assign_mapped_polygons([unnamed, point], [boundary])
        self.assertEqual(result["features"], [])
        self.assertEqual(stats["skip_name"], 1)
        self.assertEqual(stats["skip_geometry"], 1)


if __name__ == "__main__":
    unittest.main()
