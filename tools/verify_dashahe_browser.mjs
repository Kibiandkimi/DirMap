#!/usr/bin/env node
/** Inspect the real sheet and its test course on a server using the isolated
 * --workdir produced by verify_dashahe_real.py. Never publishes a live class.
 */
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { parseArgs } from 'node:util';
import { chromium, expect } from '@playwright/test';

const { values } = parseArgs({ options: {
  'base-url': { type: 'string', default: 'http://127.0.0.1:8769' },
  report: { type: 'string' }, output: { type: 'string', default: '/tmp/dirmap-dashahe-browser/report.json' },
} });
if (!values.report) throw new Error('Pass --report from verify_dashahe_real.py');
const source = JSON.parse(await readFile(values.report, 'utf8'));
if (!source.allChecksPassed || !source.courseWorkflow?.releaseId) throw new Error('The real-sheet workflow must pass first');
const base = values['base-url'].replace(/\/$/, ''), output = resolve(values.output), folder = dirname(output);
await mkdir(folder, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.DIRMAP_BROWSER_PATH || '/usr/bin/google-chrome' });
const context = await browser.newContext({ viewport: { width: 1440, height: 980 } });
await context.route(url => url.origin !== new URL(base).origin, route => route.abort('internetdisconnected'));
const page = await context.newPage(), errors = [], screenshots = [];
page.on('pageerror', error => errors.push(error.message));
async function screenshot(name) {
  const path = join(folder, name+'.png');
  await page.screenshot({ path }); screenshots.push(path);
}
try {
  const overlayReady = page.waitForResponse(response => new URL(response.url()).pathname ===
    `/assets/maps/${source.assetId}/overlay.png` && response.status() === 200);
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await page.locator('#site-select').selectOption('dashahe-park');
  await page.locator(`[data-map="${source.assetId}"]`).click();
  const numbers = page.locator('.control-row:has([data-required]:enabled) .control-number');
  const expected = Array.from({ length: 32 }, (_, i) => String(31+i));
  await expect(numbers).toHaveText(expected);
  await expect(page.locator('.control-row [data-reference-photo]')).toHaveCount(32);
  const response = await page.request.get(`${base}/api/maps/${source.assetId}`);
  expect(response.ok()).toBe(true);
  const state = await response.json();
  expect(state.overlayScope).toBe('full-sheet');
  expect(state.controls.every(c => c.calibrated)).toBe(true);
  await page.locator('[data-tab="registration"]').click();
  await expect(page.locator('#registration-quality')).toContainText('道路网络对齐');
  const overlay = await overlayReady;
  await overlay.finished(); expect((await overlay.body()).byteLength).toBeGreaterThan(1024);
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await screenshot('teacher-registration');
  await page.locator('[data-tab="terrain"]').click();
  await page.locator('#contour-review').click();
  await expect(page.locator('.contour-row')).toHaveCount(state.contours.length);
  await page.locator('[data-focus-line]').first().click();
  await screenshot('printed-contours');
  await page.getByRole('button', { name: '关闭等高线', exact: true }).click();

  await page.locator('[data-tab="course"]').click();
  const referenceControl = state.controls.find(control => control.label === '33');
  await page.locator(`.control-row [data-control-edit="${referenceControl.id}"]`).click();
  const referenceCard = page.getByRole('button', { name: '查看目标 33 的参考照片', exact: true });
  await expect(referenceCard).toBeInViewport();
  await expect.poll(() => page.locator('.editor-reference img').evaluate(image => image.naturalWidth)).toBeGreaterThan(200);
  await screenshot('teacher-reference-beside-map');
  const smallScreens = [];
  for (const viewport of [{ width: 375, height: 667 }, { width: 360, height: 640 }]) {
    await page.setViewportSize(viewport);
    const layout = await page.locator('.editor-dialog').evaluate(dialog => ({
      contentHeight: dialog.scrollHeight, visibleHeight: dialog.clientHeight,
      overflow: getComputedStyle(dialog).overflowY,
    }));
    expect(layout.contentHeight <= layout.visibleHeight+1 || ['auto', 'scroll'].includes(layout.overflow)).toBe(true);
    const bounds = await page.locator('.editor-dialog').boundingBox();
    await page.mouse.move(bounds.x+bounds.width-7, bounds.y+bounds.height/2);
    await page.mouse.wheel(0, 700);
    await expect(page.locator('#save-edits')).toBeInViewport();
    smallScreens.push({ ...viewport, ...layout, saveReachableByScrolling: true });
    await screenshot(`teacher-reference-${viewport.width}x${viewport.height}`);
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('#edit-mode').selectOption('approach');
  const canvas = await page.locator('.editor-dialog canvas').boundingBox();
  const point = referenceControl.approachPixel ?? referenceControl.pixel;
  await page.mouse.click(canvas.x+point[0]/state.width*canvas.width, canvas.y+point[1]/state.height*canvas.height);
  await expect(page.locator('#point-confirmation')).toBeVisible();
  const pending = await page.locator('#point-preview').innerText();
  await expect(referenceCard).toBeInViewport();
  await referenceCard.click();
  await expect.poll(() => page.locator('.sheet-photo-view img').evaluate(image => image.naturalWidth)).toBeGreaterThan(200);
  await page.getByRole('button', { name: '关闭参考照片', exact: true }).click();
  await expect(page.locator('#point-preview')).toHaveText(pending);
  await expect(page.locator('#confirm-point')).toBeEnabled();
  await screenshot('teacher-reference-during-point-preview');
  await page.setViewportSize({ width: 844, height: 390 });
  const landscape = await page.locator('.editor-dialog').evaluate(dialog => ({
    contentHeight: dialog.scrollHeight, visibleHeight: dialog.clientHeight,
    overflow: getComputedStyle(dialog).overflowY,
  }));
  expect(landscape.contentHeight <= landscape.visibleHeight+1 || ['auto', 'scroll'].includes(landscape.overflow)).toBe(true);
  const dialog = await page.locator('.editor-dialog').boundingBox();
  await page.mouse.move(dialog.x+dialog.width-7, dialog.y+dialog.height/2);
  await page.mouse.wheel(0, 700);
  await expect(page.locator('#confirm-point')).toBeInViewport();
  await screenshot('teacher-reference-landscape');
  await page.getByRole('button', { name: '关闭原图', exact: true }).click();

  const id = source.courseWorkflow.releaseId;
  const courseResponse = await page.request.get(`${base}/api/releases/${id}`);
  expect(courseResponse.ok()).toBe(true);
  const course = await courseResponse.json();
  expect(course.superseded).toBe(false);
  const target52 = course.controls.find(control => control.label === '52');
  const arrival52 = course.plan.approaches.find(approach => approach.controlId === target52.id);
  if (source.navigation.coveredPassage52) {
    expect(arrival52.method).toBe('exact');
    expect(arrival52.pixel).toEqual(target52.pixel);
    expect(arrival52.automatic).toBe(false);
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${base}/?course=${id}`, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#offline-status')).toContainText('本课已保存', { timeout: 180000 });
  await page.waitForFunction(() => !!navigator.serviceWorker.controller);
  await page.locator('#mobile-panel').click();
  const targetTeam = course.plan.routes.find(route => route.controlIds.includes(target52.id));
  await page.locator('#student-team').selectOption(targetTeam.teamId);
  await page.locator(`#student-stops [data-reference-photo="${target52.id}"]`).click();
  await expect.poll(() => page.locator('.sheet-photo-view img').evaluate(image => image.naturalWidth)).toBeGreaterThan(200);
  await screenshot('student-reference-52');
  await page.getByRole('button', { name: '关闭参考照片', exact: true }).click();
  await page.locator('#student-reference-album').click();
  await expect(page.locator('.sheet-photo-card')).toHaveCount(32);
  await screenshot('student-reference-album');
  await page.locator('.sheet-photo-card').filter({ hasText: '目标 62' }).click();
  await expect.poll(() => page.locator('.sheet-photo-view img').evaluate(image => image.naturalWidth)).toBeGreaterThan(200);
  await screenshot('student-reference-62');
  await page.getByRole('button', { name: '关闭参考照片', exact: true }).click();

  await context.setOffline(true);
  await page.reload({ waitUntil: 'domcontentloaded' });
  const photosLoadedOffline = await page.evaluate(async id => {
    const course = await (await fetch(`/api/releases/${id}`)).json();
    const results = [];
    for (const photo of course.sheetPhotos) {
      const response = await fetch(`/api/releases/${id}/sheet-photos/${photo.id}.jpg`);
      if (!response.ok) throw new Error(`Offline photo ${photo.id}: ${response.status}`);
      const bitmap = await createImageBitmap(await response.blob());
      results.push({ label: photo.label, width: bitmap.width, height: bitmap.height }); bitmap.close();
    }
    return results;
  }, id);
  expect(photosLoadedOffline.map(p => p.label).sort()).toEqual(expected);
  expect(photosLoadedOffline.every(p => p.width > 200 && p.height > 200)).toBe(true);
  await page.locator('#mobile-panel').click();
  await page.locator('#student-reference-album').click();
  await page.locator('.sheet-photo-card').filter({ hasText: '目标 31' }).click();
  await expect.poll(() => page.locator('.sheet-photo-view img').evaluate(image => image.naturalWidth)).toBeGreaterThan(200);
  await screenshot('student-reference-offline');
  expect(errors).toEqual([]);
  const report = {
    checkedAt: new Date().toISOString(), sourceSha256: source.sourceSha256,
    implementationSignature: source.implementationSignature,
    scope: '真实大沙河图的桌面核对、手机尺寸参考照片、32 张照片离线重载；浏览器模拟不代表真机或现场定位验收。',
    teacherControlLabels: expected, contourCandidates: state.contours.length,
    editorReference: { label: '33', visibleDuringPointPreview: true, photoViewerPreservedPendingPoint: true, smallScreens, landscape },
    target52Method: arrival52.method, target52Pixel: arrival52.pixel,
    photosLoadedOffline, screenshots, allChecksPassed: true,
  };
  await writeFile(output, JSON.stringify(report, null, 2)+'\n');
  console.log(JSON.stringify({ allChecksPassed: true, offlinePhotos: photosLoadedOffline.length, output, screenshots }, null, 2));
} catch (error) {
  await screenshot('failure'); throw error;
} finally {
  await context.setOffline(false); await context.close(); await browser.close();
}
