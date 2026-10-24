#!/usr/bin/env python3
"""Create a separately labelled estimated neighbourhood layer from contiguous OSM road blocks."""
from __future__ import annotations
import argparse, hashlib, heapq, json, math, re, unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable
from shapely.geometry import MultiPolygon, Point, shape, mapping
from shapely.ops import unary_union
from shapely.strtree import STRtree
from shapely.validation import make_valid

PLACE_RANK = {"quarter":4,"neighbourhood":4,"neighborhood":4,"microhood":4,"macrohood":4,"suburb":2}
MAX_ZONE_AREA_KM2 = 1.2
MIN_SHARED_EDGE_DEGREES = 0.000005


def fold_name(value: Any) -> str:
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.replace("’", "'").replace(chr(96), "'")
    return re.sub(r"[^\w\u0600-\u06ff]+", " ", value, flags=re.UNICODE).strip()


def polygonal(geometry: Any):
    if geometry is None or geometry.is_empty:
        return None
    try:
        geometry = make_valid(geometry)
    except Exception:
        return None
    if geometry.geom_type == "Polygon":
        return geometry if geometry.area > 0 else None
    if geometry.geom_type == "MultiPolygon":
        parts = [p for p in geometry.geoms if not p.is_empty and p.area > 0]
        if not parts:
            return None
        return MultiPolygon(parts) if len(parts) > 1 else parts[0]
    return None


def area_km2(geometry: Any) -> float:
    _, miny, _, maxy = geometry.bounds
    lat = (miny + maxy) / 2
    return float(geometry.area) * 111.32 * max(0.15, math.cos(math.radians(lat))) * 110.574


def geojsonseq(path: Path | None) -> Iterable[dict]:
    if path is None or not path.is_file():
        return
    with path.open("r", encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            line = line.strip().lstrip("\x1e").strip()
            if not line:
                continue
            try:
                feature = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid GeoJSONSeq record at {path}:{line_no}: {exc}") from exc
            if isinstance(feature, dict) and feature.get("type") == "Feature":
                yield feature


def flatten_properties(feature: dict) -> dict:
    props = dict(feature.get("properties") or {})
    tags = props.get("tags")
    if isinstance(tags, dict):
        props.update(tags)
    return props


def collect_place_points(features: Iterable[dict]) -> list[dict]:
    places = []
    for feature in features:
        props = flatten_properties(feature)
        if (feature.get("geometry") or {}).get("type") != "Point":
            continue
        place = str(props.get("place") or "").strip().casefold()
        boundary = str(props.get("boundary") or "").strip().casefold()
        rank = PLACE_RANK.get(place, 1 if boundary == "place" else 0)
        if rank <= 0:
            continue
        name = str(props.get("name:fr") or props.get("name_fr") or props.get("name")
                    or props.get("name:en") or props.get("name:ar") or props.get("name_ar") or "").strip()
        if len(name) < 2:
            continue
        try:
            point = shape(feature["geometry"])
        except Exception:
            continue
        if point.is_empty or not isinstance(point, Point):
            continue
        places.append({"name":name,"key":fold_name(name),"rank":rank,
                       "sourceId":str(props.get("@id") or feature.get("id") or ""),
                       "geometry":point})
    return places


def select_valid_blocks(block_features: list[dict], mapped_features: list[dict]):
    mapped_geometries = []
    for feature in mapped_features:
        props = feature.get("properties") or {}
        if props.get("boundaryKind") != "mapped" or props.get("estimated") is not False:
            continue
        try:
            geom = polygonal(shape(feature.get("geometry")))
        except Exception:
            geom = None
        if geom is not None:
            mapped_geometries.append(geom)
    mapped_tree = STRtree(mapped_geometries) if mapped_geometries else None
    valid, valid_geometries, stats = [], [], Counter()
    for feature in block_features:
        props = feature.get("properties") or {}
        if props.get("boundaryKind") != "urban_block" or props.get("estimated") is not True:
            stats["skip_not_urban_block"] += 1
            continue
        if (feature.get("geometry") or {}).get("type") not in {"Polygon","MultiPolygon"}:
            stats["skip_not_polygon"] += 1
            continue
        try:
            geom = polygonal(shape(feature["geometry"]))
        except Exception:
            geom = None
        if geom is None:
            stats["skip_invalid_geometry"] += 1
            continue
        if not props.get("commune_id") or not props.get("commune_name"):
            stats["skip_missing_commune"] += 1
            continue
        if mapped_tree is not None:
            point = geom.representative_point()
            try:
                candidates = mapped_tree.query(point, predicate="within")
            except Exception:
                candidates = mapped_tree.query(point)
            if any(mapped_geometries[int(index)].covers(point) for index in candidates):
                stats["skip_inside_mapped_neighborhood"] += 1
                continue
        valid.append(dict(feature))
        valid_geometries.append(geom)
    return valid, valid_geometries, stats


class DisjointSet:
    def __init__(self, count: int, areas: list[float]):
        self.parent = list(range(count))
        self.size = [1] * count
        self.area = list(areas)
        self.components = count

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, a: int, b: int, area_cap: float) -> bool:
        a, b = self.find(a), self.find(b)
        if a == b or self.area[a] + self.area[b] > area_cap:
            return False
        if self.size[a] < self.size[b]:
            a, b = b, a
        self.parent[b] = a
        self.size[a] += self.size[b]
        self.area[a] += self.area[b]
        self.components -= 1
        return True


