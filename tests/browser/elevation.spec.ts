import { test, expect, localOnly, type Page } from './fixtures';

const password = 'shared-terrain-walks';

/** Crossing walks with clock times over a smooth hill, as a phone app exports them. */
function walks(name: string, west: number, south: number, east: number, north: number) {
  const step = .00005, gap = .0004, hill = (lng: number, lat: number) =>
    10 + 25 * Math.exp(-(((lng - (west + east) / 2) * 103000) ** 2 + ((lat - (south + north) / 2) * 111000) ** 2) / (2 * 300 ** 2));
  let whens = '', coordinates = '', time = Date.UTC(2026, 9, 28, 1);
  const add = (lng: number, lat: number) => {
    whens += `<when>${new Date(time).toISOString()}</when>`; time += 4000;
    coordinates += `<gx:coord>${lng.toFixed(6)} ${lat.toFixed(6)} ${hill(lng, lat).toFixed(1)}</gx:coord>`;
  };
  for (let lat = south; lat <= north; lat += gap) for (let lng = west; lng <= east; lng += step) add(lng, lat);
  for (let lng = west; lng <= east; lng += gap) for (let lat = south; lat <= north; lat += step) add(lng, lat);
  return Buffer.from(`<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2"><Document><name>${name}</name><Placemark><gx:Track>${whens}${coordinates}</gx:Track></Placemark></Document></kml>`);
}

async function register(page: Page, name: string) {
  const response = await page.request.post('/api/auth/register', { data: { username: name, password, claimOriginal: false } });
  expect(response.ok()).toBeTruthy();
  return (await response.json()).user;
}

async function finished(page: Page, job: { id: string } | null) {
  if (!job) return;
  await expect.poll(async () => (await (await page.request.get('/api/jobs/' + job.id)).json()).state, { timeout: 30000 }).toBe('succeeded');
}

async function removeOwnTracks(page: Page, owner: string, scope: string) {
  const status = await (await page.request.get('/api/elevation')).json();
  let version = status.version;
  for (const track of status.tracks.filter((t: { ownerId: string }) => t.ownerId === owner)) {
    const response = await page.request.delete('/api/elevation/tracks/' + track.id, { data: { version }, headers: { 'X-DirMap-Workbench': scope } });
    const receipt = await response.json(); version = receipt.version; await finished(page, receipt.job);
  }
}

