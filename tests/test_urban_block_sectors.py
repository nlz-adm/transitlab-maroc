import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import LineString

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "build-urban-block-sectors.py"


def load_builder():
    spec = importlib.util.spec_from_file_location("urban_block_builder", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class UrbanBlockPolygonizationTests(unittest.TestCase):
    def setUp(self):
        self.builder = load_builder()

    def test_closed_street_block_requires_and_uses_building_evidence(self):
        # A compact, closed 3 mdeg street loop (~0.1 km2 at the equator scale
        # after the latitude correction) that contains two mapped building points.
        ring = [(0.0, 0.0), (0.003, 0.0), (0.003, 0.003),
                (0.0, 0.003), (0.0, 0.0)]
        roads = [LineString([a, b]) for a, b in zip(ring, ring[1:])]
        core = (-0.01, -0.01, 0.01, 0.01)

        sectors = self.builder.polygonize_urban_blocks(
            roads, [(0.001, 0.001), (0.002, 0.002)], core, min_buildings=2
        )
        self.assertEqual(len(sectors), 1)
        geometry, building_count = sectors[0]
        self.assertEqual(building_count, 2)
        self.assertTrue(geometry.is_valid)
        self.assertGreater(self.builder.geometry_area_km2(geometry), 0)

    def test_open_or_unbuilt_road_polygons_are_not_published_as_sectors(self):
        ring = [(0.0, 0.0), (0.003, 0.0), (0.003, 0.003),
                (0.0, 0.003), (0.0, 0.0)]
        roads = [LineString([a, b]) for a, b in zip(ring, ring[1:])]
        core = (-0.01, -0.01, 0.01, 0.01)

        self.assertEqual(
            self.builder.polygonize_urban_blocks(roads, [], core, min_buildings=2),
            [],
        )
        open_roads = roads[:-1]
        self.assertEqual(
            self.builder.polygonize_urban_blocks(
                open_roads, [(0.001, 0.001), (0.002, 0.002)], core, min_buildings=2
            ),
            [],
        )

    def test_large_gap_rectangle_is_rejected_instead_of_filling_the_countryside(self):
        ring = [(0.0, 0.0), (0.04, 0.0), (0.04, 0.04),
                (0.0, 0.04), (0.0, 0.0)]
        roads = [LineString([a, b]) for a, b in zip(ring, ring[1:])]
        core = (-0.1, -0.1, 0.1, 0.1)
        self.assertEqual(
            self.builder.polygonize_urban_blocks(
                roads, [(0.01, 0.01), (0.02, 0.02)], core, min_buildings=2
            ),
            [],
        )

    def test_road_clipping_reduces_polygonization_area_without_moving_bounds(self):
        roads = [
            LineString([(-1.0, 0.0), (1.0, 0.0)]),
            LineString([(0.0, -1.0), (0.0, 1.0)]),
            LineString([(0.5, 0.5), (0.8, 0.8)]),
        ]
        clipped = self.builder.clip_roads_to_working_bounds(roads, (-0.1, -0.1, 0.1, 0.1))
        self.assertEqual(len(clipped), 2)
        self.assertTrue(all(-0.1000001 <= line.bounds[0] <= 0.1000001 for line in clipped))
        self.assertTrue(all(-0.1000001 <= line.bounds[2] <= 0.1000001 for line in clipped))
        self.assertTrue(all(-0.1000001 <= line.bounds[1] <= 0.1000001 for line in clipped))
        self.assertTrue(all(-0.1000001 <= line.bounds[3] <= 0.1000001 for line in clipped))

    def test_geojson_sequence_reader_handles_record_separators(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.geojsonseq"
            first = {"type": "Feature", "properties": {"highway": "residential"},
                     "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}}
            second = {"type": "Feature", "properties": {"building": "yes"},
                      "geometry": {"type": "Polygon", "coordinates": [[[0,0],[1,0],[1,1],[0,1],[0,0]]]}}
            path.write_text("\x1e" + json.dumps(first) + "\n" + "\x1e" + json.dumps(second) + "\n",
                            encoding="utf-8")
            records = list(self.builder.read_geojson_seq(path))
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["properties"]["highway"], "residential")
            self.assertEqual(records[1]["properties"]["building"], "yes")


if __name__ == "__main__":
    unittest.main()
