import { test, expect } from './fixtures';
import { teachingSheet, openSheet, finished } from './sheet-fixture';

test('continuous relative contour levels undo, survive reload and align to an existing track', async ({ page }) => {
  test.setTimeout(180000);
  await page.setViewportSize({ width: 390, height: 844 });
  const account = await page.request.post('/api/auth/register', { data: { username: `levels-${Date.now() % 1000000}`, password: 'contour-level-review' } });
  expect(account.ok()).toBe(true); const owner = (await account.json()).user;
  const { asset, state } = await teachingSheet(page, false, false);
  try {
    const points = [];
    for (const y of [250, 390]) for (let x = 80; x <= 640; x += 10) {
      const dx = x-400, dy = y-320, radius = Math.hypot(dx*Math.cos(.2)+dy*Math.sin(.2), (-dx*Math.sin(.2)+dy*Math.cos(.2))/.8);
      points.push(`${113.93+x*.3/103000},${22.53+(700-y)*.3/111000},${57.2-radius*.04}`);
    }
    const kml = `<kml><Document><Placemark><name>Synthetic hill reference</name><LineString><altitudeMode>absolute</altitudeMode><coordinates>${points.join(' ')}</coordinates></LineString></Placemark></Document></kml>`;
    const uploaded = await page.request.post('/api/elevation/tracks', { multipart: { files: { name: 'hill.kml', mimeType: 'application/vnd.google-earth.kml+xml', buffer: Buffer.from(kml) } } });
    expect(uploaded.ok(), await uploaded.text()).toBe(true);
    await finished(page, (await uploaded.json()).job);
    const report = await (await page.request.get(`/api/maps/${asset.id}/contours`)).json();
    expect(report.heightReference).toBe('relative'); expect(report.contours).toHaveLength(3);
    const curves = report.contours;
    async function open() {
      await openSheet(page, asset.id, true);
      await page.locator('[data-tab="terrain"]').click(); await page.locator('#contour-review').click();
      await expect(page.locator('.contour-dialog')).toBeVisible();
    }
    async function tap(index: number) {
      const [x, y] = curves[index].pixels[0];
      const box = (await page.locator('.contour-dialog canvas').boundingBox())!;
      await page.mouse.click(box.x+x/state.width*box.width, box.y+y/state.height*box.height);
    }
    const row = (index: number) => page.locator(`[data-contour="${curves[index].id}"]`);
    await open();
    await page.locator('[data-interval]').fill('2');
    await page.locator('.contour-sequence-tools summary').click();
    await page.locator('[data-sequence-next]').fill('0'); await page.locator('[data-sequence-mode]').check();
    await tap(0); await tap(0);
    await expect(row(0).locator('[data-height]')).toHaveValue('0');
    await expect(page.locator('[data-sequence-next]')).toHaveValue('2');
    await tap(1); await expect(row(1).locator('[data-height]')).toHaveValue('2');
    await page.locator('[data-map-undo]').click();
    await expect(row(1).locator('[data-height]')).toBeEmpty();
    await expect(row(1).locator('[data-use]')).not.toBeChecked();
    await tap(1);
    await page.getByRole('button', { name: '关闭等高线', exact: true }).click();
    await page.reload({ waitUntil: 'domcontentloaded' }); await open();
    await expect(row(0).locator('[data-height]')).toHaveValue('0'); await expect(row(1).locator('[data-height]')).toHaveValue('2');
    await page.locator('.contour-sequence-tools summary').click();
    await expect(page.locator('[data-sequence-next]')).toHaveValue('4');
    await page.locator('[data-sequence-mode]').check(); await tap(0);
    await expect(page.locator('[data-sequence-next]')).toHaveValue('4');
    await tap(2); await expect(row(2).locator('[data-height]')).toHaveValue('4');
    await page.screenshot({ path: '/tmp/dirmap-contour-continuous-levels.png' });
    // Reference heights must be translated to the chosen relative zero,
    // instead of mixing the existing 0/2 with an absolute value near 50 m.
    await row(0).locator('[data-focus-line]').click();
    await row(2).locator('[data-height]').fill('');
    expect(curves[0].suggestedHeightM).not.toBeNull(); expect(curves[2].suggestedHeightM).not.toBeNull();
    await page.locator('[data-suggestions]').click();
    const relativeHint = Math.round((curves[2].suggestedHeightM-curves[0].suggestedHeightM)*1e6)/1e6;
    await expect(row(2).locator('[data-height]')).toHaveValue(String(relativeHint));
    await expect(row(0).locator('[data-height]')).toHaveValue('0');
    await row(2).locator('[data-height]').fill('4');
    const posted = page.waitForRequest(request => request.url().endsWith(`/api/maps/${asset.id}/contours`) && request.method() === 'POST');
    await page.locator('[data-save]').click();
    expect((await posted).postDataJSON()).toMatchObject({ version: state.version, revision: 0, heightReference: 'relative', intervalM: 2 });
    await expect(page.locator('.contour-dialog')).toBeHidden({ timeout: 30000 });
    const saved = await (await page.request.get(`/api/maps/${asset.id}`)).json();
    expect(saved.version).toBe(state.version); expect(saved.contourRevision).toBe(1);
    const status = await (await page.request.get('/api/elevation')).json();
    const contour = status.tracks.find((track: any) => track.ownerId === owner.id && track.kind === 'contours');
    expect(contour.heightReference).toBe('relative');
    const region = status.regions.find((region: any) => region.trackIds.includes(contour.id));
    const alignment = region.alignment.tracks.find((track: any) => track.id === contour.id);
    expect(alignment.anchored).toBe(true); expect(alignment.correctionM).toBeGreaterThan(40); expect(alignment.driftRangeM).toBe(0);
    expect(region.minHeightM).toBeGreaterThan(30);
  } finally {
    const status = await (await page.request.get('/api/elevation')).json(); let version = status.version;
    for (const track of status.tracks.filter((track: any) => track.ownerId === owner.id)) {
      const result = await (await page.request.delete(`/api/elevation/tracks/${track.id}`, { data: { version } })).json();
      version = result.version; await finished(page, result.job);
    }
    await page.request.delete(`/api/maps/${asset.id}`);
  }
});

