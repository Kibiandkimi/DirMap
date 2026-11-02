import { test, expect } from './fixtures';
import { readFile } from 'node:fs/promises';

test('a teacher records GPS check points on site and sees the independent error without changing the map', async ({ page, context }) => {
  const state = await (await page.request.get('/api/maps/szu-1')).json();
  const control = state.controls.find((c: any) => c.calibrated && c.lnglat && (c.role ?? 'control') === 'control');
  await context.grantPermissions(['geolocation']);
  await context.setGeolocation({ longitude: control.lnglat[0], latitude: control.lnglat[1], accuracy: 4 });
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.control-row').first()).toBeVisible();
  await page.locator('[data-tab="registration"]').click();
  // Standing at the target: mark its centre on the sheet.
  await page.locator('#add-field-check').click();
  await expect(page.locator('dialog canvas')).toBeVisible();
  await expect(page.locator('#editor-hint')).toContainText('所站位置');
  const position = await page.locator('dialog canvas').evaluate((canvas, [x, y, width, height]) => {
    const box = canvas.getBoundingClientRect();
    return { x: box.x+x/width*box.width, y: box.y+y/height*box.height };
  }, [control.pixel[0], control.pixel[1], state.width, state.height]);
  await page.mouse.click(position.x, position.y);
  await expect(page.locator('dialog')).toBeVisible();
  await page.locator('#confirm-point').click();
  await expect(page.locator('dialog')).toHaveCount(0);
  await expect(page.locator('#field-check-list > div')).toHaveCount(1);
  await expect(page.locator('#field-check-list')).toContainText('误差 0.0 m');
  await expect(page.locator('#field-check-summary')).toContainText('1 个可用点');
  await expect(page.locator('#field-check-summary')).toContainText('至少 5 个可用点后对照门槛');
  await page.locator('#field-check-card').screenshot({ path: '/tmp/dirmap-field-checks.png' });
  // On the registration tab the map shows the point; other tabs stay clean.
  const drawn = () => page.evaluate(() => (window as any).dirmap.campusMap.fieldChecks.getFeatures().length);
  expect(await drawn()).toBe(2);
  await page.locator('[data-tab="course"]').click();
  expect(await drawn()).toBe(0);
  await page.locator('[data-tab="registration"]').click();
  expect(await drawn()).toBe(2);
  // Closing the sheet without marking a position records nothing.
  await page.locator('#add-field-check').click();
  await page.locator('[data-close]').click();
  await expect(page.locator('#add-field-check')).toBeEnabled();
  await expect(page.locator('#field-check-list > div')).toHaveCount(1);
  const after = await (await page.request.get('/api/maps/szu-1')).json();
  expect(after.version).toBe(state.version);
  await page.locator('[data-remove-field-check]').click();
  await expect(page.locator('#field-check-list > div')).toHaveCount(0);
});

