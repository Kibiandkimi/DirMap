import test from 'node:test';
import assert from 'node:assert/strict';
import { contourStep, legend, mercator, modelExtent, overlapping, sharedReliefUrl } from '../web/src/elevation-layer.ts';
import { HeightSampler, heightSample, SAMPLE_GAP_MS } from '../web/src/live-sampler.ts';

const model = { artifact: 'models/'+'a'.repeat(40)+'.npz', frame: { origin: [113.9304, 22.535] as [number, number] }, origin: [-20, -30] as [number, number],
  spacingM: 10, columns: 5, rows: 4, bbox: [113.93, 22.534, 113.931, 22.536], minHeightM: 4.2, maxHeightM: 33.9 };

test('a relief image spans half a cell beyond its outer grid nodes', () => {
  const [x0, y0, x1, y1] = modelExtent(model);
  const centre = mercator(model.frame.origin);
  // 5 columns at 10 m from -20 m: -25 m to +25 m; 4 rows from -30 m: -35 m to +5 m.
  const scale = 1 / Math.cos(22.535 * Math.PI / 180);
  assert.ok(Math.abs((x0 - centre[0]) / scale + 25) < .2 && Math.abs((x1 - centre[0]) / scale - 25) < .2);
  assert.ok(Math.abs((y0 - centre[1]) / scale + 35) < .2 && Math.abs((y1 - centre[1]) / scale - 5) < .2);
  assert.equal(sharedReliefUrl(model), `/api/elevation/artifacts/${'a'.repeat(40)}/relief.png`);
  assert.deepEqual(overlapping([model], [113.95, 22.55, 113.96, 22.56]), []);
  assert.equal(overlapping([model], [113.9, 22.5, 114, 22.6]).length, 1);
  assert.equal(contourStep(0, 20), 2); assert.equal(contourStep(0, 30), 5); assert.equal(contourStep(0, 80), 10);
  assert.equal(legend([model]), '4–34 m · 等高线 5 m');
});

test('only real heights with usable accuracy become samples', () => {
  const fix = { latitude: 22.535, longitude: 113.93, accuracy: 6, altitude: 18.5, altitudeAccuracy: 9 };
  assert.deepEqual(heightSample(fix, 1000), { t: 1000, lng: 113.93, lat: 22.535, h: 18.5, hacc: 6, vacc: 9 });
  assert.equal(heightSample({ ...fix, altitude: null }, 1), null);
  assert.equal(heightSample({ ...fix, altitude: 0, altitudeAccuracy: null }, 1), null);
  assert.equal(heightSample({ ...fix, accuracy: 40 }, 1), null);
  assert.equal(heightSample({ ...fix, altitudeAccuracy: 45 }, 1), null);
  assert.equal(heightSample({ ...fix, altitudeAccuracy: null }, 1)!.vacc, null);
});

test('the sampler thins, keeps unsent samples across reloads and stops when the course does', async () => {
  const memory = new Map<string, string>();
  const storage = { getItem: (k: string) => memory.get(k) ?? null, setItem: (k: string, v: string) => void memory.set(k, v), removeItem: (k: string) => void memory.delete(k) } as Storage;
  const sent: unknown[][] = [];
  let receipt = { enabled: true, accepted: 0 };
  const post = async (_device: string, batch: unknown[]) => { sent.push(batch); return { ...receipt, accepted: batch.length }; };
  const sampler = new HeightSampler('c'.repeat(32), post, storage);
  const fix = (lat: number) => ({ latitude: lat, longitude: 113.93, accuracy: 6, altitude: 20, altitudeAccuracy: 8 });
  assert.equal(sampler.add(fix(22.535), 0), false, 'nothing is collected until the course allows it');
  sampler.enabled = true;
  assert.equal(sampler.add(fix(22.535), 0), true);
  assert.equal(sampler.add(fix(22.53501), 1000), false, 'a phone standing still adds one sample per few seconds');
  assert.equal(sampler.add(fix(22.53501), SAMPLE_GAP_MS + 1), true);
  assert.equal(sampler.add(fix(22.5351), SAMPLE_GAP_MS + 1500), true, 'moving several metres counts at once');
  const reloaded = new HeightSampler('c'.repeat(32), post, storage);
  assert.equal(reloaded.waiting, 3); assert.equal(reloaded.deviceId, sampler.deviceId);
  reloaded.enabled = true; await reloaded.flush();
  assert.equal(sent[0].length, 3); assert.equal(reloaded.waiting, 0); assert.equal(reloaded.sent, 3);
  receipt = { enabled: false, accepted: 0 };
  reloaded.add(fix(22.536), 20000); await reloaded.flush();
  assert.equal(reloaded.enabled, false); assert.equal(reloaded.waiting, 0);
  reloaded.enabled = true; reloaded.optedOut = true;
  assert.equal(reloaded.add(fix(22.537), 40000), false);
});
