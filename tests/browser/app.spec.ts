import { test, expect } from './fixtures';
import fs from 'node:fs';

test('three real maps, source editor, local overlays and independent review state', async ({ page }) => {
  test.setTimeout(60000);
  const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  const catalog = await (await page.request.get('/api/catalog')).json();
  await expect(page.locator('[data-map]')).toHaveCount(catalog.maps.length);
  await expect(page.locator('.control-row').first()).toBeVisible();
  const firstCount = await page.locator('.control-row').count(); expect(firstCount).toBeGreaterThan(5);
  await page.locator('#stitch-maps').check();
  await page.locator('[data-tab="registration"]').click();
  await expect(page.locator('#registration-quality')).toContainText('P95');
  await page.locator('[data-tab="terrain"]').click();
  await expect(page.locator('#legend-links a')).toHaveCount(2);
  await page.locator('#show-terrain').check();
  await page.locator('#edit-terrain').click();
  await expect(page.locator('dialog canvas')).toBeVisible();
  await expect(page.locator('#edit-mode')).toHaveValue('close');
  await page.locator('[data-close]').click();
  await page.locator('[data-map="szu-2"]').click();
  await expect(page.locator('#map-title')).toContainText('2');
  // The title updates before the remaining map/terrain requests finish.
  await expect(page.locator('#plan-button')).toBeEnabled();
  await page.locator('[data-map="szu-3"]').click();
  await expect(page.locator('#map-title')).toContainText('3');
  await expect(page.locator('#release-badge')).toContainText('草稿');
  expect(errors).toEqual([]);
});

test('rendered compass cardinal directions put the heading at the top', async ({ page }) => {
  await page.goto('/'); await page.waitForSelector('.control-row');
  const values = await page.evaluate(() => {
    const app = (window as any).dirmap, map = app.campusMap.map;
    const view = map.getView(); view.setCenter([0, 0]); view.setResolution(1);
    return [[0, [0, 10]], [90, [10, 0]], [180, [0, -10]], [270, [-10, 0]]].map(([heading, coordinate]: any) => {
      app.campusMap.setHeading(heading); map.renderSync();
      const center = map.getPixelFromCoordinate([0, 0]), pixel = map.getPixelFromCoordinate(coordinate);
      return [pixel[0] - center[0], pixel[1] - center[1]];
    });
  });
  for (const [x, y] of values) { expect(x).toBeCloseTo(0, 6); expect(y).toBeCloseTo(-10, 6); }
});

test('absolute orientation, relative rejection, user drag and north-up reset', async ({ page, context }) => {
  await context.grantPermissions(['geolocation']);
  await context.setGeolocation({ latitude: 22.535, longitude: 113.9304, accuracy: 5 });
  await page.goto('/'); await page.waitForSelector('.control-row');
  await page.locator('#follow-button').click();
  await page.waitForTimeout(100);
  await page.evaluate(() => {
    window.dispatchEvent(new DeviceOrientationEvent('deviceorientation', { alpha: 270, beta: 0, gamma: 0, absolute: true }));
  });
  await expect(page.locator('#sensor-status')).toContainText('90°');
  const rotation = await page.evaluate(() => (window as any).dirmap.campusMap.map.getView().getRotation());
  expect(rotation).toBeCloseTo(-Math.PI / 2, 4);
  await page.locator('#calibrate-compass').click();
  await page.locator('#confirm-compass').click();
  await page.evaluate(() => window.dispatchEvent(new DeviceOrientationEvent('deviceorientation', { alpha: 270, beta: 0, gamma: 0, absolute: true })));
  await expect(page.locator('#sensor-status')).toContainText('罗盘方向 0°');
  await page.locator('#north-button').click(); await page.waitForTimeout(350);
  expect(await page.evaluate(() => (window as any).dirmap.campusMap.map.getView().getRotation())).toBeCloseTo(0, 4);
  const box = await page.locator('#map').boundingBox();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.down(); await page.mouse.move(box!.x + box!.width / 2 + 70, box!.y + box!.height / 2 + 10, { steps: 5 }); await page.mouse.up();
  await expect(page.locator('#follow-label')).toContainText('继续跟随');
});

