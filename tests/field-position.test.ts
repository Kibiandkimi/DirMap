import test from 'node:test';
import assert from 'node:assert/strict';
import { captureFieldPosition } from '../web/src/field-position.ts';

const position = (timestamp: number) => ({ timestamp, coords: { longitude: 113.93, latitude: 22.53, accuracy: 4 } }) as GeolocationPosition;
const later = { get coords() { throw new Error('A retired callback read position data'); } } as GeolocationPosition;

test('field capture keeps provider time, receipt time and the original WebView clock', async () => {
  const now = Date.now(), raw = position(now-1000);
  const provider = { getCurrentPosition: (success: PositionCallback) => success(raw) };
  const normal = await captureFieldPosition(provider, 'Chrome', new AbortController().signal);
  assert.equal(normal.timestamp, now-1000); assert.equal(normal.clock, 'unix-ms');
  assert.ok(normal.receivedAt >= now);
  const webview = await captureFieldPosition(provider, 'OpenHarmony 7.0 ArkWeb/6.0.0.46SP3', new AbortController().signal);
  assert.equal(webview.timestamp, webview.receivedAt); assert.equal(webview.rawTimestamp, now-1000);
  assert.equal(webview.clock, 'received');
});

test('a missing native timeout expires and cannot accept its late result', async () => {
  let callback!: PositionCallback;
  const request = captureFieldPosition({ getCurrentPosition: success => { callback = success; } }, 'Chrome', new AbortController().signal, 5);
  await assert.rejects(request, /定位超时/);
  assert.doesNotThrow(() => callback(later));
});

test('a callback delivered before a delayed timer still obeys the deadline', async () => {
  const provider = { getCurrentPosition: (callback: PositionCallback) => {
    const until = performance.now()+10;
    while (performance.now() < until) { /* simulate a suspended task queue */ }
    callback(position(Date.now()));
  } };
  await assert.rejects(captureFieldPosition(provider, 'Chrome', new AbortController().signal, 1), /定位超时/);
});

test('cancelling a field capture retires callbacks without cancelling other location consumers', async () => {
  let callback!: PositionCallback; let calls = 0;
  const provider = { getCurrentPosition: (success: PositionCallback) => { calls++; callback = success; } };
  const controller = new AbortController();
  const request = captureFieldPosition(provider, 'Chrome', controller.signal);
  const rejected = assert.rejects(request, error => (error as Error).name === 'AbortError');
  controller.abort(); await rejected;
  assert.doesNotThrow(() => callback(later));
  await assert.rejects(captureFieldPosition(provider, 'Chrome', controller.signal), { name: 'AbortError' });
  assert.equal(calls, 1);
});

test('field capture rejects stale standard fixes and missing accuracy', async () => {
  for (const raw of [position(Date.now()-60000), { ...position(Date.now()), coords: { ...position(Date.now()).coords, accuracy: 0 } }]) {
    await assert.rejects(captureFieldPosition({ getCurrentPosition: callback => callback(raw) }, 'Chrome', new AbortController().signal), /过期或无效/);
  }
});
