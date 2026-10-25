import { test, expect, type Page } from '@playwright/test';

const harmonyUA = 'Mozilla/5.0 (Phone; OpenHarmony 6.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 ArkWeb/6.0.0.46SP3 Mobile MicroMessenger/8.0.19.47(0xf380132f) Weixin NetType/WIFI Language/zh_CN';

async function prepare(page: Page, harmony = true) {
  await page.setViewportSize({ width: 390, height: 844 });
  // Install before OpenLayers captures browser timers; replacing its clock
  // after map animations begin mixes native and virtual animation deadlines.
  const time = new Date('2026-10-25T00:00:00Z');
  await page.clock.install({ time });
  await page.clock.pauseAt(new Date(time.getTime() + 1000));
  await page.addInitScript(userAgent => {
    if (userAgent) Object.defineProperty(navigator, 'userAgent', { configurable: true, value: userAgent });
    const geo = { watches: [] as any[], requests: [] as any[], cleared: [] as number[] };
    (window as any).geo = geo;
    Object.defineProperty(navigator, 'geolocation', { configurable: true, value: {
      watchPosition: (success: Function, error: Function, options: PositionOptions) => {
        geo.watches.push({ success, error, options }); return geo.watches.length;
      },
      getCurrentPosition: (success: Function, error: Function, options: PositionOptions) => geo.requests.push({ success, error, options }),
      clearWatch: (id: number) => geo.cleared.push(id),
    } });
  }, harmony ? harmonyUA : null);
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.control-row', { state: 'attached' });
  await page.clock.runFor(1000);
  // The clock is paused to exercise minute-long streams deterministically.
  // Dispatch the gesture without relying on animation-frame click scheduling;
  // mobile.spec.ts separately checks actual taps/layout with a running clock.
  await page.evaluate(() => document.getElementById('follow-button')!.click());
  expect(await watchCount(page)).toBe(1);
}

async function fix(page: Page, timestamp: number | 'now' = 0, longitude = 113.9304, source = 'watches', index = -1, accuracy = 6) {
  await page.evaluate(({ timestamp, longitude, source, index, accuracy }) => {
    (window as any).geo[source].at(index).success({ timestamp: timestamp === 'now' ? Date.now() : timestamp,
      coords: { longitude, latitude: 22.535, accuracy, heading: null, speed: 0 } });
  }, { timestamp, longitude, source, index, accuracy });
}
async function fail(page: Page, code: number, source = 'watches', index = -1) {
  await page.evaluate(({ code, source, index }) => (window as any).geo[source].at(index).error({ code }), { code, source, index });
}
const diagnostics = (page: Page) => page.evaluate(() => (window as any).dirmap.sensors.locationDiagnostics());
const requestCount = (page: Page) => page.evaluate(() => (window as any).geo.requests.length);
const watchCount = (page: Page) => page.evaluate(() => (window as any).geo.watches.length);
const center = (page: Page) => page.evaluate(() => (window as any).dirmap.campusMap.map.getView().getCenter());
async function expectLongitude(page: Page, longitude: number) { expect((await center(page))[0]).toBeCloseTo(longitude * Math.PI / 180 * 6378137, 4); }
async function compass(page: Page) {
  await page.evaluate(() => {
    const event = new DeviceOrientationEvent('deviceorientationabsolute', { alpha: 270, beta: 0, gamma: 0, absolute: true });
    Object.defineProperty(event, 'timeStamp', { value: performance.now() });
    window.dispatchEvent(event);
  });
  await page.clock.runFor(100);
  await expect(page.locator('#sensor-status')).toContainText('90°');
}

test('OpenHarmony WeChat keeps moving for a minute with non-Unix and stuck provider timestamps', async ({ page }) => {
  await prepare(page);
  await fix(page);
  for (let i = 1; i <= 60; i++) {
    await page.clock.runFor(1000);
    // Slow boot clock, a seconds-like counter, then a stuck old Unix timestamp.
    const timestamp = i < 20 ? i * 10 : i < 40 ? 1234567890 : 1792803928000;
    await fix(page, timestamp, 113.9304 + i * .00001);
  }
  await expectLongitude(page, 113.931);
  await expect(page.locator('#location-status')).toHaveText('定位精度 ±6 m');
  expect(await diagnostics(page)).toMatchObject({ clockPolicy: 'received', acceptedCount: 61, rejectedCount: 0, lastClock: 'received' });
  expect(await requestCount(page)).toBe(0);
  await compass(page);
});

