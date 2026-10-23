const COLOR_STOPS = [
  [0, [255, 247, 188]],
  [100, [254, 227, 145]],
  [500, [254, 196, 79]],
  [2000, [254, 153, 41]],
  [5000, [236, 112, 20]],
  [10000, [204, 76, 2]],
  [20000, [140, 45, 4]],
  [50000, [103, 25, 15]],
];

const MAX_DENSITY = COLOR_STOPS[COLOR_STOPS.length - 1][0];

/**
 * Continuous, log-scaled choropleth ramp shared by the map and its tests.
 * Null is reserved for missing data so it cannot be mistaken for zero density.
 */
export function populationNeighborhoodColor(density) {
  if (density === null || density === undefined || density === '') return null;
  const raw = Number(density);
  if (!Number.isFinite(raw) || raw < 0) return null;
  if (raw >= MAX_DENSITY) {
    return 'rgb(' + COLOR_STOPS[COLOR_STOPS.length - 1][1].join(',') + ')';
  }

  const value = Math.min(raw, MAX_DENSITY);
  const scaled = Math.log1p(value);
  for (let i = 1; i < COLOR_STOPS.length; i += 1) {
    const low = COLOR_STOPS[i - 1];
    const high = COLOR_STOPS[i];
    if (value > high[0]) continue;
    const lowScale = Math.log1p(low[0]);
    const highScale = Math.log1p(high[0]);
    const t = Math.max(0, Math.min(1, (scaled - lowScale) / (highScale - lowScale)));
    const channels = low[1].map((channel, index) =>
      Math.round(channel + (high[1][index] - channel) * t)
    );
    return 'rgb(' + channels.join(',') + ')';
  }
  return 'rgb(' + COLOR_STOPS[COLOR_STOPS.length - 1][1].join(',') + ')';
}

/**
 * Inverse-distance interpolation for small neighbourhood polygons that do not
 * contain an H3 cell centre. Returns null instead of inventing distant data.
 */
export function estimateDensityFromNearbyCells(
  point,
  cells,
  { maxDistanceKm = 1.5, maxNeighbors = 3 } = {}
) {
  if (!Array.isArray(point) || point.length < 2) return null;
  const longitude = Number(point[0]);
  const latitude = Number(point[1]);
  if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) return null;
  if (!Array.isArray(cells) || cells.length === 0) return null;

  const candidates = [];
  for (const cell of cells) {
    const center = cell?.center;
    if (!Array.isArray(center) || center.length < 2) continue;
    if (cell.density === null || cell.density === undefined || cell.density === '') continue;
    const density = Number(cell.density);
    const lon = Number(center[0]);
    const lat = Number(center[1]);
    if (!Number.isFinite(density) || density < 0 || !Number.isFinite(lon) || !Number.isFinite(lat)) continue;

    const dx = (lon - longitude) * 111.32 * Math.cos(latitude * Math.PI / 180);
    const dy = (lat - latitude) * 110.574;
    const distanceKm = Math.hypot(dx, dy);
    if (distanceKm <= maxDistanceKm) candidates.push({ density, distanceKm });
  }

  candidates.sort((a, b) => a.distanceKm - b.distanceKm);
  const nearest = candidates.slice(0, Math.max(1, Math.floor(maxNeighbors)));
  if (!nearest.length) return null;
  if (nearest[0].distanceKm <= 0.05) {
    return { density: nearest[0].density, count: 1 };
  }

  let weightedTotal = 0;
  let totalWeight = 0;
  for (const sample of nearest) {
    const weight = 1 / Math.pow(Math.max(sample.distanceKm, 0.1), 2);
    weightedTotal += sample.density * weight;
    totalWeight += weight;
  }
  if (!totalWeight) return null;
  return { density: weightedTotal / totalWeight, count: nearest.length };
}
