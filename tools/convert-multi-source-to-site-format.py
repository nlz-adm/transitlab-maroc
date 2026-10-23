#!/usr/bin/env python3
"""Convertit la sortie du pipeline multi-source au format du site.

Prend en entrée : assets/data/neighborhoods-estimated.geojson
Produit en sortie :
- assets/neighborhood-boundaries/01.geojson.gz ... 12.geojson.gz
- assets/neighborhood-boundaries-index.json

Usage :
    python tools/convert-multi-source-to-site-format.py \\
        --input assets/data/neighborhoods-estimated.geojson \\
        --output-dir assets/neighborhood-boundaries \\
        --index assets/neighborhood-boundaries-index.json
"""

import argparse
import gzip
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

# Mapping des codes régionaux (basé sur les départements marocains)
# Les régions administratives du Maroc (12 régions depuis 2015)
REGION_MAPPING = {
    "01": {"name": "Tanger-Tétouan-Al Hoceïma", "codes": ["MA009", "MA010"]},
    "02": {"name": "Oriental", "codes": ["MA006", "MA007"]},
    "03": {"name": "Fès-Meknès", "codes": ["MA001", "MA002"]},
    "04": {"name": "Rabat-Salé-Kénitra", "codes": ["MA003", "MA004"]},
    "05": {"name": "Béni Mellal-Khénifra", "codes": ["MA005"]},
    "06": {"name": "Casablanca-Settat", "codes": ["MA008"]},
    "07": {"name": "Marrakech-Safi", "codes": ["MA011"]},
    "08": {"name": "Drâa-Tafilalet", "codes": ["MA012"]},
    "09": {"name": "Souss-Massa", "codes": ["MA013"]},
    "10": {"name": "Guelmim-Oued Noun", "codes": ["MA014"]},
    "11": {"name": "Laâyoune-Sakia El Hamra", "codes": ["MA015"]},
    "12": {"name": "Dakhla-Oued Ed-Dahab", "codes": ["MA016"]},
}


def determine_region(commune_code: str, commune_properties: dict) -> str:
    """Détermine la région à partir du code commune ou des propriétés."""
    # Essayer d'abord avec le département (3 premiers caractères du code)
    dept_code = commune_code[:5] if len(commune_code) >= 5 else commune_code
    
    for region_code, region_info in REGION_MAPPING.items():
        if dept_code in region_info["codes"]:
            return region_code
    
    # Fallback : utiliser la propriété region si disponible
    region = commune_properties.get("region", "")
    if region:
        # Mapper les noms de région aux codes
        for code, info in REGION_MAPPING.items():
            if info["name"].lower() in region.lower():
                return code
    
    # Dernier fallback : région par défaut (Casablanca-Settat)
    return "06"


def convert_feature(feature: dict) -> dict:
    """Convertit une feature du format pipeline au format site."""
    props = feature.get("properties", {})
    
    # Mapper les propriétés
    explicit_kind = props.get("boundaryKind")
    boundary_kind = explicit_kind if explicit_kind in {"mapped", "urban_block"} else (
        "estimated" if props.get("estimated", True) else "mapped"
    )
    converted_props = {
        "name": props.get("name", ""),
        "name_fr": props.get("name_fr", "") or props.get("name", ""),
        "name_ar": props.get("name_ar", ""),
        "commune": props.get("commune_name", ""),
        "commune_id": props.get("commune_id", ""),
        "level": props.get("level") or ("Secteur urbain estimé (îlot borné par les rues)" if boundary_kind == "urban_block" else "Quartier"),
        "boundaryKind": boundary_kind,
        "estimated": bool(props.get("estimated", boundary_kind != "mapped")),
        "estimationMethod": props.get("estimationMethod", ""),
        "buildingCount": props.get("buildingCount"),
        "roadClasses": props.get("roadClasses", []),
        "areaKm2": props.get("areaKm2"),
        "source": props.get("source", "unknown"),
        "sources": props.get("sources", []),
        "sourceAgreement": props.get("sourceAgreement", 1),
        "confidence": props.get("confidence", 0.5),
        "population": props.get("population"),
        "sourceId": props.get("sourceId", ""),
        "sourceSubtype": props.get("sourceSubtype", ""),
        "sourceLicense": props.get("sourceLicense", ""),
        "sourceDetail": props.get("sourceDetail", ""),
        "sourceRef": props.get("sourceRef", ""),
        "sourceRelease": props.get("sourceRelease", ""),
    }
    
    return {
        "type": "Feature",
        "id": props.get("id", ""),
        "properties": converted_props,
        "geometry": feature.get("geometry"),
    }