test('field capture freezes its OpenHarmony clock before image picking and exports a reproducible record', async ({ page }) => {
  const state = await (await page.request.get('/api/maps/szu-1')).json();
  const control = state.controls.find((c: any) => c.calibrated && c.lnglat && (c.role ?? 'control') === 'control');
  await page.addInitScript((lnglat: number[]) => {
    Object.defineProperty(navigator, 'userAgent', { configurable: true, value: 'OpenHarmony 7.0 ArkWeb/6.0.0.46SP3 MicroMessenger/8.0.19.47' });
    Object.defineProperty(navigator, 'geolocation', { configurable: true, value: {
      getCurrentPosition: (callback: Function) => {
        (window as any).fieldCapturedAt = Date.now();
        callback({ timestamp: 1234567890, coords: { longitude: lnglat[0], latitude: lnglat[1], accuracy: 4 } });
      },
    } });
  }, control.lnglat);
  await page.clock.install();
  let id: string | null = null;
  try {
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.control-row').first()).toBeVisible();
    await expect(page.locator('#site-select')).toBeEnabled();
    await page.locator('[data-tab="registration"]').click(); await page.locator('#add-field-check').click();
    await expect(page.locator('.editor-dialog')).toBeVisible();
    const capturedAt = await page.evaluate(() => (window as any).fieldCapturedAt);
    await page.clock.fastForward(120000);
    const p = await page.locator('.editor-dialog canvas').evaluate((canvas, [x, y, width, height]) => {
      const box = canvas.getBoundingClientRect(); return { x: box.x+x/width*box.width, y: box.y+y/height*box.height };
    }, [control.pixel[0], control.pixel[1], state.width, state.height]);
    await page.mouse.click(p.x, p.y);
    const saved = page.waitForResponse(r => r.url().endsWith('/api/maps/szu-1/field-checks') && r.request().method() === 'POST');
    await page.locator('#confirm-point').click();
    const response = await saved; expect(response.ok()).toBe(true);
    const row = (await response.json()).checks.at(-1); id = row.id;
    expect(row.measuredAt).toBeGreaterThanOrEqual(capturedAt);
    expect(row.measuredAt).toBeLessThan(capturedAt+1000);
    expect(row.receivedAt).toBe(row.measuredAt);
    expect(row.rawTimestamp).toBe(1234567890); expect(row.positionClock).toBe('received');
    expect(row.sourceHash).toBe(state.sourceHash);
    const downloaded = page.waitForEvent('download'); await page.locator('#export-field-checks').click();
    const download = await downloaded;
    const record = JSON.parse(await readFile((await download.path())!, 'utf8'));
    expect(record.schema).toBe('dirmap-field-checks-v1');
    expect(record.map.registration).toEqual(state.registration);
    expect(record.map.imageSize).toEqual([state.width, state.height]);
    expect(record.checks.find((c: any) => c.id === id).measuredAt).toBe(row.measuredAt);
    expect((await (await page.request.get('/api/maps/szu-1')).json()).version).toBe(state.version);
  } finally { if (id) await page.request.delete(`/api/maps/szu-1/field-checks/${id}`); }
});

test('field capture can time out or cancel even when the WebView never finishes its request', async ({ page }) => {
  const state = await (await page.request.get('/api/maps/szu-1')).json();
  const control = state.controls.find((c: any) => c.calibrated && c.lnglat);
  const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    (window as any).fieldCallbacks = [];
    Object.defineProperty(navigator, 'geolocation', { configurable: true, value: {
      getCurrentPosition: (success: Function) => (window as any).fieldCallbacks.push(success),
    } });
  });
  await page.clock.install(); await page.goto('/', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.control-row').first()).toBeVisible();
  await expect(page.locator('#site-select')).toBeEnabled();
  await page.locator('[data-tab="registration"]').click(); await page.locator('#add-field-check').click();
  await expect(page.locator('#cancel-field-check')).toBeVisible();
  await page.clock.fastForward(20050);
  await expect(page.locator('.toast')).toContainText('定位超时');
  await expect(page.locator('#site-select')).toBeEnabled();
  await page.locator('#add-field-check').click(); await page.locator('#cancel-field-check').click();
  await expect(page.locator('#site-select')).toBeEnabled();
  await page.evaluate(() => {
    for (const callback of (window as any).fieldCallbacks) callback({ get coords() { throw new Error('retired callback used'); } });
  });
  await expect(page.locator('.editor-dialog')).toHaveCount(0);
  await page.locator('#add-field-check').click();
  await page.evaluate((lnglat: number[]) => (window as any).fieldCallbacks.at(-1)({ timestamp: Date.now(),
    coords: { longitude: lnglat[0], latitude: lnglat[1], accuracy: 4 } }), control.lnglat);
  await expect(page.locator('.editor-dialog')).toBeVisible();
  await page.getByRole('button', { name: '关闭原图', exact: true }).click();
  await expect(page.locator('#site-select')).toBeEnabled();
  await expect(page.locator('#field-check-list > div')).toHaveCount(0);
  expect(errors).toEqual([]);
});
