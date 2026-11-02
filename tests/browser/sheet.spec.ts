import { test, expect } from './fixtures';
import { teachingSheet, openSheet, finished } from './sheet-fixture';

test('phone reference cards, approach previews and released photos work offline', async ({ page, context }) => {
  test.setTimeout(180000);
  await page.setViewportSize({ width: 390, height: 844 });
  const { asset, state } = await teachingSheet(page);
  try {
    await openSheet(page, asset.id, true);
    await page.locator('#sheet-photo-album').click();
    await expect(page.locator('.sheet-photo-card')).toHaveCount(3);
    await page.locator('.sheet-photo-card').filter({ hasText: '目标 32' }).click();
    await expect(page.locator('.sheet-photo-view img')).toBeVisible();
    await expect.poll(() => page.locator('.sheet-photo-view img').evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);
    await page.screenshot({ path: '/tmp/dirmap-reference-photo-mobile.png' });
    await page.getByRole('button', { name: '关闭参考照片' }).click();
    await page.locator('#people').fill('4'); await page.locator('#teams').fill('2');
    await page.locator('#course-mode').selectOption('all_visit');
    const planning = page.waitForResponse(r => new URL(r.url()).pathname === '/api/plans' && r.request().method() === 'POST');
    await page.locator('#plan-button').click();
    const result = await finished(page, await (await planning).json());
    await expect(page.locator('#plan-errors')).toContainText('32', { timeout: 30000 });
    const target = state.controls.find((c: any) => c.label === '32');
    expect(result.unresolved.find((c: any) => c.id === target.id).candidates?.length, JSON.stringify(result.unresolved)).toBeGreaterThan(0);
    await page.locator(`#plan-errors [data-control-edit="${target.id}"]`).click();
    await expect(page.locator('[data-approach-choice="0"]')).toBeVisible();
    const reference = page.getByRole('button', { name: '查看目标 32 的参考照片', exact: true });
    await expect(reference).toBeInViewport();
    await expect.poll(() => page.locator('.editor-reference img').evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);
    await page.locator('#control-label').fill('99'); await page.locator('#control-label').blur();
    await expect(page.locator('.editor-reference')).toBeHidden();
    await page.locator('#control-label').fill('32'); await page.locator('#control-label').blur();
    await expect(reference).toBeVisible();
    for (const viewport of [{ width: 375, height: 667 }, { width: 360, height: 640 }]) {
      await page.setViewportSize(viewport);
      const accessible = await page.locator('.editor-dialog').evaluate(dialog =>
        dialog.scrollHeight <= dialog.clientHeight+1 || ['auto', 'scroll'].includes(getComputedStyle(dialog).overflowY));
      expect(accessible, `editor actions must fit or scroll at ${viewport.width} × ${viewport.height}`).toBe(true);
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('[data-approach-choice="0"]').click();
    await expect(page.locator('#point-confirmation')).toBeVisible();
    const preview = await page.locator('#point-preview').innerText();
    await expect(reference).toBeInViewport(); await reference.click();
    await expect(page.locator('.sheet-photo-dialog')).toBeVisible();
    await page.getByRole('button', { name: '关闭参考照片' }).click();
    await expect(page.locator('#point-preview')).toHaveText(preview);
    await expect(page.locator('#confirm-point')).toBeEnabled();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.setViewportSize({ width: 844, height: 390 });
    const dialog = (await page.locator('.editor-dialog').boundingBox())!;
    await page.mouse.move(dialog.x+dialog.width-7, dialog.y+dialog.height/2);
    await page.mouse.wheel(0, 700);
    await expect(page.locator('#confirm-point')).toBeInViewport();
    await expect(page.locator('#point-preview')).toHaveText(preview);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: '/tmp/dirmap-approach-preview-mobile.png' });
    await page.locator('#confirm-point').click(); await page.locator('#save-edits').click();
    await expect(page.locator('.editor-dialog')).toBeHidden();
    const saved = await (await page.request.get(`/api/maps/${asset.id}`)).json();
    const selected = saved.controls.find((c: any) => c.id === target.id);
    expect(selected.pixel).toEqual(target.pixel); expect(selected.approachPixel).not.toEqual(target.pixel);
    await page.locator('#plan-button').click();
    await expect(page.locator('.route-card')).toHaveCount(2, { timeout: 30000 });
    await page.locator('#review-button').click();
    for (const name of ['registration', 'controls', 'passages']) await page.locator('#review-' + name).check();
    if (await page.locator('#review-approaches').count()) await page.locator('#review-approaches').check();
    await page.locator('#publish-confirm').click();
    const link = page.getByLabel('学生课程链接'); await expect(link).toBeVisible({ timeout: 30000 });
    const id = new URL(await link.inputValue()).searchParams.get('course');
    await page.goto(`http://127.0.0.1:8768/?course=${id}`, { waitUntil: 'domcontentloaded' });
    await expect(page.locator('#offline-status')).toContainText('本课已保存', { timeout: 30000 });
    await page.waitForFunction(() => !!navigator.serviceWorker.controller);
    await context.setOffline(true); await page.reload({ waitUntil: 'domcontentloaded' });
    await page.locator('#mobile-panel').click();
    await page.locator('#student-stops [data-reference-photo]').first().click();
    await expect.poll(() => page.locator('.sheet-photo-view img').evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);
    await page.screenshot({ path: '/tmp/dirmap-reference-offline-mobile.png' });
  } finally { await context.setOffline(false); await page.request.delete(`/api/maps/${asset.id}`); }
});

