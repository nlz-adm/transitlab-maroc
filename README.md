# TransitLab Maroc

Carte interactive du réseau de transports au Maroc pour explorer le réseau ferré existant (Al Boraq, ONCF, tramways de Casablanca et Rabat-Salé) et **imaginer vos propres lignes**.

## Fonctionnalités

- **Exploration** : réseau ferré (Al Boraq/LGV, ONCF, tramways), gares, arrêts, 21 aéroports ONDA et l’aérodrome de Tit Mellil.
- **Choix de la commune** : recherche et zoom sur les ~1 500 communes du Maroc, classées par population.
- **Limites adaptatives** : communes dès le zoom 7, arrondissements dès le zoom 12 et quartiers urbains dès le zoom 13 dans les zones couvertes. Les contours continus sont cartographiés ; les pointillés sont des estimations multi-sources.
- **Densité de population** : chiffres du RGPH 2024 par province, commune et arrondissement, complétés au zoom par des mailles estimées multi-sources d’environ 400 m.
- **Équipements** : 50 centres commerciaux, galeries et parcs commerciaux nommés (hors magasins isolés, enseignes seules et marchés) et 4 601 indicateurs d’enseignement OpenStreetMap, chargés au zoom utile et classés par niveau quand les tags ou le nom le permettent.
- **Création de lignes** : tracez une ligne hypothétique, raccordez-la aux gares, personnalisez nom et couleur (logo généré automatiquement).
- **Exports** : GeoJSON, aperçu du tracé (PNG), plan schématique (SVG, PDF, impression).
- **Hors ligne** : service worker + cache IndexedDB, y compris les limites déjà consultées (le fond OpenStreetMap s’affiche par défaut et nécessite une connexion internet).
- **PWA** : installable (manifest web).

## Architecture

Site 100 % statique (aucun backend, tuiles OpenStreetMap chargées en ligne).

| Fichier | Rôle |
|---|---|
| `index.html` | Page d'accueil (présentation + carte intégrée) |
| `carte-interactive-maroc.html` | Application cartographique |
| `assets/map-app.js` | Moteur de la carte (Leaflet) |
| `assets/map-boot.js` | Point d'entrée différé (chargement paresseux) |
| `assets/line-index-worker.js` | Web worker (index des tronçons partagés) |
| `tools/prepare-population-neighborhoods.py` | Reproduit les mailles compressées à partir du GeoPackage Kontur/HDX |
| `tools/prepare-neighborhood-boundaries.py` | Croise les quartiers Overture/OSM, les communes urbaines, la grille Kontur/HDX et le catalogue postal Poste Maroc (Shapely 2.1+) |
| `tools/prepare-places-from-osm.py` | Rafraîchit les centres commerciaux filtrés et les établissements d’enseignement OSM |
| `sw.js` | Service worker (cache statique + données) |
| `assets/data-catalog.json` | Manifeste des couches de données |
| `assets/commune-boundaries-index.json` | Emprises des 12 fichiers régionaux de communes |
| `assets/commune-boundaries/*.geojson` | Contours communaux et arrondissements, chargés selon le zoom et la zone visible |
| `assets/neighborhood-boundaries-index.json` et `assets/neighborhood-boundaries/*.geojson.gz` | Contours cartographiés et limites estimées des quartiers, chargés à partir du zoom 13 dans la zone visible |
| `assets/population-neighborhoods-index.json` et `assets/population-neighborhoods/*.geojson.gz` | Mailles fines de population, chargées seulement dans la zone visible au zoom 11+ |
| `assets/education-establishments-index.json`, `assets/education-establishments/*.geojson.gz` | Écoles et universités, fichiers de zone chargés à la demande au zoom rapproché |
| `assets/*.json` | Réseau, communes recherchables et données complémentaires |
| `manifest.json` | Manifeste d'application web (PWA) |

## Développement

Aucune étape de build n'est requise. Lancez un serveur statique :

```bash
npx serve .
# ou : python -m http.server 8000
```

> Ouvrir directement `index.html` (`file://`) fonctionne partiellement, mais le service worker et l'import des modules exigent HTTP.

## Déploiement

