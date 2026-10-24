#!/usr/bin/env python3
"""Build street-bounded urban blocks from OSM roads, retaining source and uncertainty.

The output is explicitly an estimated urban-block layer, not a layer of official
neighbourhood boundaries. It only keeps enclosed road polygons with mapped
building-footprint evidence, clipped to a Moroccan commune.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from shapely.geometry import LineString, Point, MultiPolygon, GeometryCollection, shape, mapping, box
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree
from shapely.validation import make_valid

TILE_DEGREES = 0.12
ROAD_CLASSES = {
    "primary", "secondary", "tertiary", "unclassified",
    "residential", "living_street", "service", "pedestrian", "road",
}
MIN_AREA_KM2 = 0.00015
MAX_AREA_KM2 = 0.12
MAX_SIDE_KM = 1.25
MIN_BUILDINGS = 2


def read_geojson_seq(path: Path) -> Iterable[dict]:
    """Stream RFC 8142 GeoJSON Text Sequence without loading the country extract in memory."""
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            line = line.strip().lstrip("\x1e").strip()
            if not line:
                continue
            try:
                feature = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid GeoJSONSeq record at {path}:{line_number}: {exc}") from exc
            if isinstance(feature, dict) and feature.get("type") == "Feature":
                yield feature


def tile_key(lon: float, lat: float) -> tuple[int, int]:
    return (math.floor((lon + 180.0) / TILE_DEGREES),
            math.floor((lat + 90.0) / TILE_DEGREES))


def neighboring_tiles(key: tuple[int, int]) -> Iterable[tuple[int, int]]:
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            yield key[0] + dx, key[1] + dy


def tile_bounds(key: tuple[int, int]):
    x0 = key[0] * TILE_DEGREES - 180.0
    y0 = key[1] * TILE_DEGREES - 90.0
    return x0, y0, x0 + TILE_DEGREES, y0 + TILE_DEGREES


def km_dimensions(geometry) -> tuple[float, float, float]:
    minx, miny, maxx, maxy = geometry.bounds
    mid_lat = (miny + maxy) / 2
    km_lon = 111.32 * max(0.15, math.cos(math.radians(mid_lat)))
    width = (maxx - minx) * km_lon
    height = (maxy - miny) * 110.574
    return width, height, width * height


def geometry_area_km2(geometry) -> float:
    degree_area = max(0.0, float(geometry.area))
    minx, miny, maxx, maxy = geometry.bounds
    mid_lat = (miny + maxy) / 2
    return degree_area * 111.32 * max(0.15, math.cos(math.radians(mid_lat))) * 110.574


def polygonal(geometry):
    if geometry is None or geometry.is_empty:
        return None
    geometry = make_valid(geometry)
    if geometry.geom_type == "Polygon":
        return geometry if geometry.area > 0 else None
    if geometry.geom_type == "MultiPolygon":
        parts = [p for p in geometry.geoms if not p.is_empty and p.area > 0]
        if not parts:
            return None
        return MultiPolygon(parts) if len(parts) > 1 else parts[0]
    if geometry.geom_type == "GeometryCollection":
        parts = []
        for part in geometry.geoms:
            candidate = polygonal(part)
            if candidate is None:
                continue
            parts.extend(list(candidate.geoms) if candidate.geom_type == "MultiPolygon" else [candidate])
        if not parts:
            return None
        result = make_valid(unary_union(parts))
        if result.geom_type in {"Polygon", "MultiPolygon"} and not result.is_empty:
            return result
    return None


def linear_parts(geometry) -> list:
    """Return only non-empty line strings from a clipped road geometry."""
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "LineString":
        return [geometry] if geometry.length > 1e-10 else []
    if geometry.geom_type in {"MultiLineString", "GeometryCollection"}:
        output = []
        for part in geometry.geoms:
            output.extend(linear_parts(part))
        return output
    return []


def clip_roads_to_working_bounds(road_lines: list, bounds: tuple[float, float, float, float]) -> list:
    """Restrict linework to a small halo around one tile before expensive noding/polygonizing."""
    window = box(*bounds)
    clipped_lines = []
    for line in road_lines:
        if not line.envelope.intersects(window):
            continue
        try:
            clipped_lines.extend(linear_parts(line.intersection(window)))
        except Exception:
            continue
    return clipped_lines


def order_active_tiles(keys: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Use a stable hash order so a 10k cap samples dense tiles across all Morocco."""
    return sorted(
        keys,
        key=lambda key: hashlib.sha1(f"{key[0]}:{key[1]}".encode("ascii")).digest(),
    )


def reached_sector_cap(count: int, target_max: int) -> bool:
    """A zero cap means unlimited; otherwise stop exactly at the requested target."""
    return target_max > 0 and count >= target_max


