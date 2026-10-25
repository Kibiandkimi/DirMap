import test from 'node:test';
import assert from 'node:assert/strict';
import { parseCustomRoutes, routeToken } from '../web/src/course-routes.ts';
import type { Control } from '../web/src/types.ts';
const controls = ['31', '32', '43'].map((label, i) => ({ id: `c${i}`, label, required: true, state: 'candidate', role: 'control' })) as Control[];
const teams = ['第一队', '第二队'].map(id => ({ id, people: 3, speedMps: 1.4 }));

test('teacher route drafts accept labels, identifiers and common Chinese separators', () => {
  const result = parseCustomRoutes({ 第一队: '31 → 43，32', 第二队: 'c0、c1' }, teams, controls, ['c0', 'c1'], true);
  assert.deepEqual(result, [{ teamId: '第一队', controlIds: ['c0', 'c2', 'c1'] }, { teamId: '第二队', controlIds: ['c0', 'c1'] }]);
});
test('custom drafts cannot silently omit targets, reorder meetings or resolve ambiguous labels', () => {
  assert.throws(() => parseCustomRoutes({ 第一队: '31 32', 第二队: '31 32' }, teams, controls, [], false), /43/);
  assert.throws(() => parseCustomRoutes({ 第一队: '31 43 32', 第二队: '32 31' }, teams, controls, ['c0', 'c1'], false), /顺序/);
  assert.throws(() => parseCustomRoutes({ 第一队: '31 43 32', 第二队: '31 32 31' }, teams, controls, [], false), /重复/);
  const duplicate = controls.map(c => ({ ...c, label: '31' }));
  assert.throws(() => parseCustomRoutes({ 第一队: '31', 第二队: '31' }, teams, duplicate, [], false), /编号重复/);
});
test('map-picked route tokens round-trip even when a label collides with another identifier', () => {
  const tricky = controls.map(c => ({ ...c })); tricky[0].label = 'c1';
  const tokens = tricky.map(c => routeToken(c.id, tricky)).join(' → ');
  assert.equal(routeToken('c0', tricky), 'c0');
  assert.deepEqual(parseCustomRoutes({ 第一队: tokens, 第二队: tokens }, teams, tricky, [], false)[0].controlIds, ['c0', 'c1', 'c2']);
});
