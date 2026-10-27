import { test, expect } from './fixtures';
import fs from 'node:fs';

test('original photo collage imports, finds every control and opens in its working pixel frame', async ({ page }) => {
  test.setTimeout(180000);
  const fixture = JSON.parse(fs.readFileSync('evidence/new-map-fixture.json', 'utf8'));
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await expect(page.locator('.control-row').first()).toBeVisible();
  // The upload names its place in the query string (here the default campus).
  const imported = page.waitForResponse(response => new URL(response.url()).pathname === '/api/maps/upload'
    && response.request().method() === 'POST');
  const source = process.env.DIRMAP_RECOGNITION_SOURCE || fixture.sourceFile;
  // Workbench originals use source.img internally; upload them under their
  // actual image format so the browser exercises the JPEG import contract.
  await page.locator('#upload-input').setInputFiles({ name: 'photo-collage.jpg', mimeType: 'image/jpeg', buffer: fs.readFileSync(source) });
  const asset = await (await imported).json();
  expect(asset.sha256).toBe(fixture.sourceHash);
  await expect(page.locator('#process-current')).toBeEnabled();
  await expect(page.locator('#map-title')).toContainText(asset.name);
  await expect(page.locator('#map-subtitle')).toHaveText('原图待处理');
  const submitted = page.waitForResponse(response => response.url().endsWith(`/api/maps/${asset.id}/process`));
  await page.locator('#process-current').click();
  const job = await (await submitted).json();
  await expect.poll(async () => (await (await page.request.get('/api/jobs/' + job.id)).json()).state,
    { timeout: 120000, intervals: [500, 1000] }).toBe('succeeded');
  await expect(page.locator('.control-row')).toHaveCount(18, { timeout: 10000 });
  const state = await (await page.request.get('/api/maps/' + asset.id)).json();
  const controls = state.controls.filter((control: any) => control.role === 'control');
  expect(controls.map((control: any) => control.label).sort()).toEqual(fixture.labels);
  expect(state.controls.filter((control: any) => control.role === 'start_finish')).toHaveLength(1);
  expect(state.controls.filter((control: any) => control.role === 'start')).toHaveLength(0);
  expect(state.controls.some((control: any) => control.id.startsWith('manual-'))).toBe(false);
  expect(state.controls.every((control: any) => control.calibrated)).toBe(true);
  expect(state.sourceImage.encodedSize).toEqual(fixture.originalSize);
  expect(state.sourceImage.workingSize).toEqual([state.width, state.height]);
  expect(Math.max(state.width, state.height)).toBe(4096);
  expect(state.sourceImage.resized).toBe(true);
  const preview = page.waitForResponse(response => response.url().includes(`/assets/maps/${asset.id}/working.jpg`));
  await page.locator('#edit-controls').click();
  expect((await preview).status()).toBe(200);
  await expect(page.locator('dialog canvas')).toHaveAttribute('width', String(state.width));
  await expect(page.locator('dialog canvas')).toHaveAttribute('height', String(state.height));
  await expect(page.locator(`dialog a[href="/assets/maps/${asset.id}/original"]`)).toBeVisible();
  const dimensions = await page.evaluate((url: string) => new Promise<number[]>((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve([image.naturalWidth, image.naturalHeight]);
    image.onerror = () => reject(new Error('Working map preview failed to load'));
    image.src = url;
  }), `/assets/maps/${asset.id}/working.jpg?v=${state.version}`);
  expect(dimensions).toEqual(state.sourceImage.workingSize);
  expect(errors).toEqual([]);
  fs.writeFileSync('evidence/runtime/new-map-processing.json', JSON.stringify({
    auditedAt: new Date().toISOString(), sourceHash: asset.sha256, freshUpload: true,
    reusedTeacherControls: false, sourceImage: state.sourceImage,
    labels: controls.map((control: any) => control.label).sort(),
    calibratedControls: controls.filter((control: any) => control.calibrated).length,
    startFinishCount: 1, falsePhotoStarts: 0, workingPreviewSize: dimensions,
    registrationMethod: state.quality.method,
    baseAssetId: state.quality.baseAssetId ?? null,
    recoveredBoundaryObjects: state.quality.recoveredBoundaryObjectIds ?? [],
    processingElapsedSec: state.processingElapsedSec, allChecksPassed: true,
    independentFieldValidation: false,
    scope: 'Fresh original-file upload through the browser, processing job, geographic projection and pixel editor; development regression only.',
  }, null, 2) + '\n');
});
