#!/usr/bin/env python3
"""Extract actual named neighbourhood polygons from an OSM GeoJSONSeq export."""
from __future__ import annotations
import argparse, json, math, re, unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from shapely.geometry import MultiPolygon, shape, mapping
from shapely.validation import make_valid

PLACE_TYPES = {"quarter", "suburb", "neighbourhood", "neighborhood", "microhood", "macrohood"}
NON_NEIGHBORHOOD_PLACES = {"city", "town", "village", "hamlet", "municipality", "county", "state", "region", "country"}
MIN_AREA_KM2, MAX_AREA_KM2 = 0.00005, 150.0
DUPLICATE_IOU, DUPLICATE_CONTAINMENT, DUPLICATE_AREA_RATIO = 0.82, 0.94, 1.30


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
    minx, miny, maxx, maxy = geometry.bounds
    lat = (miny + maxy) / 2
    return float(geometry.area) * 111.32 * max(0.15, math.cos(math.radians(lat))) * 110.574


def read_geojsonseq(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            line = line.strip().lstrip("\x1e").strip()
            if not line:
                continue
            try:
                feature = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid GeoJSONSeq record at {path}:{number}: {exc}") from exc
            if isinstance(feature, dict) and feature.get("type") == "Feature":
                yield feature


def flatten_properties(feature: dict) -> dict:
    props = dict(feature.get("properties") or {})
    if isinstance(props.get("tags"), dict):
        props.update(props["tags"])
    return props


def osm_feature_to_neighbourhood(feature: dict, stats: Counter) -> dict | None:
    props = flatten_properties(feature)
    place = str(props.get("place") or "").strip().casefold()
    boundary = str(props.get("boundary") or "").strip().casefold()
    if place not in PLACE_TYPES and boundary != "place":
        stats["skip_not_neighborhood_tag"] += 1
        return None
    if place in NON_NEIGHBORHOOD_PLACES:
        stats["skip_settlement_or_admin"] += 1
        return None
    name = str(props.get("name:fr") or props.get("name_fr") or props.get("name")
                or props.get("name:en") or props.get("name:ar") or props.get("name_ar") or "").strip()
    if len(name) < 2:
        stats["skip_name"] += 1
        return None
    geom_doc = feature.get("geometry") or {}
    if geom_doc.get("type") not in {"Polygon", "MultiPolygon"}:
        stats["skip_non_polygon"] += 1
        return None
    try:
        geom = polygonal(shape(geom_doc))
    except Exception:
        geom = None
    if geom is None:
        stats["skip_invalid_geometry"] += 1
        return None
    size = area_km2(geom)
    if not math.isfinite(size) or size < MIN_AREA_KM2:
        stats["skip_too_small"] += 1
        return None
    if size > MAX_AREA_KM2:
        stats["skip_too_large"] += 1
        return None

    source_id = str(props.get("@id") or props.get("osm_id") or feature.get("id") or "").strip()
    subtype = place or "boundary=place"
    label = {"quarter":"quartier", "neighbourhood":"quartier", "neighborhood":"quartier",
             "suburb":"suburbain", "microhood":"micro-quartier", "macrohood":"macro-quartier"}.get(place, "quartier cartographié")
    stats["accepted_osm_polygons"] += 1
    stats[f"accepted_place_{place or 'boundary_place'}"] += 1
    return {
        "type": "Feature",
        "id": "osm-" + source_id if source_id else "",
        "properties": {
            "name": name, "name_fr": str(props.get("name:fr") or props.get("name_fr") or name),
            "name_ar": str(props.get("name:ar") or props.get("name_ar") or ""),
            "source": "osm", "sourceDetail": "OpenStreetMap named neighbourhood polygon",
            "sourceId": source_id, "sourceSubtype": subtype,
            "sourceLicense": "ODbL-1.0", "sourceUrl": "https://www.openstreetmap.org/copyright",
            "level": f"Quartier cartographié OSM ({label})",
            "estimated": False, "boundaryKind": "mapped", "areaKm2": round(size, 5),
            "sourceAgreement": 1, "confidence": 0.88,
        },
        "geometry": mapping(geom),
    }


def is_same_geometry(a: Any, b: Any, names_match: bool) -> bool:
    if a is None or b is None or a.is_empty or b.is_empty or not a.envelope.intersects(b.envelope):
        return False
    aa, ab = a.area, b.area
    if min(aa, ab) <= 0 or max(aa, ab) / min(aa, ab) > DUPLICATE_AREA_RATIO:
        return False
    try:
        inter = a.intersection(b).area
    except Exception:
        return False
    containment = inter / min(aa, ab)
    union_area = aa + ab - inter
    iou = inter / union_area if union_area > 0 else 0
    return (iou >= 0.72 or containment >= DUPLICATE_CONTAINMENT) if names_match else iou >= DUPLICATE_IOU


def merge_osm_polygons(existing: list[dict], osm_features: Iterable[dict]):
    output, parsed, stats = list(existing), [], Counter()
    for feature in existing:
        props = feature.get("properties") or {}
        try:
            geom = polygonal(shape(feature.get("geometry")))
        except Exception:
            geom = None
        if geom is not None:
            parsed.append((geom, fold_name(props.get("name_fr") or props.get("name") or props.get("name_ar")), str(props.get("sourceId") or "")))

    for feature in osm_features:
        converted = osm_feature_to_neighbourhood(feature, stats)
        if converted is None:
            continue
        props = converted["properties"]
        geom = shape(converted["geometry"])
        source_id, name = str(props.get("sourceId") or ""), fold_name(props.get("name_fr") or props.get("name"))
        duplicate = False
        for old_geom, old_name, old_id in parsed:
            if (source_id and source_id == old_id) or is_same_geometry(geom, old_geom, bool(name and name == old_name)):
                duplicate = True
                break
        if not duplicate:
            for old in output[len(existing):]:
                try:
                    old_geom = shape(old["geometry"])
                except Exception:
                    continue
                old_props = old.get("properties") or {}
                old_name = fold_name(old_props.get("name_fr") or old_props.get("name"))
                if is_same_geometry(geom, old_geom, bool(name and name == old_name)):
                    duplicate = True
                    break
        if duplicate:
            stats["deduplicated_osm_polygons"] += 1
            continue
        output.append(converted)
        parsed.append((geom, name, source_id))
        stats["new_osm_polygons"] += 1
    return {"type": "FeatureCollection", "features": output}, stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--existing", required=True, type=Path)
    parser.add_argument("--osm-geojsonseq", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    doc = json.loads(args.existing.read_text(encoding="utf-8"))
    existing = [f for f in doc.get("features", [])
                if (f.get("properties") or {}).get("boundaryKind") == "mapped"
                and (f.get("properties") or {}).get("estimated") is False
                and (f.get("geometry") or {}).get("type") in {"Polygon", "MultiPolygon"}]
    if len(existing) < 30:
        raise SystemExit(f"Only {len(existing)} existing mapped polygons; refusing incomplete baseline.")
    output, stats = merge_osm_polygons(existing, read_geojsonseq(args.osm_geojsonseq))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    lines = [
        "# OSM neighbourhood polygon enrichment", "",
        "- Source: current Morocco OpenStreetMap PBF extract via Geofabrik.",
        "- Accepted tags: named place=quarter/suburb/neighbourhood/microhood/macrohood polygons and named boundary=place polygons.",
        "- Only source-mapped Polygon/MultiPolygon features are accepted; points and lines are never converted to areas.",
        "- City/village/admin boundaries and generic settlement tags are not promoted to neighbourhood polygons.",
        f"- Area filter: {MIN_AREA_KM2} to {MAX_AREA_KM2} km².",
        "- Duplicate rule: geometry overlap plus area similarity; shared name alone is not sufficient.",
        f"- Existing mapped polygons: {len(existing)}",
        f"- New mapped OSM neighbourhood polygons: {stats['new_osm_polygons']}",
        f"- Duplicate OSM polygons skipped: {stats['deduplicated_osm_polygons']}",
        f"- Output candidates before commune assignment: {len(output)}", "",
        "## OSM candidate audit", "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(stats.items()))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"OSM neighbourhood polygons added: {stats['new_osm_polygons']}; duplicates skipped: "
          f"{stats['deduplicated_osm_polygons']}; mapped candidate total: {len(output)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
