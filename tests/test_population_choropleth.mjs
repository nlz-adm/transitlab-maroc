import test from 'node:test';
import assert from 'node:assert/strict';
import {
  populationNeighborhoodColor,
  estimateDensityFromNearbyCells,
} from '../assets/population-choropleth.mjs';

function channels(color) {
  const match = /^rgb\\((\\d+),(\\d+),(\\d+)\\)$/.exec(color || '');
  assert.ok(match, `Expected an rgb() colour, received: ${color}`);
  return match.slice(1).map(Number);
}

test('the population ramp is continuous, ordered and has clear endpoints', () => {
  assert.equal(populationNeighborhoodColor(0), 'rgb(255,247,188)');
  assert.equal(populationNeighborhoodColor(50000), 'rgb(103,25,15)');
  const samples = [0, 25, 100, 250, 500, 1000, 2000, 3500, 5000, 7500, 10000, 15000, 20000, 35000, 50000];
  const colors = samples.map(populationNeighborhoodColor);
  assert.ok(colors.every((color) => /^rgb\\(\\d+,\\d+,\\d+\\)$/.test(color)));
  assert.ok(new Set(colors).size >= 12, 'The ramp should expose many distinct intermediate colours');
  assert.notEqual(populationNeighborhoodColor(100), populationNeighborhoodColor(500));
  assert.notEqual(populationNeighborhoodColor(5000), populationNeighborhoodColor(20000));
});

test('missing and invalid density values are not misrepresented as zero', () => {
  assert.equal(populationNeighborhoodColor(null), null);
  assert.equal(populationNeighborhoodColor(undefined), null);
  assert.equal(populationNeighborhoodColor(''), null);
  assert.equal(populationNeighborhoodColor(-1), null);
  assert.equal(populationNeighborhoodColor(Number.NaN), null);
});

test('small neighbourhood polygons get an estimate from nearby grid cells', () => {
  const estimate = estimateDensityFromNearbyCells([0, 0], [
    { center: [0.001, 0], density: 100 },
    { center: [0.004, 0], density: 300 },
    { center: [0.008, 0], density: 900 },
  ]);
  assert.ok(estimate);
  assert.equal(estimate.count, 3);
  assert.ok(estimate.density > 100 && estimate.density < 900);
});

test('an almost exact grid match wins and distant cells are not borrowed', () => {
  const exact = estimateDensityFromNearbyCells([0, 0], [
    { center: [0.0001, 0], density: 222 },
    { center: [0.004, 0], density: 900 },
  ]);
  assert.deepEqual(exact, { density: 222, count: 1 });
  const outsideRadius = estimateDensityFromNearbyCells([0, 0], [
    { center: [1, 1], density: 500 },
  ]);
  assert.equal(outsideRadius, null);
});
