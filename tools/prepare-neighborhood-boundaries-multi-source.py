#!/usr/bin/env python3
"""Pipeline multi-sources pour les limites de quartiers au Maroc.

Remplace ``tools/prepare-neighborhood-boundaries.py`` en combinant plusieurs
sources ouvertes complementaires pour maximiser la couverture :

1. **Overture Maps** (theme ``places``)  -- seeds existants (fichier local).
2. **OpenStreetMap / Overpass API**     -- nœuds/ways ``place=quarter|suburb|
   neighbourhood|locality``.
3. **GeoNames** (``MA.zip``)            -- toponymes PPLQ/PPLX/PPLL/PPLF/PPL.
4. **Poste Maroc 2018**                 -- noms de quartiers (xlsx local),
   geocodes via Nominatim.
5. **Kontur Population / HDX**          -- masque residentiel (optionnel).
6. **HCP RGPH 2024**                    -- contexte communal (optionnel).

Chaque feature produite expose dans ``properties`` :

- ``source``            : source principale du quartier
- ``sources``           : liste des sources qui confirment ce quartier
- ``sourceAgreement``   : nb de sources independantes concordantes
- ``confidence``        : score de confiance (0..1)
- ``estimated``         : bool, True si polygone issu d'une estimation Voronoi

Usage::

    python tools/prepare-neighborhood-boundaries-multi-source.py \\
        --areas assets/data/division_area.geojson \\
        --points assets/data/division.geojson \\
        --postal data/codes-postaux-quartiers-2018.xlsx \\
        --output assets/data/neighborhoods-estimated.geojson \\
        --report reports/neighborhood-coverage.md
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests
from shapely.geometry import MultiPoint, Point, Polygon, MultiPolygon, mapping, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree
from shapely.validation import make_valid

try:
    from shapely import voronoi_polygons  # type: ignore
except ImportError:  # shapely < 2.0
    from shapely.ops import voronoi_diagram as voronoi_polygons  # type: ignore


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CACHE_DIR = Path(os.environ.get("TRANSITLAB_CACHE", Path.home() / ".cache" / "transitlab-maroc"))
CACHE_TTL_SECONDS = 14 * 24 * 3600
USER_AGENT = (
    "transitlab-maroc/1.0 "
    "(+https://github.com/nlz-adm/transitlab-maroc; contact: nlz-adm)"
)

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org/search"
GEONAMES_URL = "https://download.geonames.org/export/dump/MA.zip"

# Seuils (abaisses par rapport a l'ancien pipeline)
MIN_ESTIMATED_SEEDS = 2
POPULATION_THRESHOLD = 5
RURAL_POCKET_MIN_POP = 2000

# Codes GeoNames retenus (quartiers / localites habitees)
GEONAMES_FEATURE_CODES = {"PPLQ", "PPLX", "PPLL", "PPLF", "PPLS", "PPL"}

# Tags OSM recherches via Overpass
OSM_PLACE_TAGS = "quarter|suburb|neighbourhood|locality|hamlet"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Table de translitteration arabe basique (pour comparaison)
_AR_TABLE = str.maketrans({
    "\u0623": "\u0627",  # alef hamza above -> alef
    "\u0625": "\u0627",  # alef hamza below
    "\u0622": "\u0627",  # alef madda
    "\u0649": "\u064a",  # alef maksura -> ya
    "\u0629": "\u0647",  # ta marbuta -> ha
    "\u0624": "\u0648",  # waw hamza
    "\u0626": "\u064a",  # ya hamza
    "\u0640": "",         # tatweel
})

# Stopwords a retirer pour la cle de deduplication
_STOPWORDS = {
    "quartier", "hay", "el", "al", "l", "d", "du", "de", "la", "le", "les",
    "\u062d\u064a", "\u0627\u0644", "\u0644",  # hay, al, l
}


def normalize_name(value: str) -> str:
    """Normalise un nom arabe/francais pour comparaison.

    - unifie les alefs, ta marbuta, alef maksura
    - retire les diacritiques et tatweel
    - lowercase
    - remplace les separateurs par un espace unique
    - retire les stopwords courants
    """
    if not value:
        return ""
    # Unification arabe
    value = unicodedata.normalize("NFKC", value)
    value = value.translate(_AR_TABLE)
    value = re.sub(r"[\u064b-\u0652]", "", value)  # diacritiques arabes
    # Retirer les diacritiques latines
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"[^0-9a-z\u0600-\u06ff]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    # Retirer les stopwords
    tokens = [t for t in value.split() if t not in _STOPWORDS and len(t) > 1]
    return " ".join(tokens)


def name_id(value: str) -> str:
    """Identifiant stable d'un nom (pour les fusions)."""
    n = normalize_name(value)
    return hashlib.md5(n.encode("utf-8")).hexdigest()[:12] if n else ""


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Distance haversine en km."""
    from math import asin, cos, radians, sin, sqrt
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = (sin(dlat / 2) ** 2 +
         cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2)
    return 2 * R * asin(sqrt(a))


@dataclass
class Seed:
    """Point/semence d'un quartier."""
    name: str
    lon: float
    lat: float
    source: str
    name_ar: str = ""
    name_fr: str = ""
    population: Optional[int] = None
    kind: str = "point"  # "point" ou "polygon"
    polygon: Optional[Any] = None  # shapely Polygon si kind == "polygon"

    @property
    def point(self) -> Point:
        return Point(self.lon, self.lat)

    @property
    def normalized(self) -> str:
        return normalize_name(self.name or self.name_fr or self.name_ar)