Hébergé sur GitHub Pages. Publiez la branche `main` dans les réglages du dépôt.

## Données & licences

Le **code** est distribué sous licence MIT (voir [`LICENSE`](LICENSE)). Les **données** conservent leurs licences propres :

| Source | Contenu | Licence |
|---|---|---|
| [OpenStreetMap](https://www.openstreetmap.org/) / [Overture Maps divisions](https://docs.overturemaps.org/guides/divisions/) | Réseau ferré, lieux, centres commerciaux et établissements scolaires; contours de quartiers OSM et points utilisés pour les limites estimées | ODbL |
| [Poste Maroc · codes postaux des quartiers](https://www.data.gov.ma/data/fr/dataset/codes-postaux-des-quartiers) | Noms et codes postaux 2018, utilisés seulement lorsqu’un quartier est rapproché exactement | ODbL |
| [Ministère de l’Éducation nationale · Portail Open Data](https://data.gov.ma/data/fr/dataset/api_dataset/listes-des-etablissements-scolaires-publics) | Répertoire officiel des établissements publics, par cycle (consultation de référence; positions de la carte tirées d’OpenStreetMap) | ODbL |
| [HCP · RGPH 2024](https://www.hcp.ma/reg-casablanca/نتائج-الإحصاء-العام-للسكان-والسكنى-2024_a126.html) | Population officielle par commune et arrondissement; la densité est calculée avec la surface des polygones | CC BY 4.0 |
| [Kontur Population · HDX](https://data.humdata.org/dataset/38e5a215-ebdf-4ef8-b52a-96bd03b4de9e) | Mailles H3 d’environ 400 m, édition 2023; fusion de GHSL, Meta HRSL, empreintes Microsoft, couverture Copernicus, Land Information New Zealand et OpenStreetMap | CC BY 4.0 |
| OCHA / HDX (COD-AB) | Limites administratives (régions, provinces) | Données ouvertes OCHA |
| [morocco-communes](https://github.com/zkousama/morocco-communes), OpenStreetMap et HCP | 1 502 contours communaux disponibles et 41 arrondissements dans 6 villes | ODbL pour les contours ; CC BY 4.0 pour les données HCP |
| OpenStreetMap | Fond de carte tuilé (optionnel, en ligne) | ODbL |

La couche de limites est désactivée au démarrage et s’active depuis « Limites communales et quartiers ». À partir du zoom 13, elle affiche des contours cartographiés et des secteurs estimatifs associés à des lieux habités, découpés par les limites communales. Les secteurs estimatifs sont une aide visuelle exploratoire et ne représentent pas des limites officielles. Les nombres de secteurs, leur répartition et la couverture du jeu publié sont décrits dans `assets/data-catalog.json` et `assets/neighborhood-boundaries-index.json`. Le pipeline multi-sources est en développement ; toute nouvelle génération doit passer des contrôles de qualité avant de remplacer le jeu publié.

Les repères commerciaux sont limités aux lieux nommés cartographiés comme centre/mall, complétés par quelques centres vérifiés sur les sites de leurs gestionnaires. Les boutiques isolées, enseignes de grande distribution, marchés et souks sont filtrés. La liste contient actuellement 50 lieux et ne constitue pas un recensement exhaustif. Les établissements scolaires proviennent des objets OpenStreetMap; leur classification collège/lycée s’appuie sur les tags disponibles et les noms (français ou arabes). Les positions et la couverture peuvent varier et ne constituent pas un inventaire exhaustif du ministère. Le répertoire public MENPS est consultable séparément.

Attribution obligatoire : « © les contributeurs OpenStreetMap, Overture Maps Foundation » (ODbL), « Poste Maroc » (ODbL) pour les noms et codes postaux, « HCP » (CC BY 4.0) pour les chiffres du RGPH 2024, « Kontur Population / HDX » (CC BY 4.0) pour la grille résidentielle multi-sources, « OCHA / HDX » pour les limites régionales et le jeu [morocco-communes](https://github.com/zkousama/morocco-communes) pour les contours communaux et d’arrondissements.

Leaflet est vendu localement sous licence BSD-2-Clause (voir `assets/leaflet.js`).
