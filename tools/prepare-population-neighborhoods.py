#!/usr/bin/env python3
"""Download and prepare Kontur's Morocco H3 population grid for the map.

The generated GeoJSON chunks are only a fine-scale, modelled visualization.
Official 2024 HCP commune/arrondissement totals remain the administrative data.
No extra Python packages are required.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sqlite3
import struct
import tempfile
import urllib.request
from collections import defaultdict
from pathlib import Path


SOURCE_URL = (
    "https://geodata-eu-central-1-kontur-public.s3.amazonaws.com/"
    "kontur_datasets/kontur_population_MA_20231101.gpkg.gz"
)
SOURCE_NAME = "Kontur Population · Maroc · édition du 1 novembre 2023"
SOURCE_LICENSE = "CC BY 4.0"
SOURCE_PAGE = "https://data.humdata.org/dataset/38e5a215-ebdf-4ef8-b52a-96bd03b4de9e"
SOURCE_METHOD = (
    "Fusion corrigée de GHSL, Facebook/Meta HRSL, empreintes de bâtiments Microsoft, "
    "couverture terrestre Copernicus, Land Information New Zealand et OpenStreetMap."
)
GRID_STEP = 0.5
EARTH_RADIUS_M = 6_371_008.8
WEB_MERCATOR_RADIUS_M = 6_378_137.0


def transform_web_mercator(x: float, y: float) -> tuple[float, float]:
    longitude = math.degrees(x / WEB_MERCATOR_RADIUS_M)
    latitude = math.degrees(math.atan(math.sinh(y / WEB_MERCATOR_RADIUS_M)))
    return longitude, latitude


def parse_gpkg_polygon(blob: bytes) -> list[tuple[float, float]]:
    if blob[:2] != b"GP" or len(blob) < 22:
        raise ValueError("Géométrie GeoPackage inconnue")
    flags = blob[3]
    envelope_kind = (flags >> 1) & 0b111
    envelope_bytes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get(envelope_kind)
    if envelope_bytes is None:
        raise ValueError(f"Enveloppe GeoPackage inconnue : {envelope_kind}")

    offset = 8 + envelope_bytes
    byte_order = blob[offset]
    endian = "<" if byte_order == 1 else ">"
    geometry_type = struct.unpack_from(endian + "I", blob, offset + 1)[0] & 0xFF
    if geometry_type != 3:
        raise ValueError(f"Géométrie attendue Polygon, reçue : {geometry_type}")

    ring_count = struct.unpack_from(endian + "I", blob, offset + 5)[0]
    if ring_count < 1:
        return []
    point_count = struct.unpack_from(endian + "I", blob, offset + 9)[0]
    cursor = offset + 13
    points = []
    for _ in range(point_count):
        x, y = struct.unpack_from(endian + "dd", blob, cursor)
        cursor += 16
        points.append(transform_web_mercator(x, y))
    return points


def spherical_area_km2(ring: list[tuple[float, float]]) -> float:
    """Approximate a small polygon's area on the Earth, without GIS packages."""
    area = 0.0
    for index, (lon1, lat1) in enumerate(ring[:-1]):
        lon2, lat2 = ring[index + 1]
        area += math.radians(lon2 - lon1) * (
            2 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2))
        )
    return abs(area * EARTH_RADIUS_M**2 / 2) / 1_000_000


def download_source(temp_dir: Path) -> Path:
    archive_path = temp_dir / "kontur_population_MA_20231101.gpkg.gz"
    gpkg_path = temp_dir / "kontur_population_MA_20231101.gpkg"
    if not gpkg_path.exists():
        if not archive_path.exists():
            print(f"Téléchargement de la source : {SOURCE_URL}")
            urllib.request.urlretrieve(SOURCE_URL, archive_path)
        print("Décompression de la source GeoPackage")
        with gzip.open(archive_path, "rb") as source, gpkg_path.open("wb") as target:
            while block := source.read(1024 * 1024):
                target.write(block)
    return gpkg_path