test('relative contours without loaded height references remain visibly pending', async ({ page }) => {
  test.setTimeout(120000);
  const account = await page.request.post('/api/auth/register', { data: { username: `waiting-${Date.now() % 1000000}`, password: 'contour-reference-waiting' } });
  expect(account.ok()).toBe(true); const owner = (await account.json()).user;
  const { asset } = await teachingSheet(page, false, false);
  const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
  try {
    await openSheet(page, asset.id); await page.locator('[data-tab="terrain"]').click(); await page.locator('#contour-review').click();
    await expect(page.locator('[data-height-reference]')).toHaveValue('relative');
    await page.locator('[data-interval]').fill('2');
    const rows = page.locator('.contour-row');
    for (let i = 0; i < 3; i++) { await rows.nth(i).locator('[data-height]').fill(String(i*2)); await rows.nth(i).locator('[data-use]').check(); }
    await page.locator('[data-save]').click(); await expect(page.locator('.contour-dialog')).toBeHidden({ timeout: 30000 });
    await page.goto('/elevation', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('#height-empty')).toContainText('等待覆盖这些线的轨迹或已知标高');
    await expect(page.locator('.height-track').filter({ hasText: '已核对等高线' })).toContainText('等待重叠轨迹或已知标高');
    expect(errors).toEqual([]);
  } finally {
    const status = await (await page.request.get('/api/elevation')).json(); let version = status.version;
    for (const track of status.tracks.filter((track: any) => track.ownerId === owner.id)) {
      const result = await (await page.request.delete(`/api/elevation/tracks/${track.id}`, { data: { version } })).json();
      version = result.version; await finished(page, result.job);
    }
    await page.request.delete(`/api/maps/${asset.id}`);
  }
});
