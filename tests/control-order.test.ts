import test from 'node:test';
import assert from 'node:assert/strict';
import { sortedControls } from '../web/src/control-order.ts';
import { draftControlIds, routeToken } from '../web/src/course-routes.ts';
import type { Control } from '../web/src/types.ts';

test('dictionary order is independent of detection and custom route order', () => {
  const controls = ['2', '31', '10', '1'].map((label, i) => ({ id: `c${i}`, label })) as Control[];
  assert.deepEqual(sortedControls(controls).map(c => c.label), ['1', '10', '2', '31']);
  assert.deepEqual(controls.map(c => c.label), ['2', '31', '10', '1']);
  assert.deepEqual(draftControlIds('31 → 2，10', controls), ['c1', 'c0', 'c2']);
  controls[0].label = 'c1';
  assert.equal(routeToken('c0', controls), 'c0');
  assert.deepEqual(draftControlIds('c0 c1', controls), ['c0', 'c1']);
});
