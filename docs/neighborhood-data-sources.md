# Sources de donnees - Limites de quartiers

Ce document decrit les sources ouvertes combinees par le pipeline
`tools/prepare-neighborhood-boundaries-multi-source.py` pour produire les
limites de quartiers affichees dans `assets/data/neighborhoods-estimated.geojson`.

## Vue d'ensemble

| # | Source | Type | Couverture | Licence | Role dans le pipeline |
|---|---|---|---|---|---|
| 1 | Overture Maps (places) | Polygones + points | Partielle (urbain) | ODbl / CC-BY selon contributeur | Source primaire (qualite elevee) |
| 2 | OpenStreetMap (Overpass) | Points `place=*` | Bonne | ODbL | Complement Overture |
| 3 | GeoNames `MA.txt` | Points PPLQ/PPLX/PPLL/PPLF | Tres bonne | CC-BY 4.0 | Toponymes officiels |
| 4 | Poste Maroc 2018 + Nominatim | Points geocodes | Bonne (urbain) | Donnees publiques | Noms admin postes |
| 5 | Kontur Population / HDX | Raster -> masque | Nationale | CC-BY 4.0 | Clip residentiel |
| 6 | HCP RGPH 2024 | Polygones communaux | Nationale | Licence ouverte | Contexte demographique |

## 1. Overture Maps

- **URL** : <https://docs.overturemaps.org/>
- **Theme** : `places` (sous-type `place`)
- **Extraction** : via le fichier local `assets/data/division.geojson` (seeds)
- **Points forts** : polygones de qualite, noms multilingues
- **Limites** : couverture inegale, meilleures donnees pour Casablanca/Rabat

## 2. OpenStreetMap (via Overpass)

- **Endpoint** : <https://overpass-api.de/api/interpreter>
- **Fallbacks** : `overpass.kumi.systems`, `overpass.private.coffee`
- **Requete** :
  ```overpass
  [out:json][timeout:300];
  area["ISO3166-1"="MA"][admin_level=2]->.ma;
  (
    node["place"~"^(quarter|suburb|neighbourhood|locality|hamlet)$"](area.ma);
    way["place"~"^(quarter|suburb|neighbourhood|locality|hamlet)$"](area.ma);
    relation["place"~"^(quarter|suburb|neighbourhood|locality|hamlet)$"](area.ma);
  );
  out center;
  ```
- **Points forts** : communautaire, mise a jour continue
- **Limites** : noms incomplets dans certaines villes

## 3. GeoNames

- **URL** : <https://download.geonames.org/export/dump/MA.zip>
- **Fichiers** : `MA.txt` (~30 000 entrees)
- **Feature codes retenus** :
  - `PPLQ` : quartier / section de ville
  - `PPLX` : section de ville (subdivision)
  - `PPLL` : localite peuplee
  - `PPLF` : ferme / lieu-dit habite
  - `PPLS` : etablissement
  - `PPL` : ville/village
- **Points forts** : coordonnees precises, population, noms alternatifs
- **Limites** : certains PPL sont des villages entiers, pas des quartiers

## 4. Poste Maroc 2018

- **Fichier** : `data/codes-postaux-quartiers-2018.xlsx`
- **Contenu** : noms de quartiers par commune + codes postaux
- **Traitement** : les noms non couverts par Overture/Overpass/GeoNames sont
  geocodes via l'API Nominatim (1 req/s, charte OSM).
- **Points forts** : reference administrative officielle
- **Limites** : pas de geometries, seulement des noms

## 5. Kontur Population / HDX

- **Masque residentiel** : `assets/data/kontur_population_MA.geojson`
  (fichier optionnel, genere depuis Kontur + HDX)
- **Utilisation** : clip des polygones Voronoi pour ne garder que les zones
  effectivement peuplees
- **Points forts** : couverture nationale, resolution ~100 m
- **Limites** : ne distingue pas residentiel/commercial

## 6. HCP RGPH 2024

- **Source** : Haut-Commissariat au Plan, Recensement 2024
- **Usage** : population communale (pour `RURAL_POCKET_MIN_POP`)
- **URL** : <https://www.hcp.ma/>

## Strategie de fusion

### Deduplication

Deux seeds referent au meme quartier si :
1. Leur **nom normalise** est identique, **ou**
2. Leur distance est < 700 m **et** un nom est inclus dans l'autre.

La normalisation (`normalize_name`) :
- unifie les alefs arabes (`أ إ آ` -> `ا`)
- remplace ta marbuta (`ة`) par ha (`ه`)
- retire diacritiques et tatweel (`ـ`)
- lowercase
- retire les stopwords (`quartier`, `حي`, `el`, `al`)

### Estimation Voronoi

Pour les communes sans polygones Overture :
1. Construire le diagramme de Voronoi des seeds
2. Clipper par l'enveloppe communale
3. Si le masque residentiel est fourni : intersection avec `buffer(500 m)`
4. Retirer les cellules vides

### Score de confiance

`confidence = 0.4*agreement_norm + 0.25*has_polygon + 0.15*pop_known + 0.1*multi_source + 0.1*overture_polygon`

## Proprietes produites

Chaque feature expose :

| Propriete | Type | Description |
|---|---|---|
| `id` | string | Hash MD5 (12 chars) de `commune_id:name` |
| `name` | string | Nom principal (latin ou arabe) |
| `name_ar` | string | Variante arabe si disponible |
| `name_fr` | string | Variante francaise si disponible |
| `commune_id` | string | ID de la commune parente |
| `commune_name` | string | Nom de la commune |
| `region` | string | Region administrative |
| `source` | string | Source principale du seed |
| `sources` | array | Liste de toutes les sources concordantes |
| `sourceAgreement` | integer | Nombre de sources independantes |
| `confidence` | float | Score 0..1 |
| `estimated` | boolean | `true` si polygone genere par Voronoi |
| `population` | integer | Population (si connue) |

## Execution

```bash
pip install "shapely>=2.1" requests openpyxl

python tools/prepare-neighborhood-boundaries-multi-source.py \
    --areas assets/data/division_area.geojson \
    --points assets/data/division.geojson \
    --postal data/codes-postaux-quartiers-2018.xlsx \
    --residential assets/data/kontur_population_MA.geojson \
    --previous assets/data/neighborhoods-estimated.geojson \
    --output assets/data/neighborhoods-estimated.geojson \
    --report reports/neighborhood-coverage.md
```

## Ameliorations futures

- [ ] Integrer les donnees HCP au niveau quartier (RGPH 2024 detaille)
- [ ] Scraper les plans d'amenagement urbains (agences urbaines)
- [ ] Utiliser les empreintes de batiments (Microsoft/OSM) pour affiner
- [ ] Contribuer les quartiers manquants a OpenStreetMap (effet long terme)
- [ ] Ajouter les donnees des operateurs telecom (zones de couverture)
