import { test, expect } from './fixtures';

test('ambiguous places can be compared, chosen, restored and cleared when switching maps', async ({ page }) => {
  const original = await (await page.request.get('/api/maps/szu-1')).json();
  const overlay = await (await page.request.get(`/assets/maps/szu-1/overlay.png?v=${original.version}`)).body();
  const candidates = [0, 1].map(index => ({
    id: `place-${index + 1}`, preview: `place-${index + 1}.png`, support: .35 - index * .02,
    extent: original.extent.map((v: number, axis: number) => v + (axis % 2 === 0 ? index * 300 : 0)),
    p95ErrorM: 4, method: 'global_search_layer_correlation', thinValidation: false,
    registration: original.registration, quality: original.quality,
  }));
  // The API/job transaction is checked with real state in test_offered_places.
  // Here controlled responses exercise the browser's refusal and preview UI.
  let current = { ...original, registration: null, quality: null, extent: undefined, reviewed: false,
    controls: original.controls.map((c: object) => ({ ...c, lnglat: null, calibrated: false })),
    registrationCandidates: candidates, failures: ['存在吻合度相近的不同位置，请补充同名点确定位置'] };
  let submitted: unknown = null;
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/maps/szu-1', route => route.fulfill({ json: current }));
  await page.route(/\/assets\/maps\/szu-1\/(?:place-[12]|overlay)\.png(?:\?.*)?$/, route =>
    route.fulfill({ contentType: 'image/png', body: overlay }));
  await page.route('**/api/maps/szu-1/places/place-2', route => {
    submitted = route.request().postDataJSON();
    current = { ...current, version: current.version + 1, registration: original.registration,
      quality: { ...original.quality, state: 'needs_review', chosenPlace: { id: 'place-2', support: .33, offered: 2 } },
      extent: candidates[1].extent, controls: original.controls };
    return route.fulfill({ json: { id: 'place-browser-choice', kind: 'place', assetId: 'szu-1', state: 'succeeded',
      progress: 100, result: current } });
  });
  const previewLayers = () => page.evaluate(() => (window as any).dirmap.campusMap.map.getLayers().getArray()
    .filter((layer: any) => /\/place-[12]\.png/.test(layer.getSource()?.getUrl?.() ?? ''))
    .map((layer: any) => ({ opacity: layer.getOpacity(), extent: layer.getSource().getImageExtent(), visible: layer.getVisible() })));
  await page.goto('/');
  await page.locator('[data-tab="registration"]').click();
  await expect(page.locator('.place-choice')).toHaveCount(2);
  await expect(page.locator('#registration-quality')).toContainText('不能自行确定');
  await expect(page.locator('#plan-button')).toBeDisabled();
  await page.locator('[data-place-preview="place-1"]').click();
  await expect(page.locator('[data-place-preview="place-1"]')).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(async () => (await previewLayers())[0]?.extent).toEqual(candidates[0].extent);
  await page.locator('#overlay-opacity').evaluate((element: HTMLInputElement) => {
    element.value = '25'; element.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await expect.poll(async () => (await previewLayers())[0]?.opacity).toBe(.25);
  await page.locator('[data-place-preview="place-1"]').click();
  await expect.poll(previewLayers).toEqual([]);
  await page.locator('[data-place-preview="place-2"]').click();
  await expect.poll(async () => (await previewLayers())[0]?.extent).toEqual(candidates[1].extent);
  await page.locator('[data-place-adopt="place-2"]').click();
  await expect(page.locator('[data-place-adopt="place-2"]')).toHaveText('已采用');
  expect(submitted).toEqual({ version: original.version });
  await expect.poll(previewLayers).toEqual([]);
  await page.reload();
  await page.locator('[data-tab="registration"]').click();
  await expect(page.locator('[data-place-adopt="place-2"]')).toBeDisabled();
  await page.locator('[data-place-preview="place-1"]').click();
  const visible = await page.evaluate(() => (window as any).dirmap.campusMap.map.getLayers().getArray()
    .filter((layer: any) => [10, 40].includes(layer.getZIndex())).map((layer: any) => layer.getVisible()));
  expect(visible.length).toBeGreaterThan(0);
  expect(visible.every((shown: boolean) => !shown)).toBe(true);
  await page.setViewportSize({ width: 390, height: 844 });
  if (await page.locator('#teacher-panel').isHidden()) await page.locator('#mobile-panel').click();
  await expect(page.locator('.place-choice').first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath('registration-candidates-mobile.png'), fullPage: true });
  await page.locator('[data-map="szu-3"]').click();
  await expect(page.locator('#map-title')).toContainText('3');
  await expect.poll(previewLayers).toEqual([]);
  expect(errors).toEqual([]);
});
