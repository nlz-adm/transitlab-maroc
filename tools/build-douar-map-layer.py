#!/usr/bin/env python3
"""Build a provenance-aware point directory for HCP 2024 rural douars.

HCP supplies names, population and household counts, but not douar polygons. Coordinates
are joined only from a published OSM/GeoNames matching file. Missing locations stay null;
this tool never invents points or boundaries.
"""
from __future__ import annotations

import argparse
import gzip
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HCP_SOURCE_URL = (
    "https://www.hcp.ma/Population-et-menages-par-douars-selon-les-resultats-du-recensement-general-de-la-population-et-de-l-habitat-de-2024_a4208.html"
)
HCP_WORKBOOK_URL = "https://www.hcp.ma/file/245768/"
UPSTREAM_REPOSITORY = "https://github.com/zkousama/morocco-communes"
REGIONS = tuple(f"{number:02d}" for number in range(1, 13))


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read valid JSON from {path}: {exc}") from exc


def load_places(path: Path) -> tuple[dict[str, dict], Counter, Counter]:
    payload = read_json(path)
    rows = payload.get("places") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Place-matching file must contain a 'places' list")

    places: dict[str, dict] = {}
    source_counts: Counter = Counter()
    match_counts: Counter = Counter()
    invalid = 0
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            invalid += 1
            continue
        code, latin_name, source, latitude, longitude, source_id, match_method = row[:7]
        code = str(code or "").strip()
        source = str(source or "").strip().lower()
        try:
            lat, lon = float(latitude), float(longitude)
        except (TypeError, ValueError):
            invalid += 1
            continue
        if (
            not code
            or source not in {"osm", "geonames"}
            or not (-90 <= lat <= 90)
            or not (-180 <= lon <= 180)
        ):
            invalid += 1
            continue
        if code in places:
            raise ValueError(f"Duplicate place match for HCP douar code {code}")
        item = {
            "latinName": str(latin_name or "").strip(),
            "source": source,
            "lat": lat,
            "lon": lon,
            "sourceId": str(source_id or "").strip(),
            "matchMethod": str(match_method or "unknown").strip(),
        }
        places[code] = item
        source_counts[source] += 1
        match_counts[item["matchMethod"]] += 1
    if invalid:
        raise ValueError(f"Invalid matching rows: {invalid}; refusing a partial point import")
    return places, source_counts, match_counts


def douar_properties(record: dict, region: str, place: dict | None) -> dict:
    code = str(record.get("code") or "").strip()
    commune_code = str(record.get("communeCode") or "").strip()
    fraction_code = str(record.get("fraction") or "").strip()
    name_obj = record.get("name") or {}
    arabic_name = str(name_obj.get("ar") or "").strip() if isinstance(name_obj, dict) else ""
    if not code or not arabic_name or not commune_code or not fraction_code:
        raise ValueError(f"Douar record missing code/name/commune/fraction in region {region}")
    if not commune_code.startswith(region + "."):
        raise ValueError(f"Douar {code} is stored in the wrong region file ({region})")

    location_source = place.get("source") if place else ""
    source_license = "ODbL 1.0" if location_source == "osm" else (
        "CC BY 4.0" if location_source == "geonames" else ""
    )
    latin_name = place.get("latinName", "") if place else ""
    return {
        "code": code,
        "name": latin_name or arabic_name,
        "name_fr": latin_name,
        "name_ar": arabic_name,
        "level": "Douar rural",
        "regionCode": region,
        "communeCode": commune_code,
        "fractionCode": fraction_code,
        "douarType": str(record.get("type") or ""),
        "population": int(record.get("population") or 0),
        "households": int(record.get("households") or 0),
        "hasHcpTopics": record.get("topics") is not None,
        "geometryStatus": "mapped_point" if place else "unlocated",
        "locationSource": location_source,
        "locationSourceId": place.get("sourceId", "") if place else "",
        "locationMatchMethod": place.get("matchMethod", "") if place else "",
        "source": "hcp_rgph_2024_douar",
        "sourceUrl": HCP_SOURCE_URL,
        "sourceLicense": "CC BY 4.0",
        "locationSourceLicense": source_license,
        "boundaryKind": "point_only" if place else "unlocated",
        "estimated": False,
    }


