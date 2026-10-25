import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizePosition, positionClockPolicy } from '../web/src/geolocation.ts';
import { OffRouteMonitor } from '../web/src/navigation.ts';

const now = 1792803928000;
const position = (timestamp: number, coords = {}) => ({ timestamp, coords: {
  longitude: 113.9304, latitude: 22.535, accuracy: 5, heading: 90, speed: 2, ...coords,
} }) as GeolocationPosition;

test('standard Unix milliseconds and seconds use the same time downstream', () => {
  for (const timestamp of [now - 1000, (now - 1000) / 1000]) {
    const fix = normalizePosition(position(timestamp), now)!;
    assert.equal(fix.timestamp, now - 1000);
    assert.equal(fix.rawTimestamp, timestamp);
    assert.equal(fix.receivedAt, now);
    assert.equal(fix.coords.longitude, 113.9304);
    assert.equal(fix.toJSON().timestamp, now - 1000);
  }
});
test('opaque WebView clocks do not become stale as page uptime overtakes their value', () => {
  for (const timestamp of [0, NaN, undefined, 1000, 49000, 59000, 700000]) {
    const fix = normalizePosition(position(timestamp as number), now)!;
    assert.equal(fix.timestamp, now);
    assert.equal(fix.clock, 'received');
  }
});
test('normalization does not freshen old fixes, future Unix clocks or invalid coordinates', () => {
  for (const timestamp of [now - 11000, (now - 11000) / 1000, now + 1000, (now + 1000) / 1000, -1]) {
    assert.equal(normalizePosition(position(timestamp), now), null);
  }
  for (const coords of [{ latitude: NaN }, { longitude: 181 }, { latitude: 91 }, { accuracy: -1 }, { accuracy: NaN }]) {
    for (const policy of ['provider', 'received'] as const) assert.equal(normalizePosition(position(now, coords), now, policy), null);
  }
});
test('a zero provider timestamp can trigger sustained off-route detection with a Unix API timestamp', () => {
  const monitor = new OffRouteMonitor(), path: [number, number][] = [[113.929, 22.530], [113.933, 22.530]];
  for (const [delay, expected] of [[0, false], [1000, false], [3100, true]] as const) {
    const fix = normalizePosition(position(0), now + delay)!;
    assert.equal(monitor.update({ lnglat: [fix.coords.longitude, fix.coords.latitude], accuracy: fix.coords.accuracy, timestamp: fix.timestamp }, path, now + delay), expected);
    assert.ok(fix.timestamp > 1e12);
  }
});

test('OpenHarmony / ArkWeb uses the confirmed callback compatibility policy without changing other browsers', () => {
  const userAgent = 'Mozilla/5.0 (Phone; OpenHarmony 6.1) AppleWebKit/537.36 Chrome/132.0.0.0 Safari/537.36 ArkWeb/6.0.0.46SP3 Mobile MicroMessenger/8.0.19.47';
  const policy = positionClockPolicy(userAgent);
  assert.equal(policy, 'received');
  for (const timestamp of [-1, 0, 2000, 1234567890, now - 60000, now * 1000]) {
    const fix = normalizePosition(position(timestamp), now, policy)!;
    assert.equal(fix.timestamp, now);
    assert.equal(fix.rawTimestamp, timestamp);
    assert.equal(fix.clock, 'received');
  }
  for (const other of ['Mozilla/5.0 (iPhone) Safari/605.1.15', 'Mozilla/5.0 (Linux; Android 16) Chrome/132.0.0.0 MicroMessenger/8.0']) {
    assert.equal(positionClockPolicy(other), 'provider');
    assert.equal(normalizePosition(position(now - 60000), now, positionClockPolicy(other)), null);
  }
});