# ---------------------------------------------------------------------------
# Session HTTP avec cache
# ---------------------------------------------------------------------------

class CachedSession:
    """Session requests avec cache disque base sur l'URL."""

    def __init__(self, cache_dir: Path = CACHE_DIR, ttl: int = CACHE_TTL_SECONDS):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.cache_dir = cache_dir
        self.ttl = ttl
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, url: str) -> Path:
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{h}.bin"

    def get(self, url: str, *, force: bool = False, timeout: int = 120,
            **kwargs) -> requests.Response:
        path = self._cache_path(url)
        if not force and path.exists():
            age = time.time() - path.stat().st_mtime
            if age < self.ttl:
                print(f"    [cache] {path.name} ({age/3600:.1f}h)")
                resp = requests.Response()
                resp.status_code = 200
                resp._content = path.read_bytes()
                resp.url = url
                return resp
        print(f"    [download] {url[:80]}")
        resp = self.session.get(url, timeout=timeout, **kwargs)
        resp.raise_for_status()
        path.write_bytes(resp.content)
        return resp


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def load_overture_seeds(points_path: Optional[str]) -> List[Seed]:
    """Charge les seeds existants (fichier GeoJSON local, ex: division.geojson)."""
    seeds: List[Seed] = []
    if not points_path or not Path(points_path).exists():
        return seeds
    with open(points_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    for feat in data.get("features", []):
        props = feat.get("properties", {}) or {}
        name = (props.get("name") or props.get("name_fr")
                or props.get("name_ar") or "").strip()
        if not name:
            continue
        geom = feat.get("geometry") or {}
        if geom.get("type") == "Point":
            lon, lat = geom["coordinates"][:2]
        elif geom.get("type") in {"Polygon", "MultiPolygon"}:
            # Utiliser le centroide comme seed + conserver le polygone
            g = make_valid(shape(geom))
            c = g.centroid
            lon, lat = c.x, c.y
        else:
            continue
        seeds.append(Seed(
            name=name,
            lon=lon, lat=lat,
            source="overture",
            name_ar=props.get("name_ar", ""),
            name_fr=props.get("name_fr", ""),
            population=props.get("population"),
            kind="polygon" if geom.get("type") in {"Polygon", "MultiPolygon"} else "point",
            polygon=make_valid(shape(geom)) if geom.get("type") in {"Polygon", "MultiPolygon"} else None,
        ))
    print(f"  -> Overture: {len(seeds)} seeds charges")
    return seeds


def _osm_geometry_coords(geometry: list) -> List[Tuple[float, float]]:
    """Convertit la géométrie Overpass (lat/lon) en coordonnées Shapely (lon/lat)."""
    coords: List[Tuple[float, float]] = []
    for point in geometry or []:
        try:
            lon, lat = float(point["lon"]), float(point["lat"])
        except (KeyError, TypeError, ValueError):
            continue
        coords.append((lon, lat))
    return coords


def _stitch_osm_rings(members: list) -> List[List[Tuple[float, float]]]:
    """Assemble les ways membres d'une relation OSM en anneaux fermés."""
    segments = []
    for member in members or []:
        if member.get("type") != "way" or member.get("role", "outer") not in {"outer", "inner", ""}:
            continue
        coords = _osm_geometry_coords(member.get("geometry", []))
        if len(coords) >= 2:
            segments.append(coords)

    rings: List[List[Tuple[float, float]]] = []
    while segments:
        ring = segments.pop(0)
        changed = True
        while changed and ring[0] != ring[-1]:
            changed = False
            for i, segment in enumerate(segments):
                if segment[0] == ring[-1]:
                    ring.extend(segment[1:])
                elif segment[-1] == ring[-1]:
                    ring.extend(list(reversed(segment[:-1])))
                elif segment[-1] == ring[0]:
                    ring = segment[:-1] + ring
                elif segment[0] == ring[0]:
                    ring = list(reversed(segment[1:])) + ring
                else:
                    continue
                segments.pop(i)
                changed = True
                break
        if len(ring) >= 4 and ring[0] == ring[-1]:
            rings.append(ring)
    return rings


def _osm_element_polygon(element: dict) -> Optional[Any]:
    """Retourne le vrai polygone d'un way fermé ou d'une relation surfacique OSM."""
    element_type = element.get("type")
    if element_type == "way":
        coords = _osm_geometry_coords(element.get("geometry", []))
        if len(coords) >= 4 and coords[0] == coords[-1]:
            polygon = make_valid(Polygon(coords))
            if not polygon.is_empty and polygon.area > 0:
                return polygon
        return None

    if element_type != "relation":
        return None

    members = element.get("members", []) or []
    outer_rings = _stitch_osm_rings([m for m in members if m.get("role", "outer") == "outer"])
    inner_rings = _stitch_osm_rings([m for m in members if m.get("role") == "inner"])
    polygons = []
    for outer in outer_rings:
        holes = []
        shell = Polygon(outer)
        for inner in inner_rings:
            try:
                if shell.contains(Point(inner[0])):
                    holes.append(inner)
            except Exception:
                continue
        polygon = make_valid(Polygon(outer, holes))
        if polygon.geom_type == "Polygon":
            if not polygon.is_empty and polygon.area > 0:
                polygons.append(polygon)
        elif polygon.geom_type == "MultiPolygon":
            polygons.extend(part for part in polygon.geoms if not part.is_empty and part.area > 0)
    if not polygons:
        return None
    result = make_valid(MultiPolygon(polygons)) if len(polygons) > 1 else polygons[0]
    return None if result.is_empty else result


def osm_element_to_seed(element: dict) -> Optional[Seed]:
    """Convertit un élément place OSM en seed et conserve sa géométrie si elle est surfacique."""
    tags = element.get("tags", {}) or {}
    name = tags.get("name") or tags.get("name:fr") or tags.get("name:ar") or ""
    if not name:
        return None

    polygon = _osm_element_polygon(element)
    center = element.get("center") or {}
    lat = center.get("lat")
    lon = center.get("lon")
    if polygon is not None:
        point = polygon.representative_point()
        lon, lat = point.x, point.y
    elif element.get("type") == "node":
        lat, lon = element.get("lat"), element.get("lon")
    elif element.get("geometry"):
        coords = _osm_geometry_coords(element.get("geometry", []))
        if coords:
            lon = sum(x for x, _ in coords) / len(coords)
            lat = sum(y for _, y in coords) / len(coords)
    if lat is None or lon is None:
        return None

    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    return Seed(
        name=name,
        lon=lon, lat=lat,
        source="osm_overpass",
        name_ar=tags.get("name:ar", ""),
        name_fr=tags.get("name:fr", ""),
        kind="polygon" if polygon is not None else "point",
        polygon=polygon,
    )


def fetch_overpass(session: CachedSession) -> List[Seed]:
    """Récupère les noms de quartiers et leurs géométries de surface depuis Overpass."""
    query = f"""[out:json][timeout:300];
area["ISO3166-1"="MA"][admin_level=2]->.ma;
(
  node(area.ma)["place"~"^({OSM_PLACE_TAGS})$"];
  way(area.ma)["place"~"^(quarter|suburb|neighbourhood)$"];
  relation(area.ma)["place"~"^(quarter|suburb|neighbourhood)$"];
);
out body geom;
"""
    last_err: Optional[Exception] = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            resp = session.get(endpoint + "?data=" + requests.utils.quote(query))
            resp.raise_for_status()
            data = resp.json()
            seeds = [seed for element in data.get("elements", [])
                     if (seed := osm_element_to_seed(element)) is not None]
            mapped = sum(1 for seed in seeds if seed.polygon is not None)
            print(f"  -> Overpass: {len(seeds)} seeds, dont {mapped} contours OSM conservés "
                  f"(endpoint {endpoint.split('//')[1].split('/')[0]})")
            return seeds
        except Exception as e:
            last_err = e
            continue
    print(f"  ! Overpass: echec sur tous les endpoints ({last_err})")
    return []


def fetch_geonames(session: CachedSession) -> List[Seed]:
    """Recupere les toponymes MA.zip depuis GeoNames."""
    try:
        resp = session.get(GEONAMES_URL)
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            with z.open("MA.txt") as f:
                text = f.read().decode("utf-8")
    except Exception as e:
        print(f"  ! GeoNames: {e}")
        return []

    seeds: List[Seed] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) < 15:
            continue
        geoname_id, name, _, alternates = cols[0], cols[1], cols[2], cols[3]
        lat_s, lon_s = cols[4], cols[5]
        feat_class, feat_code = cols[6], cols[7]
        pop_s = cols[14]
        if feat_class != "P" or feat_code not in GEONAMES_FEATURE_CODES:
            continue
        try:
            lat = float(lat_s)
            lon = float(lon_s)
            pop = int(pop_s) if pop_s else None
        except ValueError:
            continue
        # Determiner la variante arabe / francaise
        has_ar = any("\u0600" <= ch <= "\u06FF" for ch in name)
        name_ar = name if has_ar else ""
        name_fr = name if not has_ar else ""
        # Chercher une variante dans alternatenames
        for alt in (alternates or "").split(","):
            alt = alt.strip()
            if not alt:
                continue
            alt_has_ar = any("\u0600" <= ch <= "\u06FF" for ch in alt)
            if alt_has_ar and not name_ar:
                name_ar = alt
            elif not alt_has_ar and not name_fr:
                name_fr = alt
        seeds.append(Seed(
            name=name, lon=lon, lat=lat,
            source="geonames",
            name_ar=name_ar, name_fr=name_fr,
            population=pop,
        ))
    print(f"  -> GeoNames: {len(seeds)} seeds")
    return seeds


