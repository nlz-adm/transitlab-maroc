# Method for reliable neighbourhood-boundary coverage

## Source hierarchy

1. **Mapped polygon boundaries**: Overture Maps `division_area` features whose subtype is `neighborhood`, `microhood`, or `macrohood`, plus named OSM polygons tagged `place=quarter/suburb/neighbourhood` or `boundary=place`. Retain source IDs, subtype, retrieval time and licence. The OSM polygon enrichment must discard points and lines, exclude generic city/village/admin boundaries, clip to a commune, and deduplicate by geometry rather than by name alone.
2. **Official census geography**: HCP census districts are a separate geography level. HCP's 2014 cartographic material shows urban census-district boundaries, urban blocks and streets; these must be digitized/georeferenced before use and labelled as census districts, not automatically renamed as neighbourhoods. Reference: https://www.rgph2014.hcp.ma/Cartographie-du-recensement_a32.html
3. **Population estimates**: GHSL/Kontur/HDX raster or grid data can support the colour value only. They must never be used to fabricate a neighbourhood boundary.

## Integrity rules

- Never present Voronoi cells around place points as real neighbourhood limits.\n- A named point, postal row, locality label, or census unit is not itself a neighbourhood boundary. Only publish area geometry supplied by an actual source, and keep any later digitized/estimated boundary type explicit.
- Prefer a source-mapped polygon over a place point when the same entity is found in more than one source.
- Clip imported polygons to the Moroccan commune boundary union and assign them to the commune by maximum polygon overlap.
- Preserve per-feature source metadata and the ODbL attribution.
- If a source refresh returns no new geometry or fails validation, stop the update and retain the prior published dataset.
- Publish coverage counts by source and commune so low-coverage cities are visible instead of hidden by country-wide totals.

## Current known limit

The currently published mapped-only layer has broad gaps. The Overture importer is designed to add area geometries from the official Overture release, but the number actually added must be read from the run's generated audit report. HCP district maps offer a further route for priority urban areas, but these are census districts and not a ready-made nationwide GeoJSON of neighbourhoods.
