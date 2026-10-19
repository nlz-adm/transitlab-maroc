# TransitLab Maroc

Carte interactive du réseau de transports au Maroc pour explorer le réseau ferré existant (Al Boraq, ONCF, tramways de Casablanca et Rabat-Salé) et **imaginer vos propres lignes**.

## Fonctionnalités

- **Exploration** : réseau ferré (Al Boraq/LGV, ONCF, tramways), gares et arrêts.
- **Choix de la commune** : recherche et zoom sur les ~1 500 communes du Maroc, classées par population.
- **Couches** : limites des régions, limites des provinces, aéroports.
- **Création de lignes** : tracez une ligne hypothétique, raccordez-la aux gares, personnalisez nom et couleur (logo généré automatiquement).
- **Exports** : GeoJSON, aperçu du tracé (PNG), plan schématique (SVG, PDF, impression).
- **Hors ligne** : service worker + cache IndexedDB (le fond de carte tuilé reste optionnel et en ligne).
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
| `assets/*.json` | Données géographiques (réseau, communes, provinces) |
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
| OpenStreetMap | Fond de carte tuilé (optionnel, en ligne) | ODbL |

Attribution obligatoire : « © les contributeurs OpenStreetMap » (ODbL) et « OCHA / HDX » pour les limites administratives.

Leaflet est vendu localement sous licence BSD-2-Clause (voir `assets/leaflet.js`).
