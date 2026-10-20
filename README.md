# TransitLab Maroc

Carte interactive du réseau de transports au Maroc pour explorer le réseau ferré existant (Al Boraq, ONCF, tramways de Casablanca et Rabat-Salé) et **imaginer vos propres lignes**.

## Fonctionnalités

- **Exploration** : réseau ferré (Al Boraq/LGV, ONCF, tramways), gares, arrêts, 21 aéroports ONDA et l’aérodrome de Tit Mellil.
- **Choix de la commune** : recherche et zoom sur les ~1 500 communes du Maroc, classées par population.
- **Limites adaptatives** : communes dès le zoom 7 et arrondissements dès le zoom 12 dans les six grandes villes couvertes.
- **Densité de population** : RGPH 2024, par province à l’échelle nationale, par commune au zoom, puis par arrondissement dans les six grandes villes.
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
| `sw.js` | Service worker (cache statique + données) |
| `assets/data-catalog.json` | Manifeste des couches de données |
| `assets/commune-boundaries-index.json` | Emprises des 12 fichiers régionaux de communes |
| `assets/commune-boundaries/*.geojson` | Contours communaux et arrondissements, chargés au zoom et selon la zone visible |
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
| OpenStreetMap | Réseau ferré (voies, gares, arrêts), aéroports et géométries | ODbL |
| [HCP · RGPH 2024](https://www.hcp.ma/reg-casablanca/نتائج-الإحصاء-العام-للسكان-والسكنى-2024_a126.html) | Population officielle par commune et arrondissement; la densité est calculée avec la surface des polygones | CC BY 4.0 |
| OCHA / HDX (COD-AB) | Limites administratives (régions, provinces) | Données ouvertes OCHA |
| [morocco-communes](https://github.com/zkousama/morocco-communes), OpenStreetMap et HCP | 1 502 contours communaux disponibles et 41 arrondissements dans 6 villes | ODbL pour les contours ; CC BY 4.0 pour les données HCP |
| OpenStreetMap | Fond de carte tuilé (optionnel, en ligne) | ODbL |

La carte affine la densité au zoom : provinces à l’échelle nationale, communes à partir du zoom 7, puis arrondissements au zoom 12 dans Casablanca, Rabat, Salé, Fès, Tanger et Marrakech. Le HCP publie aussi des effectifs par douar en milieu rural, mais pas de population par quartier urbain à l’échelle nationale ; ces douars ne sont pas des quartiers et leurs chiffres ne sont donc pas présentés comme tels.

Attribution obligatoire : « © les contributeurs OpenStreetMap » (ODbL), « HCP » (CC BY 4.0) pour les codes et noms, « OCHA / HDX » pour les limites régionales existantes et le jeu [morocco-communes](https://github.com/zkousama/morocco-communes) pour ses limites communales et d’arrondissements.

Leaflet est vendu localement sous licence BSD-2-Clause (voir `assets/leaflet.js`).
