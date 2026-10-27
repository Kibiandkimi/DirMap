import { test, expect, localOnly, type Page } from './fixtures';

async function publishCourse(page: Page) {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.locator('[data-map="szu-3"]').click();
  await expect(page.locator('#mixed-rule-summary')).toContainText('31、32、33');
  const state = await (await page.request.get('/api/maps/szu-3')).json();
  const kept = new Set(['31', '32', '33', '34', '35']);
  for (const control of state.controls.filter((c: any) => (c.role ?? 'control') === 'control')) {
    await page.locator(`[data-required="${control.id}"]`).setChecked(kept.has(control.label));
  }
  await page.locator('#people').fill('4'); await page.locator('#teams').fill('2');
  await page.locator('#plan-button').click();
  await expect(page.locator('.route-card')).toHaveCount(2, { timeout: 30000 });
  await page.locator('#review-button').click();
  for (const name of ['registration', 'controls', 'passages']) await page.locator('#review-'+name).check();
  if (await page.locator('#review-approaches').count()) await page.locator('#review-approaches').check();
  await page.locator('#publish-confirm').click();
  const link = page.getByLabel('学生课程链接');
  await expect(link).toBeVisible({ timeout: 30000 });
  await expect(page.getByRole('link', { name: '各小组实时位置 ↗' })).toHaveAttribute('href', /#groups$/);
  return new URL(await link.inputValue()).searchParams.get('course')!;
}

test('classmates team up with a temporary code to share positions, messages, a meeting point and a call for help', async ({ page, browser }) => {
  test.setTimeout(150000);
  const courseId = await publishCourse(page);
  const course = await (await page.request.get(`/api/releases/${courseId}`)).json();
  expect(course.title).toBe('深大粤海定向越野课');
  const phone = async (latitude: number) => {
    const context = await localOnly(await browser.newContext({ viewport: { width: 1280, height: 900 },
      geolocation: { latitude, longitude: 113.9310, accuracy: 6 }, permissions: ['geolocation'] }));
    const phone = await context.newPage();
    await phone.goto(`http://127.0.0.1:8768/?course=${courseId}`, { waitUntil: 'domcontentloaded' });
    await expect(phone.locator('#student-team option')).toHaveCount(2);
    return { context, phone };
  };
  // About 110 m apart: the second phone is north of the first.
  const a = await phone(22.5340), b = await phone(22.5350);
  await expect(a.phone.locator('#site-name')).toHaveText('深圳大学粤海校区');
  await a.phone.locator('#group-section summary').click();
  await a.phone.locator('#group-name').fill('小王');
  await a.phone.locator('[data-group-action="create"]').click();
  const code = (await a.phone.locator('#group-code-text').innerText()).replace(/\s/g, '');
  expect(code).toMatch(/^\d{6}$/);
  await expect(a.phone.locator('#group-meta')).toContainText('1 人');

  await b.phone.locator('#group-section summary').click();
  await b.phone.locator('#group-name').fill('小李');
  await b.phone.locator('#group-code').fill(`${code.slice(0, 3)} ${code.slice(3)}`);
  await b.phone.locator('[data-group-action="join"]').click();
  await expect(b.phone.locator('#group-members .group-member')).toHaveCount(2);
  await expect(a.phone.locator('#group-members')).toContainText('小李', { timeout: 10000 });

  // Sharing is off until each member turns it on.
  await expect(b.phone.locator('#group-members')).toContainText('未共享位置');
  await a.phone.locator('#group-share').check();
  await b.phone.locator('#group-share').check();
  await expect(b.phone.locator('#group-members')).toContainText(/小王.*南 1\d\d m/, { timeout: 15000 });
  await expect(a.phone.locator('#group-members')).toContainText(/小李.*北 1\d\d m/, { timeout: 15000 });

  await b.phone.locator('[data-group-quick="我到了"]').click();
  await expect(a.phone.locator('#group-messages')).toContainText('小李 我到了', { timeout: 10000 });
  await a.phone.locator('#group-text').fill('在体育馆门口等');
  await a.phone.locator('#group-text').press('Enter');
  await expect(b.phone.locator('#group-messages')).toContainText('在体育馆门口等', { timeout: 10000 });

  a.phone.once('dialog', dialog => dialog.accept('东门集合'));
  await a.phone.locator('[data-group-action="pin"]').click();
  await a.phone.locator('#map').click({ position: { x: 500, y: 400 } });
  await expect(b.phone.locator('#group-messages')).toContainText('⚑ 东门集合', { timeout: 10000 });

  b.phone.once('dialog', dialog => dialog.accept());
  await b.phone.locator('#group-help-button').click();
  await expect(a.phone.locator('#group-help')).toContainText('小李 请求帮助', { timeout: 10000 });
  await expect(b.phone.locator('#group-help')).toContainText('你已发出求助');

  // The course's teacher sees each group's members, positions and help calls.
  const teacher = await page.context().newPage();
  await teacher.goto(`/?course=${courseId}#groups`, { waitUntil: 'domcontentloaded' });
  await expect(teacher.locator('#group-overview')).toContainText(`${code.slice(0, 3)} ${code.slice(3)}`, { timeout: 15000 });
  await expect(teacher.locator('#group-overview')).toContainText('求助：小李');
  await expect(teacher.locator('#group-section')).toBeHidden();
  await teacher.close();

  await b.phone.locator('[data-group-action="resolved"]').click();
  await expect(a.phone.locator('#group-help')).toBeHidden({ timeout: 10000 });
  // Membership and sharing survive a reload.
  await b.phone.reload({ waitUntil: 'domcontentloaded' });
  await expect(b.phone.locator('#group-code-text')).toHaveText(`${code.slice(0, 3)} ${code.slice(3)}`);
  await expect(b.phone.locator('#group-share')).toBeChecked();
  // Turning sharing off removes the position from the others' view.
  await b.phone.locator('#group-share').uncheck();
  await expect(a.phone.locator('#group-members')).toContainText(/小李.*未共享位置/, { timeout: 10000 });

  a.phone.once('dialog', dialog => dialog.accept());
  await a.phone.locator('[data-group-action="close"]').click();
  await expect(a.phone.locator('[data-group-action="create"]')).toBeVisible();
  await expect(b.phone.locator('[data-group-action="create"]')).toBeVisible({ timeout: 15000 });
  await a.context.close(); await b.context.close();
});

test('a group can be joined without a course from an invitation link', async ({ browser }) => {
  test.setTimeout(60000);
  const context = await localOnly(await browser.newContext({ viewport: { width: 390, height: 844 },
    geolocation: { latitude: 22.5599, longitude: 113.9566, accuracy: 8 }, permissions: ['geolocation'] }));
  const host = await context.newPage();
  await host.goto('http://127.0.0.1:8768/?group=', { waitUntil: 'domcontentloaded' });
  await expect(host.locator('#group-only-heading')).toBeVisible();
  await expect(host.locator('#check-in')).toBeHidden();
  await host.locator('#group-name').fill('领队');
  await host.locator('[data-group-action="create"]').click();
  const code = (await host.locator('#group-code-text').innerText()).replace(/\s/g, '');
  const guestContext = await localOnly(await browser.newContext({ viewport: { width: 390, height: 844 } }));
  const guest = await guestContext.newPage();
  await guest.goto(`http://127.0.0.1:8768/?group=${code}`, { waitUntil: 'domcontentloaded' });
  await expect(guest.locator('#group-code')).toHaveValue(code);
  await guest.locator('#group-name').fill('队员');
  await guest.locator('[data-group-action="join"]').click();
  await expect(guest.locator('#group-members')).toContainText('领队');
  // Nothing scrolls sideways on a phone.
  expect(await guest.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await guest.locator('#student-panel').screenshot({ path: '/tmp/dirmap-group-phone.png' });
  await context.close(); await guestContext.close();
});
