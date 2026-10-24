import test from 'node:test';
import assert from 'node:assert/strict';
import { validateDouarIndex, isDouarPointVisible } from '../assets/douars-layer.mjs';

function fixture() {
  return {
    version: 'morocco-hcp-douars-2024-7204f75e503c',
    features: 33189,
    locatedPoints: 14860,
    unlocatedFeatures: 18329,
    regions: Array.from({ length: 12 }, (_, index) => {
      const code = String(index + 1).padStart(2, '0');
      return {
        code,
        url: 'assets/douars/' + code + '.geojson.gz',
        count: index === 0 ? 33178 : 1,
        pointCount: index === 0 ? 14849 : 1,
        unlocatedCount: index === 0 ? 18329 : 0,
      };
    }).map((region, index) => index === 0 ? region : {
      ...region,
      count: index === 1 ? 1 : 0,
      pointCount: index === 1 ? 1 : 0,
      unlocatedCount: index === 1 ? 0 : 0,
    }),
  };
}

function consistentFixture() {
  const regions = Array.from({ length: 12 }, (_, index) => {
    const code = String(index + 1).padStart(2, '0');
    const count = index === 0 ? 33178 : index === 1 ? 11 : 0;
    const points = index === 0 ? 14849 : index === 1 ? 11 : 0;
    return {
      code,
      url: 'assets/douars/' + code + '.geojson.gz',
      count,
      pointCount: points,
      unlocatedCount: count - points,
    };
  });
  return {
    version: 'morocco-hcp-douars-2024-7204f75e503c',
    features: 33189,
    locatedPoints: 14860,
    unlocatedFeatures: 18329,
    regions,
  };
}

test('national HCP douar index is accepted only when counts reconcile', () => {
  const index = consistentFixture();
  assert.equal(validateDouarIndex(index, index.version), index);
  const wrongVersion = { ...index, version: 'old' };
  assert.throws(() => validateDouarIndex(wrongVersion, index.version), /version/);
  const badCounts = { ...index, locatedPoints: 14000 };
  assert.throws(() => validateDouarIndex(badCounts, index.version), /Compteurs/);
});

test('all 12 region chunks need unique codes, safe paths and consistent counters', () => {
  const index = consistentFixture();
  const duplicate = { ...index, regions: [...index.regions] };
  duplicate.regions[1] = { ...duplicate.regions[1], code: '01' };
  assert.throws(() => validateDouarIndex(duplicate, index.version), /dupliqué/);

  const unsafe = { ...index, regions: index.regions.map((r, i) =>
    i === 0 ? { ...r, url: 'https://example.com/attacker.geojson.gz' } : r) };
  assert.throws(() => validateDouarIndex(unsafe, index.version), /incohérents|région/);
});

test('the map displays only source-matched point geometries in the viewport', () => {
  const bounds = {
    contains: ([lat, lon]) => lat >= 30 && lat <= 36 && lon >= -12 && lon <= -1,
  };
  assert.equal(isDouarPointVisible({
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [-6.8, 33.2] },
  }, bounds), true);
  assert.equal(isDouarPointVisible({
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [40, 33.2] },
  }, bounds), false);
  assert.equal(isDouarPointVisible({
    type: 'Feature',
    geometry: { type: 'Polygon', coordinates: [] },
  }, bounds), false);
  assert.equal(isDouarPointVisible({
    type: 'Feature',
    geometry: null,
  }, bounds), false);
});