def write_gzip_json(path: Path, payload: dict) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with path.open("wb") as output:
        with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
            compressed.write(raw)
    return path.stat().st_size


def build_dataset(
    input_dir: Path,
    places_path: Path,
    output_dir: Path,
    index_path: Path,
    report_path: Path,
    *,
    source_commit: str,
    min_features: int = 30000,
    min_points: int = 10000,
    min_communes: int = 1000,
) -> dict:
    if not source_commit or len(source_commit) < 7:
        raise ValueError("A pinned upstream commit SHA is required for reproducibility")
    places, place_sources, match_methods = load_places(places_path)
    matched_codes = set(places)
    emitted_codes: set[str] = set()
    features_total = 0
    points_total = 0
    households_total = 0
    population_total = 0
    topic_missing = 0
    all_communes: set[str] = set()
    region_rows = []
    all_type_counts: Counter = Counter()

    for region in REGIONS:
        source_path = input_dir / f"{region}.json"
        records = read_json(source_path)
        if not isinstance(records, list) or not records:
            raise ValueError(f"Expected a non-empty JSON array in {source_path}")

        features = []
        points = []
        communes: set[str] = set()
        region_population = 0
        region_households = 0
        types: Counter = Counter()
        seen_codes: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise ValueError(f"Invalid douar record in {source_path}")
            code = str(record.get("code") or "").strip()
            if not code or code in seen_codes:
                raise ValueError(f"Missing or duplicate douar code in {source_path}: {code!r}")
            seen_codes.add(code)
            emitted_codes.add(code)
            commune_code = str(record.get("communeCode") or "")
            communes.add(commune_code)
            all_communes.add(commune_code)
            population = int(record.get("population") or 0)
            households = int(record.get("households") or 0)
            if population < 0 or households < 0:
                raise ValueError(f"Negative demographic count for douar {code}")
            region_population += population
            region_households += households
            types[str(record.get("type") or "unknown")] += 1
            all_type_counts[str(record.get("type") or "unknown")] += 1
            if record.get("topics") is None:
                topic_missing += 1

            place = places.get(code)
            properties = douar_properties(record, region, place)
            geometry = (
                {"type": "Point", "coordinates": [place["lon"], place["lat"]]}
                if place else None
            )
            feature = {
                "type": "Feature",
                "id": "douar-" + code,
                "properties": properties,
                "geometry": geometry,
            }
            features.append(feature)
            if place:
                points.append(feature)

        output_path = output_dir / f"{region}.geojson.gz"
        bytes_compressed = write_gzip_json(
            output_path, {"type": "FeatureCollection", "features": features}
        )
        coords = [feature["geometry"]["coordinates"] for feature in points]
        bbox = (
            [
                min(point[0] for point in coords),
                min(point[1] for point in coords),
                max(point[0] for point in coords),
                max(point[1] for point in coords),
            ]
            if coords else None
        )
        region_rows.append({
            "code": region,
            "url": output_path.as_posix(),
            "count": len(features),
            "pointCount": len(points),
            "unlocatedCount": len(features) - len(points),
            "communes": len(communes),
            "population": region_population,
            "households": region_households,
            "types": dict(sorted(types.items())),
            "bbox": bbox,
            "bytesCompressed": bytes_compressed,
        })
        features_total += len(features)
        points_total += len(points)
        population_total += region_population
        households_total += region_households

    if matched_codes - emitted_codes:
        examples = ", ".join(sorted(matched_codes - emitted_codes)[:5])
        raise ValueError(f"Matched places refer to unknown HCP douar codes: {examples}")
    if features_total < min_features:
        raise ValueError(f"Only {features_total} douars; minimum required is {min_features}")
    if points_total < min_points:
        raise ValueError(f"Only {points_total} matched points; minimum required is {min_points}")
    if len(all_communes) < min_communes:
        raise ValueError(
            f"Only {len(all_communes)} communes represented; minimum required is {min_communes} "
            "(national production default: 1000)"
        )
    if points_total != len(places):
        raise ValueError(f"Matched point accounting mismatch: {points_total} != {len(places)}")

    index = {
        "version": "morocco-hcp-douars-2024-" + source_commit[:12],
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "upstream": {
            "repository": UPSTREAM_REPOSITORY,
            "commit": source_commit,
            "douarDataset": "data/v1/douars/{region}.json",
            "matchedPoints": "api/data/douar-places.json",
        },
        "source": "Haut-Commissariat au Plan · RGPH 2024 · population et ménages par douar",
        "sourceDate": "RGPH 2024; source workbook published October 2025",
        "sourceUrl": HCP_SOURCE_URL,
        "sourceWorkbookUrl": HCP_WORKBOOK_URL,
        "sourceLicense": "CC BY 4.0",
        "pointSources": {
            "osm": {"count": int(place_sources.get("osm", 0)), "license": "ODbL 1.0"},
            "geonames": {"count": int(place_sources.get("geonames", 0)), "license": "CC BY 4.0"},
        },
        "matchMethods": dict(sorted(match_methods.items())),
        "features": features_total,
        "locatedPoints": points_total,
        "unlocatedFeatures": features_total - points_total,
        "population": population_total,
        "households": households_total,
        "communes": len(all_communes),
        "topicsWithheldOrMissing": topic_missing,
        "types": dict(sorted(all_type_counts.items())),
        "regions": region_rows,
        "bytesCompressed": sum(row["bytesCompressed"] for row in region_rows),
        "limitation": (
            "HCP fournit les noms, populations et ménages des douars ruraux, pas leurs contours. "
            "Seuls les douars rapprochés d'un point GeoNames/OSM sont affichés sur la carte. "
            "Un point représente un lieu, pas la limite de son territoire. Les autres restent "
            "dans le fichier avec geometry=null ; aucun point ni polygone n'est inventé."
        ),
    }
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Douars ruraux · RGPH 2024", "",
        f"- Généré le : {index['generatedAt']}",
        f"- Source HCP : [{HCP_SOURCE_URL}]({HCP_SOURCE_URL})",
        f"- Jeu en amont : [{UPSTREAM_REPOSITORY}]({UPSTREAM_REPOSITORY}) à {source_commit}",
        "- Licence des données HCP : CC BY 4.0 ; crédit HCP et indiquer les transformations.",
        "- Points de localisation : OpenStreetMap (ODbL) et GeoNames (CC BY 4.0), appariés à un douar HCP.",
        f"- Douars RGPH 2024 : {features_total:,}".replace(",", " "),
        f"- Points de localisation sourcés : {points_total:,}".replace(",", " "),
        f"- Douars sans localisation fiable : {features_total - points_total:,}".replace(",", " "),
        f"- Communes représentées : {len(all_communes):,}".replace(",", " "),
        f"- Population HCP couverte : {population_total:,}".replace(",", " "),
        f"- Ménages HCP couverts : {households_total:,}".replace(",", " "),
        "", "## Couverture par région", "",
        "| Code région | Douars | Points | Sans localisation | Communes |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in region_rows:
        lines.append(
            f"| {row['code']} | {row['count']} | {row['pointCount']} | "
            f"{row['unlocatedCount']} | {row['communes']} |"
        )
    lines += [
        "", "## Limites d'interprétation", "",
        index["limitation"],
        "Cette couche complète la couche des quartiers urbains. Les douars ruraux ne sont pas "
        "comptabilisés comme quartiers urbains et les points ne sont pas des frontières.",
        "",
    ]
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path, help="Directory containing 01.json..12.json")
    parser.add_argument("--places", required=True, type=Path, help="GeoNames/OSM to HCP douar matches JSON")
    parser.add_argument("--output-dir", type=Path, default=Path("assets/douars"))
    parser.add_argument("--index", type=Path, default=Path("assets/douars-index.json"))
    parser.add_argument("--report", type=Path, default=Path("reports/douars-coverage.md"))
    parser.add_argument("--source-commit", required=True, help="Pinned upstream repository commit SHA")
    parser.add_argument("--min-features", type=int, default=30000)
    parser.add_argument("--min-points", type=int, default=10000)
    parser.add_argument("--min-communes", type=int, default=1000)
    args = parser.parse_args()

    index = build_dataset(
        args.input_dir, args.places, args.output_dir, args.index, args.report,
        source_commit=args.source_commit,
        min_features=args.min_features,
        min_points=args.min_points,
        min_communes=args.min_communes,
    )
    print(
        f"Douars HCP: {index['features']} entrées, {index['locatedPoints']} points, "
        f"{index['unlocatedFeatures']} sans localisation, {index['communes']} communes."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
