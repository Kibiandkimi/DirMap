import test from 'node:test';
import assert from 'node:assert/strict';
import { resultsCsv } from '../web/src/class-results.ts';
import type { ClassProgress, Control, Route } from '../web/src/types.ts';

const controls = [{ id: 'c1', label: '31' }, { id: 'c2', label: '=1+1' }, { id: 'c3', label: '33' }] as Control[];
const routes = [{ teamId: '第 1 队', people: 3, controlIds: ['c1', 'c2'] }, { teamId: 'B,"二"', people: 2, controlIds: ['c3', 'c1'] }] as Route[];

test('class results list every planned target with phone check-in times and safe cells', () => {
  const at = new Date(2026, 9, 27, 9, 5, 7).getTime();
  const progress: ClassProgress = { serverTime: 0, teams: [
    { teamId: '第 1 队', completed: ['c1'], checkIns: { c1: at }, departed: [], finished: false, reportedAt: 1 },
    { teamId: 'B,"二"', completed: ['c3', 'c1'], checkIns: { c3: at }, departed: [], finished: true, reportedAt: 1 }] };
  const csv = resultsCsv(routes, progress, controls, ['c1']);
  assert.ok(csv.startsWith('\uFEFF队伍,人数,顺序,目标,类型,状态,打点时间（手机记录）\r\n'));
  assert.deepEqual(csv.slice(1).trim().split('\r\n').slice(1), [
    '第 1 队,3,1,31,全班必到,已完成,2026-10-27 09:05:07',
    "第 1 队,3,2,'=1+1,本队分担,未完成,",
    '第 1 队,3,,终点,终点,未到达,',
    '"B,""二""",2,1,33,本队分担,已完成,2026-10-27 09:05:07',
    '"B,""二""",2,2,31,全班必到,已完成,',
    '"B,""二""",2,,终点,终点,未到达,',
  ]);
});
test('with a class start the results add the time since the start', () => {
  const start = new Date(2026, 9, 27, 9, 0, 0).getTime();
  const progress: ClassProgress = { serverTime: 0, teams: [
    { teamId: '第 1 队', completed: ['c1', 'c2'], checkIns: { c1: start + 125_000, c2: start - 1 }, departed: [], finished: true, reportedAt: 1 }] };
  const rows = resultsCsv(routes.slice(0, 1), progress, controls, [], start).slice(1).trim().split('\r\n');
  assert.equal(rows[0], '队伍,人数,顺序,目标,类型,状态,打点时间（手机记录）,距开课（分:秒）');
  assert.ok(rows[1].endsWith(',2026-10-27 09:02:05,2:05'));
  assert.ok(rows[2].endsWith(',2026-10-27 08:59:59,'));
  assert.equal(rows[3], '第 1 队,3,,终点,终点,未到达,,');
  progress.teams[0].arrived = true; progress.teams[0].arrivedAt = start + 3_600_000;
  assert.equal(resultsCsv(routes.slice(0, 1), progress, controls, [], start).trim().split('\r\n').at(-1), '第 1 队,3,,终点,终点,已到达,2026-10-27 10:00:00,60:00');
});