def assign_groups(geometries: list, props: list[dict], target: int, area_cap: float = MAX_ZONE_AREA_KM2):
    count = len(geometries)
    areas = [max(area_km2(geom), 1e-9) for geom in geometries]
    dsu = DisjointSet(count, areas)
    tree = STRtree(geometries)
    edges = []
    commune_ids = [str((p or {}).get("commune_id") or "") for p in props]
    for i, geom in enumerate(geometries):
        try:
            neighbours = tree.query(geom, predicate="touches")
        except Exception:
            neighbours = tree.query(geom)
        for raw_j in neighbours:
            j = int(raw_j)
            if j <= i or commune_ids[i] != commune_ids[j]:
                continue
            try:
                shared = geom.boundary.intersection(geometries[j].boundary).length
            except Exception:
                continue
            if shared < MIN_SHARED_EDGE_DEGREES:
                continue
            score = shared / math.sqrt(max(areas[i] * areas[j], 1e-12))
            if score > 0:
                heapq.heappush(edges, (-score, -score, -shared, i, j))

    merge_operations = 0
    initial_edges = len(edges)
    while dsu.components > target and edges:
        neg_priority, neg_base_score, neg_shared, i, j = heapq.heappop(edges)
        ri, rj = dsu.find(i), dsu.find(j)
        if ri == rj:
            continue
        base_score, shared = -neg_base_score, -neg_shared
        current_score = base_score / math.sqrt(max(dsu.size[ri] * dsu.size[rj], 1))
        if not math.isclose(-neg_priority, current_score, rel_tol=1e-8, abs_tol=1e-12):
            heapq.heappush(edges, (-current_score, neg_base_score, neg_shared, i, j))
            continue
        if dsu.union(ri, rj, area_cap):
            merge_operations += 1

    members: dict[int,list[int]] = defaultdict(list)
    for idx in range(count):
        members[dsu.find(idx)].append(idx)
    groups = list(members.values())
    dropped = 0
    if len(groups) > target:
        buckets: dict[tuple[int,int],list[list[int]]] = defaultdict(list)
        for group in groups:
            points = [geometries[i].representative_point() for i in group]
            lon = sum(p.x for p in points) / len(points)
            lat = sum(p.y for p in points) / len(points)
            key = (math.floor((lon+180.0)/0.12), math.floor((lat+90.0)/0.12))
            buckets[key].append(group)
        tile_order = sorted(buckets, key=lambda k: hashlib.sha1(f"{k[0]}:{k[1]}".encode("ascii")).digest())
        for values in buckets.values():
            values.sort(key=lambda g:min(str(props[i].get("id") or "") for i in g))
        selected, depth = [], 0
        while len(selected) < target:
            added = False
            for key in tile_order:
                values = buckets[key]
                if depth < len(values):
                    selected.append(values[depth])
                    added = True
                    if len(selected) >= target:
                        break
            if not added:
                break
            depth += 1
        dropped = len(groups) - len(selected)
        groups = selected
    if len(groups) != target:
        raise ValueError(f"Impossible d'obtenir exactement {target} zones : {len(groups)} groupes restants.")
    return groups, {
        "input_blocks":count,"candidate_edges":initial_edges,"merge_operations":merge_operations,
        "groups_before_disconnected_sampling":len(members),"groups_dropped_disconnected":dropped,
        "output_groups":len(groups),"area_cap_km2":area_cap,
    }


