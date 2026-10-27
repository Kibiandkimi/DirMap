import test from 'node:test';
import assert from 'node:assert/strict';
import { activePins, bearingDeg, directionName, formatCode, memberSummary, mergeMessages, metresBetween, relativeTime } from '../web/src/group-geo.ts';
import { localScales, type GroupMember, type GroupMessage, type Pixel } from '../web/src/types.ts';

const origin: Pixel = [113.9566, 22.5599];
const scale = localScales(origin);
const point = (east: number, north: number): Pixel => [origin[0] + east / scale[0], origin[1] + north / scale[1]];

test('distance and direction between members match local metres', () => {
  assert.ok(Math.abs(metresBetween(origin, point(300, 400)) - 500) < .5);
  assert.equal(directionName(bearingDeg(origin, point(0, 100))), '北');
  assert.equal(directionName(bearingDeg(origin, point(100, 0))), '东');
  assert.equal(directionName(bearingDeg(origin, point(-100, -100))), '西南');
  assert.equal(directionName(359), '北');
  assert.equal(directionName(-90), '西');
});

test('times, codes and messages read naturally and merge without repeats', () => {
  assert.equal(relativeTime(3), '刚刚');
  assert.equal(relativeTime(42), '42 秒前');
  assert.equal(relativeTime(185), '3 分钟前');
  assert.equal(relativeTime(7300), '2 小时前');
  assert.equal(formatCode('123456'), '123 456');
  const message = (seq: number, kind: GroupMessage['kind'] = 'text', at = 0): GroupMessage => ({ seq, memberId: 'a', kind, text: String(seq), at, lnglat: kind === 'pin' ? origin : null });
  const merged = mergeMessages([message(1), message(2)], [message(2), message(4), message(3)], 3);
  assert.deepEqual(merged.map(m => m.seq), [2, 3, 4]);
  const pins = activePins([message(1, 'pin', 0), message(2, 'pin', 5000), message(3, 'text', 5000), message(4, 'pin', 5100)], 5200, 3600, 3);
  assert.deepEqual(pins.map(m => m.seq), [2, 4]);
});

test('a member is described by where they are relative to me', () => {
  const base: GroupMember = { id: 'b', name: '乙', teamId: null, color: 1, host: false, online: true, seenAt: 100, sharing: true, helpAt: null,
    position: { lnglat: point(0, -250), accuracyM: 5, headingDeg: null, at: 95 } };
  assert.equal(memberSummary(base, origin, 100), '南 250 m · 刚刚');
  assert.equal(memberSummary({ ...base, position: { ...base.position!, lnglat: point(5, 5) } }, origin, 100), '就在附近 · 刚刚');
  assert.equal(memberSummary(base, null, 160), '已共享 · 1 分钟前');
  assert.equal(memberSummary({ ...base, sharing: false, position: null }, origin, 100), '未共享位置');
  assert.equal(memberSummary({ ...base, sharing: false, position: null, online: false, seenAt: 0 }, origin, 600), '离线 · 10 分钟前');
});