def load_poste_maroc_names(postal_path: Optional[str]) -> List[str]:
    """Extrait les noms de quartiers du fichier Poste Maroc 2018 (xlsx)."""
    if not postal_path or not Path(postal_path).exists():
        return []
    try:
        import openpyxl  # type: ignore
        wb = openpyxl.load_workbook(postal_path, read_only=True, data_only=True)
    except Exception as e:
        print(f"  ! Poste Maroc: impossible d'ouvrir ({e})")
        return []
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(c or "").lower() for c in rows[0]]
    name_idx = None
    for i, h in enumerate(header):
        if "quartier" in h or h == "nom" or "localite" in h:
            name_idx = i
            break
    if name_idx is None:
        # Fallback: premiere colonne texte
        name_idx = 0
    seen: set = set()
    names: List[str] = []
    for row in rows[1:]:
        if name_idx >= len(row):
            continue
        n = (row[name_idx] or "")
        if not isinstance(n, str):
            n = str(n)
        n = n.strip()
        if not n or n.lower() in seen:
            continue
        seen.add(n.lower())
        names.append(n)
    print(f"  -> Poste Maroc: {len(names)} noms uniques")
    return names


def geocode_postal(names: List[str], session: CachedSession,
                   max_per_name: int = 3) -> List[Seed]:
    """Geocode les noms Poste Maroc via Nominatim (rate-limited)."""
    if not names:
        return []
    seeds: List[Seed] = []
    # Maroc viewbox approximatif (lon_min, lat_min, lon_max, lat_max)
    viewbox = "-13.0,21.0,-1.0,36.0"
    processed = 0
    last_req = 0.0
    for name in names:
        processed += 1
        if processed % 50 == 0:
            print(f"    geocoding {processed}/{len(names)}...")
        # Respecter la charte Nominatim : 1 req/s max
        elapsed = time.time() - last_req
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        last_req = time.time()
        params = {
            "q": name + ", Maroc",
            "format": "json",
            "limit": max_per_name,
            "countrycodes": "ma",
            "viewbox": viewbox,
            "bounded": "0",
            "addressdetails": "1",
        }
        try:
            resp = session.get(NOMINATIM_ENDPOINT, params=params)
            results = resp.json()
        except Exception:
            continue
        if not results:
            continue
        # Prendre le premier resultat de classe place / boundary
        best = None
        for r in results:
            cls = r.get("class", "")
            if cls in {"place", "boundary", "landuse"}:
                best = r
                break
        if best is None:
            best = results[0]
        try:
            lat = float(best["lat"])
            lon = float(best["lon"])
        except (KeyError, ValueError):
            continue
        display = best.get("display_name", name)
        has_ar = any("\u0600" <= ch <= "\u06FF" for ch in name)
        seeds.append(Seed(
            name=name, lon=lon, lat=lat,
            source="poste_maroc_nominatim",
            name_ar=name if has_ar else "",
            name_fr=name if not has_ar else "",
        ))
    print(f"  -> Nominatim: {len(seeds)} points geocodes")
    return seeds


