# Mapped neighbourhood polygon assignment

- Input geometry: source-mapped OSM/Overture polygons only; no point-to-Voronoi conversion.
- Commune assignment: maximum polygon overlap, then intersection-clipping to that commune.
- Input source polygons: 335
- Accepted mapped polygons: 335
- Polygons clipped to commune: 84
- Rejected as estimated: 0
- Rejected (missing name): 0
- Rejected (invalid/non-polygon geometry): 0
- Rejected (no usable commune overlap): 0

Only actual mapped polygons are emitted; urban coverage gaps are handled by the separate road-bounded sector layer.
