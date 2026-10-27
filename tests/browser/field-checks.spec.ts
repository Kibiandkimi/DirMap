import { test, expect } from './fixtures';

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
  await page.locator('dialog canvas').evaluate((canvas, [x, y, width, height]) => {
    const box = canvas.getBoundingClientRect();
    canvas.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 1,
      clientX: box.x+x/width*box.width, clientY: box.y+y/height*box.height }));
  }, [control.pixel[0], control.pixel[1], state.width, state.height]);
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
