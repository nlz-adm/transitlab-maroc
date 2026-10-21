#!/usr/bin/env python3
"""Prepare mapped and indicative urban-neighbourhood outlines for the Morocco map.

Inputs are Overture's divisions and division_area GeoJSON extracts, the open
Poste Maroc neighbourhood/postcode workbook, and the map's existing H3
population grid. Exact mapped outlines remain distinct from estimated
Voronoi areas derived from named OSM neighbourhood points.

Install Shapely 2.1+, download the two Overture extracts for Morocco, then run:

  python tools/prepare-neighborhood-boundaries.py \
    --areas /path/to/division_area.geojson \
    --points /path/to/division.geojson \
    --postal /path/to/codes-postaux-quartiers-2018.xlsx
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import math
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

from shapely import make_valid, voronoi_polygons
from shapely.geometry import MultiPoint, Point, box, mapping, shape
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree


ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES_DIR = ROOT / "assets" / "commune-boundaries"
POPULATION_INDEX = ROOT / "assets" / "population-neighborhoods-index.json"
OUTPUT_DIR = ROOT / "assets" / "neighborhood-boundaries"
OUTPUT_INDEX = ROOT / "assets" / "neighborhood-boundaries-index.json"
OVERTURE_RELEASE = "2026-09-23.1"
POSTAL_SOURCE_DATE = "2018-09"
POPULATION_THRESHOLD = 8
MIN_ESTIMATED_SEEDS = 3
MIN_ESTIMATED_AREA_M2 = 18_000
MAX_ESTIMATED_AREA_M2 = 12_000_000

XML_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rels": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).strip()


def name_variants(names: dict[str, Any]) -> list[tuple[str, str]]:
    variants: list[tuple[str, str]] = []
    if names.get("primary"):
        variants.append(("", str(names["primary"]).strip()))
    for item in names.get("common") or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2 and item[1]:
            variants.append((str(item[0]).casefold(), str(item[1]).strip()))
    return variants


def public_name(names: dict[str, Any], language: str) -> str:
    variants = name_variants(names)
    return next((value for locale, value in variants if locale == language), "")


def osm_url(sources: list[dict[str, Any]]) -> str:
    for source in sources:
        record_id = str(source.get("record_id") or "")
        match = re.match(r"([nwr])(\d+)(?:@\d+)?$", record_id)
        if match:
            kind = {"n": "node", "w": "way", "r": "relation"}[match.group(1)]
            return f"https://www.openstreetmap.org/{kind}/{match.group(2)}"
    return ""


def read_postal_catalog(path: Path) -> dict[tuple[str, str], set[str]]:
    """Read Poste Maroc's XLSX without adding a workbook dependency."""
    with zipfile.ZipFile(path) as workbook:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
            shared = [
                "".join(node.text or "" for node in entry.findall(".//main:t", XML_NS))
                for entry in root.findall("main:si", XML_NS)
            ]
        book = ET.fromstring(workbook.read("xl/workbook.xml"))
        rels = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
        targets = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        sheet = book.find("main:sheets/main:sheet", XML_NS)
        if sheet is None:
            raise ValueError("Le classeur Poste Maroc ne contient aucune feuille.")
        target = targets[sheet.attrib[f"{{{XML_NS['rels']}}}id"]].lstrip("/")
        if not target.startswith("xl/"):
            target = f"xl/{target}"
        root = ET.fromstring(workbook.read(target))
        rows = root.findall(".//main:sheetData/main:row", XML_NS)
        output: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
        for row in rows[1:]:
            values: dict[str, str] = {}
            for cell in row.findall("main:c", XML_NS):
                value_node = cell.find("main:v", XML_NS)
                value = value_node.text if value_node is not None else "".join(
                    node.text or "" for node in cell.findall(".//main:t", XML_NS)
                )
                if cell.attrib.get("t") == "s" and value:
                    value = shared[int(value)]
                column = re.sub(r"\d+$", "", cell.attrib["r"])
                values[column] = value
            city = normalize(values.get("A", ""))
            neighborhood = strip_generic_prefix(normalize(values.get("B", "")))
            code = re.sub(r"\D", "", values.get("C", ""))
            if city and neighborhood and code:
                output[(city, neighborhood)].add(code)
        return output