def build(source_path: Path, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks: dict[tuple[int, int], list[dict]] = defaultdict(list)
    total_population = 0

    connection = sqlite3.connect(source_path)
    try:
        layer_name = connection.execute(
            "SELECT table_name FROM gpkg_contents WHERE data_type='features' LIMIT 1"
        ).fetchone()[0]
        query = f'SELECT geom, h3, population FROM "{layer_name}" WHERE population > 0'
        for geometry_blob, h3, raw_population in connection.execute(query):
            ring = parse_gpkg_polygon(geometry_blob)
            if len(ring) < 4:
                continue
            # Keep five decimals (~1 m) to reduce transfer size while preserving
            # a clean outline at city zoom levels.
            ring = [(round(lon, 5), round(lat, 5)) for lon, lat in ring]
            center_lon = sum(point[0] for point in ring[:-1]) / (len(ring) - 1)
            center_lat = sum(point[1] for point in ring[:-1]) / (len(ring) - 1)
            tile_x = math.floor((center_lon + 180) / GRID_STEP)
            tile_y = math.floor((center_lat + 90) / GRID_STEP)
            population = int(round(float(raw_population)))
            area = spherical_area_km2(ring)
            density = round(population / area, 1) if area > 0 else 0
            chunks[(tile_x, tile_y)].append(
                {
                    "type": "Feature",
                    "id": h3,
                    "properties": {"population": population, "density": density},
                    "geometry": {"type": "Polygon", "coordinates": [ring]},
                }
            )
            total_population += population
    finally:
        connection.close()

    chunk_index = []
    for (tile_x, tile_y), features in sorted(chunks.items()):
        west = tile_x * GRID_STEP - 180
        south = tile_y * GRID_STEP - 90
        east, north = west + GRID_STEP, south + GRID_STEP
        chunk_id = f"{tile_x}_{tile_y}"
        output_path = output_dir / f"{chunk_id}.geojson.gz"
        collection = {"type": "FeatureCollection", "features": features}
        encoded = json.dumps(
            collection, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        with output_path.open("wb") as raw_file:
            with gzip.GzipFile(fileobj=raw_file, mode="wb", compresslevel=9, mtime=0) as zipped:
                zipped.write(encoded)
        chunk_index.append(
            {
                "id": chunk_id,
                "bbox": [round(west, 4), round(south, 4), round(east, 4), round(north, 4)],
                "count": len(features),
                "url": f"assets/population-neighborhoods/{chunk_id}.geojson.gz",
            }
        )

    index = {
        "version": "kontur-ma-h3-2023-1",
        "source": SOURCE_NAME,
        "sourceUrl": SOURCE_PAGE,
        "sourceDataUrl": SOURCE_URL,
        "license": SOURCE_LICENSE,
        "method": SOURCE_METHOD,
        "referenceDate": "2023-11-01",
        "cellModel": "H3 resolution 8 · maille indicative d’environ 400 m",
        "gridStepDegrees": GRID_STEP,
        "cellCount": sum(len(features) for features in chunks.values()),
        "populationSum": total_population,
        "chunks": chunk_index,
        "limitation": (
            "Estimations modélisées, pas de décompte officiel par quartier. "
            "Les totaux administratifs du HCP RGPH 2024 restent la référence."
        ),
    }
    index_path = output_dir.parent / "population-neighborhoods-index.json"
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="GeoPackage décompressé en entrée")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "assets" / "population-neighborhoods",
        help="Dossier de tuiles GeoJSON compressées générées",
    )
    arguments = parser.parse_args()

    source_path = arguments.source
    if source_path is None:
        with tempfile.TemporaryDirectory(prefix="transitlab-kontur-") as temporary:
            source_path = download_source(Path(temporary))
            summary = build(source_path, arguments.output)
    else:
        summary = build(source_path, arguments.output)

    compressed_bytes = sum(path.stat().st_size for path in arguments.output.glob("*.geojson.gz"))
    print(
        f"Généré {summary['cellCount']:,} cellules, "
        f"{len(summary['chunks'])} tuiles, "
        f"population source {summary['populationSum']:,}, "
        f"{compressed_bytes / 1_000_000:.2f} Mo compressés."
    )


if __name__ == "__main__":
    main()
