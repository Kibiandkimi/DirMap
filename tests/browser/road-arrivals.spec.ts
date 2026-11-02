import { test, expect } from './fixtures';
import { teachingSheet, openSheet, finished } from './sheet-fixture';

test('a circle in private green automatically gets a road arrival and publishes without manual point picking', async ({ page }) => {
  test.setTimeout(150000);
  await page.setViewportSize({ width: 390, height: 844 });
  const { asset, state } = await teachingSheet(page, false, false, true);
  const target = state.controls.find((c: any) => c.label === '31');
  try {
    await openSheet(page, asset.id, true);
    await page.locator('#teams').fill('1');
    const posted = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/plans');
    await page.locator('#plan-button').click();
    const plan = await finished(page, await (await posted).json());
    expect(plan.unresolved ?? []).toEqual([]);
    expect(plan.routes[0].controlIds).toContain(target.id);
    const arrival = plan.approaches.find((a: any) => a.controlId === target.id);
    expect(arrival.automatic).toBe(true); expect(arrival.method).toBe('nearby_road');
    expect(arrival.offsetM).toBeLessThan(8);
    expect(arrival.targetPixel).toEqual(target.pixel);
    // The diagonal path meets the vertical path beside this fill; either
    // is valid, as long as the arrival remains outside its x=175 boundary.
    expect(arrival.pixel[0]).toBeLessThan(175);
    await expect(page.locator('.route-card')).toHaveCount(1);
    await page.screenshot({ path: '/tmp/dirmap-road-arrival-mobile.png' });
    await page.locator('#review-button').click();
    for (const name of ['registration', 'controls', 'passages', 'approaches']) await page.locator('#review-' + name).check();
    await page.locator('#publish-confirm').click();
    const link = page.getByLabel('学生课程链接'); await expect(link).toBeVisible({ timeout: 30000 });
    const id = new URL(await link.inputValue()).searchParams.get('course');
    const course = await (await page.request.get('/api/releases/' + id)).json();
    expect(course.plan.approaches.find((a: any) => a.controlId === target.id).pixel).toEqual(arrival.pixel);
    expect(course.controls.find((c: any) => c.id === target.id).pixel).toEqual(target.pixel);
  } finally {
    const deleted = await page.request.delete('/api/maps/' + asset.id);
    await finished(page, (await deleted.json()).job);
  }
});
