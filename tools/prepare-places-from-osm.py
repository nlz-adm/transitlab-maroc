#!/usr/bin/env python3
"""Refresh Morocco mall POIs and viewport-loaded education indicators from OSM.

The public data is queried inside OSM's Morocco country area, so neighbouring
countries and islands cannot leak into the result. Run without --input to fetch
the current Overpass extract, or pass a saved Overpass JSON response for repeatable
offline regeneration.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import math
import re
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
COUNTRY_AREA_QUERY = r'''[out:json][timeout:180];
area["ISO3166-1"="MA"]["admin_level"="2"]->.ma;
(
  nwr["shop"="mall"](area.ma);
  nwr["building"="mall"](area.ma);
  nwr["amenity"~"^(school|college|university)$"](area.ma);
);
out center tags;'''

MALL_TERMS = re.compile(r"\b(mall|shopping|centre|center|plaza|square|gallery|galerie|commercial|commerciale|complexe)\b")
MALL_NAME_EXCEPTIONS = {
    "carre d eden", "socco alto", "borj fez", "sela park",
}
MALL_EXCLUSIONS = (
    "marjane", "carrefour", "bim", "kiabi", "kazyon", "geant", "marché", "marche",
    "souk", "kissaria", "kissariat", "artisanat", "artisanal", "epicerie", "estanco",
    "commercial domasa", "bijouterie", "centre national", "distribution", "exposition",
    "retail park", "super marché", "supermarche", "market",
)
GENERIC_MALL_NAMES = {"centre commercial", "centre commerciale", "mall", "shopping center", "shopping centre"}
MALL_CITY_OVERRIDES = {"carre d eden": "Marrakech", "socco alto": "Tanger", "borj fez": "Fès"}

# These recognized centres are not consistently tagged or named in OSM. Their
# existence and locations were checked against official centre/operator sources.
CURATED_MALLS = [
    {
        "name": "Almazar",
        "category": "shopping",
        "lat": 31.59282,
        "lng": -7.98674,
        "city": "Marrakech",
        "detail": "Centre commercial et loisirs",
        "url": "https://almazar.ma/",
        "source": "Site officiel · OpenStreetMap",
        "sourceUrl": "https://www.openstreetmap.org/way/58250744",
    },
    {
        "name": "Le Mall du Carrousel",
        "category": "shopping",
        "lat": 33.979922,
        "lng": -6.895822,
        "city": "Rabat",
        "detail": "Centre commercial et loisirs",
        "url": "https://lemallducarrousel.ma/",
        "source": "Site officiel · OpenStreetMap",
        "sourceUrl": "https://www.openstreetmap.org/?mlat=33.979922&mlon=-6.895822#map=17/33.979922/-6.895822",
    },
    {
        "name": "Sela Park Agadir",
        "category": "shopping",
        "lat": 30.3892647,
        "lng": -9.5101288,
        "city": "Agadir",
        "detail": "Centre commercial et loisirs",
        "url": "https://sela.ma/agadir/",
        "source": "Site officiel · OpenStreetMap",
        "sourceUrl": "https://www.openstreetmap.org/?mlat=30.3892647&mlon=-9.5101288#map=16/30.3892647/-9.5101288",
    },
]


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).strip()


def mall_name_exception(normalized: str) -> str | None:
    return next((name for name in sorted(MALL_NAME_EXCEPTIONS, key=len, reverse=True)
                 if normalized == name or normalized.startswith(name + " ")), None)


def osm_name(tags: dict) -> str:
    for key in ("name:fr", "name", "name:ar", "official_name", "alt_name"):
        value = str(tags.get(key, "")).strip()
        if value:
            return value
    return ""


def coordinates(element: dict) -> tuple[float, float] | None:
    point = element.get("center") or element
    try:
        lat, lng = float(point["lat"]), float(point["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (20.0 <= lat <= 37.0 and -18.0 <= lng <= -0.5):
        return None
    return lat, lng


def osm_url(element: dict) -> str:
    return f"https://www.openstreetmap.org/{element['type']}/{element['id']}"


def classify_school(tags: dict) -> str:
    amenity = str(tags.get("amenity", "")).casefold()
    text = " ".join(str(tags.get(key, "")) for key in ("name", "name:fr", "name:ar", "official_name", "school", "education"))
    key = normalize(text)
    arabic = text
    school_level = normalize(tags.get("school", ""))
    education_level = normalize(tags.get("education", ""))
    levels = {school_level, education_level}

    if amenity == "university" or "universit" in key or "university" in key or "faculte" in key or "faculty" in key:
        return "universite"
    if any(term in key for term in ("ecole superieure", "institut superieur", "higher education", "engineering school", "business school", "faculte", "faculty")):
        return "superieur"
    if re.search(r"\b(fs|fst|fsjes|ens|ensa|encg|est|ehtp|iscae|ensam|emsi)\b", key):
        return "superieur"

    # In Morocco, the word collège / إعدادية identifies the lower secondary cycle.
    if any(term in key for term in ("college", "collegial", "lower secondary", "middle school", "ecole preparatoire")):
        return "college"
    if any(term in arabic for term in ("إعدادية", "اعدادية", "إعدادي", "اعدادي")):
        return "college"
    if levels & {"middle", "middle school", "junior secondary", "lower secondary", "college", "collegiate"}:
        return "college"

    # ثانوية إعدادية has already been classified as collège above.
    if any(term in key for term in ("lycee", "qualifiant", "high school", "upper secondary", "secondary school")):
        return "lycee"
    if any(term in arabic for term in ("ثانوية تأهيلية", "ثانوي تأهيلي", "ثانوية", "ثانوي")):
        return "lycee"
    if levels & {"high", "high school", "upper secondary", "secondary", "senior secondary"}:
        return "lycee"

    if amenity == "college":
        # OSM uses amenity=college for tertiary education unless the local name
        # explicitly says collège; do not label that as Moroccan lower secondary.
        return "superieur"
    return "ecole"


def mall_features(elements: list[dict]) -> list[dict]:
    candidates = []
    for element in elements:
        tags = element.get("tags", {})
        if tags.get("shop") != "mall" and tags.get("building") != "mall":
            continue
        name = osm_name(tags)
        normalized = normalize(name)
        name_exception = mall_name_exception(normalized)
        point = coordinates(element)
        if not name or not normalized or not point or (not MALL_TERMS.search(normalized) and not name_exception):
            continue
        if normalized in GENERIC_MALL_NAMES or any(term in normalized for term in MALL_EXCLUSIONS):
            continue
        lat, lng = point
        city = tags.get("addr:city:fr") or tags.get("addr:city") or tags.get("addr:town") or ""
        city = re.split(r"\s+(?=[\u0600-\u06ff\u2d30-\u2d7f])", city, maxsplit=1)[0].strip()
        city = MALL_CITY_OVERRIDES.get(name_exception or "") or city
        website = tags.get("contact:website") or tags.get("website") or ""
        candidates.append({
            "name": name, "category": "shopping", "lat": lat, "lng": lng,
            "city": city, "detail": "Centre commercial · OpenStreetMap",
            "url": website, "source": "OpenStreetMap",
            "sourceUrl": osm_url(element), "_priority": {"node": 0, "way": 1, "relation": 2}.get(element.get("type"), 0),
        })

    # A mall may be represented as a point and as an outline; keep the richer
    # area representation when the same normalized name is within 300 metres.
    candidates.sort(key=lambda row: row["_priority"], reverse=True)
    deduped: list[dict] = []
    for place in candidates:
        duplicate = False
        for prior in deduped:
            place_words = set(normalize(place["name"]).split())
            prior_words = set(normalize(prior["name"]).split())
            shared_center_name = place_words == prior_words or (min(len(place_words), len(prior_words)) >= 2 and (place_words.issubset(prior_words) or prior_words.issubset(place_words)))
            if not shared_center_name:
                continue
            dx = (place["lng"] - prior["lng"]) * 88.0
            dy = (place["lat"] - prior["lat"]) * 111.0
            if math.hypot(dx, dy) <= 0.3:
                duplicate = True
                break
        if not duplicate:
            deduped.append(place)

    for place in CURATED_MALLS:
        if any(math.hypot((place["lng"] - prior["lng"]) * 88, (place["lat"] - prior["lat"]) * 111) < 0.35 for prior in deduped):
            continue
        deduped.append(dict(place))
    for place in deduped:
        place.pop("_priority", None)
    return sorted(deduped, key=lambda row: (normalize(row.get("city", "")), normalize(row["name"])))


def education_features(elements: list[dict]) -> tuple[list[dict], dict[str, int]]:
    features = []
    for element in elements:
        tags = element.get("tags", {})
        if tags.get("amenity") not in {"school", "college", "university"}:
            continue
        name = osm_name(tags)
        point = coordinates(element)
        if not name or not point:
            continue
        lat, lng = point
        kind = classify_school(tags)
        city = tags.get("addr:city") or tags.get("addr:town") or tags.get("addr:village") or ""
        eid = f"{element['type']}/{element['id']}"
        features.append({
            "id": eid,
            "geometry": {"type": "Point", "coordinates": [round(lng, 6), round(lat, 6)]},
            "properties": {
                "name": name,
                "type": kind,
                "city": city,
                "source": "OpenStreetMap",
                "osmUrl": osm_url(element),
            },
        })

    # Merge duplicated named nodes/areas, retaining richer geometry records.
    features.sort(key=lambda feature: {"node": 0, "way": 1, "relation": 2}.get(feature["id"].split("/", 1)[0], 0), reverse=True)
    accepted: list[dict] = []
    bins: dict[tuple[str, int, int], list[int]] = collections.defaultdict(list)
    for feature in features:
        props = feature["properties"]
        lng, lat = feature["geometry"]["coordinates"]
        name_key = normalize(props["name"])
        gx, gy = round(lng * 500), round(lat * 500)
        is_duplicate = False
        for bx in range(gx - 1, gx + 2):
            for by in range(gy - 1, gy + 2):
                for index in bins.get((name_key, bx, by), []):
                    prior = accepted[index]
                    plng, plat = prior["geometry"]["coordinates"]
                    if prior["properties"]["type"] != props["type"]:
                        continue
                    if math.hypot((lng - plng) * 88.0, (lat - plat) * 111.0) <= 0.18:
                        is_duplicate = True
                        break
                if is_duplicate:
                    break
            if is_duplicate:
                break
        if not is_duplicate:
            bins[(name_key, gx, gy)].append(len(accepted))
            accepted.append(feature)

    counts = dict(collections.Counter(feature["properties"]["type"] for feature in accepted))
    return accepted, counts


def fetch_overpass() -> dict:
    request = urllib.request.Request(
        OVERPASS_URL,
        data=urllib.parse.urlencode({"data": COUNTRY_AREA_QUERY}).encode("utf-8"),
        headers={"User-Agent": "TransitLabMaroc/1.0 (https://github.com/nlz-adm/transitlab-maroc)"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=240) as response:
        return json.load(response)


def write_education_chunks(features: list[dict]) -> tuple[dict, int]:
    chunks: dict[tuple[int, int], list[dict]] = collections.defaultdict(list)
    for feature in features:
        lng, lat = feature["geometry"]["coordinates"]
        chunks[(math.floor((lng + 180) / 0.5), math.floor((lat + 90) / 0.5))].append(feature)

    output_dir = ROOT / "assets" / "education-establishments"
    output_dir.mkdir(parents=True, exist_ok=True)
    for old in output_dir.glob("*.geojson.gz"):
        old.unlink()

    index_chunks = []
    bytes_total = 0
    for (x, y), records in sorted(chunks.items()):
        feature_collection = {"type": "FeatureCollection", "features": records}
        payload = json.dumps(feature_collection, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        compressed = gzip.compress(payload, compresslevel=9, mtime=0)
        chunk_id = f"{x}_{y}"
        (output_dir / f"{chunk_id}.geojson.gz").write_bytes(compressed)
        bytes_total += len(compressed)
        index_chunks.append({
            "id": chunk_id,
            "url": f"assets/education-establishments/{chunk_id}.geojson.gz",
            "bbox": [x * 0.5 - 180, y * 0.5 - 90, (x + 1) * 0.5 - 180, (y + 1) * 0.5 - 90],
            "features": len(records),
        })
    index = {
        "source": "OpenStreetMap contributors",
        "sourceDate": "",
        "license": "ODbL 1.0",
        "resolution": "Établissements cartographiés par points ou centres d’emprises",
        "featureCount": len(features),
        "countsByType": dict(collections.Counter(feature["properties"]["type"] for feature in features)),
        "chunkSizeDegrees": 0.5,
        "compressedBytes": bytes_total,
        "chunks": index_chunks,
    }
    return index, bytes_total


def write_search_index(features: list[dict]) -> int:
    records = []
    for feature in features:
        lng, lat = feature["geometry"]["coordinates"]
        props = feature["properties"]
        records.append({"name": props["name"], "type": props["type"], "city": props["city"], "lat": lat, "lng": lng, "osmUrl": props["osmUrl"]})
    compressed = gzip.compress(json.dumps({"records": records}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), compresslevel=9, mtime=0)
    (ROOT / "assets" / "education-establishments-search.json.gz").write_bytes(compressed)
    return len(compressed)


def update_static_data(malls: list[dict]) -> None:
    network_path = ROOT / "assets" / "network-data.json"
    network = json.loads(network_path.read_text(encoding="utf-8"))
    preserved = [poi for poi in network.get("pois", []) if poi.get("category") != "shopping"]
    network["pois"] = preserved + malls
    network_path.write_text(json.dumps(network, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")

    optional_path = ROOT / "assets" / "network-optional.json"
    optional = json.loads(optional_path.read_text(encoding="utf-8"))
    optional.pop("educationPlaces", None)
    optional_path.write_text(json.dumps(optional, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, help="saved Overpass JSON response; otherwise fetch a fresh response")
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8")) if args.input else fetch_overpass()
    elements = data.get("elements", [])
    malls = mall_features(elements)
    schools, counts = education_features(elements)
    update_static_data(malls)
    source_timestamp = data.get("osm3s", {}).get("timestamp_osm_base", "")
    index, compressed_bytes = write_education_chunks(schools)
    index["sourceDate"] = source_timestamp
    index_path = ROOT / "assets" / "education-establishments-index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    search_bytes = write_search_index(schools)
    print(json.dumps({"shoppingCenters": len(malls), "educationPlaces": len(schools), "educationTypes": counts,
                      "educationChunks": len(index["chunks"]), "chunkBytes": compressed_bytes,
                      "searchBytes": search_bytes, "osmTimestamp": source_timestamp}, ensure_ascii=False))


if __name__ == "__main__":
    main()