test('teacher reviews printed contours and contributes them to shared terrain', async ({ page }) => {
  test.setTimeout(180000);
  const account = await page.request.post('/api/auth/register', { data: { username: `contour-${Date.now() % 1000000}`, password: 'contour-height-review' } });
  expect(account.ok()).toBe(true); const owner = (await account.json()).user;
  const { asset } = await teachingSheet(page, false, false);
  try {
    await openSheet(page, asset.id);
    await page.locator('[data-tab="terrain"]').click();
    await page.locator('#contour-review').click();
    await expect(page.locator('.contour-dialog')).toBeVisible();
    if (await page.locator('.contour-manual').evaluate((el: HTMLDetailsElement) => !el.open)) await page.locator('.contour-manual > summary').click();
    const rows = page.locator('.contour-row:has([data-use]:enabled)');
    expect(await rows.count()).toBeGreaterThanOrEqual(3);
    await page.locator('[data-height-reference]').selectOption('reference');
    await page.locator('[data-interval]').fill('2');
    for (let i = 0; i < 3; i++) { await rows.nth(i).locator('[data-height]').fill(String(24+i*2)); await rows.nth(i).locator('[data-use]').check(); }
    await rows.first().locator('[data-focus-line]').click();
    await page.screenshot({ path: '/tmp/dirmap-contour-review.png' });
    await page.locator('.contour-dialog [data-save]').click();
    await expect(page.locator('.contour-dialog')).toBeHidden({ timeout: 30000 });
    const status = await (await page.request.get('/api/elevation')).json();
    const track = status.tracks.find((t: any) => t.ownerId === owner.id && t.enabled);
    expect(track.kind).toBe('contours'); expect(track.contourCount).toBe(3);
    expect(status.regions.some((r: any) => r.trackIds.includes(track.id) && r.supportedCells > 0)).toBe(true);
    await page.goto('/elevation', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.height-track').filter({ hasText: '已核对等高线' })).toBeVisible();
  } finally {
    const status = await (await page.request.get('/api/elevation')).json(); let version = status.version;
    for (const track of status.tracks.filter((t: any) => t.ownerId === owner.id)) {
      const result = await (await page.request.delete(`/api/elevation/tracks/${track.id}`, { data: { version } })).json();
      version = result.version; await finished(page, result.job);
    }
    await page.request.delete(`/api/maps/${asset.id}`);
  }
});
