import { test, expect, type Page } from './fixtures';

const origin = [113.9304, 22.535];
function coordinate(x: number, y: number) {
  const phi = origin[1] * Math.PI / 180, e = .00669437999014;
  const n = 6378137 / Math.sqrt(1-e*Math.sin(phi)**2);
  const m = 6378137*(1-e)/(1-e*Math.sin(phi)**2)**1.5;
  return [origin[0]+x/(n*Math.cos(phi)*Math.PI/180), origin[1]+y/(m*Math.PI/180)];
}
async function finish(page: Page, id: string) {
  await expect.poll(async () => (await (await page.request.get('/api/jobs/'+id)).json()).state,
                    { timeout: 60000 }).toBe('succeeded');
  return (await (await page.request.get('/api/jobs/'+id)).json()).result;
}
async function point(page: Page, x: number, y: number) {
  const canvas = page.locator('dialog canvas');
  await canvas.scrollIntoViewIfNeeded();
  const box = (await canvas.boundingBox())!;
  return { x: box.x+x/240*box.width, y: box.y+y/240*box.height };
}

test('teacher draws an explicit upper surface and portal, then publishes layered navigation offline', async ({ page, context }) => {
  test.setTimeout(180000);
  await page.goto('/');
  const png = await page.evaluate(() => {
    const canvas = document.createElement('canvas'); canvas.width = canvas.height = 240;
    const ctx = canvas.getContext('2d')!; ctx.fillStyle = '#fafafa'; ctx.fillRect(0, 0, 240, 240);
    ctx.strokeStyle = '#bd23a5'; ctx.lineWidth = 2;
    for (const [x, y] of [[40, 40], [200, 200]]) { ctx.beginPath(); ctx.arc(x, y, 15, 0, Math.PI*2); ctx.stroke(); }
    return canvas.toDataURL('image/png').split(',')[1];
  });
  const upload = await page.request.post('/api/maps/upload', { multipart: { file: { name: 'layered-test.png', mimeType: 'image/png', buffer: Buffer.from(png, 'base64') } } });
  expect(upload.ok()).toBe(true); const asset = await upload.json();
  const processing = await (await page.request.post(`/api/maps/${asset.id}/process`)).json();
  let state = await finish(page, processing.id);
  const anchors = [[10, 10], [230, 10], [230, 230], [10, 230]].map(pixel => ({ pixel, lnglat: coordinate(pixel[0], 240-pixel[1]), purpose: 'fit' }));
  const fitted = await page.request.post(`/api/maps/${asset.id}/anchors`, { data: { version: state.version, anchors } });
  expect(fitted.ok()).toBe(true); state = await finish(page, (await fitted.json()).id);
  const patched = await page.request.patch(`/api/maps/${asset.id}`, { data: { version: state.version, controls: [
    { id: 'upper-target', label: '31', pixel: [120, 180], radiusPx: 6, required: true, state: 'accepted', role: 'control', serviceBaseSec: 5, servicePerPersonSec: 1 },
  ] } });
  expect(patched.ok()).toBe(true);
  await page.reload(); await page.locator(`[data-map="${asset.id}"]`).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('#mobile-panel').click();
  await page.locator('[data-control-edit="upper-target"]').click();
  await page.locator('.layer-settings summary').click();
  await page.getByLabel('新层级名称').fill('桥上');
  await page.locator('#add-surface').click();
  const surface = await page.getByLabel('当前编辑层级').inputValue();
  await page.getByLabel('笔刷半径').fill('10');
  await page.locator('#surface-cost').fill('1.7');
  await page.getByLabel('编辑操作').selectOption('open');
  const a = await point(page, 120, 40), b = await point(page, 120, 200);
  await page.mouse.move(a.x, a.y); await page.mouse.down(); await page.mouse.move(b.x, b.y, { steps: 12 }); await page.mouse.up();
  await page.locator(`[data-confirm-surface="${surface}"]`).check();
  await page.locator('#portal-target').selectOption('ground');
  await page.locator('#portal-length').fill('3'); await page.locator('#portal-cost').fill('1.7');
  await page.locator('#portal-accepted').check();
  await page.getByLabel('编辑操作').selectOption('portal');
  const entrance = await point(page, 120, 40); await page.mouse.click(entrance.x, entrance.y);
  await page.locator('#portal-label').fill('北侧入口');
  await page.getByLabel('编辑操作').selectOption('inspect');
  const target = await point(page, 120, 180); await page.mouse.click(target.x, target.y);
  await page.locator('#control-surface').selectOption(surface);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: '/tmp/dirmap-layer-editor-phone.png' });
  await page.locator('#save-edits').click(); await expect(page.locator('dialog')).toHaveCount(0);
  state = await (await page.request.get(`/api/maps/${asset.id}`)).json();
  expect(state.surfaces[0]).toMatchObject({ id: surface, accepted: true });
  expect(state.portals[0]).toMatchObject({ accepted: true, label: '北侧入口', fromSurface: surface, toSurface: 'ground' });
  expect(state.controls[0].surfaceId).toBe(surface);
  await page.locator('#start-surface').selectOption('ground');
  await page.locator('#pick-start').click();
  await page.evaluate(position => (window as any).dirmap.campusMap.onClick(position, null), coordinate(60, 180));
  await page.locator('#mobile-panel').click();
  await page.locator('#people').fill('4'); await page.locator('#teams').fill('1');
  const planning = page.waitForResponse(r => r.url().endsWith('/api/plans') && r.request().method() === 'POST');
  await page.locator('#plan-button').click(); const plannedJob = await (await planning).json();
  const plan = await finish(page, plannedJob.id);
  await expect(page.locator('.route-card')).toHaveCount(1);
  expect(plan.targetSurfaces['upper-target']).toBe(surface);
  expect(plan.routes[0].transitions).toHaveLength(2);
  await expect(page.locator('#route-results')).toContainText('路线含换层入口');
  await page.locator('#review-button').click();
  for (const name of ['registration', 'controls', 'passages']) await page.locator('#review-'+name).check();
  if (await page.locator('#review-approaches').count()) await page.locator('#review-approaches').check();
  await page.locator('#publish-confirm').click();
  const link = page.getByLabel('学生课程链接'); await expect(link).toBeVisible({ timeout: 30000 });
  const courseId = new URL(await link.inputValue()).searchParams.get('course');
  await page.goto(`http://127.0.0.1:8768/?course=${courseId}`, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#offline-status')).toContainText('本课已保存', { timeout: 20000 });
  await page.locator('#mobile-panel').click();
  await expect(page.locator('#student-transitions')).toContainText('北侧入口');
  await expect(page.locator('.target-surface')).toContainText('桥上');
  await page.locator('#student-surface').selectOption(surface);
  await page.waitForFunction(() => !!navigator.serviceWorker.controller);
  await context.setOffline(true); await page.reload(); await page.locator('#mobile-panel').click();
  await expect(page.locator('#student-transitions')).toContainText('桥上');
  await expect(page.locator('#student-surface option')).toHaveCount(3);
  await context.setOffline(false);
});
