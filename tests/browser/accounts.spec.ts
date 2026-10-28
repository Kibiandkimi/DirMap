import fs from 'node:fs';
import { test, expect, localOnly } from './fixtures';

test('switching accounts in one browser isolates course drafts, pending jobs and map image caches', async ({ page, context }) => {
  test.setTimeout(120000);
  const first = `scope-a-${Date.now() % 1000000}`, second = `scope-b-${Date.now() % 1000000}`;
  const password = 'orienteering-scope';
  const enter = async (name: string, register: boolean) => {
    await page.locator('#account-open').click();
    if (register) await page.locator('[data-account-mode="register"]').click();
    await page.locator('#account-name').fill(name); await page.locator('#account-password').fill(password);
    if (register) await page.locator('#account-confirm').fill(password);
    await page.locator('#account-submit').click();
    await expect(page.locator('#account-open')).toHaveText(name);
    await expect(page.locator('.control-row').first()).toBeVisible();
  };
  const leave = async () => {
    await page.locator('#account-open').click(); await page.locator('#sign-out').click();
    await expect(page.locator('#account-open')).toHaveText('登录 / 注册');
  };
  const overlay = () => page.evaluate(() => (window as any).dirmap.campusMap.overlays[0].getSource().getUrl());
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#account-open')).toBeVisible();
  await enter(first, true);
  const defaults = { people: await page.locator('#people').inputValue(), teams: await page.locator('#teams').inputValue() };
  const firstImage = await overlay();
  await page.locator('#people').fill('77'); await page.locator('#teams').fill('7');
  const peer = await context.newPage();
  await peer.goto('/', { waitUntil: 'domcontentloaded' });
  await expect(peer.locator('#account-open')).toHaveText(first);
  await expect(peer.locator('#people')).toHaveValue('77');

  const resumed: string[] = [];
  await page.route('**/api/jobs/old-private-job', async route => {
    resumed.push(route.request().url()); await route.fulfill({ status: 404, json: { detail: 'not this account' } });
  });
  await page.evaluate(() => {
    localStorage.setItem('dirmap-pending-job-v1', JSON.stringify({ id: 'old-private-job' }));
    sessionStorage.setItem('dirmap-admin-token', 'legacy-token-from-another-workbench');
  });
  await leave();
  await expect(peer.locator('#account-open')).toHaveText('登录 / 注册');
  expect(await page.evaluate(() => sessionStorage.getItem('dirmap-admin-token'))).toBeNull();
  await enter(second, true);
  await expect(peer.locator('#account-open')).toHaveText(second);
  await expect(page.locator('#people')).toHaveValue(defaults.people);
  await expect(page.locator('#teams')).toHaveValue(defaults.teams);
  const secondImage = await overlay();
  expect(new URL(secondImage, page.url()).searchParams.get('workbench')).not.toEqual(new URL(firstImage, page.url()).searchParams.get('workbench'));
  await page.waitForLoadState('networkidle');
  expect(resumed).toEqual([]);
  await page.locator('#people').fill('18'); await page.locator('#teams').fill('3');
  await leave(); await enter(first, false);
  await expect(page.locator('#people')).toHaveValue('77');
  await expect(page.locator('#teams')).toHaveValue('7');
  await peer.close();
});