def add_place_names(zones: list[dict], places: list[dict]) -> int:
    if not places or not zones:
        return 0
    geoms = [shape(f["geometry"]) for f in zones]
    tree = STRtree(geoms)
    names_by_zone: dict[int,list[dict]] = defaultdict(list)
    for place in places:
        try:
            indices = tree.query(place["geometry"], predicate="within")
        except Exception:
            indices = tree.query(place["geometry"])
        for raw_index in indices:
            idx = int(raw_index)
            if geoms[idx].covers(place["geometry"]):
                names_by_zone[idx].append(place)
                break
    named = 0
    for idx, zone in enumerate(zones):
        candidates = names_by_zone.get(idx, [])
        if not candidates:
            continue
        best_rank = max(item["rank"] for item in candidates)
        top = [item for item in candidates if item["rank"] == best_rank]
        if len({item["key"] for item in top}) != 1:
            zone["properties"]["candidatePlaceNames"] = sorted({item["name"] for item in top})[:8]
            continue
        chosen = sorted(top, key=lambda item:(item["key"],item["sourceId"]))[0]
        zone["properties"].update({
            "name":chosen["name"],"name_fr":chosen["name"],
            "nameSource":"OpenStreetMap named place point","nameSourceId":chosen["sourceId"],
            "nameSourceSubtype":chosen["rank"],
        })
        named += 1
    return named