test('watch stalls after its first fix; repeated one-shot requests continue to move the map', async ({ page }) => {
  await prepare(page);
  await fix(page);
  for (let i = 1; i <= 8; i++) {
    await page.clock.runFor(5000);
    expect(await requestCount(page)).toBe(i);
    await fix(page, 0, 113.9304 + i * .0001, 'requests');
  }
  expect(await watchCount(page)).toBe(1);
  await expectLongitude(page, 113.9312);
  await expect(page.locator('#location-status')).toHaveText('定位精度 ±6 m');
  expect(await page.evaluate(() => (window as any).geo.requests.every((r: any) => r.options.maximumAge === 0))).toBe(true);
});

test('a stationary zero-clock stream stays live, while an exact positive-clock replay needs a new request', async ({ page }) => {
  await prepare(page);
  for (let i = 0; i < 12; i++) { await fix(page); await page.clock.runFor(1000); }
  expect(await requestCount(page)).toBe(0);
  await fix(page, 3000);
  for (let i = 0; i < 6; i++) { await page.clock.runFor(1000); await fix(page, 3000); }
  expect(await requestCount(page)).toBe(1);
  expect((await diagnostics(page)).lastRejection).toBe('repeated-watch-fix');
  await fix(page, 3000, 113.9304, 'requests');
  expect((await diagnostics(page)).lastFixAgeMs).toBe(0);
  await expect(page.locator('#location-status')).toHaveText('定位精度 ±6 m');
});

test('standard clocks reject stale, future and malformed fixes without erasing a recent good position', async ({ page }) => {
  await prepare(page, false);
  await fix(page, 'now');
  const previous = await center(page), now = await page.evaluate(() => Date.now());
  await fix(page, now - 60000, 114);
  await fix(page, now + 60000, 114);
  await fix(page, 'now', 181);
  expect(await center(page)).toEqual(previous);
  expect((await diagnostics(page)).rejectedCount).toBe(3);
  await expect(page.locator('#location-status')).toHaveText('定位精度 ±6 m');
  await page.clock.runFor(11000);
  await expect(page.locator('#location-status')).toContainText('位置暂未更新');
  const color = await page.evaluate(() => (window as any).dirmap.campusMap.location.getFeatures()[1].getStyle().getImage().getFill().getColor());
  expect(color).toBe('#7b858e');
  await fix(page, 'now', 113.931, 'requests');
  await expectLongitude(page, 113.931);
  await expect(page.locator('#location-status')).toHaveText('定位精度 ±6 m');
});

test('a pending request neither multiplies nor overrides a watch that has recovered', async ({ page }) => {
  await prepare(page);
  await fix(page);
  await page.clock.runFor(12000);
  expect(await requestCount(page)).toBe(1);
  await fix(page, 2000, 113.931);
  await fix(page, 1000, 114, 'requests');
  await expectLongitude(page, 113.931);
  expect((await diagnostics(page)).acceptedCount).toBe(2);
});

test('a missing native timeout is recovered and retired request callbacks cannot overwrite new fixes', async ({ page }) => {
  await prepare(page);
  await fix(page);
  await page.clock.runFor(21000);
  expect(await requestCount(page)).toBe(1);
  expect((await diagnostics(page)).lastErrorCode).toBe(3);
  await page.clock.runFor(1000);
  expect(await requestCount(page)).toBe(2);
  expect(await page.evaluate(() => (window as any).geo.requests[1].options.enableHighAccuracy)).toBe(false);
  await fix(page, 0, 113.931, 'requests', 1);
  await fix(page, 0, 114, 'requests', 0);
  await fail(page, 1, 'requests', 0);
  await expectLongitude(page, 113.931);
  expect((await diagnostics(page)).permissionDenied).toBe(false);
});