test('shared terrain is public to read, rebuilt from a contributor\'s walks, and only its owner manages a walk', async ({ page, browser }) => {
  test.setTimeout(120000);
  const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
  const owner = `terrain-${Date.now() % 1000000}`;
  await page.goto('/elevation', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#height-empty')).toContainText('还没有重建地形');
  // Reading needs no account; contributing asks the visitor to sign in first.
  await page.locator('#height-upload').click();
  await page.locator('[data-account-mode="register"]').click();
  await page.locator('#account-name').fill(owner); await page.locator('#account-password').fill(password); await page.locator('#account-confirm').fill(password);
  await page.locator('#account-submit').click();
  await expect(page.locator('#account-open')).toHaveText(owner);
  const park = walks('大沙河公园 晨走', 113.9545, 22.5575, 113.9585, 22.5615);
  await page.locator('#height-files').setInputFiles({ name: 'park-walk.kml', mimeType: 'application/vnd.google-earth.kml+xml', buffer: park });
  await expect(page.locator('#height-toast')).toContainText('已加入 1 份轨迹');
  await expect(page.locator('#height-empty')).toBeHidden({ timeout: 30000 });
  await expect(page.locator('#height-region option:checked')).toHaveText('大沙河公园');
  await expect(page.locator('#height-status')).toContainText('最近重建');
  const card = page.locator('.height-track').first();
  await expect(card).toContainText('大沙河公园 晨走'); await expect(card).toContainText('我的');
  await expect(card).toContainText('记录于');
  await expect(page.locator('#height-statistics')).toContainText('有观测支持的范围');
  // The north-up height map reports the surface under the pointer.
  await page.locator('#height-map').click();
  const box = (await page.locator('#height-map-canvas').boundingBox())!;
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect(page.locator('#height-inspect')).toContainText('网格参考高程');
  const download = await page.locator('#height-download').getAttribute('href');
  const raster = await page.request.get(download!);
  expect(raster.headers()['content-type']).toBe('image/tiff');
  // The same observations under another file name are recognised.
  await page.locator('#height-files').setInputFiles({ name: 'renamed-export.kml', mimeType: 'application/vnd.google-earth.kml+xml', buffer: park });
  await expect(page.locator('#height-toast')).toContainText('1 份相同轨迹未重复计入');

  // Another account sees and benefits from the walk but cannot manage it.
  const other = await localOnly(await browser.newContext());
  const visitor = await other.newPage();
  const peer = await register(visitor, `${owner}-peer`);
  await visitor.goto('/elevation', { waitUntil: 'domcontentloaded' });
  const shared = visitor.locator('.height-track').first();
  await expect(shared).toContainText(owner); await expect(shared).toContainText('已分享，参与共同重建');
  await expect(shared.locator('[data-track-delete]')).toHaveCount(0);
  const status = await (await visitor.request.get('/api/elevation')).json();
  const refused = await visitor.request.delete('/api/elevation/tracks/' + status.tracks[0].id,
    { data: { version: status.version }, headers: { 'X-DirMap-Workbench': `${peer.id}:personal` } });
  expect(refused.status()).toBe(403);
  await other.close();

  // The owner renames, then removes the walk; the shared surface follows.
  await card.locator('[data-track-name]').click();
  await page.locator('#height-track-new-name').fill('公园山坡');
  await page.locator('dialog [type=submit]').click();
  await expect(card).toContainText('公园山坡');
  await card.locator('[data-track-delete]').click();
  await page.locator('dialog [type=submit]').click();
  await expect(page.locator('#height-empty')).toContainText('还没有重建地形', { timeout: 30000 });
  await expect(page.locator('.height-track')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('route planning can weigh uphill and downhill walking from the shared terrain', async ({ page }) => {
  test.setTimeout(150000);
  const name = `slopes-${Date.now() % 1000000}`;
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  const user = await register(page, name);
  const scope = `${user.id}:personal`;
  try {
    const upload = await page.request.post('/api/elevation/tracks', { headers: { 'X-DirMap-Workbench': scope }, multipart: {
      files: { name: 'campus-walks.kml', mimeType: 'application/vnd.google-earth.kml+xml', buffer: walks('校园交叉路线', 113.926, 22.530, 113.936, 22.541) } } });
    expect(upload.ok()).toBeTruthy();
    await finished(page, (await upload.json()).job);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.locator('#account-open')).toHaveText(name);
    await expect(page.locator('.control-row').first()).toBeVisible();
    await expect(page.locator('#use-elevation')).toBeChecked();
    await page.locator('#teams').fill('2');
    await page.locator('#plan-button').click();
    await expect(page.locator('.route-card')).toHaveCount(2, { timeout: 60000 });
    const summary = page.locator('.route-card .route-elevation').first();
    await expect(summary).toContainText('预计爬升');
    await expect(summary.locator('svg.route-height-profile polyline').first()).toBeVisible();
    // Flat costs on request; the choice is remembered with the course rules.
    await page.locator('#use-elevation').uncheck();
    await page.locator('#plan-button').click();
    await expect(page.locator('.route-card')).toHaveCount(2, { timeout: 60000 });
    await expect(page.locator('.route-card .route-elevation')).toHaveCount(0);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.locator('.control-row').first()).toBeVisible();
    await expect(page.locator('#use-elevation')).not.toBeChecked();
  } finally {
    // Shared terrain reaches every workbench; leave none for later scenarios.
    await removeOwnTracks(page, user.id, scope);
  }
});
