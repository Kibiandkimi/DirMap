import { test, expect } from './fixtures';

test('a slower earlier map response cannot replace the map chosen afterwards', async ({ page }) => {
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let holding = true;
  await page.route('**/api/maps/szu-1', async route => {
    if (holding) { holding = false; await held; }
    await route.continue();
  });
  const chosen = await (await page.request.get('/api/maps/szu-3')).json();
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.locator('[data-map="szu-3"]').click();
  await expect(page.locator('#mixed-rule-summary')).toContainText('31、32、33');
  const stale = page.waitForResponse(r => r.url().endsWith('/api/maps/szu-1'));
  release(); await stale;
  await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 300)));
  await expect(page.locator('#map-title')).toContainText('3');
  await expect(page.locator('.control-row')).toHaveCount(chosen.controls.length);
  // The submitted course must use this map's own controls. Map IDs repeat
  // control IDs, so a mixed request could otherwise still be accepted.
  let body: any = null;
  await page.route('**/api/plans', async route => {
    body = route.request().postDataJSON();
    await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: '测试只检查提交内容' }) });
  });
  await page.locator('#plan-button').click();
  await expect.poll(() => body).not.toBeNull();
  const labels = new Map(chosen.controls.map((c: any) => [c.id, c.label]));
  expect(body.assetId).toBe('szu-3');
  expect(body.version).toBe(chosen.version);
  expect(body.controlIds.map((id: string) => labels.get(id)).sort()).toEqual(
    chosen.controls.filter((c: any) => (c.role ?? 'control') === 'control').map((c: any) => c.label).sort());
  expect(body.mandatoryControlIds.map((id: string) => labels.get(id)).sort()).toEqual(['31', '32', '33']);
});
