/** Real local KML models through the built application; no external writes. */
import { chromium } from '@playwright/test';
import fs from 'node:fs/promises';
import assert from 'node:assert/strict';

const base = process.argv[2] ?? 'http://127.0.0.1:8770';
if (!['127.0.0.1', 'localhost'].includes(new URL(base).hostname)) throw new Error('This verifier requires a disposable local workbench.');
const login = JSON.parse(await fs.readFile(process.argv[3] ?? '/tmp/dirmap-travel-preview-login.json', 'utf8'));
const output = process.argv[4] ?? '/tmp/dirmap-travel-real-browser.json';
const browser = await chromium.launch({ executablePath: process.env.DIRMAP_BROWSER_PATH });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.route(url => !['127.0.0.1', 'localhost'].includes(url.hostname), route => route.abort('internetdisconnected'));
const errors = [];
const page = await context.newPage();
page.on('pageerror', error => errors.push(error.message));
const report = { source: 'real KMLs in a disposable local workbench', screenshots: [] };
try {
  const signedIn = await context.request.post(base + '/api/auth/login', { data: login });
  assert.equal(signedIn.status(), 200);
  const status = await (await context.request.get(base + '/api/elevation')).json();
  assert.equal(status.tracks.length, 16);
  assert.equal(status.tracks.filter(t => t.activity === 'bicycle').length, 5);
  assert.equal(status.needsRebuild, false);
  await page.goto(base + '/elevation', { waitUntil: 'domcontentloaded' });
  await page.locator('#height-empty').waitFor({ state: 'hidden' });
  await page.locator('#height-travel .learning-chart').first().waitFor();
  await page.locator('#height-map').click();
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.equal(await page.locator('.height-track').count(), 16);
  assert.equal(await page.locator('[data-track-activity] option:checked').evaluateAll(options => options.filter(o => o.value === 'bicycle').length), 5);
  report.summary = await page.locator('#height-travel').innerText();
  await page.screenshot({ path: '/tmp/dirmap-travel-shared-desktop.png', fullPage: true });
  report.screenshots.push('/tmp/dirmap-travel-shared-desktop.png');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('#height-travel').scrollIntoViewIfNeeded();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.screenshot({ path: '/tmp/dirmap-travel-shared-mobile.png' });
  report.screenshots.push('/tmp/dirmap-travel-shared-mobile.png');

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' });
  await page.locator('.control-row').first().waitFor();
  assert.equal(await page.locator('#travel-mode').inputValue(), 'foot');
  await page.locator('#people').fill('2');
  await page.locator('#teams').fill('1');
  await page.locator('#speed').fill('1.2');
  async function plan(enabled) {
    await page.locator('#use-learned-speed').setChecked(enabled);
    const submitted = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/api/plans'));
    await page.locator('#plan-button').click();
    const response = await submitted;
    assert.equal(response.status(), 200);
    const initial = await response.json();
    const deadline = Date.now() + 90000;
    let job = initial;
    while (!['succeeded', 'failed'].includes(job.state) && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 250));
      job = await (await context.request.get(base + '/api/jobs/' + initial.id)).json();
    }
    assert.equal(job.state, 'succeeded', job.message);
    assert.ok(job.result.routes.length, JSON.stringify(job.result.unresolved));
    await page.locator('.route-card').first().waitFor();
    return job.result;
  }
  const learned = await plan(true);
  assert.equal(learned.routes[0].speedLearning.gradeLearned, true);
  assert.match(await page.locator('.route-speed').first().innerText(), /已学习上下坡/);
  report.learned = { graphHash: learned.graphHash, distanceM: learned.routes[0].distanceM,
    movingSec: learned.routes[0].travelSec, speedLearning: learned.routes[0].speedLearning };
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: '课程设置', exact: true }).click();
  await page.locator('.route-speed').first().scrollIntoViewIfNeeded();
  const cardBox = await page.locator('.route-card').first().boundingBox();
  const heightBox = await page.locator('.route-card .route-elevation').first().boundingBox();
  const speedBox = await page.locator('.route-card .route-speed').first().boundingBox();
  assert.ok(heightBox.width > cardBox.width * .85 && speedBox.width > cardBox.width * .85);
  assert.ok(speedBox.y >= heightBox.y + heightBox.height);
  report.mobileLayout = { cardWidth: cardBox.width, profileWidth: heightBox.width, explanationWidth: speedBox.width, stacked: true };
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.screenshot({ path: '/tmp/dirmap-travel-route-mobile.png' });
  report.screenshots.push('/tmp/dirmap-travel-route-mobile.png');
  await page.setViewportSize({ width: 1440, height: 1000 });
  const baseline = await plan(false);
  assert.equal(baseline.routes[0].speedLearning, undefined);
  assert.notEqual(baseline.graphHash, learned.graphHash);
  assert.equal(baseline.elevationSnapshot.revision, learned.elevationSnapshot.revision);
  report.baseline = { graphHash: baseline.graphHash, distanceM: baseline.routes[0].distanceM, movingSec: baseline.routes[0].travelSec };
  report.errors = errors;
  assert.deepEqual(errors, []);
  await fs.writeFile(output, JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify({ output, learned: report.learned, baseline: report.baseline, errors }));
} finally {
  await context.close();
  await browser.close();
}