test('an account works in its own workbench with places such as 大沙河公园', async ({ page, browser }) => {
  test.setTimeout(120000);
  const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  // Without an account the original workbench stays usable, as before.
  await expect(page.locator('.workbench-notice')).toContainText('原工作台');
  await expect(page.locator('[data-map="szu-1"]')).toBeVisible();
  const name = `teacher${Date.now() % 1000000}`;
  await page.locator('#account-open').click();
  await page.locator('[data-account-mode="register"]').click();
  await page.locator('#account-name').fill(name);
  await page.locator('#account-password').fill('orienteering-1');
  await page.locator('#account-confirm').fill('orienteering-1');
  await page.locator('#account-submit').click();
  await expect(page.locator('#account-open')).toHaveText(name, { timeout: 15000 });
  await expect(page.locator('.workbench-notice')).toHaveCount(0);
  // A new workbench starts from the processed sample sheets, unreviewed.
  await expect(page.locator('.control-row').first()).toBeVisible();
  await expect(page.locator('#release-badge')).toHaveText('备课草稿');

  await page.locator('#site-select').selectOption('dashahe-park');
  await expect(page.locator('#site-name')).toHaveText('大沙河公园');
  await expect(page.locator('#map-cards')).toContainText('大沙河公园还没有地图');
  await expect(page.locator('#map-title')).toHaveText('大沙河公园');
  const reference = await (await page.request.get('/api/reference?site=dashahe-park')).json();
  expect(reference.features.length).toBeGreaterThan(500);

  await page.locator('#upload-input').setInputFiles({ name: '大沙河公园定向图.png', mimeType: 'image/png', buffer: fs.readFileSync('____________aigc_label.png') });
  const card = page.locator('.map-card', { hasText: '大沙河公园定向图' });
  await expect(card).toBeVisible({ timeout: 20000 });
  const catalog = await (await page.request.get('/api/catalog')).json();
  const uploaded = catalog.maps.find((map: any) => map.name === '大沙河公园定向图');
  expect(uploaded.siteId).toBe('dashahe-park');
  // Neither visitors nor other accounts see this map.
  const visitor = await localOnly(await browser.newContext());
  const visible = await (await visitor.request.get('http://127.0.0.1:8768/api/catalog')).json();
  expect(visible.maps.map((map: any) => map.id)).not.toContain(uploaded.id);
  await visitor.close();
  // The campus list does not mix in park maps.
  await page.locator('#site-select').selectOption('szu-yuehai');
  await expect(page.locator('[data-map="szu-1"]')).toBeVisible();
  await expect(page.locator(`[data-map="${uploaded.id}"]`)).toHaveCount(0);
  await page.locator('#site-select').selectOption('dashahe-park');
  page.once('dialog', dialog => dialog.accept());
  await page.locator(`[data-delete-map="${uploaded.id}"]`).click();
  await expect(page.locator('#map-cards')).toContainText('大沙河公园还没有地图');

  // Any other place: frame it on the map and download its OpenStreetMap features.
  await page.evaluate(() => (window as any).dirmap.campusMap.fitBounds([113.95, 22.55, 113.956, 22.555]));
  await page.locator('#add-site').click();
  await page.locator('#site-name-input').fill('测试公园');
  await page.locator('#site-add-confirm').click();
  await expect(page.locator('#site-select')).toHaveValue(/^site-/, { timeout: 30000 });
  await expect(page.locator('#site-status')).toContainText('OpenStreetMap 参照 2 个地物', { timeout: 30000 });
  await expect(page.locator('#site-name')).toHaveText('测试公园');
  page.once('dialog', dialog => dialog.accept());
  await page.locator('[data-site-action="delete"]').click();
  await expect(page.locator('#site-select option', { hasText: '测试公园' })).toHaveCount(0);

  await page.locator('#account-open').click();
  await page.locator('#sign-out').click();
  await expect(page.locator('#account-open')).toHaveText('登录 / 注册', { timeout: 15000 });
  expect(errors).toEqual([]);
});

test('once the original workbench is claimed, visitors are asked to sign in', async ({ page }) => {
  // Claiming changes the shared test server for every later check, so the
  // server's answers after a claim are replayed here; the API tests cover
  // the claim itself.
  await page.route('**/api/auth/me', route => route.fulfill({ json: { user: null, original: { claimed: true, tokenRequired: true, hasData: null } } }));
  await page.route('**/api/catalog', route => route.fulfill({ status: 401, json: { detail: '请先登录', login: true } }));
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.login-gate')).toContainText('登录后使用');
  await expect(page.locator('.login-gate a[href="/?group="]')).toBeVisible();
  await expect(page.locator('#account-open')).toHaveText('登录 / 注册');
  await page.locator('[data-sign-in="register"]').click();
  await expect(page.locator('#account-submit')).toHaveText('注册并进入我的工作台');
  // Nothing to claim any more, so the claim option is not offered.
  await expect(page.locator('.claim-original')).toHaveCount(0);
});