def compute_bbox(features: List[dict]) -> List[float]:
    """Calcule la bounding box d'une liste de features."""
    if not features:
        return [-180, -90, 180, 90]
    
    west = south = float("inf")
    east = north = float("-inf")
    
    for feature in features:
        geom = feature.get("geometry", {})
        coords = geom.get("coordinates", [])
        geom_type = geom.get("type", "")
        
        # Extraire tous les points
        def extract_points(c):
            if isinstance(c, (int, float)):
                return
            if len(c) >= 2 and isinstance(c[0], (int, float)):
                yield c
                return
            for item in c:
                yield from extract_points(item)
        
        for point in extract_points(coords):
            lon, lat = point[0], point[1]
            west = min(west, lon)
            south = min(south, lat)
            east = max(east, lon)
            north = max(north, lat)
    
    return [west, south, east, north]


def main():
    parser = argparse.ArgumentParser(description="Conversion pipeline → site")
    parser.add_argument("--input", required=True, help="GeoJSON du pipeline")
    parser.add_argument("--output-dir", required=True, help="Dossier de sortie")
    parser.add_argument("--index", required=True, help="Fichier index de sortie")
    parser.add_argument("--mapped-only", action="store_true",
                        help="Exclure les anciennes cellules Voronoï synthétiques")
    parser.add_argument("--urban-blocks", help="GeoJSON des îlots urbains estimés, issus des rues et bâtiments OSM")
    parser.add_argument("--min-urban-blocks", type=int, default=10000,
                        help="Nombre minimal d'îlots urbains requis avant publication (défaut : 10000)")
    args = parser.parse_args()
    
    # Charger les données
    print(f"Chargement de {args.input}...")
    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    features = data.get("features", [])
    print(f"  → {len(features)} features chargées")
    if args.mapped_only:
        features = [
            feature for feature in features
            if (feature.get("properties") or {}).get("estimated") is False
        ]
        print(f"  → {len(features)} contours cartographiés conservés (cellules Voronoï exclues)")
    urban_block_count = 0
    if args.urban_blocks:
        blocks_path = Path(args.urban_blocks)
        if not blocks_path.is_file():
            raise SystemExit(f"Fichier des îlots urbains manquant : {blocks_path}")
        blocks_data = json.loads(blocks_path.read_text(encoding="utf-8"))
        blocks = [
            feature for feature in blocks_data.get("features", [])
            if (feature.get("properties") or {}).get("boundaryKind") == "urban_block"
            and (feature.get("properties") or {}).get("estimated") is True
            and (feature.get("geometry") or {}).get("type") in {"Polygon", "MultiPolygon"}
        ]
        if len(blocks) < args.min_urban_blocks:
            raise SystemExit(f"Seulement {len(blocks)} îlots urbains estimés ; {args.min_urban_blocks} requis. Données publiées conservées.")
        features.extend(blocks)
        urban_block_count = len(blocks)
        print(f"  → {urban_block_count} îlots urbains estimés retenus, distincts des quartiers cartographiés")
    if not features:
        if args.mapped_only:
            raise SystemExit("ERREUR: aucune géométrie éligible ; index et données publiés conservés.")
        raise SystemExit("ERREUR: entrée GeoJSON vide ; index et données publiés conservés.")
    
    # Grouper par région
    by_region = defaultdict(list)
    for feature in features:
        props = feature.get("properties", {})
        commune_code = props.get("commune_id", "")
        region = determine_region(commune_code, props)
        
        converted = convert_feature(feature)
        by_region[region].append(converted)
    
    # Créer le dossier de sortie
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.mapped_only:
        # Eliminate unreferenced legacy files so estimates cannot linger on the site.
        for old_file in output_dir.glob("*.geojson.gz"):
            old_file.unlink()
    
    # Générer les fichiers régionaux
    index_regions = []
    total_features = 0
    total_mapped = 0
    total_estimated = 0
    total_urban_blocks = 0
    communes_with_features = set()
    
    for region_code in sorted(by_region.keys()):
        region_features = by_region[region_code]
        if not region_features:
            continue
        
        # Statistiques
        mapped = sum(1 for f in region_features if f["properties"]["boundaryKind"] == "mapped")
        urban_blocks = sum(1 for f in region_features if f["properties"]["boundaryKind"] == "urban_block")
        estimated = sum(1 for f in region_features if f["properties"]["boundaryKind"] == "estimated")
        communes = set(f["properties"]["commune_id"] for f in region_features)
        
        total_features += len(region_features)
        total_mapped += mapped
        total_estimated += estimated
        total_urban_blocks += urban_blocks
        communes_with_features.update(communes)
        
        # Créer le GeoJSON régional
        region_geojson = {
            "type": "FeatureCollection",
            "features": region_features,
        }
        
        # Sauvegarder en gzippé
        output_file = output_dir / f"{region_code}.geojson.gz"
        json_bytes = json.dumps(region_geojson, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with gzip.open(output_file, "wb", compresslevel=6) as f:
            f.write(json_bytes)
        
        compressed_size = output_file.stat().st_size
        bbox = compute_bbox(region_features)
        
        index_regions.append({
            "code": region_code,
            "url": f"assets/neighborhood-boundaries/{region_code}.geojson.gz",
            "bbox": bbox,
            "count": len(region_features),
            "mapped": mapped,
            "estimated": estimated,
            "urbanBlocks": urban_blocks,
            "communes": len(communes),
            "bytesCompressed": compressed_size,
        })
        
        print(f"  Région {region_code}: {len(region_features)} quartiers "
              f"({mapped} mappés, {estimated} estimés), {len(communes)} communes")
    
    # Générer l'index
    index = {
        "version": f"morocco-neighborhood-boundaries-{time.strftime('%Y-%m-%d')}-multi-source",
        "generatedAt": time.strftime("%Y-%m-%d"),
        "regions": index_regions,
        "features": total_features,
        "mappedFeatures": total_mapped,
        "estimatedFeatures": total_estimated,
        "urbanBlockFeatures": total_urban_blocks,
        "communesWithFeatures": len(communes_with_features),
        "communesTotal": 1502,  # Total communes au Maroc
        "coverage": round(100 * len(communes_with_features) / 1502, 1),
        "sources": {
            "mappedGeometry": "Polygones OpenStreetMap (place=neighbourhood/suburb/quarter)",
            "estimatedGeometry": "Îlots urbains estimés à partir de polygones fermés par des rues OSM et vérifiés par des empreintes de bâtiments",
            "gridGeometry": "Aucune cellule Voronoï synthétique publiée",
            "communeContext": "morocco-communes 1.10.0 (HCP RGPH 2024)",
        },
        "licenses": ["ODbL 1.0", "CC-BY 4.0"],
        "limitations": "Les quartiers cartographiés conservent leur géométrie source. Les îlots urbains estimés suivent des rues OSM et exigent des empreintes de bâtiments ; ils ne sont pas des quartiers officiels. La densité est estimée séparément à partir de Kontur/HDX.",
        "pipeline": "prepare-neighborhood-boundaries-multi-source.py",
    }
    
    # Sauvegarder l'index
    with open(args.index, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    
    print(f"\n→ Index sauvegardé dans {args.index}")
    print(f"→ Total: {total_features} secteurs ({total_mapped} quartiers cartographiés, {total_urban_blocks} îlots urbains estimés, {total_estimated} autres estimations)")
    print(f"→ Couverture: {len(communes_with_features)}/1502 communes ({index['coverage']}%)")
    print("\nConversion terminée !")


if __name__ == "__main__":
    main()