# ---------------------------------------------------------------------------
# Fusion / deduplication
# ---------------------------------------------------------------------------

def merge_seeds(all_seeds: List[Seed], merge_radius_km: float = 0.7) -> List[Seed]:
    """Fusionne les seeds qui referent au meme quartier.

    Regles de fusion :
    - meme nom normalise -> fusion
    - distance < merge_radius_km ET noms normalises tres proches -> fusion
    """
    if not all_seeds:
        return []
    # Index spatial
    tree = STRtree([s.point for s in all_seeds])
    merged = list(all_seeds)  # copie
    removed: set = set()

    for i, a in enumerate(all_seeds):
        if i in removed:
            continue
        # Voisins proches
        candidates = tree.query(a.point.buffer(merge_radius_km / 111.0))
        for idx in candidates:
            j = int(idx)
            if j <= i or j in removed:
                continue
            b = all_seeds[j]
            dist = haversine_km(a.lon, a.lat, b.lon, b.lat)
            if dist > merge_radius_km:
                continue
            na, nb = a.normalized, b.normalized
            if not na or not nb:
                continue
            if na == nb:
                # Fusion : garder celui avec le plus d'info
                winner, loser = (i, j) if (a.polygon is not None, bool(a.population), len(a.name or "")) >= (b.polygon is not None, bool(b.population), len(b.name or "")) else (j, i)
                removed.add(loser)
                m = merged[winner]
                m.sources = getattr(m, "sources", {m.source}) | {all_seeds[loser].source}
            elif na in nb or nb in na:
                # Noms proches (un inclus dans l'autre)
                winner, loser = (i, j) if (a.polygon is not None, len(na), bool(a.population)) >= (b.polygon is not None, len(nb), bool(b.population)) else (j, i)
                removed.add(loser)
                m = merged[winner]
                m.sources = getattr(m, "sources", {m.source}) | {all_seeds[loser].source}

    result: List[Seed] = []
    for i, s in enumerate(merged):
        if i in removed:
            continue
        if not hasattr(s, "sources"):
            s.sources = {s.source}  # type: ignore[attr-defined]
        result.append(s)
    print(f"  -> Fusion: {len(all_seeds)} -> {len(result)} seeds ({len(all_seeds)-len(result)} fusionnes)")
    return result


