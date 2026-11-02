import { test } from 'node:test';
import assert from 'node:assert/strict';
import { terrainContours } from '../web/src/terrain-contours.ts';

test('3D and 2D contours share levels, interpolation and support gaps', () => {
  const grid = { rows: 4, columns: 4, minHeightM: 10, maxHeightM: 16, height: Array.from({ length: 16 }, (_, i) => 10 + 2 * Math.floor(i / 4)) };
  const curves = terrainContours(grid);
  assert(curves.length > 0);
  for (const line of curves) for (const point of [line.a, line.b]) {
    assert.equal(line.height % 2, 0);
    assert.equal(10 + point[1] * 2, line.height);
  }
  const gaps = { ...grid, height: grid.height.map((h, i) => i % 4 === 1 ? null : h) };
  const remaining = terrainContours(gaps);
  assert(remaining.length < curves.length);
  assert(remaining.every(line => line.a[0] >= 2 && line.b[0] >= 2));
});
