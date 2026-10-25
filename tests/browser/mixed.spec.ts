import { test, expect, type Page } from '@playwright/test';
import fs from 'node:fs';

async function openMixed(page: Page) {
  await page.goto('/');
  await page.locator('[data-map="szu-3"]').click();
  await expect(page.locator('#course-mode')).toHaveValue('mixed_coverage');
  await expect(page.locator('#mixed-rule-summary')).toContainText('31、32、33');
}

async function generate(page: Page) {
  const response = page.waitForResponse(r => r.url().endsWith('/api/plans') && r.request().method() === 'POST');
  await page.locator('#plan-button').click();
  const job = await (await response).json();
  await expect(page.locator('.route-card').first()).toBeVisible({ timeout: 30000 });
  const completed = await (await page.request.get('/api/jobs/'+job.id)).json();
  expect(completed.state).toBe('succeeded');
  return completed.result;
}

function checkCoverage(plan: any, controls: any[], order: string[], ordered: boolean, teams: number) {
  const labels = new Map(controls.map(c => [c.id, c.label]));
  const counts = new Map<string, number>();
  expect(plan.routes).toHaveLength(teams);
  expect(plan.request.mode).toBe('mixed_coverage');
  expect(plan.coverage.mandatoryOrder).toBe(ordered);
  expect(plan.coverage.mandatoryControlIds.map((id: string) => labels.get(id))).toEqual(order);
  for (const route of plan.routes) {
    expect(new Set(route.controlIds).size).toBe(route.controlIds.length);
    const compulsory = route.controlIds.map((id: string) => labels.get(id)).filter((label: string) => order.includes(label));
    expect(ordered ? compulsory : [...compulsory].sort()).toEqual(ordered ? order : [...order].sort());
    for (const id of route.controlIds) {
      const label = labels.get(id)!;
      counts.set(label, (counts.get(label) ?? 0)+1);
    }
    expect(route.geometry.length).toBeGreaterThan(10);
  }
  expect(Object.fromEntries([...counts].sort())).toEqual(Object.fromEntries(
    Array.from({ length: 16 }, (_, i) => [String(31+i), order.includes(String(31+i)) ? teams : 1])));
}

test('map 3 mixed rules plan all actual controls and retain a configurable compulsory order', async ({ page }) => {
  test.setTimeout(90000);
  await openMixed(page);
  for (const label of ['31', '32', '33']) await expect(page.getByLabel(label+' 每队必到', { exact: true })).toBeChecked();
  await expect(page.locator('#mandatory-order')).not.toBeChecked();
  const state = await (await page.request.get('/api/maps/szu-3')).json();
  await page.locator('#people').fill('20'); await page.locator('#teams').fill('5');
  checkCoverage(await generate(page), state.controls, ['31', '32', '33'], false, 5);
  await expect(page.locator('#mixed-coverage-result')).toContainText('其余 13 个点均已分配');
  await page.locator('#mandatory-order').check();
  await expect(page.locator('.route-card')).toHaveCount(0);
  await page.getByRole('button', { name: '上移必到点 33', exact: true }).click();
  await page.getByRole('button', { name: '上移必到点 33', exact: true }).click();
  await page.locator('[data-map="szu-2"]').click();
  await expect(page.locator('#course-mode')).toHaveValue('all_visit');
  await page.locator('[data-map="szu-3"]').click();
  await expect(page.locator('#mandatory-order')).toBeChecked();
  await expect(page.locator('#mandatory-order-list .mandatory-order-row span')).toHaveText(['1. 33', '2. 31', '3. 32']);
  await page.reload(); await page.locator('[data-map="szu-3"]').click();
  await expect(page.locator('#mandatory-order')).toBeChecked();
  await expect(page.locator('#mandatory-order-list .mandatory-order-row span')).toHaveText(['1. 33', '2. 31', '3. 32']);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.locator('#mobile-panel').click();
  await page.locator('#people').fill('20'); await page.locator('#teams').fill('5');
  checkCoverage(await generate(page), state.controls, ['33', '31', '32'], true, 5);
});

test('mixed rules survive publication and offline reload while obsolete routing data is refused offline', async ({ page, context }) => {
  test.setTimeout(120000);
  await openMixed(page);
  await page.locator('#people').fill('12'); await page.locator('#teams').fill('3');
  await page.locator('#mandatory-order').check();
  await page.getByRole('button', { name: '上移必到点 33', exact: true }).click();
  await generate(page);
  await page.locator('#review-button').click();
  for (const name of ['registration', 'controls', 'passages']) await page.locator('#review-'+name).check();
  if (await page.locator('#review-approaches').count()) await page.locator('#review-approaches').check();
  await page.locator('#publish-confirm').click();
  const link = page.getByLabel('学生课程链接');
  await expect(link).toBeVisible({ timeout: 30000 });
  const id = new URL(await link.inputValue()).searchParams.get('course');
  const course = await (await page.request.get('/api/releases/'+id)).json();
  const schema = JSON.parse(fs.readFileSync('dirmap/schemas.json', 'utf8')).routing;
  expect(course.routingSchema).toBe(schema);
  expect(course.plan.draft).toBe(false);
  checkCoverage(course.plan, course.controls, ['31', '33', '32'], true, 3);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`http://127.0.0.1:8768/?course=${id}`);
  await expect(page.locator('#offline-status')).toContainText('本课已保存', { timeout: 20000 });
  await page.waitForFunction(() => !!navigator.serviceWorker.controller);
  await expect(page.locator('#student-coverage')).toContainText('全员必到：31、33、32');
  await expect(page.locator('#student-coverage')).toContainText('按教师指定顺序');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const teamIds = course.plan.routes.map((r: any) => r.teamId);
  await page.locator('#mobile-panel').click();
  for (const team of teamIds) {
    await page.locator('#student-team').selectOption(team);
    await expect(page.locator('.student-stop').filter({ hasText: '全员必到' })).toHaveCount(3);
  }
  await page.locator('#student-team').selectOption(teamIds[0]);
  await page.locator('#check-in').click();
  await context.setOffline(true); await page.reload();
  await expect(page.locator('#student-coverage')).toContainText('全员必到：31、33、32');
  await expect(page.locator('#student-progress')).toHaveText(`1 / ${course.plan.routes[0].controlIds.length}`);
  const shellNames = await page.evaluate(async () => (await caches.keys()).filter(name => name.startsWith('dirmap-shell-')));
  expect(shellNames).toHaveLength(1);
  expect(shellNames[0]).toMatch(/^dirmap-shell-[0-9a-f]{16}$/);
  // Simulate a previously saved pre-fix course. The upgraded app must reject
  // it without needing an online server to mark it superseded first.
  await page.evaluate(async (courseId) => {
    const cache = await caches.open('dirmap-courses-v1'), url = '/api/releases/'+courseId;
    const response = (await cache.match(url))!, payload = await response.json();
    payload.routingSchema = 'native-quadtree-v2-bounded-approaches-v2'; payload.superseded = false;
    await cache.put(url, new Response(JSON.stringify(payload), { headers: response.headers }));
  }, id);
  await page.reload();
  await expect(page.locator('#release-badge')).toHaveText('课程已更新');
  await expect(page.locator('#check-in')).toBeDisabled();
  await expect(page.locator('#undo-check-in')).toBeDisabled();
  await expect(page.locator('.student-stop')).toHaveCount(0);
  await context.setOffline(false);
});
