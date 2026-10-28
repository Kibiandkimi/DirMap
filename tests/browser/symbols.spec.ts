import { test, expect } from './fixtures';

test('an imported course with separate start and finish plans and publishes between its recognized endpoints', async ({ page }) => {
  test.setTimeout(180000);
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.control-row').first()).toBeVisible();
  const image = Buffer.from(await page.evaluate(() => {
    const canvas = document.createElement('canvas'); canvas.width = 900; canvas.height = 700;
    const ctx = canvas.getContext('2d')!;
    ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 900, 700);
    ctx.strokeStyle = ctx.fillStyle = '#c832cd'; ctx.lineWidth = 3;
    const triangleRadius = 2.4 * 22 / Math.sqrt(3);
    ctx.beginPath();
    for (let i = 0; i < 3; i++) {
      const angle = Math.PI / 9 + i * Math.PI * 2 / 3;
      const x = 140 + Math.cos(angle) * triangleRadius, y = 140 + Math.sin(angle) * triangleRadius;
      if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
    }
    ctx.closePath(); ctx.stroke();
    ctx.font = '22px sans-serif';
    for (const [i, [x, y]] of [[420, 140], [690, 370], [200, 530]].entries()) {
      ctx.beginPath(); ctx.arc(x, y, 22, 0, Math.PI * 2); ctx.stroke();
      ctx.fillText(String(31 + i), x + 27, y - 15);
    }
    for (const radius of [22 * .8, 22 * 1.2]) {
      ctx.beginPath(); ctx.arc(760, 580, radius, 0, Math.PI * 2); ctx.stroke();
    }
    return canvas.toDataURL('image/png').split(',')[1];
  }), 'base64');
  const uploaded = page.waitForResponse(response => new URL(response.url()).pathname === '/api/maps/upload' && response.request().method() === 'POST');
  await page.locator('#upload-input').setInputFiles({ name: 'standard-start-finish.png', mimeType: 'image/png', buffer: image });
  const asset = await (await uploaded).json();
  try {
    await expect(page.locator('#process-current')).toBeEnabled();
    const processing = page.waitForResponse(response => new URL(response.url()).pathname === `/api/maps/${asset.id}/process`);
    await page.locator('#process-current').click();
    const process = await (await processing).json();
    await expect.poll(async () => (await (await page.request.get(`/api/jobs/${process.id}`)).json()).state,
      { timeout: 60000 }).toBe('succeeded');
    const detected = await (await page.request.get(`/api/maps/${asset.id}`)).json();
    expect(detected.controls.filter((c: any) => c.role === 'control')).toHaveLength(3);
    expect(detected.controls.filter((c: any) => c.role === 'start')).toHaveLength(1);
    expect(detected.controls.filter((c: any) => c.role === 'finish')).toHaveLength(1);

    // This blank test sheet has no buildings to register automatically.
    // Four known corners supply its frame; no detected point is used to fit.
    const registered = await page.request.post(`/api/maps/${asset.id}/anchors`, { data: {
      version: detected.version,
      anchors: [
        { pixel: [0, 0], lnglat: [113.9284, 22.5365] },
        { pixel: [899, 0], lnglat: [113.9324, 22.5365] },
        { pixel: [899, 699], lnglat: [113.9324, 22.5335] },
        { pixel: [0, 699], lnglat: [113.9284, 22.5335] },
      ],
    } });
    expect(registered.ok()).toBe(true);
    const registration = await registered.json();
    await expect.poll(async () => (await (await page.request.get(`/api/jobs/${registration.id}`)).json()).state,
      { timeout: 60000 }).toBe('succeeded');
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.locator(`[data-map="${asset.id}"]`).click();
    await expect(page.locator('#pick-start')).toContainText('已识别原图起点');
    await expect(page.locator('#pick-finish')).toContainText('已识别原图终点');
    await expect(page.locator('#return-start')).not.toBeChecked();
    await page.locator('#course-mode').selectOption('all_visit');
    await page.locator('#people').fill('4'); await page.locator('#teams').fill('2');
    const planning = page.waitForRequest(request => new URL(request.url()).pathname === '/api/plans' && request.method() === 'POST');
    await page.locator('#plan-button').click();
    const body = (await planning).postDataJSON();
    expect(body.controlIds).toHaveLength(3);
    for (const [actual, expected] of [[body.startPixel, [140, 140]], [body.finishPixel, [760, 580]]]) {
      expect(Math.hypot(actual[0] - expected[0], actual[1] - expected[1])).toBeLessThan(2);
    }
    await expect(page.locator('.route-card')).toHaveCount(2, { timeout: 30000 });
    await expect(page.locator('#plan-errors')).toBeEmpty();
    await page.screenshot({ path: '/tmp/dirmap-separate-course-symbols.png' });
    await page.locator('#review-button').click();
    for (const name of ['registration', 'controls', 'passages']) await page.locator('#review-' + name).check();
    if (await page.locator('#review-approaches').count()) await page.locator('#review-approaches').check();
    await page.locator('#publish-confirm').click();
    const link = page.getByLabel('学生课程链接'); await expect(link).toBeVisible({ timeout: 30000 });
    const id = new URL(await link.inputValue()).searchParams.get('course');
    const course = await (await page.request.get(`/api/releases/${id}`)).json();
    expect(course.superseded).toBe(false);
    expect(course.plan.routes).toHaveLength(2);
    for (const route of course.plan.routes) {
      expect(route.controlIds).toHaveLength(3);
      expect(route.geometry[0]).not.toEqual(route.geometry.at(-1));
    }
  } finally { await page.request.delete(`/api/maps/${asset.id}`); }
});
