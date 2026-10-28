import { test } from 'node:test';
import assert from 'node:assert/strict';
import { routeArrows } from '../web/src/route-arrows.ts';

test('route arrows follow traversal through corners, repeated points and return legs', () => {
  const arrows = routeArrows([[0, 0], [0, 0], [100, 0], [100, 100], [0, 100]], 50);
  assert.deepEqual(arrows.map(a => a.point), [[25, 0], [75, 0], [100, 25], [100, 75], [75, 100], [25, 100]]);
  assert.deepEqual(arrows.map(a => a.rotation), [Math.PI / 2, Math.PI / 2, 0, 0, -Math.PI / 2, -Math.PI / 2]);
  const backwards = routeArrows([[0, 100], [100, 100], [100, 0]], 50);
  assert.equal(Math.abs(backwards.at(-1)!.rotation), Math.PI);
});

test('dense and tiny paths keep bounded arrow counts and finite positions', () => {
  assert.deepEqual(routeArrows([], 50), []);
  assert.deepEqual(routeArrows([[0, 0], [0, 0]], 50), []);
  assert.deepEqual(routeArrows([[0, 0], [1, 1]], 50), []);
  const arrows = routeArrows([[0, 0], [100000, 0]], .1, 100);
  assert.equal(arrows.length, 100);
  assert.ok(arrows[0].point[0] < 1000 && arrows.at(-1)!.point[0] > 99000);
});