test('phone layout has no horizontal overflow and source editing is reachable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/'); await page.waitForSelector('.control-row', { state: 'attached' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expect(page.locator('#follow-button')).toBeInViewport();
  await page.locator('#mobile-panel').click();
  await page.locator('#edit-controls').click();
  await expect(page.locator('dialog')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.locator('[data-close]').click();
});

test('actual image graph to course form to rendered multi-team paths', async ({ page }) => {
  test.setTimeout(60000);
  const path = 'evidence/runtime/browser-course.json';
  test.skip(!fs.existsSync(path), 'Run the real-map verification script to prepare the live route fixture');
  const fixture = JSON.parse(fs.readFileSync(path, 'utf8'));
  await page.goto('/'); await page.waitForSelector('.control-row');
  await page.locator(`[data-map="${fixture.assetId}"]`).click();
  await expect(page.locator('#map-title')).toContainText(fixture.name);
  const boxes = page.locator('[data-required]');
  for (let i = 0; i < await boxes.count(); i++) {
    const input = boxes.nth(i), id = await input.getAttribute('data-required');
    if (await input.isDisabled()) continue;
    await input.setChecked(fixture.controlIds.includes(id));
  }
  await page.locator('#people').fill('12'); await page.locator('#teams').fill('3');
  await page.locator('#course-mode').selectOption('split_coverage');
  await page.locator('#solver-budget').selectOption('30');
  await page.locator('#pick-start').click();
  const pixel = await page.evaluate((lnglat: number[]) => {
    const app = (window as any).dirmap, map = app.campusMap.map;
    const lon = lnglat[0] * Math.PI / 180 * 6378137;
    const lat = Math.log(Math.tan(Math.PI / 4 + lnglat[1] * Math.PI / 360)) * 6378137;
    map.getView().setCenter([lon, lat]); map.renderSync();
    return map.getPixelFromCoordinate([lon, lat]);
  }, fixture.startLnglat);
  await page.locator('#map').click({ position: { x: pixel[0], y: pixel[1] } });
  await expect(page.locator('#pick-start')).toContainText('已设置');
  await page.locator('#plan-button').click();
  await expect(page.locator('.route-card')).toHaveCount(3, { timeout: 30000 });
  await expect(page.locator('#route-results')).toContainText('模型内已证明最优');
  await expect(page.locator('#plan-errors')).toBeEmpty();
  await expect(page.locator('#review-button')).toBeVisible();
  await expect(page.locator('#route-results')).toContainText('到访位置建议');
  await page.locator('[data-approach-review]').first().click();
  await expect(page.locator('#accept-approach')).toBeVisible();
  await expect(page.locator('#control-role')).toHaveValue('control');
  await page.locator('[data-close]').click();
});

test('built application serves working same-origin planning requests', async ({ page }) => {
  const fixture = JSON.parse(fs.readFileSync('evidence/runtime/browser-course.json', 'utf8'));
  const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
  await page.goto('http://127.0.0.1:8768/'); await page.waitForSelector('.control-row');
  const result = await page.evaluate(async (fixture) => {
    const state = await (await fetch('/api/maps/' + fixture.assetId)).json();
    const request = { assetId: fixture.assetId, version: state.version, controlIds: fixture.controlIds,
      startPixel: fixture.startPixel, finishPixel: fixture.startPixel, mode: 'all_visit',
      teams: [{ id: 'production-check', people: 3, speedMps: 1.4 }], objective: 'min_max_finish_then_sum' };
    const response = await fetch('/api/plans', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request) });
    if (!response.ok) throw new Error(await response.text());
    let job = await response.json();
    while (job.state === 'queued' || job.state === 'running') {
      await new Promise(resolve => setTimeout(resolve, 200));
      job = await (await fetch('/api/jobs/' + job.id)).json();
    }
    return job;
  }, fixture);
  expect(result.state).toBe('succeeded');
  expect(result.result.routes).toHaveLength(1);
  expect(result.result.routes[0].geometry.length).toBeGreaterThan(10);
  expect(errors).toEqual([]);
});

