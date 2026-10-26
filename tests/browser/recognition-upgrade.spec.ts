import { test, expect } from './fixtures';

test('obsolete recognition can be refreshed without losing calibration or teacher edits, then planned', async ({ page }) => {
  test.setTimeout(180000);
  const assetId = 'upload-f4fd82f6605e';
  const before = await (await page.request.get(`/api/maps/${assetId}`)).json();
  const teacherControls = before.controls.filter((c: any) => c.id.startsWith('manual-') || ['accepted', 'rejected'].includes(c.state) || c.labelSource === 'manual' || c.manualFields?.length);
  expect(teacherControls.some((c: any) => c.label === '43')).toBe(true);
  expect(before.edits.length).toBeGreaterThan(0);
  // Keep the upgrade entry reproducible after the saved project fixtures
  // themselves have been upgraded. Processing and routing use the real API;
  // test_api.py separately verifies stale-schema detection on persisted data.
  await page.route(`**/api/maps/${assetId}`, async route => {
    const response = await route.fetch();
    const state = await response.json();
    if (state.version === before.version) {
      state.terrainSchema = 'older-recognition';
      state.terrainStale = true;
    }
    await route.fulfill({ response, json: state });
  });
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.locator(`[data-map="${assetId}"]`).click();
  await expect(page.locator('#analysis-update')).toBeVisible();
  await expect(page.locator('#plan-button')).toBeDisabled();
  const submission = page.waitForResponse(r => r.url().includes(`/api/maps/${assetId}/process?preserveRegistration=true`) && r.request().method() === 'POST');
  await page.locator('#refresh-recognition').click();
  const submitted = await submission;
  expect(submitted.ok()).toBe(true);
  const job = await submitted.json();
  await expect(page.locator('[data-map="szu-1"]')).toBeDisabled();
  await expect(page.locator('#analysis-update')).toBeHidden({ timeout: 120000 });
  await expect(page.locator('#plan-button')).toBeEnabled();
  const finished = await (await page.request.get(`/api/jobs/${job.id}`)).json();
  expect(finished.state).toBe('succeeded');
  const after = await (await page.request.get(`/api/maps/${assetId}`)).json();
  expect(after.version).toBe(before.version + 1);
  expect(after.terrainStale).toBe(false);
  expect(after.registration).toEqual(before.registration);
  expect(after.frame).toEqual(before.frame);
  expect(after.quality).toEqual(before.quality);
  expect(after.edits).toEqual(before.edits);
  expect(after.recognitionExclusions ?? []).toEqual(before.recognitionExclusions ?? []);
  expect(after.controls.filter((c: any) => c.role === 'control').map((c: any) => c.label).sort()).toEqual(Array.from({ length: 17 }, (_, i) => String(i + 31)));
  for (const old of teacherControls) {
    const kept = after.controls.find((c: any) => c.id === old.id);
    for (const key of ['pixel', 'label', 'state', 'labelSource', 'manualFields', 'required', 'approachPixel']) expect(kept[key]).toEqual(old[key]);
  }
  await page.locator('#teams').fill('1');
  await page.locator('#course-mode').selectOption('all_visit');
  await page.locator('#plan-button').click();
  await expect(page.locator('.route-card')).toHaveCount(1, { timeout: 30000 });
  await expect(page.locator('#plan-errors')).toBeEmpty();
});
