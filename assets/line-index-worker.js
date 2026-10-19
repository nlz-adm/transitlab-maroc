'use strict';

function coordKey(coord) {
  return Number(coord[0]).toFixed(5) + ',' + Number(coord[1]).toFixed(5);
}

function edgeKey(a, b) {
  const first = coordKey(a);
  const second = coordKey(b);
  return first < second ? first + '|' + second : second + '|' + first;
}

self.onmessage = ({ data }) => {
  if (data?.type !== 'indexSharedTracks') return;
  const edges = new Map();
  for (const feature of data.lines || []) {
    if (feature?.geometry?.type !== 'LineString') continue;
    const name = feature.properties?.line;
    const coordinates = feature.geometry.coordinates || [];
    for (let index = 1; index < coordinates.length; index += 1) {
      const a = coordinates[index - 1];
      const b = coordinates[index];
      const key = edgeKey(a, b);
      let edge = edges.get(key);
      if (!edge) {
        edge = { a: a.slice(), b: b.slice(), lines: new Set() };
        edges.set(key, edge);
      }
      edge.lines.add(name);
    }
  }

  const result = [];
  for (const edge of edges.values()) {
    if (edge.lines.size < 2) continue;
    const lines = [...edge.lines].sort((a, b) => a.localeCompare(b, 'fr', { numeric: true }));
    result.push({
      a: edge.a,
      b: edge.b,
      lines,
      offsets: lines.map((name, index) => [name, index - (lines.length - 1) / 2])
    });
  }
  self.postMessage({ type: 'sharedTrackIndex', edges: result });
};
