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
OVERTURE_FETCHER_PATH = ROOT / "tools" / "fetch-overture-neighborhood-areas.py"


def load_overture_fetcher_module():
    spec = importlib.util.spec_from_file_location("overture_neighborhood_fetcher", OVERTURE_FETCHER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_pipeline_module():
    spec = importlib.util.spec_from_file_location("neighborhood_pipeline", PIPELINE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class NeighborhoodPipelineTests(unittest.TestCase):
    def test_overture_import_accepts_only_named_moroccan_neighbourhood_areas(self):
        module = load_overture_fetcher_module()
        country = box(-10, 31, -9, 33)
        polygon = {
            "type": "Polygon",
            "coordinates": [[[-9.8, 32.0], [-9.75, 32.0], [-9.75, 32.05],
                             [-9.8, 32.05], [-9.8, 32.0]]],
        }
        raw = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "id": "ma-neighbourhood", "properties": {
                    "country": "MA", "subtype": "neighborhood",
                    "names": {"primary": "Quartier Test"}, "sources": [{"dataset": "OpenStreetMap", "record_id": "w123"}],
                }, "geometry": polygon},
                {"type": "Feature", "id": "city-locality", "properties": {
                    "country": "MA", "subtype": "locality", "names": {"primary": "Safi"},
                }, "geometry": polygon},
                {"type": "Feature", "id": "foreign-neighbourhood", "properties": {
                    "country": "DZ", "subtype": "neighborhood", "names": {"primary": "Foreign"},
                }, "geometry": polygon},
            ],
        }
        features, counts = module.normalize_overture_features(raw, country, "2026-10-23T12:00:00+00:00")
        self.assertEqual(len(features), 1)
        self.assertEqual(counts["skip_subtype"], 1)
        self.assertEqual(counts["skip_country"], 1)
        props = features[0]["properties"]
        self.assertEqual(props["name"], "Quartier Test")
        self.assertEqual(props["boundaryKind"], "mapped")
        self.assertFalse(props["estimated"])
        self.assertEqual(props["sourceLicense"], "ODbL-1.0")
        self.assertEqual(props["sourceRef"], "w123")

    def test_overture_polygon_dedup_replaces_same_named_old_shape_and_preserves_new_shapes(self):
        module = load_overture_fetcher_module()
        old = {
            "type": "Feature", "properties": {"name": "Quartier Test", "boundaryKind": "mapped"},
            "geometry": {"type": "Polygon", "coordinates": [[[-9.8, 32.0], [-9.75, 32.0], [-9.75, 32.05], [-9.8, 32.05], [-9.8, 32.0]]]},
        }
        replacement = {
            "type": "Feature", "properties": {"name": "Quartier Test", "sourceSubtype": "neighborhood", "boundaryKind": "mapped"},
            "geometry": {"type": "Polygon", "coordinates": [[[-9.8, 32.0], [-9.745, 32.0], [-9.745, 32.05], [-9.8, 32.05], [-9.8, 32.0]]]},
        }
        new = {
            "type": "Feature", "properties": {"name": "Autre Quartier", "sourceSubtype": "neighborhood", "boundaryKind": "mapped"},
            "geometry": {"type": "Polygon", "coordinates": [[[-9.6, 32.0], [-9.55, 32.0], [-9.55, 32.05], [-9.6, 32.05], [-9.6, 32.0]]]},
        }
        merged, net_new = module.merge_mapped_features([old], [replacement, new])
        self.assertEqual(len(merged), 2)
        self.assertEqual(net_new, 1)
        self.assertEqual(merged[0]["properties"]["sourceSubtype"], "neighborhood")

    def test_overpass_closed_way_keeps_its_actual_neighborhood_polygon(self):
        module = load_pipeline_module()
        element = {
            "type": "way",
            "id": 10,
            "tags": {"name": "Quartier test", "place": "neighbourhood"},
            "geometry": [
                {"lon": -9.1, "lat": 32.3},
                {"lon": -9.0, "lat": 32.3},
                {"lon": -9.0, "lat": 32.4},
                {"lon": -9.1, "lat": 32.4},
                {"lon": -9.1, "lat": 32.3},
            ],
        }
        seed = module.osm_element_to_seed(element)
        self.assertIsNotNone(seed)
        self.assertEqual(seed.kind, "polygon")
        self.assertIsNotNone(seed.polygon)
        self.assertGreater(seed.polygon.area, 0)
        self.assertAlmostEqual(seed.polygon.bounds[0], -9.1, places=5)
        self.assertAlmostEqual(seed.polygon.bounds[2], -9.0, places=5)

    def test_overpass_relation_stitches_outer_way_members_into_polygon(self):
        module = load_pipeline_module()
        a = {"lon": -9.1, "lat": 32.3}
        b = {"lon": -9.0, "lat": 32.3}
        c = {"lon": -9.0, "lat": 32.4}
        d = {"lon": -9.1, "lat": 32.4}
        element = {
            "type": "relation",
            "id": 20,
            "tags": {"name": "Quartier relation", "place": "quarter"},
            "members": [
                {"type": "way", "role": "outer", "geometry": [a, b, c]},
                {"type": "way", "role": "outer", "geometry": [c, d, a]},
            ],
        }
        seed = module.osm_element_to_seed(element)
        self.assertIsNotNone(seed)
        self.assertEqual(seed.kind, "polygon")
        self.assertIsNotNone(seed.polygon)
        self.assertGreater(seed.polygon.area, 0)

    def test_merge_keeps_polygon_boundary_when_name_matches_point(self):
        module = load_pipeline_module()
        point_seed = module.Seed(name="Quartier test", lon=-9.05, lat=32.35, source="geonames", population=500)
        polygon_seed = module.Seed(name="Quartier test", lon=-9.05, lat=32.35, source="osm_overpass",
                                   kind="polygon", polygon=box(-9.1, 32.3, -9.0, 32.4))
        merged = module.merge_seeds([point_seed, polygon_seed])
        self.assertEqual(len(merged), 1)
        self.assertIsNotNone(merged[0].polygon)
        self.assertEqual(merged[0].source, "osm_overpass")

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
    def test_converter_mapped_only_drops_voronoi_estimates(self):
        import gzip

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / "input.geojson"
            output_dir = directory / "regions"
            index_path = directory / "index.json"
            polygon = {
                "type": "Polygon",
                "coordinates": [[[-9.1, 32.3], [-9.0, 32.3], [-9.0, 32.4],
                                 [-9.1, 32.4], [-9.1, 32.3]]],
            }
            source.write_text(json.dumps({
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "properties": {
                        "name": "Contour connu", "commune_id": "MA01101",
                        "source": "osm_overpass", "estimated": False,
                    }, "geometry": polygon},
                    {"type": "Feature", "properties": {
                        "name": "Cellule estimée", "commune_id": "MA01101",
                        "source": "geonames", "estimated": True,
                    }, "geometry": polygon},
                ],
            }), encoding="utf-8")

            result = subprocess.run([
                sys.executable, str(CONVERTER_PATH),
                "--input", str(source),
                "--output-dir", str(output_dir),
                "--index", str(index_path),
                "--mapped-only",
            ], capture_output=True, text=True, check=False)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(index["features"], 1)
            self.assertEqual(index["mappedFeatures"], 1)
            self.assertEqual(index["estimatedFeatures"], 0)
            with gzip.open(output_dir / "07.geojson.gz", "rt", encoding="utf-8") as stream:
                data = json.load(stream)
            self.assertEqual(data["features"][0]["properties"]["boundaryKind"], "mapped")

    def test_converter_adds_only_road_bounded_sectors_as_estimated_blocks(self):
        import gzip

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / "input.geojson"
            blocks_path = directory / "urban-blocks.geojson"
            output_dir = directory / "regions"
            index_path = directory / "index.json"
            polygon = {
                "type": "Polygon",
                "coordinates": [[[-9.1, 32.3], [-9.099, 32.3], [-9.099, 32.301],
                                 [-9.1, 32.301], [-9.1, 32.3]]],
            }
            source.write_text(json.dumps({
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "properties": {
                        "name": "Contour réel", "commune_id": "MA01101",
                        "region": "Marrakech-Safi", "source": "osm_overpass", "estimated": False,
                    }, "geometry": polygon},
                    {"type": "Feature", "properties": {
                        "name": "Cellule Voronoï", "commune_id": "MA01101",
                        "source": "geonames", "estimated": True,
                    }, "geometry": polygon},
                ],
            }), encoding="utf-8")
            blocks_path.write_text(json.dumps({
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "properties": {
                        "name": "Îlot urbain estimé 0001", "commune_id": "MA01101",
                        "commune_name": "Safi", "region": "Marrakech-Safi",
                        "source": "osm_road_block", "estimated": True, "boundaryKind": "urban_block",
                        "buildingCount": 12, "estimationMethod": "road polygonization",
                    }, "geometry": polygon},
                ],
            }), encoding="utf-8")

            result = subprocess.run([
                sys.executable, str(CONVERTER_PATH),
                "--input", str(source),
                "--output-dir", str(output_dir),
                "--index", str(index_path),
                "--mapped-only",
                "--urban-blocks", str(blocks_path),
                "--min-urban-blocks", "1",
            ], capture_output=True, text=True, check=False)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(index["features"], 2)
            self.assertEqual(index["mappedFeatures"], 1)
            self.assertEqual(index["urbanBlockFeatures"], 1)
            self.assertEqual(index["estimatedFeatures"], 0)
            with gzip.open(output_dir / "07.geojson.gz", "rt", encoding="utf-8") as stream:
                data = json.load(stream)
            kinds = {feature["properties"]["boundaryKind"] for feature in data["features"]}
            self.assertEqual(kinds, {"mapped", "urban_block"})
            block = next(feature for feature in data["features"] if feature["properties"]["boundaryKind"] == "urban_block")
            self.assertEqual(block["properties"]["level"], "Secteur urbain estimé (îlot borné par les rues)")
            self.assertEqual(block["properties"]["buildingCount"], 12)

    def test_neighborhood_index_matches_catalog_and_compressed_regions(self):
        import gzip

        index_path = ROOT / "assets" / "neighborhood-boundaries-index.json"
        catalog_path = ROOT / "assets" / "data-catalog.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        layer = catalog["layers"]["neighborhoodBoundaries"]
        regions = index["regions"]

        self.assertGreaterEqual(len(regions), 4, "La couverture des vrais contours doit concerner plusieurs régions.")
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
                if index.get("urbanBlockFeatures", 0) > 0:
                    self.assertEqual(
                        sum(1 for feature in geojson["features"]
                            if (feature.get("properties") or {}).get("boundaryKind") == "urban_block"),
                        region.get("urbanBlocks", 0),
                    )
                if index.get("estimatedFeatures", 0) == 0 and index.get("urbanBlockFeatures", 0) == 0:
                    self.assertTrue(
                        all((feature.get("properties") or {}).get("boundaryKind") == "mapped"
                            for feature in geojson["features"]),
                        "Sans îlots urbains listés, la couche ne doit contenir que les contours cartographiés."
                    )

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

    def test_frontend_neighborhood_overlay_and_population_fallback_are_wired(self):
        index = json.loads((ROOT / "assets" / "neighborhood-boundaries-index.json").read_text(encoding="utf-8"))
        app = (ROOT / "assets" / "map-app.js").read_text(encoding="utf-8")
        helper = (ROOT / "assets" / "population-choropleth.mjs").read_text(encoding="utf-8")
        worker = (ROOT / "sw.js").read_text(encoding="utf-8")

        self.assertIn(f"NEIGHBORHOOD_BOUNDARY_VERSION='{index['version']}'", app)
        self.assertIn("import { populationNeighborhoodColor, estimateDensityFromNearbyCells }", app)
        self.assertIn("densityMethod='interpolated'", app)
        self.assertIn("estimateDensityFromNearbyCells(center,cells,{maxDistanceKm:1.5,maxNeighbors:3})", app)
        self.assertIn("if(zoom>=NEIGHBORHOOD_BOUNDARY_MIN_ZOOM){removePopulationRegionLayers();setPopulationArrondissementLayer([]);await updatePopulationNeighborhoodLayer(refreshId,zoom,bounds);", app)
        self.assertIn("raw=feature.properties?.densityEstimate", app)
        self.assertIn("urbanBlock?'#71808e'", app)
        self.assertIn("return kind==='mapped'||kind==='urban_block'", app)
        self.assertIn("Pas de limite de quartier cartographiée dans cette zone", app)
        self.assertIn("function isPlausibleNeighborhoodGeometry(feature)", app)
        self.assertIn("return kind==='mapped'||kind==='urban_block'", app)
        self.assertIn("visibleNeighborhoodFeatures(await loadNeighborhoodRegion(region.code),bounds)", app)
        self.assertIn("boundaryFeatureBbox(feature)", app)
        self.assertIn("neighborhoodHaloPane", app)
        self.assertIn("export function populationNeighborhoodColor", helper)
        self.assertIn(r"\/assets\/", worker)
        self.assertIn(r"\.json$", worker)
        self.assertIn("request.mode === 'navigate'", worker)
        self.assertIn("fetch(request, { cache: 'no-cache' })", worker)

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
        assets = re.findall(r"""['"]([^'"]+)['"]""", worker[start:end])
        for asset in assets:
            with self.subTest(asset=asset):
                if asset.startswith("./"):
                    asset = asset[2:]
                if asset:
                    self.assertTrue((ROOT / asset).is_file(), f"Ressource PWA en précache absente : {asset}")


if __name__ == "__main__":
    unittest.main()