def sample_features_by_tile(features: list[dict], target_max: int,
                            tile_property: str = "_samplingTile") -> list[dict]:
    """Round-robin sample accepted sectors across all active road/building tiles."""
    buckets: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for feature in features:
        props = feature.get("properties") or {}
        raw_key = str(props.get(tile_property) or "")
        try:
            key = tuple(int(value) for value in raw_key.split(":"))
            if len(key) != 2:
                continue
        except (TypeError, ValueError):
            continue
        buckets[key].append(feature)

    for values in buckets.values():
        values.sort(key=lambda feature: str(feature.get("id") or ""))

    tile_order = order_active_tiles(buckets.keys())
    selected: list[dict] = []
    depth = 0
    unlimited = target_max == 0
    while unlimited or len(selected) < target_max:
        added_this_round = False
        for key in tile_order:
            values = buckets[key]
            if depth >= len(values):
                continue
            selected.append(values[depth])
            added_this_round = True
            if not unlimited and len(selected) >= target_max:
                break
        if not added_this_round:
            break
        depth += 1
    return selected


def polygonize_urban_blocks(road_lines: list, building_points: list,
                            core_bounds: tuple[float, float, float, float],
                            min_buildings: int = MIN_BUILDINGS) -> list[tuple[Any, int]]:
    """Polygonize an actual local street graph; keep enclosed blocks with building evidence."""
    if len(road_lines) < 4 or len(building_points) < min_buildings:
        return []
    try:
        noded = unary_union(road_lines)
        candidates = list(polygonize(noded))
    except Exception:
        return []

    points = [p if isinstance(p, Point) else Point(p) for p in building_points]
    tree = STRtree(points) if points else None
    x0, y0, x1, y1 = core_bounds
    output = []
    for raw in candidates:
        poly = polygonal(raw)
        if poly is None:
            continue
        centre = poly.representative_point()
        if not (x0 <= centre.x < x1 and y0 <= centre.y < y1):
            continue
        area = geometry_area_km2(poly)
        width, height, _ = km_dimensions(poly)
        if area < MIN_AREA_KM2 or area > MAX_AREA_KM2 or max(width, height) > MAX_SIDE_KM:
            continue
        if not tree:
            continue
        try:
            candidates_idx = tree.query(poly, predicate="intersects")
            count = len(candidates_idx)
        except Exception:
            count = sum(1 for point in points if poly.covers(point))
        if count < min_buildings:
            continue
        simplified = polygonal(poly.simplify(0.000008, preserve_topology=True))
        if simplified is not None:
            output.append((simplified, count))
    return output