# ---------------------------------------------------------------------------
# Assignment aux communes
# ---------------------------------------------------------------------------

def assign_to_communes(seeds: List[Seed], communes: List[Dict]) -> Dict[str, List[Seed]]:
    """Assigne chaque seed a sa commune contenante (ou la plus proche)."""
    commune_geoms = [make_valid(shape(c["geometry"])) for c in communes]
    tree = STRtree(commune_geoms)
    assignment: Dict[str, List[Seed]] = defaultdict(list)

    for seed in seeds:
        # 1) contains
        containing = None
        for idx in tree.query(seed.point):
            if commune_geoms[int(idx)].contains(seed.point):
                containing = communes[int(idx)]
                break
        if containing is None:
            # 2) nearest sous 10 km
            min_dist = float("inf")
            for c, g in zip(communes, commune_geoms):
                d = seed.point.distance(g)
                if d < min_dist:
                    min_dist = d
                    containing = c
            if min_dist > 0.1:  # ~10 km
                continue
        cid = containing["properties"].get("id") or containing["properties"].get("code")
        assignment[str(cid)].append(seed)

    covered = sum(1 for v in assignment.values() if len(v) >= MIN_ESTIMATED_SEEDS)
    print(f"  -> Assignment: {len(assignment)} communes touchees, "
          f"{covered} avec >= {MIN_ESTIMATED_SEEDS} seeds")
    return assignment


