import importlib.util
import unittest
from pathlib import Path

from shapely.geometry import Point, box, mapping, shape

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "build-estimated-neighborhood-zones.py"
SPEC = importlib.util.spec_from_file_location("estimated_neighborhood_zones", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def block(name, geom, commune="C1", serial=0):
    return {
        "type": "Feature",
        "id": f"urban-block-{commune}-{serial}",
        "properties": {
            "name": name,
            "boundaryKind": "urban_block",
            "estimated": True,
            "commune_id": commune,
            "commune_name": "Test commune " + commune,
            "region": "Test region",
            "region_code": "06" if commune == "C1" else "07",
            "_samplingTile": f"{serial}:0",
        },
        "geometry": mapping(geom),
    }


def grid_blocks(rows=3, cols=4, commune="C1", offset_x=-7.6, offset_y=33.6, start=0):
    items = []
    serial = start
    for row in range(rows):
        for col in range(cols):
            geom = box(offset_x + col * 0.001, offset_y + row * 0.001,
                       offset_x + (col + 1) * 0.001, offset_y + (row + 1) * 0.001)
            items.append(block(f"block-{serial}", geom, commune=commune, serial=serial))
            serial += 1
    return items


class EstimatedNeighborhoodZoneTests(unittest.TestCase):
    def test_dissolves_contiguous_road_blocks_to_exact_target(self):
        candidates = grid_blocks(rows=3, cols=4)
        result, stats = MODULE.build_zones(candidates, [], [], target=4)
        zones = result["features"]
        self.assertEqual(len(zones), 4)
        self.assertEqual(stats["input_blocks"], 12)
        self.assertEqual(sum(f["properties"]["blockCount"] for f in zones), 12)
        self.assertTrue(all(f["properties"]["boundaryKind"] == "estimated_neighborhood" for f in zones))
        self.assertTrue(all(f["properties"]["estimated"] is True for f in zones))
        self.assertTrue(all(shape(f["geometry"]).is_valid for f in zones))
        self.assertTrue(all(f["properties"]["areaKm2"] > 0 for f in zones))
        self.assertTrue(all(f["properties"]["level"] == "Unité de quartier estimée" for f in zones))

    def test_never_merges_blocks_across_commune_boundaries(self):
        a = grid_blocks(rows=2, cols=3, commune="C1", offset_x=-7.6, start=0)
        b = grid_blocks(rows=2, cols=3, commune="C2", offset_x=-7.6, offset_y=33.602, start=100)
        result, _ = MODULE.build_zones(a + b, [], [], target=4)
        counts = {}
        for item in result["features"]:
            commune = item["properties"]["commune_id"]
            counts[commune] = counts.get(commune, 0) + 1
        self.assertEqual(counts, {"C1": 2, "C2": 2})

    def test_excludes_blocks_already_inside_a_real_mapped_neighborhood(self):
        candidates = grid_blocks(rows=3, cols=4)
        mapped = [{
            "type": "Feature",
            "properties": {"boundaryKind": "mapped", "estimated": False},
            "geometry": mapping(box(-7.6, 33.6, -7.5991, 33.6009)),
        }]
        result, stats = MODULE.build_zones(candidates, mapped, [], target=3)
        self.assertEqual(len(result["features"]), 3)
        self.assertEqual(stats["skip_inside_mapped_neighborhood"], 1)
        self.assertEqual(stats["input_blocks"], 11)

    def test_uses_only_unambiguous_named_osm_points_for_labels(self):
        candidates = grid_blocks(rows=1, cols=4)
        point = {
            "type": "Feature",
            "id": "node/42",
            "properties": {"name": "Hay Al Amal", "place": "neighbourhood", "@id": "node/42"},
            "geometry": mapping(Point(-7.5995, 33.6005)),
        }
        result, stats = MODULE.build_zones(candidates, [], [point], target=4)
        named = [f for f in result["features"] if f["properties"].get("nameSource")]
        self.assertEqual(len(named), 1)
        self.assertEqual(named[0]["properties"]["name"], "Hay Al Amal")
        self.assertEqual(named[0]["properties"]["nameSource"], "OpenStreetMap named place point")
        self.assertEqual(stats["zones_with_unambiguous_osm_place_names"], 1)
        self.assertEqual(named[0]["properties"]["boundaryKind"], "estimated_neighborhood")
        self.assertTrue(named[0]["properties"]["estimated"])

    def test_rejects_unlabelled_or_non_urban_features(self):
        candidates = grid_blocks(rows=1, cols=3)
        candidates.append({
            "type": "Feature", "id": "fake",
            "properties": {"boundaryKind": "mapped", "estimated": False, "commune_id": "C1", "commune_name": "x"},
            "geometry": mapping(box(-7.59, 33.6, -7.589, 33.601)),
        })
        with self.assertRaisesRegex(ValueError, "îlots utilisables"):
            MODULE.build_zones(candidates, [], [], target=4)


if __name__ == "__main__":
    unittest.main()
