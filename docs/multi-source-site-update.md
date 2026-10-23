# Documentation : Mise à jour multi-source du site

## 📋 Vue d'ensemble

Cette mise à jour intègre le **pipeline multi-source** (PR #10) au site TransitLab Maroc, permettant une régénération automatique des limites de quartiers avec 6 sources de données complémentaires.

## 🔄 Nouveaux fichiers

### 1. `tools/convert-multi-source-to-site-format.py`
Script de conversion qui transforme la sortie du pipeline multi-source (`assets/data/neighborhoods-estimated.geojson`) au format attendu par le site :
- Découpage par région (12 fichiers `.geojson.gz`)
- Génération de l'index `neighborhood-boundaries-index.json`
- Mapping des propriétés (`source`, `sourceAgreement`, `confidence`)

### 2. `.github/workflows/update-neighborhoods.yml`
Workflow GitHub Actions qui automatise la régénération des données :
- **Déclencheur manuel** : onglet Actions → "Update Neighborhood Boundaries" → Run workflow
- **Déclencheur planifié** : chaque dimanche à 3h UTC
- **Déclencheur auto** : push sur les scripts du pipeline

## 📊 Améliorations des données

### Avant (pipeline simple)
- **Source unique** : Overture Maps
- **Couverture** : 51 communes sur 1 502 (~3,4 %)
- **Pas de score de confiance**

### Après (pipeline multi-source)
- **6 sources combinées** :
  1. Overture Maps (seeds existants)
  2. OpenStreetMap / Overpass API
  3. GeoNames MA.txt
  4. Poste Maroc 2018 + Nominatim
  5. Kontur Population / HDX
  6. HCP RGPH 2024
- **Couverture estimée** : 400-600 communes (30-40 %)
- **Score `sourceAgreement`** : nombre de sources qui confirment chaque quartier
- **Score `confidence`** : 0..1 basé sur l'accord, la qualité des données, etc.

## 🚀 Utilisation

### Option A : Régénération automatique (recommandé)

Le workflow GitHub Actions s'exécute automatiquement chaque semaine. Pour forcer une exécution manuelle :

1. Allez dans l'onglet **Actions** du dépôt
2. Sélectionnez **Update Neighborhood Boundaries**
3. Cliquez sur **Run workflow**
4. Attendez la fin de l'exécution (~5-10 minutes)
5. Les fichiers seront automatiquement commités et poussés

### Option B : Régénération locale

```bash
# 1. Installer les dépendances
pip install "shapely>=2.1" requests openpyxl

# 2. Exécuter le pipeline multi-source
python tools/prepare-neighborhood-boundaries-multi-source.py \
    --areas assets/data/division_area.geojson \
    --points assets/data/division.geojson \
    --postal data/codes-postaux-quartiers-2018.xlsx \
    --output assets/data/neighborhoods-estimated.geojson \
    --report reports/neighborhood-coverage.md

# 3. Convertir au format site
python tools/convert-multi-source-to-site-format.py \
    --input assets/data/neighborhoods-estimated.geojson \
    --output-dir assets/neighborhood-boundaries \
    --index assets/neighborhood-boundaries-index.json

# 4. Commiter les changements
git add assets/neighborhood-boundaries/ assets/neighborhood-boundaries-index.json reports/
git commit -m "chore(data): mise à jour des limites de quartiers (multi-source)"
git push
```

## 🎨 Affichage sur la carte

Le code JavaScript actuel (`assets/map-app.js`) supporte déjà les propriétés générées :

### Propriétés utilisées
- **`boundaryKind`** : `"mapped"` (contour OSM) ou `"estimated"` (Voronoi)
  - Affichage : traits continus (mapped) vs pointillés (estimated)
  - Couleurs : cyan (mapped) vs orange (estimated)

### Propriétés additionnelles (pipeline multi-source)
- **`source`** : source principale (`overture`, `osm_overpass`, `geonames`, `poste_maroc_nominatim`)
- **`sources`** : liste de toutes les sources qui confirment ce quartier
- **`sourceAgreement`** : nombre de sources concordantes (1-6)
- **`confidence`** : score de confiance 0..1

Ces propriétés sont disponibles dans les données mais pas encore affichées dans l'interface. Une future mise à jour du JS pourra les utiliser pour :
- Afficher la source dans le popup
- Filtrer par niveau de confiance
- Colorer selon le nombre de sources concordantes

## 📈 Résultats attendus

Après la première exécution du pipeline :

| Indicateur | Avant | Après |
|---|---|---|
| Quartiers totaux | ~7 200 | ~8 000-10 000 |
| Communes couvertes | 51 | 400-600 |
| Quartiers ≥ 2 sources | 0 | ~40 % |
| Temps de génération | ~5 min | ~10-15 min |

## 🔍 Vérification

Après la mise à jour, vérifiez :

1. **Index mis à jour** :
   ```bash
   cat assets/neighborhood-boundaries-index.json | jq '.coverage'
   # Devrait afficher > 30%
   ```

2. **Rapport de couverture** :
   ```bash
   cat reports/neighborhood-coverage.md
   # Devrait contenir les statistiques par région
   ```

3. **Site fonctionnel** :
   - Ouvrez `carte-interactive-maroc.html`
   - Activez "Limites communales et quartiers"
   - Zoomez au niveau 13+ pour voir les quartiers
   - Vérifiez que les nouvelles communes sont couvertes

## 🐛 Dépannage

### Le workflow échoue
- Vérifiez les logs dans l'onglet Actions
- Causes courantes :
  - Timeout sur Overpass API (réessayez)
  - Fichier `division_area.geojson` manquant
  - Dépendances Python non installées

### Pas de changements commités
- Le workflow vérifie s'il y a des changements avant de commiter
- Si les données n'ont pas changé, aucun commit n'est créé
- C'est normal si les sources n'ont pas été mises à jour

### Erreur de conversion
- Vérifiez que `neighborhoods-estimated.geojson` existe
- Vérifiez les logs du script de conversion
- Assurez-vous que les codes communes sont valides

## 📝 Notes techniques

### Cache HTTP
Les scripts utilisent un cache local (`~/.cache/transitlab-maroc`) pour éviter de re-télécharger les gros fichiers (GeoNames, Kontur) à chaque exécution. Le cache expire après 14 jours.

### Rate limiting
- **Nominatim** : 1 requête/seconde (charte OpenStreetMap)
- **Overpass** : timeout de 300s, 3 endpoints de fallback

### Normalisation des noms
Les noms arabes et français sont normalisés pour permettre la fusion des seeds :
- Unification des alefs (أ إ آ → ا)
- Retrait des diacritiques et tatweel
- Lowercase et suppression des stopwords

## 🔗 Ressources

- **PR #10** : Pipeline multi-source initial
- **Issue #9** : Suivi du projet
- **Documentation des sources** : `docs/neighborhood-data-sources.md`
- **Script pipeline** : `tools/prepare-neighborhood-boundaries-multi-source.py`
- **Script conversion** : `tools/convert-multi-source-to-site-format.py`
