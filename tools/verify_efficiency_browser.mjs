/** Exercise the real recorded data in a disposable local workbench. */
import { chromium, expect } from '@playwright/test';
import fs from 'node:fs/promises';
import assert from 'node:assert/strict';

const base = process.argv[2] ?? 'http://127.0.0.1:8770';
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(base).hostname));
const credentials = JSON.parse(await fs.readFile(process.argv[3] ?? '/tmp/dirmap-efficiency-preview-login.json', 'utf8'));
const directory = process.argv[4] ?? '/tmp/dirmap-efficiency-visual';
await fs.mkdir(directory, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.DIRMAP_BROWSER_PATH || '/usr/bin/google-chrome' });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.route(url => !['localhost', '127.0.0.1'].includes(url.hostname), route => route.abort('internetdisconnected'));
const page = await context.newPage(), errors = [], screenshots = [];
const reliefRequests = [];
page.on('response', response => { if (response.status() === 200 && response.url().includes('/relief.png')) reliefRequests.push(response.url()); });
page.on('pageerror', error => errors.push(error.message));
const screenshot = async (name, fullPage = false) => {
  const path = `${directory}/${name}.png`; await page.screenshot({ path, fullPage }); screenshots.push(path);
};
try {
  const login = await context.request.post(base + '/api/auth/login', { data: credentials }); assert.equal(login.status(), 200);
  const shared = await (await context.request.get(base + '/api/elevation')).json();
  assert.equal(shared.tracks.filter(t => t.kind !== 'contours').length, 16);
  assert.equal(shared.tracks.filter(t => t.activity === 'bicycle').length, 5);
  const campus = shared.regions.find(r => r.name.includes('粤海'));
  await page.goto(`${base}/elevation?region=${campus.id}`, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#height-empty')).toBeHidden();
  await expect(page.locator('#height-travel .efficiency-map')).toBeVisible();
  await expect(page.locator('#height-3d')).toBeEnabled();
  await expect.poll(async () => Number(await page.locator('#height-canvas').getAttribute('data-contour-segments'))).toBeGreaterThan(0);
  const on = await page.locator('#height-canvas').screenshot();
  await page.locator('#height-contours').uncheck();
  assert.equal((await page.locator('#height-canvas').screenshot()).equals(on), false);
  await page.locator('#height-contours').check();
  await screenshot('real-shared-3d', true);
  const track = shared.tracks.find(t => campus.trackIds.includes(t.id) && t.speedLearning?.inferredActivity === 'foot');
  await page.locator('#height-track-select').selectOption(track.id);
  await expect(page.locator('#height-track-detail')).toContainText(track.name);
  await expect(page.locator('#height-travel [data-inspect-track]')).toHaveCount(1);
  await page.locator('#height-map').click();
  await screenshot('real-single-track', true);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('#height-track-detail').scrollIntoViewIfNeeded();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await screenshot('real-track-mobile');
  const automatic = shared.tracks.find(t => t.heightReference === 'shape');
  await page.locator('#height-track-select').selectOption(automatic.id);
  await expect(page.locator('#height-selected-name')).toContainText(automatic.name);
  await expect(page.locator('#height-track-detail')).toContainText('自动等高线');
  await page.locator('.height-toolbar').scrollIntoViewIfNeeded();
  await screenshot('real-automatic-contours-mobile');
  assert.equal((await (await context.request.get(base + '/api/elevation')).json()).version, shared.version);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.control-row').first()).toBeVisible();
  await page.locator('#elevation-layer').check(); await page.locator('#elevation-style').selectOption('contours');
  await expect.poll(() => reliefRequests.some(url => url.endsWith('?style=contours'))).toBe(true);
  await page.locator('#people').fill('2'); await page.locator('#teams').fill('1');
  await page.locator('#plan-button').click();
  await expect(page.locator('.route-card')).toHaveCount(1, { timeout: 90000 });
  await expect(page.locator('.route-card .time-composition')).toBeVisible();
  await expect(page.locator('.route-card .route-speed')).toContainText('跑走共同学习');
  await screenshot('real-course-contour-overlay');
  await page.locator('#review-button').click();
  for (const field of ['registration', 'controls', 'passages']) await page.locator('#review-' + field).check();
  if (await page.locator('#review-approaches').count()) await page.locator('#review-approaches').check();
  await page.locator('#review-collect-heights').uncheck();
  await page.locator('#publish-confirm').click();
  const link = page.getByLabel('学生课程链接'); await expect(link).toBeVisible({ timeout: 30000 });
  const courseUrl = await link.inputValue();
  const student = await context.newPage(), frozenRequests = [];
  student.on('response', response => { if (response.status() === 200 && response.url().includes('/relief.png')) frozenRequests.push(response.url()); });
  await student.goto(courseUrl, { waitUntil: 'domcontentloaded' });
  await expect(student.locator('#elevation-style')).toHaveValue('contours');
  await expect.poll(() => frozenRequests.some(url => url.endsWith('?style=contours'))).toBe(true);
  // Saving the course must include the alternate style even if it was not
  // viewed. Remove that variant first to distinguish prefetch from a hit.
  await expect(student.locator('#offline-status')).toContainText('本课已保存', { timeout: 30000 });
  await student.locator('#elevation-style').selectOption('relief');
  await student.evaluate(async () => {
    const cache = await caches.open('dirmap-reliefs-v1');
    for (const request of await cache.keys()) if (new URL(request.url).searchParams.get('style') === 'contours') await cache.delete(request);
  });
  await student.locator('#save-offline').click();
  await expect(student.locator('#offline-status')).toContainText('本课已保存', { timeout: 30000 });
  await expect.poll(() => student.evaluate(async () => (await (await caches.open('dirmap-reliefs-v1')).keys()).some(request => new URL(request.url).searchParams.get('style') === 'contours'))).toBe(true);
  await page.close(); await context.setOffline(true);
  await student.reload({ waitUntil: 'domcontentloaded' });
  await expect(student.locator('#release-badge')).toContainText('离线');
  await student.locator('#elevation-style').selectOption('contours');
  const decoded = await student.evaluate(async () => {
    const cache = await caches.open('dirmap-reliefs-v1');
    const request = (await cache.keys()).find(request => new URL(request.url).searchParams.get('style') === 'contours');
    const image = new Image(); image.src = request.url; await image.decode(); return image.naturalWidth;
  });
  assert.ok(decoded > 0);
  const offlineShot = `${directory}/real-contours-offline.png`; await student.screenshot({ path: offlineShot }); screenshots.push(offlineShot);
  await student.close();
  assert.deepEqual(errors, []);
  await fs.writeFile(`${directory}/report.json`, JSON.stringify({ checkedAt: new Date().toISOString(), source: '16 real KML recordings and automatically recognized Dashahe contours in a local copy',
    screenshots, desktopAndMobile: true, contourOnlyOverlay: true, frozenCourseOverlay: true, threeDimensionalContours: true,
    singleContributionOnly: true, sourceVersionUnchangedByViewing: true, travelComposition: true,
    contourStylePrefetchedAndDecodedOffline: true, errors, allChecksPassed: true }, null, 2));
  console.log(JSON.stringify({ screenshots, allChecksPassed: true }));
} finally { await browser.close(); }
