import { test, expect } from '@playwright/test';

for (const viewport of [{ width: 390, height: 844 }, { width: 360, height: 640 }, { width: 844, height: 390 }, { width: 915, height: 412 }]) {
  test(`map stays primary and editing is reachable at ${viewport.width} x ${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.control-row', { state: 'attached' });
    await expect(page.locator('#teacher-panel')).toBeHidden();
    const layout = await page.evaluate(() => {
      const map = document.querySelector('#map')!.getBoundingClientRect();
      const top = document.querySelector('.map-topbar')!.getBoundingClientRect();
      const bottom = document.querySelector('.map-bottom')!.getBoundingClientRect();
      const center = document.elementFromPoint(map.x + map.width / 2, (top.bottom + bottom.top) / 2);
      return { width: map.width, height: map.height, clearHeight: bottom.top - top.bottom,
        overflow: document.documentElement.scrollWidth > innerWidth, centerIsMap: !!center?.closest('#map') };
    });
    expect(layout.overflow).toBe(false);
    expect(layout.width).toBeGreaterThanOrEqual(viewport.width - 1);
    expect(layout.height / viewport.height).toBeGreaterThan(.70);
    expect(layout.clearHeight / viewport.height).toBeGreaterThan(.25);
    expect(layout.centerIsMap).toBe(true);
    await page.screenshot({ path: `/tmp/dirmap-mobile-${viewport.width}x${viewport.height}.png` });
    await expect(page.locator('#follow-button')).toBeInViewport();
    await page.locator('#mobile-layers').click();
    await page.locator('#standard-map').uncheck();
    await page.locator('#mobile-layers').click();
    await page.locator('#mobile-panel').click();
    await expect(page.locator('#teacher-panel')).toBeVisible();
    await page.locator('#edit-controls').click();
    await expect(page.locator('dialog canvas')).toBeVisible();
    await page.locator('[data-close]').click();
    await page.locator('#pick-start').click();
    await expect(page.locator('#teacher-panel')).toBeHidden();
    await expect(page.locator('#map-action-hint')).toContainText('起点');
  });
}

test('phone location normalizes WebView clocks, keeps compass working and ignores retired callbacks', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => {
    const callbacks: { success: Function; error: Function }[] = [];
    (window as any).geoCallbacks = callbacks;
    Object.defineProperty(navigator, 'geolocation', { configurable: true, value: {
      watchPosition: (success: Function, error: Function) => { callbacks.push({ success, error }); return callbacks.length; },
      clearWatch: () => {},
    } });
  });
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.control-row', { state: 'attached' });
  await page.locator('#follow-button').click();
  await expect(page.locator('#location-status')).toHaveText('正在获取位置…');
  const emit = (kind: string, longitude = 113.9304) => page.evaluate(({ kind, longitude }) => {
    const timestamp = kind === 'zero' ? 0 : kind === 'seconds' ? Date.now() / 1000 : kind === 'monotonic' ? performance.now() : Date.now() - 30000;
    (window as any).geoCallbacks.at(-1).success({ timestamp, coords: { longitude, latitude: 22.535, accuracy: 6, heading: null, speed: 0 } });
  }, { kind, longitude });
  for (const [i, kind] of ['zero', 'seconds', 'monotonic'].entries()) {
    const longitude = 113.9304 + i * .0001;
    await emit(kind, longitude);
    await expect(page.locator('#location-status')).toContainText('定位精度 ±6 m');
    const centerX = await page.evaluate(() => (window as any).dirmap.campusMap.map.getView().getCenter()[0]);
    expect(centerX).toBeCloseTo(longitude * Math.PI / 180 * 6378137, 4);
  }
  const previousCenter = await page.evaluate(() => (window as any).dirmap.campusMap.map.getView().getCenter());
  await emit('stale', 113.94);
  await expect(page.locator('#location-status')).toContainText('定位精度 ±6 m');
  expect(await page.evaluate(() => (window as any).dirmap.sensors.locationDiagnostics().rejectedCount)).toBe(1);
  expect(await page.evaluate(() => (window as any).dirmap.campusMap.map.getView().getCenter())).toEqual(previousCenter);
  await page.evaluate(() => window.dispatchEvent(new DeviceOrientationEvent('deviceorientationabsolute', { alpha: 270, beta: 0, gamma: 0, absolute: true })));
  await expect(page.locator('#sensor-status')).toContainText('90°');
  await page.evaluate(() => (window as any).geoCallbacks.at(-1).error({ code: 1 }));
  await expect(page.locator('#location-status')).toContainText('定位权限未开启');
  await page.locator('#follow-button').click();
  expect(await page.evaluate(() => (window as any).geoCallbacks.length)).toBe(2);
  await page.evaluate(() => (window as any).geoCallbacks[0].success({ timestamp: 0, coords: { longitude: 114, latitude: 22.6, accuracy: 1 } }));
  expect(await page.evaluate(() => (window as any).dirmap.campusMap.map.getView().getCenter())).toEqual(previousCenter);
  await emit('zero');
  await expect(page.locator('#location-status')).toContainText('定位精度 ±6 m');
});
