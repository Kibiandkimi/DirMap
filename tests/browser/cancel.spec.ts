import { test, expect } from './fixtures';

test('a teacher can cancel a long recognition job without changing the map', async ({ page }) => {
  test.setTimeout(120000);
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.locator('[data-map="szu-2"]').click();
  await expect(page.locator('#map-title')).toContainText('2');
  const before = await (await page.request.get('/api/maps/szu-2')).json();
  const submitted = page.waitForResponse(r => r.url().includes('/api/maps/szu-2/process') && r.request().method() === 'POST');
  await page.locator('#process-current').click();
  const job = await (await submitted).json();
  await page.locator('#cancel-job').click();
  await expect(page.locator('#toast')).toContainText('任务已取消，原有数据保持不变', { timeout: 90000 });
  await expect(page.locator('#cancel-job')).toBeHidden();
  const finished = await (await page.request.get('/api/jobs/'+job.id)).json();
  expect(finished).toMatchObject({ state: 'failed', cancelled: true });
  const after = await (await page.request.get('/api/maps/szu-2')).json();
  expect(after.version).toBe(before.version);
  expect(after.controls).toEqual(before.controls);
  expect(await page.evaluate(() => localStorage.getItem('dirmap-workbench-v2:original-anonymous:pending-job'))).toBeNull();
  await page.locator('.job-history summary').click();
  await expect(page.locator(`[data-resume-job="${job.id}"]`)).toContainText('已取消');
  await expect(page.locator('#plan-button')).toBeEnabled();
});