def load_communes(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    communes, geometries = [], []
    for feature in data.get("features") or []:
        props = feature.get("properties") or {}
        code = str(props.get("code") or props.get("id") or feature.get("id") or "")
        if not code:
            continue
        try:
            geometry = polygonal(shape(feature["geometry"]))
        except Exception:
            geometry = None
        if geometry is None:
            continue
        communes.append({
            "code": code,
            "name": str(props.get("name_fr") or props.get("name") or ""),
            "name_ar": str(props.get("name_ar") or ""),
            "region": str(props.get("region") or ""),
            "region_code": str(props.get("region_code") or ""),
            "geometry": geometry,
        })
        geometries.append(geometry)
    if len(communes) < 1000:
        raise SystemExit(f"Refusing incomplete commune coverage: only {len(communes)} polygons")
    return communes, geometries, STRtree(geometries)


def assign_and_clip(poly, communes, commune_geometries, commune_tree):
    try:
        candidates = commune_tree.query(poly, predicate="intersects")
    except Exception:
        candidates = commune_tree.query(poly)
    winner, winner_geom, winner_area = None, None, 0.0
    for raw_index in candidates:
        index = int(raw_index)
        geometry = commune_geometries[index]
        try:
            overlap = poly.intersection(geometry).area
        except Exception:
            continue
        if overlap > winner_area:
            winner_area = overlap
            winner = communes[index]
            winner_geom = geometry
    if winner is None or winner_area <= 0:
        return None, None
    try:
        clipped = polygonal(poly.intersection(winner_geom))
    except Exception:
        return None, None
    if clipped is None:
        return None, None
    area = geometry_area_km2(clipped)
    width, height, _ = km_dimensions(clipped)
    if area < MIN_AREA_KM2 or area > MAX_AREA_KM2 or max(width, height) > MAX_SIDE_KM:
        return None, None
    return winner, clipped


def main() -> int:
    global TILE_DEGREES
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--communes", required=True, type=Path)
    parser.add_argument("--roads", required=True, type=Path, help="OSM roads as GeoJSON Text Sequence")
    parser.add_argument("--buildings", required=True, type=Path, help="OSM building polygons as GeoJSON Text Sequence")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--target-min", type=int, default=10000)
    parser.add_argument("--target-max", type=int, default=10000,
                        help="Maximum number of sectors to emit; 0 disables the cap")
    parser.add_argument("--tile-size", type=float, default=TILE_DEGREES)
    parser.add_argument("--min-buildings", type=int, default=MIN_BUILDINGS)
    parser.add_argument("--all-output", type=Path, help="Optional GeoJSON output containing all accepted blocks before sampling")
    args = parser.parse_args()
    TILE_DEGREES = args.tile_size
    if args.target_max < 0 or (args.target_max and args.target_max < args.target_min):
        raise SystemExit("--target-max must be 0 (unlimited) or greater than/equal to --target-min.")

    if not args.roads.is_file() or not args.buildings.is_file():
        raise SystemExit("Missing roads/buildings extract; refusing to create synthetic fallback geometry.")

    communes, commune_geometries, commune_tree = load_communes(args.communes)
    road_tiles: dict[tuple[int, int], list] = defaultdict(list)
    road_class_counts = Counter()
    road_count = 0

    for feature in read_geojson_seq(args.roads):
        props = feature.get("properties") or {}
        highway = str(props.get("highway") or "").lower()
        if highway not in ROAD_CLASSES:
            continue
        geom = feature.get("geometry") or {}
        kind = geom.get("type")
        lines = [geom.get("coordinates") or []] if kind == "LineString" else (
            geom.get("coordinates") or [] if kind == "MultiLineString" else []
        )
        for coords in lines:
            # Split ways into actual node-to-node segments, so every short street edge is
            # assigned to a nearby tile and road topology is retained at tile borders.
            for a, b in zip(coords, coords[1:]):
                if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)) or len(a) < 2 or len(b) < 2:
                    continue
                vals = (a[0], a[1], b[0], b[1])
                if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vals):
                    continue
                if a[0] == b[0] and a[1] == b[1]:
                    continue
                line = LineString([(float(a[0]), float(a[1])), (float(b[0]), float(b[1]))])
                if line.length > 0.03:
                    continue
                mid = line.interpolate(0.5, normalized=True)
                road_tiles[tile_key(mid.x, mid.y)].append(line)
                road_count += 1
                road_class_counts[highway] += 1

    building_tiles: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
    building_count_total = 0
    for feature in read_geojson_seq(args.buildings):
        props = feature.get("properties") or {}
        btype = props.get("building")
        if not btype or str(btype).lower() == "no":
            continue
        raw_geom = feature.get("geometry") or {}
        try:
            geom = polygonal(shape(raw_geom)) if raw_geom else None
        except Exception:
            geom = None
        if geom is None:
            continue
        point = geom.representative_point()
        building_tiles[tile_key(point.x, point.y)].append((point.x, point.y))
        building_count_total += 1

    active_tiles = order_active_tiles(
        key for key, points in building_tiles.items()
        if len(points) >= max(8, args.min_buildings * 4)
    )
    if not active_tiles:
        raise SystemExit("No building-active tiles; refusing to publish empty urban sectors.")

    output = []
    seen = set()
    per_commune = Counter()
    road_class_names = sorted(road_class_counts.keys())
    for tile_number, key in enumerate(active_tiles, 1):
        core = tile_bounds(key)
        middle_lat = (core[1] + core[3]) / 2
        # An urban block is capped at 1.25 km across, so a 1.5 km halo is
        # enough to close blocks crossing the tile border without noding a 40 km
        # neighbourhood on every tile.
        margin_lon = 1.5 / (111.32 * max(0.15, math.cos(math.radians(middle_lat)))) + 0.001
        margin_lat = 1.5 / 110.574 + 0.001
        working = (core[0] - margin_lon, core[1] - margin_lat,
                   core[2] + margin_lon, core[3] + margin_lat)
        local_keys = list(neighboring_tiles(key))
        raw_road_lines = [line for k in local_keys for line in road_tiles.get(k, [])]
        road_lines = clip_roads_to_working_bounds(raw_road_lines, working)
        wx0, wy0, wx1, wy1 = working
        local_building_coords = [
            point for k in local_keys for point in building_tiles.get(k, [])
            if wx0 <= point[0] <= wx1 and wy0 <= point[1] <= wy1
        ]
        if len(road_lines) < 12 or len(local_building_coords) < args.min_buildings:
            continue
        for poly, n_buildings in polygonize_urban_blocks(
            road_lines, local_building_coords, core, args.min_buildings
        ):
            commune, clipped = assign_and_clip(poly, communes, commune_geometries, commune_tree)
            if commune is None:
                continue
            digest = hashlib.sha1(clipped.wkb).hexdigest()[:14]
            if digest in seen:
                continue
            seen.add(digest)
            per_commune[commune["code"]] += 1
            serial = per_commune[commune["code"]]
            name = f"Îlot urbain estimé {serial:04d}"
            area = round(geometry_area_km2(clipped), 5)
            output.append({
                "type": "Feature",
                "id": f"urban-block-{commune['code']}-{digest}",
                "properties": {
                    "name": name,
                    "name_fr": name,
                    "name_ar": "",
                    "commune_id": commune["code"],
                    "commune_name": commune["name"],
                    "region": commune["region"],
                    "region_code": commune["region_code"],
                    "level": "Secteur urbain estimé (îlot borné par les rues)",
                    "boundaryKind": "urban_block",
                    "estimated": True,
                    "estimationMethod": "OSM local-road polygonization + building-footprint evidence",
                    "source": "osm_road_block",
                    "sourceDetail": "OpenStreetMap roads and building footprints; polygonized street blocks",
                    "sourceLicense": "ODbL-1.0",
                    "sourceUrl": "https://www.openstreetmap.org/copyright",
                    "buildingCount": int(n_buildings),
                    "roadClasses": road_class_names,
                    "areaKm2": area,
                    "confidence": 0.78,
                    "_samplingTile": f"{key[0]}:{key[1]}",
                },
                "geometry": mapping(clipped),
            })

    candidate_count = len(output)
    candidate_tile_count = len({
        str((feature.get("properties") or {}).get("_samplingTile") or "")
        for feature in output
    })
    if args.all_output:
        if candidate_count < args.target_min:
            raise SystemExit(
                f"Only {candidate_count} candidate blocks available; {args.target_min} required. "
                "Refusing to write incomplete candidates for neighbourhood grouping."
            )
        args.all_output.parent.mkdir(parents=True, exist_ok=True)
        args.all_output.write_text(
            json.dumps({"type": "FeatureCollection", "features": output}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        print(f"All accepted road-block candidates saved: {candidate_count} to {args.all_output}")
    output = sample_features_by_tile(output, args.target_max)
    selected_tile_count = len({
        str((feature.get("properties") or {}).get("_samplingTile") or "")
        for feature in output
    })

    # Rename the sample consecutively within each commune after the spatial sample
    # so user-facing labels do not contain gaps from rejected/unselected candidates.
    selected_per_commune = Counter()
    for feature in output:
        props = feature["properties"]
        code = str(props.get("commune_id") or "")
        selected_per_commune[code] += 1
        name = f"Îlot urbain estimé {selected_per_commune[code]:04d}"
        props["name"] = name
        props["name_fr"] = name

    per_commune = selected_per_commune

    report = [
        "# Urban block sector generation",
        "",
        "- Source roads: OpenStreetMap Geofabrik Morocco PBF, filtered to local/urban road classes.",
        "- Source buildings: OpenStreetMap building-footprint polygons.",
        "- Method: polygonize noded road segments; retain only enclosed polygons with building evidence; clip to commune boundaries.",
        "- Semantics: estimated urban blocks, not official or named neighbourhood boundaries.",
        f"- Road segments loaded: {road_count}",
        f"- Building footprints loaded: {building_count_total}",
        f"- Active building tiles: {len(active_tiles)}",
        f"- Candidate sectors before sampling: {candidate_count}",
        f"- Selected sectors after spatial sampling: {len(output)}",
        f"- Target minimum: {args.target_min}",
        f"- Target maximum: {args.target_max or 'unlimited'}",
        f"- Active tiles with candidates: {candidate_tile_count}",
        f"- Tiles represented in the sample: {selected_tile_count}",
        f"- Sampling: deterministic round-robin across tile buckets, tile order hashed by SHA1, candidates ordered by stable feature ID",
        f"- Communes represented after sampling: {len(per_commune)}",
        f"- Road class counts: {dict(road_class_counts)}",
        "",
        "## Highest coverage communes",
        "",
    ]
    for code, count in per_commune.most_common(30):
        report.append(f"- {code}: {count}")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(report) + "\n", encoding="utf-8")

    if len(output) < args.target_min:
        raise SystemExit(
            f"Only {len(output)} street-bounded sectors passed the geometry/building filters; "
            f"{args.target_min} required. Previous published data must be retained."
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"type": "FeatureCollection", "features": output}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Urban sectors accepted: {len(output)} (target {args.target_min}..{args.target_max or 'unlimited'}); "
          f"{len(per_commune)} communes; {road_count} road segments; {building_count_total} building footprints.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
