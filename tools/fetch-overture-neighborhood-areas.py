#!/usr/bin/env python3
"""Fetch source-mapped Overture neighbourhood polygons and merge them with existing mapped boundaries.

This intentionally does not turn division points into polygons. Only Overture
division_area geometries of neighbourhood-like subtypes are admitted.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, mapping, shape
from shapely.ops import unary_union
from shapely.validation import make_valid

ALLOWED_SUBTYPES = {"neighborhood", "microhood", "macrohood"}
ALLOWED_COUNTRIES = {"MA", "MAR", "MOROCCO"}
OVERTURE_DOCS = "https://docs.overturemaps.org/guides/divisions/"
ODBL_URL = "https://opendatacommons.org/licenses/odbl/1-0/"


def normalize_name(value: Any) -> str:
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9\u0600-\u06ff]+", " ", value).strip()


def polygonal_only(geometry: Any) -> Any | None:
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
            if candidate.geom_type == "Polygon":
                parts.append(candidate)
            elif candidate.geom_type == "MultiPolygon":
                parts.extend(candidate.geoms)
        if not parts:
            return None
        merged = make_valid(unary_union(parts))
        return merged if merged.geom_type in {"Polygon", "MultiPolygon"} and not merged.is_empty else None
    return None


def area_km2(geometry: Any) -> float:
    centroid = geometry.representative_point()
    km_per_degree_lon = 111.32 * max(0.15, math.cos(math.radians(centroid.y)))
    return float(geometry.area * km_per_degree_lon * 110.574)


def primary_name(properties: dict) -> str:
    names = properties.get("names") or {}
    if isinstance(names, dict):
        for key in ("primary", "common", "name"):
            value = names.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for key in ("name", "name:fr", "name_fr", "name:ar", "name_ar"):
        value = properties.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def country_code(properties: dict) -> str:
    value = properties.get("country") or properties.get("country_code") or properties.get("countryCode") or ""
    if isinstance(value, dict):
        value = value.get("iso_3166_1_alpha_2") or value.get("code") or ""
    return str(value).strip().upper()


def source_datasets(properties: dict) -> list[str]:
    value = properties.get("sources") or []
    output = []
    if isinstance(value, dict):
        value = [value]
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            dataset = item.get("dataset") or item.get("property") or item.get("provider")
            if dataset:
                output.append(str(dataset))
        elif item:
            output.append(str(item))
    return sorted(set(output))


def normalize_overture_features(collection: dict, country_geometry: Any, fetched_at: str) -> tuple[list[dict], dict]:
    """Filter, validate, and clip Overture GeoJSON features to the Moroccan admin union."""
    output: list[dict] = []
    counts = {
        "input": len(collection.get("features", [])),
        "accepted": 0,
        "skip_subtype": 0,
        "skip_country": 0,
        "skip_name": 0,
        "skip_geometry": 0,
        "skip_area": 0,
    }
    seen = set()
    country_geometry = make_valid(country_geometry)

    for feature in collection.get("features", []):
        props = feature.get("properties") or {}
        subtype = str(props.get("subtype") or "").casefold()
        if subtype not in ALLOWED_SUBTYPES:
            counts["skip_subtype"] += 1
            continue

        country = country_code(props)
        region = str(props.get("region") or "").upper()
        if country not in ALLOWED_COUNTRIES and not (not country and region.startswith("MA-")):
            counts["skip_country"] += 1
            continue

        name = primary_name(props)
        if not name:
            counts["skip_name"] += 1
            continue

        raw_geometry = feature.get("geometry")
        try:
            geometry = polygonal_only(shape(raw_geometry)) if raw_geometry else None
        except Exception:
            geometry = None
        if geometry is None:
            counts["skip_geometry"] += 1
            continue

        try:
            original_area = geometry.area
            clipped = polygonal_only(geometry.intersection(country_geometry))
        except Exception:
            clipped = None
        if clipped is None or original_area <= 0:
            counts["skip_geometry"] += 1
            continue
        retained_ratio = clipped.area / original_area
        # Avoid importing neighbouring-country polygons merely because the query bbox
        # crosses a border. Keep a polygon only when most of it lies in Morocco.
        if retained_ratio < 0.80:
            counts["skip_country"] += 1
            continue

        size_km2 = area_km2(clipped)
        # A feature typed as a neighbourhood should not be an entire city-sized region.
        if size_km2 < 0.0001 or size_km2 > 250:
            counts["skip_area"] += 1
            continue

        overture_id = str(props.get("id") or feature.get("id") or "")
        signature = (overture_id or f"{subtype}:{normalize_name(name)}:{clipped.wkb_hex[:32]}")
        if signature in seen:
            continue
        seen.add(signature)

        names = props.get("names") or {}
        name_fr = (
            props.get("name:fr")
            or props.get("name_fr")
            or (names.get("fr") if isinstance(names, dict) else "")
            or ""
        )
        name_ar = (
            props.get("name:ar")
            or props.get("name_ar")
            or (names.get("ar") if isinstance(names, dict) else "")
            or ""
        )
        source_ref = ""
        for source in props.get("sources") or []:
            if isinstance(source, dict) and source.get("record_id"):
                source_ref = str(source["record_id"])
                break

        output.append({
            "type": "Feature",
            "id": overture_id or signature,
            "properties": {
                "name": name,
                "name_fr": str(name_fr or name),
                "name_ar": str(name_ar or ""),
                "source": "overture",
                "sourceDetail": "Overture Maps division_area",
                "sourceId": overture_id,
                "sourceSubtype": subtype,
                "sourceRef": source_ref,
                "sourceLicense": "ODbL-1.0",
                "sourceUrl": OVERTURE_DOCS,
                "sourceRelease": "latest-at-fetch",
                "sourceFetchedAt": fetched_at,
                "upstreamSources": source_datasets(props),
                "country": "MA",
                "estimated": False,
                "boundaryKind": "mapped",
                "areaKm2": round(size_km2, 4),
                "sourceAgreement": 1,
                "confidence": 0.9,
            },
            "geometry": mapping(clipped),
        })
        counts["accepted"] += 1

    return output, counts


def duplicate_ratio(a: Any, b: Any) -> float:
    if a is None or b is None or a.is_empty or b.is_empty:
        return 0.0
    if not a.envelope.intersects(b.envelope):
        return 0.0
    denom = min(a.area, b.area)
    if denom <= 0:
        return 0.0
    try:
        return a.intersection(b).area / denom
    except Exception:
        return 0.0


def merge_mapped_features(existing: list[dict], supplement: list[dict]) -> tuple[list[dict], int]:
    """Keep existing mapped data and prefer fresh Overture geometry for an equivalent named polygon."""
    merged = list(existing)
    added = 0
    parsed_existing = []
    for feature in existing:
        props = feature.get("properties") or {}
        try:
            geom = polygonal_only(shape(feature["geometry"]))
        except Exception:
            geom = None
        parsed_existing.append((
            normalize_name(props.get("name_fr") or props.get("name") or props.get("name_ar")),
            geom,
            props.get("sourceSubtype")
        ))

    for feature in supplement:
        props = feature["properties"]
        name = normalize_name(props.get("name_fr") or props.get("name") or props.get("name_ar"))
        try:
            geometry = polygonal_only(shape(feature["geometry"]))
        except Exception:
            geometry = None
        if geometry is None:
            continue
        duplicate_at = None
        for i, (old_name, old_geom, old_subtype) in enumerate(parsed_existing):
            if old_geom is None:
                continue
            same_name = name and name == old_name
            same_subtype = not old_subtype or old_subtype == props.get("sourceSubtype")
            if same_subtype and same_name and duplicate_ratio(geometry, old_geom) >= 0.70:
                duplicate_at = i
                break
        if duplicate_at is not None:
            merged[duplicate_at] = feature
            parsed_existing[duplicate_at] = (name, geometry, props.get("sourceSubtype"))
            continue
        merged.append(feature)
        parsed_existing.append((name, geometry, props.get("sourceSubtype")))
        added += 1
    return merged, added


def load_commune_union(path: Path) -> tuple[Any, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    features = data.get("features") or []
    if not features:
        raise ValueError(f"No commune polygon features in {path}")
    geometries = []
    for feature in features:
        try:
            geometry = polygonal_only(shape(feature.get("geometry")))
            if geometry is not None:
                geometries.append(geometry)
        except Exception:
            continue
    if len(geometries) < 100:
        raise ValueError(f"Refusing incomplete Morocco boundary union: only {len(geometries)} polygons")
    return make_valid(unary_union(geometries)), features


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--communes", required=True, help="GeoJSON FeatureCollection of Morocco commune boundaries")
    parser.add_argument("--existing", required=True, help="GeoJSON FeatureCollection of existing mapped neighbourhood polygons")
    parser.add_argument("--output", required=True, help="Merged GeoJSON seeds for the neighbourhood pipeline")
    parser.add_argument("--report", required=True, help="Markdown audit report path")
    parser.add_argument("--timeout", type=int, default=1200, help="Download timeout in seconds")
    parser.add_argument("--min-added", type=int, default=1, help="Require at least this many net new polygons")
    parser.add_argument("--allow-no-new", action="store_true", help="Allow zero new polygons when the existing mapped baseline is retained")
    args = parser.parse_args()

    communes_path = Path(args.communes)
    existing_path = Path(args.existing)
    output_path = Path(args.output)
    report_path = Path(args.report)

    if not existing_path.is_file():
        raise SystemExit(f"Existing mapped seed file not found: {existing_path}")
    country_geometry, _ = load_commune_union(communes_path)
    old = json.loads(existing_path.read_text(encoding="utf-8"))
    existing_features = old.get("features") or []
    if not existing_features:
        raise SystemExit("No existing mapped polygons; refusing to overwrite a missing baseline.")

    west, south, east, north = country_geometry.bounds
    pad = 0.03
    bbox = (west - pad, south - pad, east + pad, north + pad)
    cli = shutil.which("overturemaps")
    if not cli:
        raise SystemExit("Overture CLI is missing. Install the official package with pip install overturemaps.")

    fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    raw_collection = None
    diagnostics = []
    with tempfile.TemporaryDirectory(prefix="transitlab-overture-") as tmp:
        raw_path = Path(tmp) / "division-area.geojson"
        command = [
            cli, "download",
            f"--bbox={bbox[0]:.6f},{bbox[1]:.6f},{bbox[2]:.6f},{bbox[3]:.6f}",
            "-f", "geojson",
            "--type=division_area",
            "-o", str(raw_path),
        ]
        for attempt in range(1, 4):
            run = subprocess.run(command, text=True, capture_output=True, timeout=args.timeout, check=False)
            diagnostics.append(f"Attempt {attempt}: exit={run.returncode}; stdout={run.stdout[-1000:]}; stderr={run.stderr[-1600:]}")
            if run.returncode == 0 and raw_path.is_file() and raw_path.stat().st_size > 10:
                try:
                    raw_collection = json.loads(raw_path.read_text(encoding="utf-8"))
                    break
                except json.JSONDecodeError:
                    pass
            if attempt < 3:
                time.sleep(3 * attempt)

    if raw_collection is None:
        raise SystemExit("Overture division_area download failed after 3 attempts; existing published data remains untouched.\n" + "\n".join(diagnostics))
    if raw_collection.get("type") != "FeatureCollection":
        raise SystemExit("Overture download did not return a GeoJSON FeatureCollection.")

    overture_features, counts = normalize_overture_features(raw_collection, country_geometry, fetched_at)
    merged, net_added = merge_mapped_features(existing_features, overture_features)
    if net_added < args.min_added and not args.allow_no_new:
        raise SystemExit(
            f"Overture returned {len(overture_features)} valid polygons but added only {net_added} new boundaries. "
            "The current published dataset is preserved; inspect the audit report before lowering the threshold."
        )
    if not merged:
        raise SystemExit("No existing or newly mapped neighborhood polygons remain; refusing to write an empty baseline.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps({"type": "FeatureCollection", "features": merged}, ensure_ascii=False), encoding="utf-8")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Overture neighbourhood boundary import",
        "",
        f"- Retrieved at UTC: {fetched_at}",
        f"- Overture source: {OVERTURE_DOCS}",
        f"- Licence: {ODBL_URL}",
        f"- Spatial filter: Moroccan commune-boundary union, bbox {bbox}",
        f"- Raw division_area features returned: {counts['input']}",
        f"- Accepted mapped neighborhood polygons: {counts['accepted']}",
        f"- Net new polygons after deduplication: {net_added}",
        f"- Existing mapped polygons preserved or refreshed: {len(existing_features)}",
        f"- Combined seeds passed to pipeline: {len(merged)}",
        "",
        "## Rejection counts",
        "",
    ]
    for key in ("skip_subtype", "skip_country", "skip_name", "skip_geometry", "skip_area"):
        lines.append(f"- {key}: {counts[key]}")
    lines.extend(["", "## Download diagnostics", ""])
    lines.extend(f"- {item}" for item in diagnostics)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Overture mapped polygons accepted: {counts['accepted']}; net new: {net_added}; total seeds: {len(merged)}")
    print(f"Audit report written to {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
