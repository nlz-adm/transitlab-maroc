#!/usr/bin/env python3
"""Assign source-mapped neighbourhood polygons to the Moroccan commune they overlap most.

This step intentionally avoids synthesizing neighbourhood polygons from place points.
Unmapped urban areas are handled separately by the road-bounded urban-block pipeline.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, mapping, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree
from shapely.validation import make_valid


def polygonal_only(geometry: Any):
    if geometry is None or geometry.is_empty:
        return None
    geometry = make_valid(geometry)
    if geometry.geom_type == "Polygon":
        return geometry if geometry.area > 0 else None
    if geometry.geom_type == "MultiPolygon":
        parts = [part for part in geometry.geoms if not part.is_empty and part.area > 0]
        if not parts:
            return None
        return MultiPolygon(parts) if len(parts) > 1 else parts[0]
    if geometry.geom_type == "GeometryCollection":
        parts = []
        for part in geometry.geoms:
            candidate = polygonal_only(part)
            if candidate is None:
                continue
            parts.extend(list(candidate.geoms) if candidate.geom_type == "MultiPolygon" else [candidate])
        if not parts:
            return None
        merged = make_valid(unary_union(parts))
        if merged.geom_type in {"Polygon", "MultiPolygon"} and not merged.is_empty:
            return merged
    return None


def approximate_area_km2(geometry: Any) -> float:
    minx, miny, maxx, maxy = geometry.bounds
    lat = (miny + maxy) / 2
    return float(geometry.area) * 111.32 * max(0.15, math.cos(math.radians(lat))) * 110.574


def prepare_communes(commune_features: list[dict]):
    communes = []
    geometries = []
    for feature in commune_features:
        props = feature.get("properties") or {}
        code = str(props.get("code") or props.get("id") or feature.get("id") or "")
        if not code:
            continue
        try:
            geometry = polygonal_only(shape(feature.get("geometry")))
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
    if not communes:
        raise ValueError("No valid commune polygons found")
    return communes, geometries, STRtree(geometries)


def assign_mapped_polygons(features: list[dict], commune_features: list[dict]):
    """Keep only genuine source polygons and clip each to its best-overlap commune."""
    communes, commune_geometries, tree = prepare_communes(commune_features)
    output = []
    stats = {
        "input": len(features),
        "accepted": 0,
        "skip_estimated": 0,
        "skip_name": 0,
        "skip_geometry": 0,
        "skip_commune": 0,
        "clipped": 0,
    }

    for feature in features:
        props = feature.get("properties") or {}
        # Older published mapped records may lack boundaryKind but still carry estimated=false.
        if props.get("estimated") is True:
            stats["skip_estimated"] += 1
            continue
        name = str(props.get("name_fr") or props.get("name") or props.get("name_ar") or "").strip()
        if not name:
            stats["skip_name"] += 1
            continue
        try:
            geometry = polygonal_only(shape(feature.get("geometry")))
        except Exception:
            geometry = None
        if geometry is None:
            stats["skip_geometry"] += 1
            continue

        try:
            candidate_indices = tree.query(geometry, predicate="intersects")
        except Exception:
            candidate_indices = tree.query(geometry)
        winner_index = None
        winner_area = 0.0
        for candidate in candidate_indices:
            idx = int(candidate)
            try:
                overlap = geometry.intersection(commune_geometries[idx]).area
            except Exception:
                continue
            if overlap > winner_area:
                winner_area = overlap
                winner_index = idx
        if winner_index is None or winner_area <= 0:
            stats["skip_commune"] += 1
            continue

        commune = communes[winner_index]
        commune_geom = commune_geometries[winner_index]
        try:
            clipped = polygonal_only(geometry.intersection(commune_geom))
        except Exception:
            clipped = None
        if clipped is None:
            stats["skip_geometry"] += 1
            continue
        if clipped.area < geometry.area * 0.05:
            stats["skip_commune"] += 1
            continue
        if clipped.area < geometry.area * 0.999999:
            stats["clipped"] += 1

        converted = dict(props)
        converted.update({
            "name": name,
            "name_fr": str(props.get("name_fr") or props.get("name") or name),
            "name_ar": str(props.get("name_ar") or ""),
            "commune_id": commune["code"],
            "commune_name": commune["name"],
            "region": commune["region"],
            "region_code": commune["region_code"],
            "level": str(props.get("level") or "Quartier cartographié"),
            "boundaryKind": "mapped",
            "estimated": False,
            "areaKm2": round(approximate_area_km2(clipped), 5),
        })
        output.append({
            "type": "Feature",
            "id": feature.get("id") or props.get("sourceId") or "",
            "properties": converted,
            "geometry": mapping(clipped),
        })
        stats["accepted"] += 1

    return {"type": "FeatureCollection", "features": output}, stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--communes", required=True, type=Path)
    parser.add_argument("--mapped-seeds", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--min-polygons", type=int, default=30)
    args = parser.parse_args()

    communes_doc = json.loads(args.communes.read_text(encoding="utf-8"))
    seeds_doc = json.loads(args.mapped_seeds.read_text(encoding="utf-8"))
    communes = communes_doc.get("features") or []
    if len(communes) < 1000:
        raise SystemExit(f"Refusing incomplete commune input: only {len(communes)} polygons")

    output, stats = assign_mapped_polygons(seeds_doc.get("features") or [], communes)
    lines = [
        "# Mapped neighbourhood polygon assignment",
        "",
        "- Input geometry: source-mapped OSM/Overture polygons only; no point-to-Voronoi conversion.",
        "- Commune assignment: maximum polygon overlap, then intersection-clipping to that commune.",
        f"- Input source polygons: {stats['input']}",
        f"- Accepted mapped polygons: {stats['accepted']}",
        f"- Polygons clipped to commune: {stats['clipped']}",
        f"- Rejected as estimated: {stats['skip_estimated']}",
        f"- Rejected (missing name): {stats['skip_name']}",
        f"- Rejected (invalid/non-polygon geometry): {stats['skip_geometry']}",
        f"- Rejected (no usable commune overlap): {stats['skip_commune']}",
        "",
        "Only actual mapped polygons are emitted; urban coverage gaps are handled by the separate road-bounded sector layer.",
    ]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if stats["accepted"] < args.min_polygons:
        raise SystemExit(
            f"Only {stats['accepted']} mapped polygons passed assignment; {args.min_polygons} required. "
            "Existing published data is preserved."
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    print(f"Assigned {stats['accepted']} mapped neighbourhood polygons to communes; "
          f"{stats['clipped']} clipped to commune limits.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
