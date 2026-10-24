# Douars ruraux du RGPH 2024

TransitLab imports the HCP's published 2024 rural douar census as a separate data layer. It is a complement to the urban neighbourhood work, not a substitute for it: a douar is a rural settlement unit and must not be counted as an urban quartier.

## Sources and licences

- HCP, Population et ménages par douars selon les résultats du RGPH 2024, published October 2025: [official HCP page](https://www.hcp.ma/Population-et-menages-par-douars-selon-les-resultats-du-recensement-general-de-la-population-et-de-l-habitat-de-2024_a4208.html), [source workbook](https://www.hcp.ma/file/245768/). The census names and counts are attributed to HCP and reused under its CC BY 4.0 terms.
- The regional HCP-derived records and cautious name-to-place matches are taken from [zkousama/morocco-communes](https://github.com/zkousama/morocco-communes), pinned to commit 7204f75e503c59c7f34c2b7ced7fe47ccff5558c.
- Point locations from OpenStreetMap are ODbL; GeoNames locations are CC BY 4.0. The point source and matching method are retained per record.

## Geometry policy

The dataset contains 33,189 named douars, but only those with a supported OSM/GeoNames match receive a Point geometry (14,860 in the pinned source). The remaining records stay searchable/data-complete with a null geometry. No centroid, Voronoi polygon or border is generated for an unmatched douar. A point marks a settlement location; it is not the settlement's boundary.

The matching data describes name-based or nearby matches to HCP census codes. These are useful map locations, but users should not read them as an official survey of each douar's perimeter.

## Output

The build script writes one compressed GeoJSON chunk per region and an index at assets/douars-index.json. Chunks contain all region records so census names and household/population counts are not lost; Leaflet renders only the mapped Point features within the current viewport. At zoom levels below 8 the layer stays hidden to avoid a dense country-wide point cloud.

The existing urban layer remains separate: mapped OSM/Overture neighbourhood polygons plus road-bounded urban blocks estimated from roads/buildings. The 10,000 urban blocks are explicitly labelled as estimated blocks, not official neighbourhood limits.
