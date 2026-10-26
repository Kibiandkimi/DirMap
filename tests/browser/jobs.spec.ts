import { test, expect } from './fixtures';

for (const dropResponse of [false, true]) {
  test(`reload recovers the original real-map plan after losing ${dropResponse ? 'submission response' : 'progress connection'}`, async ({ page }) => {
    test.setTimeout(90000);
    const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
    await page.goto('/');
    await page.locator('[data-map="szu-3"]').click();
    await expect(page.locator('#course-mode')).toHaveValue('mixed_coverage');
    await page.locator('#people').fill('15'); await page.locator('#teams').fill('3');
    await page.locator('#speed').fill('1.7'); await page.locator('#stagger').fill('20');
    await page.locator('#mandatory-order').check();
    let submissions = 0;
    page.on('request', request => { if (request.url().endsWith('/api/plans') && request.method() === 'POST') submissions++; });
    let resolveJob!: (job: any) => void;
    const accepted = new Promise<any>(resolve => { resolveJob = resolve; });
    if (dropResponse) {
      await page.route('**/api/plans', async route => {
        const response = await route.fetch();
        resolveJob(await response.json());
        await route.abort('connectionreset');
      });
    } else {
      await page.route(/\/api\/jobs\/[0-9a-f]{16}$/, route => route.abort('connectionrefused'));
      page.on('response', async response => {
        if (response.url().endsWith('/api/plans') && response.request().method() === 'POST') resolveJob(await response.json());
      });
    }
    await page.locator('#plan-button').click();
    const job = await accepted;
    await page.waitForFunction(() => !!JSON.parse(localStorage.getItem('dirmap-pending-job-v1') ?? 'null')?.requestId);
    if (dropResponse) {
      await expect(page.locator('#plan-button')).toBeEnabled();
      expect(await page.evaluate(() => JSON.parse(localStorage.getItem('dirmap-pending-job-v1')!).id)).toBeUndefined();
    } else {
      await page.waitForFunction(() => !!JSON.parse(localStorage.getItem('dirmap-pending-job-v1') ?? 'null')?.id);
      await page.unroute(/\/api\/jobs\/[0-9a-f]{16}$/);
    }
    await page.reload();
    await expect(page.locator('.route-card')).toHaveCount(3, { timeout: 45000 });
    await expect(page.locator('#map-title')).toContainText('3');
    await expect(page.locator('#course-mode')).toHaveValue('mixed_coverage');
    await expect(page.locator('#mandatory-order')).toBeChecked();
    await expect(page.locator('#people')).toHaveValue('15');
    await expect(page.locator('#teams')).toHaveValue('3');
    await expect(page.locator('#speed')).toHaveValue('1.7');
    await expect(page.locator('#stagger')).toHaveValue('20');
    await expect(page.locator('#mixed-coverage-result')).toContainText('其余 13 个点均已分配');
    expect(submissions).toBe(1);
    expect(await page.evaluate(() => localStorage.getItem('dirmap-pending-job-v1'))).toBeNull();
    const current = await (await page.request.get('/api/jobs/'+job.id)).json();
    expect(current.state).toBe('succeeded'); expect(current.attempts).toBe(1);
    expect(current.result.request.teams.map((team: any) => team.departureSec)).toEqual([0, 20, 40]);
    await page.locator('.job-history summary').click();
    await expect(page.locator(`[data-resume-job="${job.id}"]`)).toContainText('已完成');
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(errors).toEqual([]);
  });
}
