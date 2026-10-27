import fs from 'node:fs';
import { test, expect, localOnly, type Page } from './fixtures';

async function publishMixedCourse(page: Page) {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.locator('[data-map="szu-3"]').click();
  await expect(page.locator('#mixed-rule-summary')).toContainText('31、32、33');
  const state = await (await page.request.get('/api/maps/szu-3')).json();
  const kept = new Set(['31', '32', '33', '34', '35']);
  for (const control of state.controls.filter((c: any) => (c.role ?? 'control') === 'control')) {
    await page.locator(`[data-required="${control.id}"]`).setChecked(kept.has(control.label));
  }
  await page.locator('#people').fill('4'); await page.locator('#teams').fill('2'); await page.locator('#stagger').fill('600');
  await page.locator('#plan-button').click();
  await expect(page.locator('.route-card')).toHaveCount(2, { timeout: 30000 });
  await page.locator('#review-button').click();
  for (const name of ['registration', 'controls', 'passages']) await page.locator('#review-'+name).check();
  if (await page.locator('#review-approaches').count()) await page.locator('#review-approaches').check();
  await page.locator('#publish-confirm').click();
  const link = page.getByLabel('学生课程链接');
  await expect(link).toBeVisible({ timeout: 30000 });
  await expect(page.getByRole('link', { name: '查看全班进度 ↗' })).toHaveAttribute('href', /#progress$/);
  await page.getByRole('button', { name: '课程二维码' }).click();
  const qr = page.locator('dialog img.course-qr');
  await expect(qr).toHaveAttribute('src', /qr\.png\?origin=http%3A%2F%2F127\.0\.0\.1%3A5174$/);
  await expect.poll(() => qr.evaluate(image => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(200);
  await page.locator('dialog [data-close]').click();
  return new URL(await link.inputValue()).searchParams.get('course')!;
}

async function reachFirstMeeting(phone: Page, teamId: string, route: any, meetings: string[]) {
  await phone.locator('#student-team').selectOption(teamId);
  const steps = route.controlIds.findIndex((id: string) => meetings.includes(id))+1;
  for (let i = 0; i < steps; i++) await phone.locator('#check-in').click();
  await expect(phone.locator('#meeting-departure')).toBeVisible();
  return route.controlIds[steps-1];
}

test('phones share check-ins so a class meeting shows who has arrived, and offline check-ins catch up', async ({ page, browser }) => {
  test.setTimeout(150000);
  const courseId = await publishMixedCourse(page);
  // Staggered teams count their departure from the teacher's start.
  await page.locator('#release-history summary').click();
  await page.locator(`[data-release="${courseId}"] [data-session="start"]`).click();
  await expect(page.locator(`[data-release="${courseId}"]`)).toContainText('开课');
  const course = await (await page.request.get(`/api/releases/${courseId}`)).json();
  const [first, second] = course.plan.routes, meetings = course.plan.coverage.mandatoryControlIds;
  const phones = await Promise.all([0, 1].map(async () => {
    const context = await localOnly(await browser.newContext({ viewport: { width: 1280, height: 900 } }));
    const phone = await context.newPage();
    await phone.goto(`http://127.0.0.1:8768/?course=${courseId}`, { waitUntil: 'domcontentloaded' });
    await expect(phone.locator('#student-team option')).toHaveCount(2);
    return { context, phone };
  }));
  const [a, b] = phones;
  await a.phone.locator('#student-team').selectOption(first.teamId);
  await b.phone.locator('#student-team').selectOption(second.teamId);
  await expect(a.phone.locator('#class-clock')).toContainText('本队已可出发');
  await expect(b.phone.locator('#class-clock')).toHaveText(/^开课 0:\d\d · 本队还有 9:\d\d 出发$/);
  await expect(a.phone.locator('#student-overview')).toContainText('开课即出发');
  await expect(b.phone.locator('#student-overview')).toContainText('开课后 10 分 0 秒出发');
  await b.phone.locator('#student-panel').screenshot({ path: '/tmp/dirmap-class-clock.png' });
  const meeting = await reachFirstMeeting(a.phone, first.teamId, first, meetings);
  await expect(a.phone.locator('#meeting-arrivals')).toContainText(`已到 1 / 2 队 · 未到：${second.teamId}`);
  expect(await reachFirstMeeting(b.phone, second.teamId, second, meetings)).toBe(meeting);
  await expect(b.phone.locator('#meeting-arrivals')).toContainText('各队均已上报到达');
  await a.phone.evaluate(() => dispatchEvent(new Event('online')));
  await expect(a.phone.locator('#meeting-arrivals')).toContainText('各队均已上报到达');
  await a.phone.locator('#class-board summary').click();
  const reached = first.controlIds.indexOf(meeting)+1;
  await expect(a.phone.locator(`[data-team-progress="${first.teamId}"]`)).toContainText(`${reached} / ${first.controlIds.length}`);
  await expect(a.phone.locator(`[data-team-progress="${second.teamId}"]`)).toContainText(`${second.controlIds.indexOf(meeting)+1} / ${second.controlIds.length}`);
  await a.phone.locator('#student-panel').screenshot({ path: '/tmp/dirmap-class-progress.png' });
  const download = a.phone.waitForEvent('download');
  await a.phone.locator('#export-results').click();
  const csv = fs.readFileSync(await (await download).path(), 'utf8');
  const rows = csv.replace(/^\uFEFF/, '').trim().split('\r\n');
  expect(rows[0]).toBe('队伍,人数,顺序,目标,类型,状态,打点时间（手机记录）,距开课（分:秒）');
  expect(rows).toHaveLength(3+first.controlIds.length+second.controlIds.length);
  expect(rows.filter(row => row.includes(',已完成,'))).toHaveLength(reached+second.controlIds.indexOf(meeting)+1);
  await expect(a.phone.locator('.student-stop.done > small').first()).toHaveText(/^已完成 \d{1,2}:\d{2}/);
  // A check-in made while the previous report is still in flight follows
  // right after it, well before the next 15-second course check.
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await b.context.route('**/progress', async route => {
    if (route.request().method() === 'POST') await gate;
    await route.continue();
  });
  const teamB = async () => (await (await page.request.get(`/api/releases/${courseId}/progress`)).json())
    .teams.find((t: any) => t.teamId === second.teamId);
  const atMeeting = second.controlIds.indexOf(meeting)+1;
  await b.phone.locator('#meeting-departure').click();
  await b.phone.locator('#check-in').click();
  release();
  await expect.poll(async () => (await teamB()).completed.length, { timeout: 5000 }).toBe(atMeeting+1);
  expect((await teamB()).departed).toEqual([meeting]);
  // The next check-in happens without a connection and is shared later.
  await a.context.setOffline(true);
  await a.phone.locator('#meeting-departure').click();
  await a.phone.locator('#check-in').click();
  await expect(a.phone.locator('#student-progress')).toHaveText(`${reached+1} / ${first.controlIds.length}`);
  const server = async () => (await (await page.request.get(`/api/releases/${courseId}/progress`)).json())
    .teams.find((t: any) => t.teamId === first.teamId);
  expect((await server()).completed).toHaveLength(reached);
  await a.context.setOffline(false);
  await a.phone.evaluate(() => dispatchEvent(new Event('online')));
  await expect.poll(async () => (await server()).completed.length, { timeout: 15000 }).toBe(reached+1);
  const shared = await server();
  expect(shared.departed).toEqual([meeting]);
  expect(Object.keys(shared.checkIns)).toEqual(shared.completed);
  await a.phone.reload({ waitUntil: 'domcontentloaded' });
  await a.phone.locator('#student-team').selectOption(first.teamId);
  await expect(a.phone.locator('#student-progress')).toHaveText(`${reached+1} / ${first.controlIds.length}`);
  // Finish the remaining targets, then record reaching the finish.
  while (!await a.phone.locator('#arrive-finish').isVisible()) {
    await a.phone.locator(await a.phone.locator('#meeting-departure').isVisible() ? '#meeting-departure' : '#check-in').click();
  }
  await a.phone.locator('#arrive-finish').click();
  await expect(a.phone.locator('#next-control')).toContainText('已到达终点');
  await expect.poll(async () => (await server()).arrived, { timeout: 5000 }).toBe(true);
  expect((await server()).finished).toBe(true);
  await a.phone.evaluate(() => dispatchEvent(new Event('online')));
  await expect(a.phone.locator(`[data-team-progress="${first.teamId}"]`)).toContainText('已到终点');
  // Undo takes back the arrival before any check-in.
  await a.phone.locator('#undo-check-in').click();
  await expect(a.phone.locator('#arrive-finish')).toBeVisible();
  await expect.poll(async () => (await server()).arrived, { timeout: 5000 }).toBe(false);
  expect((await server()).completed).toHaveLength(first.controlIds.length);
  await a.phone.locator('#arrive-finish').click();
  await expect.poll(async () => (await server()).arrived, { timeout: 5000 }).toBe(true);
  // After a reload the teacher finds the course and its progress again.
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.locator('[data-map="szu-3"]').click();
  await page.locator('#release-history summary').click();
  const row = page.locator(`[data-release="${courseId}"]`);
  await expect(row).toContainText('当前有效');
  await expect(row).toContainText('2/2 队已打点 · 1 队完成');
  await expect(row.getByRole('link', { name: '全班进度 ↗' })).toHaveAttribute('href', new RegExp(`course=${courseId}#progress$`));
  page.once('dialog', dialog => dialog.accept());
  await row.locator('[data-session="reset"]').click();
  await expect(row.locator('[data-session="start"]')).toBeVisible();
  // That link is a read-only view; a stray tap cannot report for a team.
  await page.goto(await row.getByRole('link', { name: '全班进度 ↗' }).getAttribute('href') as string, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#viewer-note')).toBeVisible();
  await expect(page.locator('#class-board')).toHaveAttribute('open', '');
  await expect(page.locator(`[data-team-progress="${second.teamId}"]`)).toBeVisible();
  for (const id of ['#check-in', '#undo-check-in', '#meeting-departure']) await expect(page.locator(id)).toBeHidden();
  // Paper backup: one page per team with the server-drawn route.
  await expect(a.phone.locator('#print-team')).toHaveAttribute('href', `/?course=${courseId}&print=${encodeURIComponent(first.teamId)}`);
  await page.goto(`http://127.0.0.1:8768/?course=${courseId}&print=all`, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.print-sheet')).toHaveCount(2);
  await expect(page.locator('.print-sheet').first().locator('tbody tr')).toHaveCount(first.controlIds.length);
  await expect.poll(() => page.locator('.print-map').evaluateAll(images => images.every(image => (image as HTMLImageElement).naturalWidth > 0))).toBe(true);
  await page.locator('.print-sheet').first().screenshot({ path: '/tmp/dirmap-print-sheet.png' });
  const pdf = (await page.pdf({ format: 'A4' })).toString('latin1');
  expect((pdf.match(/\/Type\s*\/Page[^s]/g) ?? []).length).toBe(2);
  // The added class view and print link fit a small phone.
  await b.phone.setViewportSize({ width: 360, height: 640 });
  await b.phone.locator('#mobile-panel').click();
  await b.phone.locator('#class-board summary').click();
  await expect(b.phone.locator(`[data-team-progress="${first.teamId}"]`)).toBeVisible();
  expect(await b.phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await b.phone.locator('#class-board').scrollIntoViewIfNeeded();
  await b.phone.screenshot({ path: '/tmp/dirmap-class-board-phone.png' });
  for (const { context } of phones) await context.close();
});
