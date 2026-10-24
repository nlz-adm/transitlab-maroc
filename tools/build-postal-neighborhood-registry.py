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
                "nameKey": key,
                "city": city,
                "cityKey": fold(city),
                "sourceId": str(props.get("@id") or feature.get("id") or ""),
                "geometry": geom,
            })
    return candidates


def build_registry(records: list[dict[str, str]], candidates: list[dict[str, Any]]):
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_name[candidate["nameKey"]].append(candidate)

    features = []
    city_stats: dict[str, Counter] = defaultdict(Counter)
    seen_names: set[tuple[str, str]] = set()
    duplicate_names: set[tuple[str, str]] = set()
    totals = Counter()
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
                area = float(geom.area) if polygon else float("inf")
                return (int(polygon), -area)
            chosen = max(viable, key=rank)

        geometry = None
        if chosen:
            geometry = mapping(chosen["geometry"])
            is_polygon = chosen["geometry"].geom_type in {"Polygon", "MultiPolygon"}
            status = "mapped_boundary" if is_polygon else "mapped_point"
            source, source_id = "osm", chosen["sourceId"]
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
    index = {
        "version": "morocco-postal-neighborhood-directory-" + datetime.now(timezone.utc).strftime("%Y%m%d"),
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": "Barid Al Maghrib · Codes postaux des quartiers",
        "sourceDate": "2018-09",
        "sourceUrl": SOURCE_URL,
        "license": "ODbL-1.0",
        "features": len(features),
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
        "# Répertoire des quartiers de Poste Maroc", "",
        f"- Généré le : {index['generatedAt']}",
        f"- Source : [Barid Al Maghrib — Codes postaux des quartiers]({SOURCE_URL})",
        "- Date du jeu source : septembre 2018",
        "- Licence déclarée : ODbL 1.0",
        f"- Enregistrements conservés : {len(features)}",
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
    parser.add_argument("--postal-xlsx", required=True, type=Path)
    parser.add_argument("--osm-geojsonseq", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=Path("assets/neighborhood-directory/postal-neighborhoods.geojson.gz"))
    parser.add_argument("--index", type=Path, default=Path("assets/neighborhood-directory-index.json"))
    parser.add_argument("--report", type=Path, default=Path("reports/postal-neighborhood-coverage.md"))
    args = parser.parse_args()
    records = load_postal_rows(args.postal_xlsx)
    candidates = read_geojsonseq(args.osm_geojsonseq)
    features, totals, city_stats, duplicates = build_registry(records, candidates)
    index = write_outputs(features, totals, city_stats, duplicates, args.output, args.index, args.report)
    print(f"Quartiers Poste Maroc : {index['features']} entrées; "
          f"{index['mappedBoundaryFeatures']} contours, {index['mappedPointFeatures']} points, "
          f"{index['unlocatedFeatures']} sans localisation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
