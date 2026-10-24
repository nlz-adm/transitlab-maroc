#!/usr/bin/env python3
"""Build a provenance-aware directory of named Moroccan neighborhoods.

Poste Maroc supplies city, neighborhood name and postcode, but no neighborhood
polygons. Geometry is attached only when a named OSM feature matches in the same
city, or when a unique name occurs nationwide. Unmatched records remain unlocated.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shapely.geometry import shape, mapping
from shapely.strtree import STRtree
from shapely.validation import make_valid

SOURCE_URL = "https://data.gov.ma/data/dataset/e0093dbf-38f0-440a-91da-ad3f0243f378/resource/7f257007-d512-4a2e-9f7f-ba29ee4ac8b1/download/codes-postaux-quartiers-2018.xlsx"
PLACE_TYPES = {"quarter", "suburb", "neighbourhood", "neighborhood", "locality"}
PREFIXES = {"quartier", "quartiers", "hay", "حي", "al", "el", "les", "le", "la", "de", "du", "des", "cite", "residence"}


def fold(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value)).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("’", "'").replace(chr(96), "'")
    text = re.sub(r"[^0-9a-z\u0600-\u06ff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def name_key(value: Any) -> str:
    parts = fold(value).split()
    while parts and parts[0] in PREFIXES:
        parts.pop(0)
    return " ".join(parts)


def cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def load_postal_rows(path: Path) -> list[dict[str, str]]:
    """Read the official Poste Maroc spreadsheet or its Parquet mirror."""
    if path.suffix.casefold() == ".parquet":
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise RuntimeError("Reading the Poste Maroc Parquet mirror requires pyarrow") from exc

        table = pq.read_table(path)
        available = set(table.column_names)
        city_column = next((name for name in ("ville", "city", "commune") if name in available), None)
        name_column = next((name for name in ("quartier", "neighborhood", "neighbourhood") if name in available), None)
        code_column = next((name for name in ("nouveau_code_postal", "code_postal", "postal_code") if name in available), None)
        source_column = "source_record_id" if "source_record_id" in available else None
        sheet_column = "source_sheet" if "source_sheet" in available else None
        if city_column is None or name_column is None:
            raise ValueError("Parquet mirror has no supported Ville/Quartier columns")

        records: list[dict[str, str]] = []
        for row_no, row in enumerate(table.to_pylist(), start=1):
            city, name = cell_text(row.get(city_column)), cell_text(row.get(name_column))
            if not city or not name:
                continue
            postcode = cell_text(row.get(code_column)) if code_column else ""
            source_record_id = cell_text(row.get(source_column)) if source_column else ""
            source_sheet = cell_text(row.get(sheet_column)) if sheet_column else "parquet"
            source_key = source_record_id or "\x1f".join(
                (source_sheet, str(row_no), fold(city), fold(name), postcode)
            )
            records.append({
                "sourceId": hashlib.sha1(source_key.encode("utf-8")).hexdigest()[:16],
                "sourceRow": source_record_id or f"{source_sheet}:{row_no}",
                "city": city,
                "cityKey": fold(city),
                "name": name,
                "nameKey": name_key(name),
                "postalCode": postcode,
            })
        if not records:
            raise ValueError("No rows with Ville and Quartier columns found in Poste Maroc Parquet mirror")
        return records

    import openpyxl

    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    records: list[dict[str, str]] = []
    for sheet in book.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        found = None
        for row_no, row in enumerate(rows[:35]):
            headers = [fold(cell) for cell in row]
            city_idx = next((i for i, h in enumerate(headers)
                             if h in {"ville", "city", "commune", "localite"}), None)
            name_idx = next((i for i, h in enumerate(headers)
                             if h in {"quartier", "quartiers", "neighborhood", "neighbourhood", "nom quartier"}), None)
            code_idx = next((i for i, h in enumerate(headers)
                             if "code postal" in h or h == "postal code"), None)
            if city_idx is not None and name_idx is not None:
                found = (row_no, city_idx, name_idx, code_idx)
                break
        if found is None:
            continue
        header_row, city_idx, name_idx, code_idx = found
        for row_no, row in enumerate(rows[header_row + 1:], start=header_row + 2):
            if max(city_idx, name_idx) >= len(row):
                continue
            city, name = cell_text(row[city_idx]), cell_text(row[name_idx])
            if not city or not name:
                continue
            postcode = cell_text(row[code_idx]) if code_idx is not None and code_idx < len(row) else ""
            source_key = "\x1f".join((sheet.title, str(row_no), fold(city), fold(name), postcode))
            records.append({
                "sourceId": hashlib.sha1(source_key.encode("utf-8")).hexdigest()[:16],
                "sourceRow": f"{sheet.title}:{row_no}",
                "city": city,
                "cityKey": fold(city),
                "name": name,
                "nameKey": name_key(name),
                "postalCode": postcode,
            })
    book.close()
    if not records:
        raise ValueError("No rows with Ville and Quartier columns found in Poste Maroc workbook")
    return records

def read_geojsonseq(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    candidates = []
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            try:
                feature = json.loads(line)
            except json.JSONDecodeError:
                continue
            if feature.get("type") != "Feature" or not feature.get("geometry"):
                continue
            props = feature.get("properties") or {}
            tags = props.get("tags")
            if isinstance(tags, dict):
                props = {**props, **tags}
            place = fold(props.get("place"))
            if place not in PLACE_TYPES and fold(props.get("boundary")) != "place":
                continue
            name = str(props.get("name:fr") or props.get("name_fr") or props.get("name")
                        or props.get("name:ar") or props.get("name_ar") or "").strip()
            key = name_key(name)
            if not key:
                continue
            try:
                geom = make_valid(shape(feature["geometry"]))
            except Exception:
                continue
            if geom.is_empty or geom.geom_type not in {"Point", "Polygon", "MultiPolygon"}:
                continue
            city = str(props.get("addr:city") or props.get("is_in:city") or props.get("city") or "").strip()
            candidates.append({
                "name": name,
                "nameFr": str(props.get("name:fr") or props.get("name_fr") or ""),
                "nameAr": str(props.get("name:ar") or props.get("name_ar") or ""),
                "nameKey": key,
                "city": city,
                "cityKey": fold(city),
                "sourceId": str(props.get("@id") or feature.get("id") or ""),
                "source": "openstreetmap",
                "sourceDetail": "Entité nommée OpenStreetMap",
                "sourceUrl": "https://www.openstreetmap.org/",
                "sourceLicense": "ODbL-1.0",
                "placeType": place or ("boundary=place" if fold(props.get("boundary")) == "place" else ""),
                "geometry": geom,
            })
    return candidates


def read_mapped_boundary_candidates(path: Path | None) -> list[dict[str, Any]]:
    """Read source-mapped OSM/Overture polygons already assigned to a real commune."""
    if path is None or not path.is_file():
        return []
    try:
        collection = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read mapped neighborhood polygons from {path}: {exc}") from exc
    candidates = []
    for feature in collection.get("features", []):
        props = feature.get("properties") or {}
        if props.get("estimated") is True or props.get("boundaryKind") == "urban_block":
            continue
        source_raw = fold(props.get("source") or props.get("geometrySource") or "")
        if "overture" in source_raw:
            source = "overture"
        elif source_raw in {"osm", "openstreetmap"} or "openstreetmap" in source_raw:
            source = "openstreetmap"
        else:
            continue
        name = str(props.get("name_fr") or props.get("name") or props.get("name_ar") or "").strip()
        key = name_key(name)
        if not key:
            continue
        try:
            geometry = make_valid(shape(feature.get("geometry")))
        except Exception:
            continue
        if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            continue
        city = str(props.get("commune_name") or props.get("communeName") or props.get("city") or "").strip()
        source_id = str(props.get("sourceId") or props.get("source_id") or feature.get("id") or "")
        candidates.append({
            "name": name,
            "nameFr": str(props.get("name_fr") or props.get("name") or name),
            "nameAr": str(props.get("name_ar") or ""),
            "nameKey": key,
            "city": city,
            "cityKey": fold(city),
            "sourceId": source_id,
            "source": source,
            "sourceDetail": str(props.get("sourceDetail") or (
                "Overture Maps division_area" if source == "overture"
                else "Contour de quartier cartographié OpenStreetMap"
            )),
            "sourceUrl": str(props.get("sourceUrl") or (
                "https://docs.overturemaps.org/guides/divisions/"
                if source == "overture" else "https://www.openstreetmap.org/"
            )),
            "sourceLicense": str(props.get("sourceLicense") or "ODbL-1.0"),
            "placeType": str(props.get("sourceSubtype") or props.get("level") or "neighbourhood"),
            "communeCode": str(props.get("commune_id") or props.get("communeCode") or ""),
            "geometry": geometry,
        })
    return candidates


def prepare_commune_areas(path: Path | None):
    """Prepare an STRtree for assigning untagged OSM names to the commune containing their geometry."""
    if path is None or not path.is_file():
        return [], [], None
    collection = json.loads(path.read_text(encoding="utf-8"))
    communes, geometries = [], []
    for feature in collection.get("features", []):
        props = feature.get("properties") or {}
        try:
            geometry = make_valid(shape(feature.get("geometry")))
        except Exception:
            continue
        if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            continue
        name = str(props.get("name_fr") or props.get("name") or props.get("nom") or "").strip()
        code = str(props.get("code") or props.get("commune_id") or props.get("id") or feature.get("id") or "").strip()
        if not code:
            continue
        communes.append({"name": name, "code": code, "geometry": geometry})
        geometries.append(geometry)
    return communes, geometries, STRtree(geometries) if geometries else None


def enrich_candidates_with_communes(candidates, communes, geometries, tree):
    """Add a commune code/name from the actual geometry without guessing the neighbourhood boundary."""
    if not tree:
        return
    for candidate in candidates:
        geometry = candidate["geometry"]
        point = geometry if geometry.geom_type == "Point" else geometry.representative_point()
        candidate_indices = tree.query(point)
        matches = []
        for raw_index in candidate_indices:
            index = int(raw_index)
            commune_geometry = geometries[index]
            if commune_geometry.covers(point):
                matches.append(index)
        if not matches:
            continue
        winner = min(matches, key=lambda index: geometries[index].area)
        commune = communes[winner]
        candidate["communeCode"] = commune["code"]
        candidate["communeName"] = commune["name"]
        if not candidate.get("city"):
            candidate["city"] = commune["name"]
            candidate["cityKey"] = fold(commune["name"])


def build_registry(
    records: list[dict[str, str]],
    candidates: list[dict[str, Any]],
    *,
    include_unmatched_mapped: bool = False,
):
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_name[candidate["nameKey"]].append(candidate)

    features = []
    city_stats: dict[str, Counter] = defaultdict(Counter)
    seen_names: set[tuple[str, str]] = set()
    duplicate_names: set[tuple[str, str]] = set()
    totals = Counter()
    postal_pairs = {(row["cityKey"], row["nameKey"]) for row in records}
    postal_name_keys = {row["nameKey"] for row in records}
    used_source_ids: set[tuple[str, str]] = set()
    for row in records:
        pair = (row["cityKey"], row["nameKey"])
        if pair in seen_names:
            duplicate_names.add(pair)
        seen_names.add(pair)
        all_named = by_name.get(row["nameKey"], [])
        viable = []
        for item in all_named:
            if item["cityKey"] and item["cityKey"] != row["cityKey"]:
                continue
            if not item["cityKey"] and len(all_named) > 1:
                # Same-named places exist in multiple locations, but the OSM object
                # has no city tag. Do not guess which one the postal row means.
                continue
            viable.append(item)
        chosen = None
        if viable:
            def rank(item):
                geom = item["geometry"]
                polygon = geom.geom_type in {"Polygon", "MultiPolygon"}
                area = float(geom.area) if polygon else 0.0
                return (int(polygon), area)
            chosen = max(viable, key=rank)

        geometry = None
        if chosen:
            geometry = mapping(chosen["geometry"])
            is_polygon = chosen["geometry"].geom_type in {"Polygon", "MultiPolygon"}
            status = "mapped_boundary" if is_polygon else "mapped_point"
            source = str(chosen.get("source") or "openstreetmap")
            source_id = str(chosen.get("sourceId") or "")
            if source_id:
                used_source_ids.add((source, source_id))
        else:
            status, source, source_id = "unlocated", "", ""
        totals[status] += 1
        city_stats[row["city"]][status] += 1

        properties = {
            "name": row["name"],
            "name_fr": row["name"],
            "city": row["city"],
            "postalCode": row["postalCode"],
            "source": "poste_maroc_2018",
            "sourceDetail": "Barid Al Maghrib · Codes postaux des quartiers",
            "sourceUrl": SOURCE_URL,
            "sourceLicense": "ODbL-1.0",
            "sourceRow": row["sourceRow"],
            "sourceId": row["sourceId"],
            "geometryStatus": status,
            "geometrySource": source,
            "geometrySourceId": source_id,
            "boundaryKind": "mapped" if status == "mapped_boundary" else (
                "neighborhood_point" if status == "mapped_point" else "unlocated"
            ),
            "estimated": False if status == "mapped_boundary" else None,
        }
        features.append({
            "type": "Feature",
            "id": row["sourceId"],
            "properties": properties,
            "geometry": geometry,
        })

    supplement_count = 0
    if include_unmatched_mapped:
        # Prefer a real polygon over a point for the same normalized name and commune.
        # Postal records always remain untouched; only additional source-mapped OSM entities
        # not already matched to a postal row are appended.
        def supplemental_rank(item):
            geom = item["geometry"]
            is_polygon = geom.geom_type in {"Polygon", "MultiPolygon"}
            return (0 if is_polygon else 1, -float(geom.area) if is_polygon else 0.0)

        added_pairs: set[tuple[str, str]] = set()
        for candidate in sorted(candidates, key=supplemental_rank):
            name_key_value = candidate["nameKey"]
            city_key_value = candidate.get("cityKey") or ""
            source_id = candidate.get("sourceId") or ""
            source = str(candidate.get("source") or "openstreetmap")
            if source_id and (source, source_id) in used_source_ids:
                continue
            pair = (city_key_value, name_key_value)
            if city_key_value and pair in postal_pairs:
                continue
            if not city_key_value and name_key_value in postal_name_keys:
                # Without a city match we cannot safely tell whether this is another spelling
                # of a postal row or a different place with the same name.
                continue
            if pair in added_pairs:
                continue
            geom_obj = candidate["geometry"]
            if geom_obj.is_empty or geom_obj.geom_type not in {"Point", "Polygon", "MultiPolygon"}:
                continue
            status = "mapped_boundary" if geom_obj.geom_type in {"Polygon", "MultiPolygon"} else "mapped_point"
            stable_id = source_id or hashlib.sha1(
                (name_key_value + "|" + city_key_value + "|" + geom_obj.wkb_hex).encode("utf-8")
            ).hexdigest()[:16]
            properties = {
                "name": candidate["name"],
                "name_fr": candidate.get("nameFr") or candidate["name"],
                "name_ar": candidate.get("nameAr") or "",
                "city": candidate.get("city") or "",
                "communeCode": candidate.get("communeCode") or "",
                "postalCode": "",
                "source": source,
                "sourceDetail": str(candidate.get("sourceDetail") or (
                    "Entité nommée Overture Maps supplémentaire (non fusionnée avec le catalogue postal)"
                    if source == "overture"
                    else "Entité nommée OpenStreetMap supplémentaire (non fusionnée avec le catalogue postal)"
                )),
                "sourceUrl": str(candidate.get("sourceUrl") or (
                    "https://docs.overturemaps.org/guides/divisions/"
                    if source == "overture" else "https://www.openstreetmap.org/"
                )),
                "sourceLicense": str(candidate.get("sourceLicense") or "ODbL-1.0"),
                "sourceRow": "",
                "sourceId": stable_id,
                "geometryStatus": status,
                "geometrySource": source,
                "geometrySourceId": stable_id,
                "boundaryKind": "mapped" if status == "mapped_boundary" else "neighborhood_point",
                "estimated": False if status == "mapped_boundary" else None,
                "placeType": candidate.get("placeType") or "",
                "geometryAssignment": "commune_containment" if candidate.get("communeCode") else "source_city_tag",
            }
            features.append({
                "type": "Feature",
                "id": "osm-directory-" + stable_id,
                "properties": properties,
                "geometry": mapping(geom_obj),
            })
            totals[status] += 1
            city_stats[properties["city"] or "Ville non renseignée"][status] += 1
            added_pairs.add(pair)
            supplement_count += 1

    return features, totals, city_stats, len(duplicate_names)


def write_outputs(features, totals, city_stats, duplicates, output: Path, index_path: Path, report: Path):
    if len(features) < 10000:
        raise ValueError(f"Only {len(features)} official neighborhood rows; 10000 required")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps({"type": "FeatureCollection", "features": features},
                          ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with output.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
            stream.write(payload)
    postal_features = sum(1 for feature in features if (feature.get("properties") or {}).get("source") == "poste_maroc_2018")
    osm_features = sum(1 for feature in features if (feature.get("properties") or {}).get("source") == "openstreetmap")
    overture_features = sum(1 for feature in features if (feature.get("properties") or {}).get("source") == "overture")
    if postal_features == 0 and osm_features == 0 and overture_features == 0:
        # Backward-compatible default for synthetic unit-test fixtures.
        postal_features = len(features)
    if postal_features < 10000:
        raise ValueError(f"Only {postal_features} Poste Maroc rows; 10000 required regardless of supplemental sources")
    geometry_source_counts = Counter(
        str((feature.get("properties") or {}).get("geometrySource") or "")
        for feature in features
        if (feature.get("properties") or {}).get("geometrySource")
    )
    index = {
        "version": "morocco-neighborhood-directory-" + datetime.now(timezone.utc).strftime("%Y%m%d"),
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": "Barid Al Maghrib · Codes postaux des quartiers + OpenStreetMap + Overture Maps",
        "sourceDate": "Poste Maroc 2018-09 ; OSM et Overture au moment du rafraîchissement",
        "sourceUrl": SOURCE_URL,
        "license": "ODbL-1.0",
        "features": len(features),
        "postalFeatures": postal_features,
        "osmSupplementFeatures": osm_features,
        "overtureSupplementFeatures": overture_features,
        "geometrySourceCounts": dict(geometry_source_counts),
        "mappedBoundaryFeatures": totals["mapped_boundary"],
        "mappedPointFeatures": totals["mapped_point"],
        "unlocatedFeatures": totals["unlocated"],
        "duplicateCityNeighborhoodNames": duplicates,
        "url": output.as_posix(),
        "bytesCompressed": output.stat().st_size,
    }
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Répertoire des quartiers sourcés (Poste Maroc + OpenStreetMap + Overture Maps)", "",
        f"- Généré le : {index['generatedAt']}",
        f"- Source postale : [Barid Al Maghrib — Codes postaux des quartiers]({SOURCE_URL})",
        "- Date du jeu postal source : septembre 2018",
        "- Géométries supplémentaires : polygones OSM/Overture et localisations OSM, au moment du rafraîchissement",
        "- Licence déclarée : ODbL 1.0",
        f"- Entrées totales : {len(features)}",
        f"- Entrées postales conservées : {postal_features}",
        f"- Entités OSM supplémentaires : {osm_features}",
        f"- Entités Overture supplémentaires : {overture_features}",
        f"- Villes distinctes : {len(city_stats)}",
        f"- Contours de quartiers cartographiés trouvés : {totals['mapped_boundary']}",
        f"- Localisations ponctuelles trouvées (pas des frontières) : {totals['mapped_point']}",
        f"- Quartiers sans localisation fiable : {totals['unlocated']}",
        f"- Couples ville/nom répétés : {duplicates}", "",
        "## Interprétation", "",
        "La source fournit des noms, des villes et des codes postaux, mais pas de limites géographiques.",
        "Le pipeline n'attache une géométrie que lorsqu'une entité OSM nommée correspond au nom et à la ville.",
        "Les points ne sont pas présentés comme des contours. Les enregistrements sans géométrie restent dans le répertoire au lieu de recevoir des coordonnées inventées.", "",
        "## Répartition par ville", "",
        "| Ville | Contours | Points | Non localisés |", "|---|---:|---:|---:|",
    ]
    for city, count in sorted(city_stats.items(), key=lambda item: item[0].casefold()):
        lines.append(f"| {city.replace('|', '/')} | {count['mapped_boundary']} | {count['mapped_point']} | {count['unlocated']} |")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postal-data", "--postal-xlsx", dest="postal_data", required=True, type=Path)
    parser.add_argument("--osm-geojsonseq", type=Path, default=None)
    parser.add_argument("--mapped-boundaries", type=Path, default=None, help="GeoJSON of mapped OSM/Overture polygons after commune assignment")
    parser.add_argument("--communes", type=Path, default=None, help="Optional commune polygons for spatially assigning named entities")
    parser.add_argument("--include-unmatched-mapped", "--include-unmatched-osm", dest="include_unmatched_mapped", action="store_true", help="Append source-mapped OSM/Overture entities not matched to postal rows")
    parser.add_argument("--output", type=Path, default=Path("assets/neighborhood-directory/postal-neighborhoods.geojson.gz"))
    parser.add_argument("--index", type=Path, default=Path("assets/neighborhood-directory-index.json"))
    parser.add_argument("--report", type=Path, default=Path("reports/postal-neighborhood-coverage.md"))
    args = parser.parse_args()
    records = load_postal_rows(args.postal_data)
    candidates = read_geojsonseq(args.osm_geojsonseq)
    candidates.extend(read_mapped_boundary_candidates(args.mapped_boundaries))
    communes, commune_geometries, commune_tree = prepare_commune_areas(args.communes)
    enrich_candidates_with_communes(candidates, communes, commune_geometries, commune_tree)
    features, totals, city_stats, duplicates = build_registry(
        records, candidates, include_unmatched_mapped=args.include_unmatched_mapped
    )
    index = write_outputs(features, totals, city_stats, duplicates, args.output, args.index, args.report)
    print(f"Quartiers Poste Maroc : {index['features']} entrées; "
          f"{index['mappedBoundaryFeatures']} contours, {index['mappedPointFeatures']} points, "
          f"{index['unlocatedFeatures']} sans localisation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
