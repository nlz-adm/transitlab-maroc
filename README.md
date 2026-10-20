# TransitLab Maroc

Carte interactive du réseau de transports au Maroc pour explorer le réseau ferré existant (Al Boraq, ONCF, tramways de Casablanca et Rabat-Salé) et **imaginer vos propres lignes**.

## Fonctionnalités

- **Exploration** : réseau ferré (Al Boraq/LGV, ONCF, tramways), gares et arrêts.
- **Choix de la commune** : recherche et zoom sur les ~1 500 communes du Maroc, classées par population.
- **Limites adaptatives** : régions à l’échelle du pays, communes à partir du zoom 8 et arrondissements à partir du zoom 12 dans les villes couvertes.
- **Création de lignes** : tracez une ligne hypothétique, raccordez-la aux gares, personnalisez nom et couleur (logo généré automatiquement).
- **Exports** : GeoJSON, aperçu du tracé (PNG), plan schématique (SVG, PDF, impression).
- **Hors ligne** : service worker + cache IndexedDB, y compris les limites déjà consultées (le fond de carte tuilé reste optionnel et en ligne).
- **PWA** : installable (manifest web).

## Architecture

Site 100 % statique (aucun backend, aucune dépendance d'exécution externe hors fond de carte optionnel).

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
| OpenStreetMap | Réseau ferré (voies, gares, arrêts), communes (centres, noms, population) | ODbL |
| OCHA / HDX (COD-AB) | Limites administratives (régions, provinces) | Données ouvertes OCHA |
| [morocco-communes](https://github.com/zkousama/morocco-communes), OpenStreetMap et HCP | 1 502 contours communaux sur 1 503, 41 arrondissements dans 6 villes, codes et noms | ODbL pour les contours ; CC BY 4.0 pour les données HCP |
| OpenStreetMap | Fond de carte tuilé (optionnel, en ligne) | ODbL |

Les limites de quartiers n’ont pas de couverture nationale homogène. À fort zoom, la carte affiche les contours d’arrondissements disponibles dans les six villes du jeu de données ; elle ne fabrique pas de limites pour les quartiers non cartographiés.

Attribution obligatoire : « © les contributeurs OpenStreetMap » (ODbL), « HCP » (CC BY 4.0) pour les codes et noms, « OCHA / HDX » pour les limites régionales existantes et le jeu [morocco-communes](https://github.com/zkousama/morocco-communes) pour ses limites communales et d’arrondissements.

Leaflet est vendu localement sous licence BSD-2-Clause (voir `assets/leaflet.js`).
