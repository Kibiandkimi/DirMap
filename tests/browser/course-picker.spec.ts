import { test, expect } from './fixtures';
import { teachingSheet, openSheet, editableControl } from './sheet-fixture';

test('dictionary list, number picking and manual sequence survive reload independently', async ({ page }) => {
  test.setTimeout(120000);
  const { asset, state } = await teachingSheet(page, false, false);
  try {
    const lookup: Record<string, string> = { '31': '2', '32': '10', '33': '1' };
    const controls = state.controls.map((control: any) => ({ ...editableControl(control), label: lookup[control.label] ?? control.label }));
    const patched = await page.request.patch(`/api/maps/${asset.id}`, { data: { version: state.version, controls } });
    expect(patched.ok(), await patched.text()).toBe(true);
    await openSheet(page, asset.id);
    const numbered = page.locator('.control-row:has([data-required]:enabled) .control-number');
    await expect(numbered).toHaveText(['1', '10', '2']);
    await page.locator('#people').fill('4'); await page.locator('#teams').fill('2');
    await page.locator('#course-mode').selectOption('custom');
    const ids = Object.fromEntries(controls.map((c: any) => [c.label, c.id]));
    const first = page.locator('[data-team-index="0"]');
    for (const label of ['2', '10', '1']) await first.locator(`[data-route-add="${ids[label]}"]`).click();
    await first.getByRole('button', { name: '上移目标 1', exact: true }).click();
    await first.locator(`[data-route-add="${ids['10']}"]`).click();
    await first.locator(`[data-route-add="${ids['10']}"]`).click();
    await expect(page.locator('#custom-team-0')).toHaveValue('2 → 1 → 10');
    await expect(first.locator('.route-stop')).toHaveText(['第 1 站', '第 2 站', '第 3 站']);
    await expect(first.locator('.route-target')).toHaveText(['目标 2', '目标 1', '目标 10']);
    const rows = await first.locator('.route-sequence li').evaluateAll(items => items.map(item => ({ x: item.getBoundingClientRect().x, y: item.getBoundingClientRect().y })));
    expect(rows[0].x).toBe(rows[1].x); expect(rows[1].x).toBe(rows[2].x);
    expect(rows[0].y).toBeLessThan(rows[1].y); expect(rows[1].y).toBeLessThan(rows[2].y);
    await first.screenshot({ path: '/tmp/dirmap-explicit-route-order.png' });
    const second = page.locator('[data-team-index="1"]'); await second.locator('summary').click();
    for (const label of ['1', '10']) await second.locator(`[data-route-add="${ids[label]}"]`).click();
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.locator('#custom-team-0')).toHaveValue('2 → 1 → 10');
    await expect(page.locator('#custom-team-1')).toHaveValue('1 → 10');
    await expect(numbered).toHaveText(['1', '10', '2']);
    const submitted = page.waitForRequest(r => new URL(r.url()).pathname === '/api/plans' && r.method() === 'POST');
    await page.locator('#plan-button').click();
    expect((await submitted).postDataJSON().customRoutes.map((r: any) => r.controlIds)).toEqual([[ids['2'], ids['1'], ids['10']], [ids['1'], ids['10']]]);
    await expect(page.locator('.route-card')).toHaveCount(2, { timeout: 30000 });
    await page.locator('#course-mode').selectOption('fixed_order');
    await page.getByRole('button', { name: '上移 2', exact: true }).click();
    await expect(numbered).toHaveText(['1', '2', '10']);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(numbered).toHaveText(['1', '2', '10']);
  } finally { await page.request.delete(`/api/maps/${asset.id}`); }
});