test('high-accuracy failure switches provider and can later regain an accurate position', async ({ page }) => {
  await prepare(page);
  await fail(page, 3);
  expect(await watchCount(page)).toBe(2);
  expect(await page.evaluate(() => (window as any).geo.watches[1].options.enableHighAccuracy)).toBe(false);
  await fix(page, 0, 113.9304, 'watches', 1, 80);
  await page.clock.runFor(5000);
  expect(await page.evaluate(() => (window as any).geo.requests[0].options.enableHighAccuracy)).toBe(true);
  await fix(page, 0, 113.931, 'requests', 0);
  await fix(page, 0, 114, 'watches', 0);
  await expectLongitude(page, 113.931);
  await expect(page.locator('#location-status')).toHaveText('定位精度 ±6 m');
  await compass(page);
});

test('permission denial stops automatic requests, preserves compass and allows explicit retry', async ({ page }) => {
  await prepare(page);
  await fix(page);
  await fail(page, 1);
  await page.clock.runFor(60000);
  expect(await requestCount(page)).toBe(0);
  expect(await watchCount(page)).toBe(1);
  await expect(page.locator('#location-status')).toContainText('定位权限未开启');
  await compass(page);
  await page.locator('#follow-button').evaluate(button => (button as HTMLButtonElement).click());
  expect(await watchCount(page)).toBe(2);
  await fix(page, 0, 113.931);
  await fix(page, 0, 114, 'watches', 0);
  await expectLongitude(page, 113.931);
});

test('repeated provider errors back off, and a good watch fix resets the recovery delay', async ({ page }) => {
  await prepare(page);
  await fix(page);
  await page.clock.runFor(5000);
  await fail(page, 2, 'requests');
  await page.clock.runFor(1000);
  expect(await requestCount(page)).toBe(2);
  await fail(page, 2, 'requests');
  await page.clock.runFor(4000);
  expect(await requestCount(page)).toBe(2);
  await page.clock.runFor(1000);
  expect(await requestCount(page)).toBe(3);
  await fail(page, 2, 'requests');
  await page.clock.runFor(9000);
  expect(await requestCount(page)).toBe(3);
  await page.clock.runFor(1000);
  expect(await requestCount(page)).toBe(4);
  await fix(page, 0, 113.931);
  expect((await diagnostics(page)).consecutiveErrors).toBe(0);
  await expectLongitude(page, 113.931);
});

test('background and page-cache restoration retire watch and request callbacks without stopping later recovery', async ({ page }) => {
  await prepare(page);
  await fix(page);
  await page.clock.runFor(5000);
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.clock.runFor(60000);
  expect(await requestCount(page)).toBe(1);
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  expect(await watchCount(page)).toBe(2);
  await fix(page, 0, 113.931);
  await fix(page, 0, 114, 'requests', 0);
  await fail(page, 1, 'watches', 0);
  await expectLongitude(page, 113.931);
  await page.evaluate(() => {
    window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true }));
    window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
  });
  expect(await watchCount(page)).toBe(3);
  await fix(page, 0, 113.9312);
  await fix(page, 0, 114, 'watches', 1);
  await expectLongitude(page, 113.9312);
  await page.clock.runFor(5000);
  expect(await requestCount(page)).toBe(2);
});

test('the phone diagnostic view identifies this fix and contains no coordinates', async ({ page }) => {
  await prepare(page);
  await fix(page, 1234567890);
  await page.clock.resume();
  await page.locator('#open-help').click();
  await page.locator('#location-diagnostics').click();
  const output = await page.locator('#location-diagnostic-text').inputValue();
  expect(JSON.parse(output)).toMatchObject({ version: '2026-10-25.1', clockPolicy: 'received', acceptedCount: 1 });
  expect(output).not.toMatch(/longitude|latitude|113\.9304|22\.535/);
  await expect(page.locator('#retry-location')).toBeInViewport();
  await page.locator('#retry-location').click();
  expect(await watchCount(page)).toBe(2);
});