def build_zones(block_features: list[dict], mapped_features: list[dict],
                place_features: Iterable[dict], target: int = 10000,
                area_cap_km2: float = MAX_ZONE_AREA_KM2):
    if target < 1:
        raise ValueError("target doit être positif")
    blocks, geometries, filter_stats = select_valid_blocks(block_features, mapped_features)
    if len(blocks) < target:
        raise ValueError(f"Seulement {len(blocks)} îlots utilisables ; {target} zones demandées.")
    props = [f.get("properties") or {} for f in blocks]
    groups, group_stats = assign_groups(geometries, props, target, area_cap_km2)
    place_points = collect_place_points(place_features)
    zones, region_counts, communes = [], Counter(), set()
    for serial, indices in enumerate(groups, start=1):
        member_features = [blocks[i] for i in indices]
        merged = polygonal(unary_union([geometries[i] for i in indices]))
        if merged is None:
            raise ValueError(f"Groupe {serial} sans géométrie polygonale valide")
        first = props[indices[0]]
        commune_id, commune_name = str(first.get("commune_id") or ""), str(first.get("commune_name") or "")
        region, region_code = str(first.get("region") or ""), str(first.get("region_code") or "")
        block_ids = sorted(str(f.get("id") or "") for f in member_features)
        digest = hashlib.sha1("|".join(block_ids).encode("utf-8")).hexdigest()[:14]
        name = f"Zone de quartier estimée {serial:05d}"
        area = area_km2(merged)
        if area > area_cap_km2 * 1.05:
            raise ValueError(f"Zone {serial} dépasse le plafond d'aire : {area:.3f} km²")
        zones.append({
            "type":"Feature","id":f"estimated-neighborhood-{commune_id}-{digest}",
            "properties":{
                "name":name,"name_fr":name,"name_ar":"",
                "commune_id":commune_id,"commune_name":commune_name,"region":region,"region_code":region_code,
                "level":"Unité de quartier estimée","boundaryKind":"estimated_neighborhood","estimated":True,
                "estimationMethod":"dissolution de blocs routiers OSM contigus, groupés par commune",
                "source":"osm_road_block_cluster",
                "sourceDetail":"Contiguous street-bounded blocks from OSM roads and building footprints, dissolved to neighbourhood-scale units",
                "sourceLicense":"ODbL-1.0","sourceUrl":"https://www.openstreetmap.org/copyright",
                "blockCount":len(indices),"sourceBlockIdsHash":digest,
                "sourceTileCount":len({str((f.get("properties") or {}).get("_samplingTile") or "") for f in member_features}),
                "areaKm2":round(area,5),"confidence":0.62,
            },
            "geometry":mapping(merged),
        })
        region_counts[region_code] += 1
        communes.add(commune_id)
    named = add_place_names(zones, place_points)
    stats = {
        **filter_stats,**group_stats,"input_block_features":len(block_features),
        "source_mapped_neighborhoods":sum(
            1 for f in mapped_features
            if (f.get("properties") or {}).get("boundaryKind")=="mapped"
            and (f.get("properties") or {}).get("estimated") is False
        ),
        "place_points_considered":len(place_points),"zones_with_unambiguous_osm_place_names":named,
        "communes_covered":len(communes),"regions_covered":len({(f.get("properties") or {}).get("region_code") for f in zones}),
        "zones_by_region":dict(sorted(region_counts.items())),
        "mean_blocks_per_zone":round(sum(int((f.get("properties") or {}).get("blockCount") or 0) for f in zones)/len(zones),3),
        "median_blocks_per_zone":sorted(int((f.get("properties") or {}).get("blockCount") or 0) for f in zones)[len(zones)//2],
        "max_zone_area_km2":round(max(float((f.get("properties") or {}).get("areaKm2") or 0) for f in zones),5),
    }
    return {"type":"FeatureCollection","features":zones},stats


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks",required=True,type=Path)
    parser.add_argument("--mapped-boundaries",required=True,type=Path)
    parser.add_argument("--osm-geojsonseq",type=Path,default=None)
    parser.add_argument("--output",required=True,type=Path)
    parser.add_argument("--report",required=True,type=Path)
    parser.add_argument("--target",type=int,default=10000)
    parser.add_argument("--max-area-km2",type=float,default=MAX_ZONE_AREA_KM2)
    args=parser.parse_args()
    if args.target<1 or args.max_area_km2<=0:
        raise SystemExit("--target and --max-area-km2 must be positive")
    blocks_doc=json.loads(args.blocks.read_text(encoding="utf-8"))
    mapped_doc=json.loads(args.mapped_boundaries.read_text(encoding="utf-8"))
    candidates=list(geojsonseq(args.osm_geojsonseq))
    try:
        zones,stats=build_zones(blocks_doc.get("features") or [],mapped_doc.get("features") or [],candidates,args.target,args.max_area_km2)
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(zones,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
    lines=[
        "# Estimated neighbourhood-scale zones from OSM street blocks","",
        "- These are estimated neighbourhood-scale zones, not official or named administrative neighbourhood boundaries.",
        "- Input: OSM street-bounded block polygons with building-footprint evidence.",
        "- Method: dissolve contiguous blocks along shared road edges; groups never cross commune boundaries.",
        "- Source-mapped neighbourhood polygons remain separate; block cores covered by a mapped neighbourhood are excluded.",
        "- OSM names are borrowed only from an unambiguous named neighbourhood/suburb point inside a zone. The polygon remains estimated.",
        "- Generic zone names are identifiers, not real neighbourhood names.",
        f"- Maximum block-area sum per group: {args.max_area_km2:.2f} km².",
        f"- Requested zones: {args.target}",f"- Published estimated zones: {len(zones.get('features') or [])}",
        f"- Candidate block polygons: {stats['input_block_features']}",
        f"- Candidate blocks excluded inside mapped neighbourhoods: {stats.get('skip_inside_mapped_neighborhood',0)}",
        f"- Blocks used for grouping: {stats['input_blocks']}",f"- Contiguous merge operations: {stats['merge_operations']}",
        f"- Disconnected groups dropped to meet exact target: {stats['groups_dropped_disconnected']}",
        f"- Zones with unambiguous OSM point-derived name: {stats['zones_with_unambiguous_osm_place_names']}",
        f"- Communes covered: {stats['communes_covered']}",f"- Regions covered: {stats['regions_covered']}",
        f"- Mean blocks per zone: {stats['mean_blocks_per_zone']}",f"- Median blocks per zone: {stats['median_blocks_per_zone']}",
        f"- Maximum zone area: {stats['max_zone_area_km2']} km²","","## Zone counts by region","",
    ]
    lines.extend(f"- {code}: {count}" for code,count in stats["zones_by_region"].items())
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text("\n".join(lines)+"\n",encoding="utf-8")
    print(f"Estimated neighbourhood-scale zones: {len(zones['features'])} (target {args.target}), "
          f"{stats['communes_covered']} communes, {stats['zones_with_unambiguous_osm_place_names']} named from OSM place points.")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