# ---------------------------------------------------------------------------
# Estimation Voronoi
# ---------------------------------------------------------------------------

def estimate_boundaries(commune_geom: Any, seeds: List[Seed]) -> List[Any]:
    """Genere des polygones Voronoi dans l'enveloppe communale."""
    points = [s.point for s in seeds if s.point is not None]
    if not points:
        return []
    if len(points) == 1:
        # Single seed : buffer de 800 m intersecte avec la commune
        buffered = points[0].buffer(0.008)  # ~800 m
        clipped = commune_geom.intersection(buffered)
        return [clipped] if not clipped.is_empty else []
    try:
        mp = MultiPoint(points)
        try:
            # Shapely >= 2.0 utilise `extend_to`; le fallback garde la compatibilité
            # avec les anciennes versions qui exposent `envelope`.
            polys = voronoi_polygons(mp, extend_to=commune_geom)
        except TypeError as exc:
            if "extend_to" not in str(exc):
                raise
            polys = voronoi_polygons(mp, envelope=commune_geom)
        # polys peut etre une collection
        if hasattr(polys, "geoms"):
            cells = list(polys.geoms)
        elif hasattr(polys, "__iter__"):
            cells = list(polys)
        else:
            cells = [polys]
        # Clipper avec la commune
        clipped = []
        for cell in cells:
            c = commune_geom.intersection(cell)
            if not c.is_empty:
                clipped.append(make_valid(c))
        return clipped
    except Exception as e:
        print(f"    ! Voronoi: {e}")
        return []


# ---------------------------------------------------------------------------
# Confiance
# ---------------------------------------------------------------------------

