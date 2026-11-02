import { test, expect } from './fixtures';
import { teachingSheet, openSheet, finished } from './sheet-fixture';

function timedTrip(index: number) {
  const whens: string[] = [], coords: string[] = [];
  let time = Date.UTC(2026, 9, 10 + index);
  const pace = [1, 1.35, 1.8][index];
  for (const y of [120, 260, 400, 540]) for (let x = 60; x <= 650; x += 5) {
    const height = 45 - Math.hypot(x - 400, (y - 320) / .8) * .035 + index;
    whens.push(`<when>${new Date(time).toISOString()}</when>`);
    coords.push(`<gx:coord>${113.93 + x * .3 / 103000} ${22.53 + (700 - y) * .3 / 111000} ${height}</gx:coord>`);
    time += (x < 350 ? 1500 : 2100) / pace;
  }
  return Buffer.from(`<kml xmlns:gx="http://www.google.com/kml/ext/2.2"><Document><name>跑走交替 ${index + 1}</name><Placemark><gx:Track>${whens.join('')}${coords.join('')}</gx:Track></Placemark></Document></kml>`);
}

test('automatic contours, per-track data and shared foot efficiency are readable on desktop and phone', async ({ page }) => {
  test.setTimeout(180000);
  const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
  const account = await page.request.post('/api/auth/register', { data: { username: `efficiency-${Date.now() % 1000000}`, password: 'foot-efficiency-test' } });
  expect(account.ok()).toBe(true); const owner = (await account.json()).user;
  const { asset, state } = await teachingSheet(page, false, false);
  try {
    expect(state.automaticContours.used).toBe(3);
    expect(Object.keys(state.contourHeights ?? {})).toHaveLength(0);
    await openSheet(page, asset.id);
    await page.locator('[data-tab="terrain"]').click(); await page.locator('#contour-review').click();
    await expect(page.locator('.contour-auto-summary')).toContainText('已自动加入 3 条');
    await expect(page.locator('.contour-manual')).not.toHaveAttribute('open');
    await page.getByRole('button', { name: '关闭等高线', exact: true }).click();
    await page.locator('#elevation-layer').check();
    const ids: string[] = [];
    for (let i = 0; i < 3; i++) {
      const response = await page.request.post('/api/elevation/tracks', { multipart: { files: { name: `mixed-${i}.kml`, mimeType: 'application/vnd.google-earth.kml+xml', buffer: timedTrip(i) } } });
      expect(response.ok(), await response.text()).toBe(true);
      const receipt = await response.json(); ids.push(receipt.added[0]); await finished(page, receipt.job);
      const before = await (await page.request.get('/api/elevation')).json();
      const changed = await page.request.patch('/api/elevation/tracks/' + ids[i], { data: { version: before.version, activity: ['walk', 'run', 'foot'][i] } });
      expect(changed.ok()).toBe(true); await finished(page, (await changed.json()).job);
    }
    const shared = await (await page.request.get('/api/elevation')).json();
    const region = shared.regions.find((r: any) => r.trackIds.includes(ids[0]));
    const model = shared.travelModels.find((m: any) => m.regionId === region.id);
    expect(model.profiles.foot.tracks).toBeGreaterThanOrEqual(3);
    expect(model.profiles.foot.corridorCount).toBeGreaterThan(0);
    const latestArtifact = region.artifact.replace(/^models\//, '').replace(/\.npz$/, '');
    await page.locator('#refresh-terrain').click();
    // No page reload or layer toggle: completing recognition refreshes the
    // already visible elevation overlay to the newly reconstructed model.
    await expect.poll(() => page.evaluate(() => ((window as any).dirmap.campusMap.elevation as any[]).map(layer => layer.getSource().getUrl())), { timeout: 30000 }).toContainEqual(expect.stringContaining(latestArtifact));
    await page.goto('/elevation?region=' + region.id, { waitUntil: 'domcontentloaded' });
    await expect(page.locator('#height-empty')).toBeHidden();
    await expect(page.locator('#height-travel svg.efficiency-map')).toBeVisible();
    expect(await page.locator('#height-travel svg.efficiency-map polyline').count()).toBeGreaterThan(0);
    await expect(page.locator('#height-travel')).toContainText('相对通行效率');
    await expect(page.locator('[data-track-activity]').first()).toHaveValue('foot');
    const canvas = page.locator('#height-canvas');
    if (await page.locator('#height-3d').isEnabled()) {
      await expect.poll(async () => Number(await canvas.getAttribute('data-contour-segments'))).toBeGreaterThan(0);
      const on = await canvas.screenshot();
      await page.locator('#height-contours').uncheck();
      expect((await canvas.screenshot()).equals(on)).toBe(false);
      await page.locator('#height-contours').check();
    }
    await page.screenshot({ path: '/tmp/dirmap-efficiency-desktop.png', fullPage: true });
    await page.locator('#height-track-select').selectOption(ids[1]);
    await expect(page.locator('#height-selected-name')).toContainText('跑走交替 2');
    await expect(page.locator('#height-track-detail')).toContainText('原始采样点');
    await expect(page.locator('#height-track-detail svg')).toHaveCount(2);
    await expect(page.locator('#height-statistics')).toContainText('本轨迹支持的范围');
    await expect(page.locator('#height-travel [data-inspect-track]')).toHaveCount(1);
    await expect(page.locator('#height-travel [data-inspect-track]')).toHaveText('跑走交替 2');
    await expect(page.locator('.height-track.selected')).toHaveCount(1);
    expect((await (await page.request.get('/api/elevation')).json()).version).toBe(shared.version);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('#height-map').click();
    await page.locator('#height-track-detail').scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: '/tmp/dirmap-efficiency-track-mobile.png', fullPage: true });
    await page.locator('#height-show-all').click();
    await expect(page.locator('#height-track-detail')).toBeHidden();
    await expect(page.locator('#height-statistics')).toContainText('有观测支持的范围');
    // An excluded source still contributes terrain, but must not display
    // another person's slope/corridor learning as this source's result.
    await page.locator('#height-track-select').selectOption(ids[1]);
    await page.locator(`[data-track-activity="${ids[1]}"]`).selectOption('exclude');
    await expect.poll(async () => (await (await page.request.get('/api/elevation')).json()).tracks.find((t: any) => t.id === ids[1]).speedLearning?.inferredActivity).toBe('excluded');
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.locator('#height-travel')).toContainText('这条轨迹未参与此类耗时学习');
    await expect(page.locator('#height-travel svg')).toHaveCount(0);
    await expect(page.locator('#height-track-detail')).toContainText('参与共同重建');
    expect(errors).toEqual([]);
  } finally {
    const status = await (await page.request.get('/api/elevation')).json(); let version = status.version;
    for (const track of status.tracks.filter((t: any) => t.ownerId === owner.id)) {
      const response = await page.request.delete('/api/elevation/tracks/' + track.id, { data: { version } });
      expect(response.ok()).toBe(true); const receipt = await response.json(); version = receipt.version; await finished(page, receipt.job);
    }
    const deleted = await page.request.delete(`/api/maps/${asset.id}`); await finished(page, (await deleted.json()).job);
  }
});
