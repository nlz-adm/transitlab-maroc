# Urban block sector generation

- Source roads: OpenStreetMap Geofabrik Morocco PBF, filtered to local/urban road classes.
- Source buildings: OpenStreetMap building-footprint polygons.
- Method: polygonize noded road segments; retain only enclosed polygons with building evidence; clip to commune boundaries.
- Semantics: estimated urban blocks, not official or named neighbourhood boundaries.
- Road segments loaded: 7768705
- Building footprints loaded: 1504252
- Active building tiles: 1351
- Candidate sectors: 10000
- Target minimum: 10000
- Target maximum: 10000
- Deterministic tile order: SHA1 sample across active building tiles
- Communes represented: 185
- Road class counts: {'tertiary': 1052050, 'secondary': 338043, 'primary': 295741, 'residential': 2170119, 'service': 506184, 'unclassified': 3319159, 'pedestrian': 17815, 'living_street': 69300, 'road': 294}

## Highest coverage communes

- 06.141.01.0: 4228
- 10.521.01.01: 754
- 02.167.01.09: 423
- 07.351.02.07: 258
- 01.511.01.0: 230
- 06.181.01.03: 218
- 02.167.01.03: 181
- 04.421.01.0: 160
- 07.161.07.13: 157
- 09.541.01.01: 155
- 09.541.01.02: 143
- 07.161.07.15: 118
- 05.091.01.07: 115
- 08.587.01.13: 100
- 07.041.09.11: 94
- 06.385.01.01: 89
- 02.381.05.13: 89
- 03.451.01.09: 86
- 09.541.01.13: 80
- 11.121.01.01: 77
- 01.331.01.03: 73
- 09.541.04.09: 66
- 07.351.05.05: 65
- 02.113.01.09: 61
- 07.161.01.01: 61
- 07.161.07.05: 60
- 09.541.09.45: 57
- 07.161.07.11: 56
- 08.401.03.01: 56
- 09.541.04.33: 54