def strip_generic_prefix(value: str) -> str:
    for prefix in (
        "quartier de ", "quartier d ", "quartier ", "hay ", "lotissement ",
        "lot ", "cite ", "residence ",
    ):
        if value.startswith(prefix):
            return value[len(prefix):].strip()
    return value


def feature_bbox(feature: dict[str, Any]) -> tuple[float, float, float, float]:
    geom = shape(feature["geometry"])
    return geom.bounds


def load_communes() -> tuple[dict[str, list[dict[str, Any]]], dict[str, tuple[list[Any], STRtree]]]:
    features_by_region: dict[str, list[dict[str, Any]]] = {}
    indexes: dict[str, tuple[list[Any], STRtree]] = {}
    for path in sorted(BOUNDARIES_DIR.glob("[0-9][0-9].geojson")):
        region = path.stem
        features = json.loads(path.read_text(encoding="utf-8")).get("features", [])
        geometries = [make_valid(shape(feature["geometry"])) for feature in features]
        features_by_region[region] = features
        indexes[region] = (geometries, STRtree(geometries))
    return features_by_region, indexes


def containing_commune(
    point: Point,
    region: str,
    commune_features: dict[str, list[dict[str, Any]]],
    commune_indexes: dict[str, tuple[list[Any], STRtree]],
) -> dict[str, Any] | None:
    indexed = commune_indexes.get(region)
    if indexed is None:
        return None
    geometries, tree = indexed
    candidates = tree.query(point)
    matches = [
        int(index) for index in candidates
        if geometries[int(index)].covers(point)
    ]
    if not matches:
        return None
    # The smallest containing polygon wins where administrative geometries touch.
    chosen = min(matches, key=lambda index: geometries[index].area)
    return commune_features[region][chosen]


class PopulationMaskSource:
    def __init__(self) -> None:
        self.index = json.loads(POPULATION_INDEX.read_text(encoding="utf-8"))
        self.chunk_cache: collections.OrderedDict[str, list[dict[str, Any]]] = collections.OrderedDict()

    def cells_for(self, bounds: tuple[float, float, float, float]) -> list[dict[str, Any]]:
        west, south, east, north = bounds
        cells: list[dict[str, Any]] = []
        for chunk in self.index.get("chunks", []):
            x0, y0, x1, y1 = chunk["bbox"]
            if x0 > east or x1 < west or y0 > north or y1 < south:
                continue
            cached = self.chunk_cache.get(chunk["id"])
            if cached is None:
                path = ROOT / chunk["url"]
                with gzip.open(path, "rt", encoding="utf-8") as stream:
                    value = json.load(stream)
                cached = value.get("features", [])
                self.chunk_cache[chunk["id"]] = cached
                while len(self.chunk_cache) > 10:
                    self.chunk_cache.popitem(last=False)
            else:
                self.chunk_cache.move_to_end(chunk["id"])
            cells.extend(cached)
        return cells


def transformed(geometry: Any, lon0: float, lat0: float, inverse: bool = False) -> Any:
    x_scale = 111_320.0 * math.cos(math.radians(lat0))
    y_scale = 111_320.0
    if inverse:
        return transform(
            lambda x, y, z=None: (x / x_scale + lon0, y / y_scale + lat0),
            geometry,
        )
    return transform(
        lambda x, y, z=None: ((x - lon0) * x_scale, (y - lat0) * y_scale),
        geometry,
    )


def extract_names(feature: dict[str, Any]) -> dict[str, Any]:
    props = feature.get("properties") or {}
    names = props.get("names") or {}
    return {
        "display": public_name(names, "fr") or str(names.get("primary") or "Quartier").strip(),
        "arabic": public_name(names, "ar"),
        "primary": str(names.get("primary") or "").strip(),
        "variants": name_variants(names),
    }


def normalized_variant_keys(names: dict[str, Any]) -> set[str]:
    return {
        strip_generic_prefix(normalize(value))
        for _locale, value in names.get("variants", [])
        if strip_generic_prefix(normalize(value))
    }