test('publish, phone offline reload, check-ins, rejoin remaining targets and invalidate stale releases', async ({ page, context }) => {
  test.setTimeout(90000);
  const fixture = JSON.parse(fs.readFileSync('evidence/runtime/browser-course.json', 'utf8'));
  await page.goto('/'); await page.waitForSelector('.control-row');
  await page.locator(`[data-map="${fixture.assetId}"]`).click();
  await expect(page.locator('#pick-start')).toContainText('已识别原图起点');
  await expect(page.locator('#pick-finish')).toContainText('已识别原图终点');
  await page.locator('#plan-button').click();
  await expect(page.locator('.route-card').first()).toBeVisible({ timeout: 30000 });
  await expect(page.locator('[data-approach-review]').first()).toBeVisible();
  await page.locator('#review-button').click();
  for (const name of ['registration', 'controls', 'passages']) await page.locator('#review-'+name).check();
  await page.locator('#publish-confirm').click();
  await expect(page.locator('#review-error')).toContainText('建议的到访位置');
  await page.locator('#review-approaches').check();
  await page.locator('#publish-confirm').click();
  const link = page.getByLabel('学生课程链接');
  await expect(link).toBeVisible({ timeout: 30000 });
  const courseId = new URL(await link.inputValue()).searchParams.get('course');
  const published = await (await page.request.get('/api/releases/'+courseId)).json();
  expect(published.plan.draft).toBe(false);
  expect(published.plan.approaches.every((a: any) => !a.automatic)).toBe(true);
  expect(published.controls.filter((c: any) => c.role === 'start_finish')).toHaveLength(1);
  expect(published.controls.filter((c: any) => c.approachPixel).length).toBeGreaterThan(0);

  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'userAgent', { configurable: true,
      value: 'Mozilla/5.0 (Phone; OpenHarmony 6.1) Chrome/132.0.0.0 ArkWeb/6.0.0.46SP3 MicroMessenger/8.0.19.47' });
    Object.defineProperty(navigator, 'geolocation', { configurable: true, value: {
      watchPosition: (callback: Function) => { (window as any).emitCoursePosition = callback; return 101; },
      clearWatch: () => {},
    } });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  const studentUrl = `http://127.0.0.1:8768/?course=${courseId}`;
  await page.goto(studentUrl);
  await expect(page.locator('#offline-status')).toContainText('本课已保存', { timeout: 20000 });
  await page.waitForFunction(() => !!navigator.serviceWorker.controller);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await context.setOffline(true); await page.reload();
  await expect(page.locator('#release-badge')).toContainText('离线');
  await expect(page.locator('.student-stop')).toHaveCount(published.plan.routes[0].controlIds.length);
  await page.locator('#mobile-panel').click();
  await page.locator('#check-in').click(); await page.reload();
  await expect(page.locator('#student-progress')).toHaveText(`1 / ${published.plan.routes[0].controlIds.length}`);
  expect(await page.evaluate(async () => {
    const names = await caches.keys();
    return names.some(name => name.startsWith('dirmap-courses-'));
  })).toBe(true);
  await context.setOffline(false); await page.reload();
  await expect(page.locator('#release-badge')).toHaveText('课堂导航');
  await page.locator('#mobile-panel').click();
  await page.locator('#undo-check-in').click();

  // Pick a legally walked point on a completed leg which is well away from
  // all remaining legs. This exercises real routing with injected GNSS,
  // without inventing an arbitrary free position inside campus obstacles.
  const route = published.plan.routes[0];
  function distanceToPath(point: number[], path: number[][]) {
    const scaleX = 111320 * Math.cos(point[1] * Math.PI / 180), scaleY = 110700;
    let best = Infinity;
    for (let i = 1; i < path.length; i++) {
      const a = [(path[i - 1][0] - point[0]) * scaleX, (path[i - 1][1] - point[1]) * scaleY];
      const b = [(path[i][0] - point[0]) * scaleX, (path[i][1] - point[1]) * scaleY];
      const dx = b[0] - a[0], dy = b[1] - a[1], t = dx || dy ? Math.max(0, Math.min(1, -(a[0] * dx + a[1] * dy) / (dx * dx + dy * dy))) : 0;
      best = Math.min(best, Math.hypot(a[0] + t * dx, a[1] + t * dy));
    }
    return best;
  }
  let offRoute: { point: number[]; completed: number; distance: number } | null = null;
  for (let count = 1; count < route.controlIds.length && !offRoute; count++) {
    const begin = route.legs[count].startIndex, remaining = route.geometry.slice(begin);
    for (const point of route.geometry.slice(0, begin).filter((_: unknown, i: number) => i % 8 === 0)) {
      const distance = distanceToPath(point, remaining);
      if (distance > 30) { offRoute = { point, completed: count, distance }; break; }
    }
  }
  expect(offRoute).not.toBeNull();
  for (let i = 0; i < offRoute!.completed; i++) await page.locator('#check-in').click();
  await page.locator('#mobile-map').click();
  await page.locator('#follow-button').click();
  await page.waitForFunction(() => typeof (window as any).emitCoursePosition === 'function');
  // An advancing ArkWeb counter which looks like stale Unix seconds must
  // reach the real navigation API with the same fresh clock as the map.
  const emit = () => page.evaluate((point: number[]) => (window as any).emitCoursePosition({ coords: { longitude: point[0], latitude: point[1], accuracy: 3, speed: 0, heading: null }, timestamp: 1234567890 + performance.now() / 1000 }), offRoute!.point);
  await emit();
  // The three-second delay is the product's sustained-deviation threshold.
  await page.waitForTimeout(3100);
  const rejoinResponse = page.waitForResponse(response => response.url().endsWith('/navigate') && response.request().method() === 'POST');
  await emit();
  const navigationResponse = await rejoinResponse;
  const navigationRequest = navigationResponse.request().postDataJSON();
  expect(navigationRequest.timestampMs).toBeGreaterThan(1e12);
  expect(Math.abs(Date.now() - navigationRequest.timestampMs)).toBeLessThan(15000);
  const rejoined = await navigationResponse.json();
  expect(rejoined.status).toBe('READY');
  expect(rejoined.route.controlIds).toEqual(route.controlIds.slice(offRoute!.completed));
  await expect(page.locator('#location-status')).toContainText('已更新返回路线');
  await expect(page.locator('#location-status')).toBeVisible();

  await page.request.patch(`/api/maps/${fixture.assetId}`, { data: { version: published.version, edits: [] } });
  await page.reload(); await expect(page.locator('#release-badge')).toHaveText('课程已更新');
  await expect(page.locator('#check-in')).toBeDisabled();
  await context.setOffline(true); await page.reload();
  await expect(page.locator('#release-badge')).toHaveText('课程已更新');
  await expect(page.locator('#check-in')).toBeDisabled();
  await context.setOffline(false);
});
