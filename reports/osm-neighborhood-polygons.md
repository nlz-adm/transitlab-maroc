# OSM neighbourhood polygon enrichment

- Source: current Morocco OpenStreetMap PBF extract via Geofabrik.
- Accepted tags: named place=quarter/suburb/neighbourhood/microhood/macrohood polygons and named boundary=place polygons.
- Only source-mapped Polygon/MultiPolygon features are accepted; points and lines are never converted to areas.
- City/village/admin boundaries and generic settlement tags are not promoted to neighbourhood polygons.
- Area filter: 5e-05 to 150.0 km².
- Duplicate rule: geometry overlap plus area similarity; shared name alone is not sufficient.
- Existing mapped polygons: 335
- New mapped OSM neighbourhood polygons: 23
- Duplicate OSM polygons skipped: 75
- Output candidates before commune assignment: 2

## OSM candidate audit

- accepted_osm_polygons: 98
- accepted_place_neighbourhood: 59
- accepted_place_quarter: 11
- accepted_place_suburb: 28
- deduplicated_osm_polygons: 75
- new_osm_polygons: 23
- skip_name: 94
- skip_non_polygon: 1962
- skip_not_neighborhood_tag: 4473
- skip_settlement_or_admin: 421