def add_feature(
    output: dict[str, list[dict[str, Any]]],
    region: str,
    feature_id: str,
    name: dict[str, Any],
    commune: dict[str, Any],
    geometry: Any,
    kind: str,
    level: str,
    sources: list[dict[str, Any]],
    postal_catalog: dict[tuple[str, str], set[str]],
) -> None:
    properties = commune.get("properties") or {}
    city = normalize(properties.get("name_fr"))
    postal_codes = sorted(set().union(*(
        postal_catalog.get((city, key), set())
        for key in normalized_variant_keys(name)
    ))) if name.get("variants") else []
    source_names = sorted({str(source.get("dataset")) for source in sources if source.get("dataset")})
    source_url = osm_url(sources)
    output[region].append({
        "type": "Feature",
        "id": feature_id,
        "properties": {
            "boundaryKind": kind,
            "level": level,
            "name": name["display"],
            "nameAr": name["arabic"],
            "commune": properties.get("name_fr") or "",
            "communeCode": properties.get("code") or "",
            "postalCodes": postal_codes,
            "postalSource": "Poste Maroc · 2018" if postal_codes else "",
            "geometrySource": "OpenStreetMap via Overture Maps" if kind == "mapped" else "Points de quartiers OpenStreetMap via Overture Maps",
            "supportingSources": ["HCP RGPH 2024 · codes/types des communes; contours OpenStreetMap", "Kontur Population / HDX · grille résidentielle multi-sources"] if kind == "estimated" else [],
            "sourceDatasets": source_names,
            "osmUrl": source_url,
        },
        "geometry": mapping(geometry),
    })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--areas", type=Path, required=True, help="Overture division_area GeoJSON extract")
    parser.add_argument("--points", type=Path, required=True, help="Overture division GeoJSON extract")
    parser.add_argument("--postal", type=Path, required=True, help="Poste Maroc 2018 XLSX")
    args = parser.parse_args()

    postal_catalog = read_postal_catalog(args.postal)
    commune_features, commune_indexes = load_communes()
    population_source = PopulationMaskSource()

    area_rows = json.loads(args.areas.read_text(encoding="utf-8")).get("features", [])
    point_rows = json.loads(args.points.read_text(encoding="utf-8")).get("features", [])
    exact_by_commune: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    mapped_features: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)

    for feature in area_rows:
        props = feature.get("properties") or {}
        if props.get("country") != "MA" or props.get("subtype") not in {"neighborhood", "macrohood"}:
            continue
        names = extract_names(feature)
        if not names["display"]:
            continue
        geometry = make_valid(shape(feature["geometry"]))
        point = geometry.representative_point()
        region = str(props.get("region") or "")[-2:]
        commune = containing_commune(point, region, commune_features, commune_indexes)
        if not commune or (commune.get("properties") or {}).get("type") != "urban":
            continue
        commune_geometry = make_valid(shape(commune["geometry"]))
        geometry = make_valid(geometry.intersection(commune_geometry))
        if geometry.is_empty:
            continue
        code = str((commune.get("properties") or {}).get("code") or "")
        record = {"feature": feature, "geometry": geometry, "names": names, "commune": commune, "region": region}
        exact_by_commune[code].append(record)
        mapped_features[code].append(record)

    # A broad macrohood duplicates the more useful child neighbourhood outlines.
    for code, records in list(exact_by_commune.items()):
        neighbourhoods = [row for row in records if (row["feature"].get("properties") or {}).get("subtype") == "neighborhood"]
        filtered = []
        for row in records:
            props = row["feature"].get("properties") or {}
            if props.get("subtype") == "macrohood" and any(
                row["geometry"].intersection(child["geometry"]).area / max(row["geometry"].area, 1) >= 0.30
                for child in neighbourhoods
            ):
                continue
            filtered.append(row)
        exact_by_commune[code] = filtered

    points_by_commune: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    skipped_point_names = {"arrondissement", "prefecture", "préfecture", "province", "district", "commune", "region"}
    for feature in point_rows:
        props = feature.get("properties") or {}
        if props.get("country") != "MA" or props.get("subtype") not in {"neighborhood", "macrohood"}:
            continue
        names = extract_names(feature)
        if not names["display"]:
            continue
        name_key = normalize(names["primary"] or names["display"])
        if any(term in name_key for term in skipped_point_names):
            continue
        coordinates = feature.get("geometry", {}).get("coordinates") or []
        if len(coordinates) < 2:
            continue
        point = Point(float(coordinates[0]), float(coordinates[1]))
        region = str(props.get("region") or "")[-2:]
        commune = containing_commune(point, region, commune_features, commune_indexes)
        if not commune or (commune.get("properties") or {}).get("type") != "urban":
            continue
        code = str((commune.get("properties") or {}).get("code") or "")
        # If a mapped outline already contains this point, that geometry is the better representation.
        if any(row["geometry"].buffer(0.0002).covers(point) for row in exact_by_commune.get(code, [])):
            continue
        points_by_commune[code].append({"feature": feature, "point": point, "names": names, "commune": commune, "region": region})

    output: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    mapped_count = 0
    estimated_count = 0
    postal_matches: set[str] = set()

    for code, records in exact_by_commune.items():
        commune = records[0]["commune"]
        region = records[0]["region"]
        commune_geom = make_valid(shape(commune["geometry"]))
        lon0, lat0 = commune_geom.representative_point().coords[0]
        for row in records:
            local = transformed(row["geometry"], lon0, lat0)
            local = make_valid(local.simplify(5, preserve_topology=True))
            props = row["feature"].get("properties") or {}
            sources = props.get("sources") or []
            level = "Grand secteur" if props.get("subtype") == "macrohood" else "Quartier"
            feature_id = "mapped:" + str(props.get("division_id") or (sources[0].get("record_id") if sources else code))
            add_feature(output, region, feature_id, row["names"], commune, transformed(local, lon0, lat0, inverse=True), "mapped", level, sources, postal_catalog)
            mapped_count += 1
            if output[region][-1]["properties"]["postalCodes"]:
                postal_matches.add(feature_id)

    for code, candidates in points_by_commune.items():
        # Prefer one seed for a repeated name, then merge coincident seeds before building cells.
        by_name: dict[str, dict[str, Any]] = {}
        for row in candidates:
            key = normalize(row["names"]["primary"] or row["names"]["display"])
            if not key:
                continue
            if key not in by_name or len(row["names"]["variants"]) > len(by_name[key]["names"]["variants"]):
                by_name[key] = row
        candidates = list(by_name.values())
        candidates.sort(key=lambda row: normalize(row["names"]["display"]))
        if len(candidates) < MIN_ESTIMATED_SEEDS:
            continue

        commune = candidates[0]["commune"]
        region = candidates[0]["region"]
        commune_geom_wgs = make_valid(shape(commune["geometry"]))
        lon0, lat0 = commune_geom_wgs.representative_point().coords[0]
        commune_geom = transformed(commune_geom_wgs, lon0, lat0)
        cells = population_source.cells_for(commune_geom_wgs.bounds)
        local_points = [transformed(row["point"], lon0, lat0) for row in candidates]
        urban_mask_shapes = []
        for feature in cells:
            if int((feature.get("properties") or {}).get("population") or 0) < POPULATION_THRESHOLD:
                continue
            cell = shape(feature["geometry"])
            if cell.intersects(box(*commune_geom_wgs.bounds)):
                cell = transformed(cell, lon0, lat0)
                if cell.intersects(commune_geom):
                    urban_mask_shapes.append(cell)
        if urban_mask_shapes:
            urban_mask = make_valid(unary_union(urban_mask_shapes).buffer(110).buffer(-110).intersection(commune_geom))
        else:
            urban_mask = commune_geom
        if urban_mask.is_empty:
            urban_mask = commune_geom

        # Keep the Voronoi estimate inside both the mapped commune and the inhabited footprint.
        # Small buffers preserve named seeds when the population grid misses their exact location.
        urban_mask = make_valid(unary_union([urban_mask, *(point.buffer(250) for point in local_points)]).intersection(commune_geom))
        voronoi = voronoi_polygons(MultiPoint(local_points), extend_to=commune_geom.envelope, ordered=True)
        exact_geometries = [
            transformed(row["geometry"], lon0, lat0)
            for row in exact_by_commune.get(code, [])
        ]
        exact_union = unary_union(exact_geometries) if exact_geometries else None

        for row, cell in zip(candidates, voronoi.geoms):
            estimate = make_valid(cell.intersection(commune_geom).intersection(urban_mask))
            if exact_union is not None and not exact_union.is_empty:
                estimate = make_valid(estimate.difference(exact_union))
            if estimate.is_empty:
                continue
            if estimate.geom_type == "GeometryCollection":
                polygons = [part for part in estimate.geoms if part.geom_type in {"Polygon", "MultiPolygon"}]
                estimate = unary_union(polygons) if polygons else estimate
            if estimate.geom_type not in {"Polygon", "MultiPolygon"}:
                continue
            if estimate.area < MIN_ESTIMATED_AREA_M2 or estimate.area > MAX_ESTIMATED_AREA_M2:
                continue
            estimate = make_valid(estimate.simplify(18, preserve_topology=True))
            props = row["feature"].get("properties") or {}
            sources = props.get("sources") or []
            level = "Grand secteur" if props.get("subtype") == "macrohood" else "Quartier"
            source_id = str(props.get("division_id") or (sources[0].get("record_id") if sources else code))
            feature_id = "estimated:" + source_id
            add_feature(output, region, feature_id, row["names"], commune, transformed(estimate, lon0, lat0, inverse=True), "estimated", level, sources, postal_catalog)
            estimated_count += 1
            if output[region][-1]["properties"]["postalCodes"]:
                postal_matches.add(feature_id)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    index_regions = []
    for region in sorted(output):
        features = sorted(output[region], key=lambda feature: (
            feature["properties"]["commune"], feature["properties"]["name"], feature["id"]
        ))
        if not features:
            continue
        west = min(feature_bbox(feature)[0] for feature in features)
        south = min(feature_bbox(feature)[1] for feature in features)
        east = max(feature_bbox(feature)[2] for feature in features)
        north = max(feature_bbox(feature)[3] for feature in features)
        file = OUTPUT_DIR / f"{region}.geojson.gz"
        data = json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with file.open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, compresslevel=9) as stream:
                stream.write(data)
        index_regions.append({
            "code": region,
            "url": f"assets/neighborhood-boundaries/{region}.geojson.gz",
            "bbox": [round(west, 6), round(south, 6), round(east, 6), round(north, 6)],
            "count": len(features),
            "mapped": sum(feature["properties"]["boundaryKind"] == "mapped" for feature in features),
            "estimated": sum(feature["properties"]["boundaryKind"] == "estimated" for feature in features),
            "bytesCompressed": file.stat().st_size,
        })

    index = {
        "version": "morocco-neighborhood-boundaries-2026-10-21",
        "generatedAt": "2026-10-21",
        "sourceRelease": OVERTURE_RELEASE,
        "regions": index_regions,
        "features": mapped_count + estimated_count,
        "mappedFeatures": mapped_count,
        "estimatedFeatures": estimated_count,
        "communesWithFeatures": len({feature["properties"]["communeCode"] for rows in output.values() for feature in rows}),
        "postalCrossChecks": len(postal_matches),
        "sources": {
            "mappedGeometry": "Overture Maps divisions / OpenStreetMap, division_area neighborhood and macrohood polygons",
            "estimatedGeometry": "Voronoi cells around OpenStreetMap / Overture named neighborhood and macrohood points, clipped to mapped urban communes and the Kontur population mask",
            "communeContext": "morocco-communes 1.10.0 geometry with HCP RGPH 2024 commune codes and urban/rural type",
            "populationMask": "Kontur Population · HDX · H3 resolution 8 · 2023, integrating GHSL, Meta HRSL, Microsoft building footprints, Copernicus, LINZ and OpenStreetMap",
            "postalNames": "Poste Maroc, codes postaux des quartiers, 2018 · name and postcode match only; source contains no neighborhood boundaries",
        },
        "licenses": ["ODbL 1.0", "CC BY 4.0"],
        "limitations": "Mapped neighborhood outlines are incomplete. Dashed estimated cells are indicative areas, not official neighborhood limits. No boundary is generated for postal names without a mapped coordinate. Postal names are from 2018.",
    }
    OUTPUT_INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "index": str(OUTPUT_INDEX),
        "mappedFeatures": mapped_count,
        "estimatedFeatures": estimated_count,
        "communesWithFeatures": index["communesWithFeatures"],
        "postalCrossChecks": len(postal_matches),
        "regions": len(index_regions),
        "compressedBytes": sum(region["bytesCompressed"] for region in index_regions),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