def compute_confidence(seed: Seed, agreement: int, has_polygon: bool,
                       max_sources: int) -> float:
    """Score de confiance 0..1."""
    s = 0.0
    # Agreement entre sources (0.4)
    s += 0.4 * min(1.0, agreement / max(1, max_sources - 1))
    # Polygone existant = haute qualite (0.25)
    if has_polygon:
        s += 0.25
    # Population connue (0.15)
    if seed.population and seed.population > 0:
        s += 0.15
    # Multi-script (0.1) si plus d'un type de source
    if hasattr(seed, "sources") and len(seed.sources) > 1:
        s += 0.1
    # Source de haute qualite (Overture avec polygone) (0.1)
    if seed.source == "overture" and seed.polygon:
        s += 0.1
    return round(min(1.0, s), 2)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Pipeline multi-sources")
    parser.add_argument("--areas", required=True, help="Communes (GeoJSON)")
    parser.add_argument("--points", default=None, help="Seeds Overture (GeoJSON)")
    parser.add_argument("--postal", default=None, help="Poste Maroc (xlsx)")
    parser.add_argument("--residential", default=None, help="Masque residentiel (GeoJSON)")
    parser.add_argument("--previous", default=None,
                        help="Fichier precedent pour comparaison (GeoJSON)")
    parser.add_argument("--output", default="assets/data/neighborhoods-estimated.geojson")
    parser.add_argument("--report", default="reports/neighborhood-coverage.md")
    parser.add_argument("--no-geocode", action="store_true",
                        help="Desactiver le geocodage Nominatim")
    args = parser.parse_args()

    print("=" * 70)
    print("TransitLab Maroc - Pipeline multi-sources quartiers")
    print("=" * 70)
    session = CachedSession()

    # 1) Communes
    print("\n[1/7] Chargement des communes...")
    with open(args.areas, "r", encoding="utf-8") as fh:
        communes = json.load(fh)["features"]
    print(f"  -> {len(communes)} communes")

    # 2) Seeds Overture
    print("\n[2/7] Seeds Overture (local)...")
    overture = load_overture_seeds(args.points)

    # 3) Overpass
    print("\n[3/7] Fetch Overpass (OSM direct)...")
    overpass = fetch_overpass(session)

    # 4) GeoNames
    print("\n[4/7] Fetch GeoNames MA.zip...")
    geonames = fetch_geonames(session)

    # 5) Poste Maroc + Nominatim
    print("\n[5/7] Poste Maroc + Nominatim...")
    postal_names = load_poste_maroc_names(args.postal)
    postal_seeds: List[Seed] = []
    if postal_names and not args.no_geocode:
        # Exclure les noms deja couverts par Overture/Overpass/GeoNames
        existing_names = {
            normalize_name(s.name) for s in overture + overpass + geonames
        }
        missing = [n for n in postal_names if normalize_name(n) not in existing_names]
        print(f"    {len(missing)}/{len(postal_names)} noms a geocoder")
        if missing:
            postal_seeds = geocode_postal(missing, session)

    # 6) Fusion
    print("\n[6/7] Fusion des seeds...")
    all_seeds = overture + overpass + geonames + postal_seeds
    print(f"  -> Total brut: {len(all_seeds)}")
    merged = merge_seeds(all_seeds)

    # 7) Assignment + estimation
    print("\n[7/7] Assignment + estimation Voronoi...")
    assigned = assign_to_communes(merged, communes)

    # Masque residentiel optionnel
    residential_mask = None
    if args.residential and Path(args.residential).exists():
        try:
            with open(args.residential, "r", encoding="utf-8") as fh:
                res_data = json.load(fh)
            cells = [make_valid(shape(f["geometry"]))
                     for f in res_data.get("features", [])]
            residential_mask = unary_union(cells)
            print(f"  -> Masque residentiel: {len(cells)} cellules")
        except Exception as e:
            print(f"  ! Masque residentiel: {e}")

    features: List[Dict] = []
    max_sources = 6
    stats = {
        "total": 0, "covered_communes": 0, "agreement_ge2": 0,
        "by_source": defaultdict(int), "by_region": defaultdict(lambda: {"communes": 0, "quartiers": 0}),
    }

    for commune in communes:
        cid = str(commune["properties"].get("id") or commune["properties"].get("code", ""))
        commune_geom = make_valid(shape(commune["geometry"]))
        seeds = assigned.get(cid, [])
        if not seeds:
            continue
        # Filtrer les communes trop petites sans seeds
        if len(seeds) < MIN_ESTIMATED_SEEDS:
            # Garder uniquement les seeds avec polygone existant
            good = [s for s in seeds if s.polygon]
            if not good:
                continue
            seeds = good
        stats["covered_communes"] += 1

        # Voronoi si pas de polygones existants pour tous les seeds
        seeds_with_polygon = [s for s in seeds if s.polygon]
        if len(seeds_with_polygon) == len(seeds):
            # Tous ont un polygone : les utiliser directement
            polys = [s.polygon for s in seeds]
        else:
            polys = estimate_boundaries(commune_geom, seeds)
            # Reordonner par proximite avec chaque seed
            ordered: List[Any] = []
            for s in seeds:
                if s.polygon:
                    ordered.append(s.polygon)
                    continue
                if not polys:
                    continue
                # Polygone le plus proche
                best = min(polys, key=lambda p: p.centroid.distance(s.point))
                ordered.append(best)
                polys = [p for p in polys if p is not best]
            polys = ordered

        if not polys or len(polys) != len(seeds):
            # Fallback : utiliser uniquement les seeds avec polygone
            polys = [s.polygon for s in seeds if s.polygon]
            seeds = [s for s in seeds if s.polygon]

        region = (commune["properties"].get("region")
                  or commune["properties"].get("region_name") or "?")
        stats["by_region"][region]["communes"] += 1

        for seed, poly in zip(seeds, polys):
            agreement = len(getattr(seed, "sources", {seed.source}))
            stats["by_source"][seed.source] += 1
            stats["total"] += 1
            if agreement >= 2:
                stats["agreement_ge2"] += 1
            confidence = compute_confidence(seed, agreement, bool(seed.polygon), max_sources)
            # Clip par le masque residentiel si fourni
            if residential_mask is not None:
                clipped = poly.intersection(residential_mask.buffer(0.005))
                if not clipped.is_empty and clipped.area / max(poly.area, 1e-9) > 0.3:
                    poly = make_valid(clipped)
            props = {
                "id": hashlib.md5(f"{cid}:{seed.name}".encode("utf-8")).hexdigest()[:12],
                "name": seed.name,
                "name_ar": seed.name_ar,
                "name_fr": seed.name_fr,
                "commune_id": cid,
                "commune_name": commune["properties"].get("name", ""),
                "region": region,
                "source": seed.source,
                "sources": sorted(getattr(seed, "sources", {seed.source})),
                "sourceAgreement": agreement,
                "confidence": confidence,
                "estimated": seed.polygon is None,
                "population": seed.population,
            }
            features.append({
                "type": "Feature",
                "properties": props,
                "geometry": mapping(poly),
            })
            stats["by_region"][region]["quartiers"] += 1

    # Refuser une sortie vide avant d'écraser un éventuel fichier précédent.
    if not features:
        print("ERREUR: aucune géométrie de quartier produite. Sortie précédente conservée.", file=sys.stderr)
        return 2

    # Sauvegarde GeoJSON
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": features},
                  fh, ensure_ascii=False, indent=2)
    print(f"\n-> {len(features)} features ecrites dans {output}")

    # Comparaison avec le fichier precedent
    comparison = ""
    if args.previous and Path(args.previous).exists():
        try:
            with open(args.previous, "r", encoding="utf-8") as fh:
                prev = json.load(fh)
            prev_features = prev.get("features", [])
            prev_communes = len({f.get("properties", {}).get("commune_id")
                                 for f in prev_features})
            comparison = (
                f"\n## Comparaison avant / apres\n\n"
                f"| Indicateur | Avant | Apres | Delta |\n"
                f"|---|---|---|---|\n"
                f"| Quartiers | {len(prev_features)} | {len(features)} | "
                f"{len(features)-len(prev_features):+d} |\n"
                f"| Communes couvertes | {prev_communes} | "
                f"{stats['covered_communes']} | "
                f"{stats['covered_communes']-prev_communes:+d} |\n"
            )
        except Exception:
            comparison = ""

    # Rapport Markdown
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Rapport de couverture des quartiers (multi-sources)",
        "",
        f"*Genere le {time.strftime('%Y-%m-%d %H:%M')} par "
        f"`prepare-neighborhood-boundaries-multi-source.py`*",
        "",
        "## Resume",
        "",
        f"- **{stats['total']}** quartiers detectes",
        f"- **{stats['covered_communes']}** communes couvertes (sur {len(communes)})",
        f"- **{stats['agreement_ge2']}** quartiers confirmes par >= 2 sources "
        f"({100*stats['agreement_ge2']/max(1,stats['total']):.1f} %)",
        "",
        "## Repartition par source principale",
        "",
        "| Source | Quartiers |",
        "|---|---|",
    ]
    for src in sorted(stats["by_source"]):
        lines.append(f"| `{src}` | {stats['by_source'][src]} |")
    lines += [
        "",
        "## Repartition par region",
        "",
        "| Region | Communes | Quartiers |",
        "|---|---|---|",
    ]
    for region in sorted(stats["by_region"]):
        r = stats["by_region"][region]
        lines.append(f"| {region} | {r['communes']} | {r['quartiers']} |")
    if comparison:
        lines.append(comparison)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"-> Rapport: {report_path}")
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
