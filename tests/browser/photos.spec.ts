import { readFile } from 'node:fs/promises';
import { test, expect, localOnly, type Page } from './fixtures';

async function course(page: Page) {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.locator('[data-map="szu-3"]').click();
  await expect(page.locator('#mixed-rule-summary')).toContainText('31、32、33');
  const state = await (await page.request.get('/api/maps/szu-3')).json();
  for (const control of state.controls.filter((c: any) => (c.role ?? 'control') === 'control')) {
    await page.locator(`[data-required="${control.id}"]`).setChecked(['31', '32', '33', '34', '35'].includes(control.label));
  }
  await page.locator('#people').fill('4'); await page.locator('#teams').fill('2');
  await page.locator('#plan-button').click();
  await expect(page.locator('.route-card')).toHaveCount(2, { timeout: 30000 });
  await page.locator('#review-button').click();
  for (const name of ['registration', 'controls', 'passages']) await page.locator('#review-'+name).check();
  if (await page.locator('#review-approaches').count()) await page.locator('#review-approaches').check();
  await page.locator('#publish-confirm').click();
  const link = page.getByLabel('学生课程链接'); await expect(link).toBeVisible({ timeout: 30000 });
  return new URL(await link.inputValue()).searchParams.get('course')!;
}

test('a group shares checkpoint photos, recovers an offline upload, exports all three formats and clears only downloaded versions', async ({ page, browser }) => {
  test.setTimeout(240000);
  const id = await course(page);
  const openPhone = async () => {
    const context = await localOnly(await browser.newContext({ viewport: { width: 390, height: 844 } }));
    // A WebView may expose the API but reject the capability check itself.
    // Downloading an already generated file must still succeed in that case.
    await context.addInitScript(() => {
      Object.defineProperty(navigator, 'share', { configurable: true, value: async () => {} });
      Object.defineProperty(navigator, 'canShare', {
        configurable: true, value: () => { throw new DOMException('Sharing is unavailable', 'NotAllowedError'); },
      });
    });
    const phone = await context.newPage();
    await phone.goto(`http://127.0.0.1:8768/?course=${id}`, { waitUntil: 'domcontentloaded' });
    await expect(phone.locator('#offline-status')).toContainText('本课已保存', { timeout: 20000 });
    await phone.waitForFunction(() => !!navigator.serviceWorker.controller);
    await phone.locator('#mobile-panel').click();
    await phone.locator('#group-section summary').click();
    return { context, phone };
  };
  const a = await openPhone(), b = await openPhone();
  try {
    await a.phone.locator('#group-name').fill('小王');
    await a.phone.locator('[data-group-action="create"]').click();
    const code = (await a.phone.locator('#group-code-text').innerText()).replace(/\s/g, '');
    await b.phone.locator('#group-name').fill('小李'); await b.phone.locator('#group-code').fill(code);
    await b.phone.locator('[data-group-action="join"]').click();
    await expect(b.phone.locator('#group-members')).toContainText('小王');
    await a.phone.locator('[data-open-album]').click(); await b.phone.locator('[data-open-album]').click();
    await expect(a.phone.locator('.checkpoint-photo')).toHaveCount(5);
    const ids = await a.phone.locator('.checkpoint-photo').evaluateAll(rows => rows.map(row => (row as HTMLElement).dataset.photoId!));
    const buffer = Buffer.from(await page.evaluate(() => {
      const canvas = document.createElement('canvas'); canvas.width = 800; canvas.height = 600;
      const ctx = canvas.getContext('2d')!;
      ctx.fillStyle = '#d0e9ed'; ctx.fillRect(0, 0, 800, 600); ctx.fillStyle = '#5c9656'; ctx.fillRect(0, 380, 800, 220);
      ctx.fillStyle = '#e48837'; ctx.fillRect(280, 170, 240, 240); ctx.fillStyle = 'white'; ctx.font = 'bold 30px sans-serif'; ctx.fillText('PHOTO TEST', 300, 260);
      return canvas.toDataURL('image/png').split(',')[1];
    }), 'base64');
    const upload = async (phone: Page, controlId: string, camera = false) => {
      const chooser = phone.waitForEvent('filechooser');
      await phone.locator(`[data-photo-id="${controlId}"] [data-photo-action="${camera ? 'camera' : 'choose'}"]`).click();
      expect(await phone.locator('#checkpoint-photo-file').getAttribute('capture')).toBe(camera ? 'environment' : null);
      await (await chooser).setFiles({ name: 'checkpoint.png', mimeType: 'image/png', buffer });
    };
    await upload(a.phone, ids[0], true);
    await expect(a.phone.locator(`[data-photo-id="${ids[0]}"] > small`)).toContainText('小王');
    await expect(b.phone.locator(`[data-photo-id="${ids[0]}"] > small`)).toContainText('小王', { timeout: 15000 });
    await upload(b.phone, ids[1]);
    await expect(a.phone.locator('#photo-album-title')).toContainText('已上传 2 张', { timeout: 15000 });
    await upload(b.phone, ids[0]);
    await expect(a.phone.locator(`[data-photo-id="${ids[0]}"] > small`)).toContainText('小李', { timeout: 15000 });
    // Unsent camera data survives page reload without ever being public.
    await a.context.setOffline(true);
    await upload(a.phone, ids[2]);
    await expect(a.phone.locator(`[data-photo-id="${ids[2]}"]`)).toHaveClass(/pending/);
    await expect(a.phone.locator('#export-photos')).toBeDisabled();
    await a.phone.reload({ waitUntil: 'domcontentloaded' });
    await a.phone.locator('#mobile-panel').click();
    await a.phone.locator('[data-open-album]').click();
    await expect(a.phone.locator(`[data-photo-id="${ids[2]}"]`)).toHaveClass(/pending/);
    await expect.poll(() => a.phone.locator(`[data-photo-id="${ids[2]}"] img`).evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);
    await a.context.setOffline(false);
    await expect(a.phone.locator('#photo-album-title')).toContainText('已上传 3 张', { timeout: 20000 });
    await expect(b.phone.locator('#photo-album-title')).toContainText('已上传 3 张', { timeout: 15000 });
    await expect(a.phone.locator('.checkpoint-photo.pending')).toHaveCount(0);
    expect(await a.phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await a.phone.screenshot({ path: '/tmp/dirmap-group-photos-phone.png' });
    await a.phone.locator(`[data-photo-id="${ids[0]}"] [data-photo-action="preview"]`).click();
    await expect(a.phone.locator('.photo-preview-dialog')).toBeVisible();
    await expect.poll(() => a.phone.locator('.photo-preview-dialog img').evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(800);
    await a.phone.locator('[data-close-photo-preview]').click();

    await a.phone.locator('#export-photos').click();
    // A lost export never exposes the destructive finish action.
    await a.phone.route('**/api/groups/*/photos/export', route => route.abort('connectionreset'), { times: 1 });
    await a.phone.locator('#make-photo-export').click();
    await expect(a.phone.locator('#make-photo-export')).toBeEnabled();
    await expect(a.phone.locator('#finish-photo-export')).toBeHidden();
    await expect(a.phone.locator('#photo-album-title')).toContainText('已上传 3 张');
    for (const [format, extension] of [['pdf', 'pdf'], ['long-image', 'jpg'], ['images', 'zip']]) {
      await a.phone.locator('#photo-export-format').selectOption(format);
      const downloading = a.phone.waitForEvent('download');
      await a.phone.locator('#make-photo-export').click();
      const download = await downloading;
      expect(await download.failure()).toBeNull(); expect(download.suggestedFilename()).toMatch(new RegExp(`\\.${extension}$`));
      const path = `/tmp/dirmap-group-photos.${extension}`;
      await download.saveAs(path);
      const bytes = await readFile(path);
      expect(bytes.length).toBeGreaterThan(1000);
      if (format === 'pdf') { expect(bytes.subarray(0, 5).toString()).toBe('%PDF-'); expect(bytes.includes(Buffer.from('/Count 3 '))).toBe(true); }
      else if (format === 'images') expect(bytes.subarray(0, 2).toString()).toBe('PK');
      else expect(bytes.subarray(0, 2).toString('hex')).toBe('ffd8');
      await expect(a.phone.locator('#photo-export-status')).toContainText('已生成 3 张照片');
      await expect(a.phone.locator('#share-photos')).toBeHidden();
      if (format === 'long-image') {
        await a.phone.locator('#view-photo-export').click();
        const preview = a.phone.locator('.photo-export-preview');
        await expect(preview).toBeVisible();
        await expect.poll(() => preview.locator('img').evaluate((img: HTMLImageElement) => img.naturalHeight)).toBe(1824);
        const viewport = preview.locator('.photo-preview-viewport');
        expect(await viewport.evaluate(el => el.scrollHeight > el.clientHeight)).toBe(true);
        expect(await a.phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        await a.phone.screenshot({ path: '/tmp/dirmap-photo-export-preview-phone.png' });
        await viewport.evaluate(el => { el.scrollTop = el.scrollHeight; });
        // The close control stays outside the scrolling strip.
        const close = preview.locator('[data-close-photo-preview]'), box = (await close.boundingBox())!;
        expect(box.y).toBeGreaterThanOrEqual(0); expect(box.y + box.height).toBeLessThan(844);
        await a.phone.setViewportSize({ width: 844, height: 390 });
        const landscape = await close.boundingBox();
        expect(landscape!.y).toBeGreaterThanOrEqual(0); expect(landscape!.y + landscape!.height).toBeLessThan(390);
        expect(await a.phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        await a.phone.setViewportSize({ width: 390, height: 844 });
        await close.click();
      } else await expect(a.phone.locator('#view-photo-export')).toBeHidden();
    }
    // Supported sharing receives the generated file directly from a real tap.
    await a.phone.evaluate(() => {
      (window as any).sharedPhotos = [];
      Object.defineProperty(navigator, 'canShare', { configurable: true, value: () => true });
      Object.defineProperty(navigator, 'share', { configurable: true, value: async ({ files }: ShareData) => {
        const active = navigator.userActivation.isActive, file = files![0];
        const bytes = new Uint8Array(await file.arrayBuffer());
        (window as any).sharedPhotos.push({ name: file.name, type: file.type, bytes: file.size, active, prefix: String.fromCharCode(...bytes.slice(0, 5)) });
        if ((window as any).cancelPhotoShare) throw new DOMException('Cancelled', 'AbortError');
        if ((window as any).denyPhotoShare) throw new DOMException('Denied', 'NotAllowedError');
      } });
    });
    await a.phone.locator('#photo-export-format').selectOption('pdf');
    const sharingFile = a.phone.waitForEvent('download');
    await a.phone.locator('#make-photo-export').click(); await sharingFile;
    await a.phone.locator('#share-photos').click();
    await expect(a.phone.locator('#photo-export-status')).toContainText('文件已交给系统分享');
    const shared = await a.phone.evaluate(() => (window as any).sharedPhotos[0]);
    expect(shared).toMatchObject({ type: 'application/pdf', active: true, prefix: '%PDF-' });
    expect(shared.name).toMatch(/\.pdf$/); expect(shared.bytes).toBeGreaterThan(1000);
    await a.phone.evaluate(() => { (window as any).cancelPhotoShare = true; });
    await a.phone.locator('#share-photos').click();
    await expect(a.phone.locator('#photo-export-status')).toContainText('已取消分享');
    await expect(a.phone.locator('#share-photos')).toBeEnabled();
    await expect(a.phone.locator('#photo-album-title')).toContainText('已上传 3 张');
    await a.phone.evaluate(() => { (window as any).cancelPhotoShare = false; (window as any).denyPhotoShare = true; });
    await a.phone.locator('#share-photos').click();
    await expect(a.phone.locator('#photo-export-status')).toContainText('分享暂不可用');
    await expect(a.phone.locator('#download-photos')).toBeVisible();
    await expect(a.phone.locator('#finish-photos')).toBeEnabled();
    // A teammate's replacement during export must survive finishing it.
    await upload(b.phone, ids[0]);
    await expect(b.phone.locator('.checkpoint-photo.pending')).toHaveCount(0);
    await expect(b.phone.locator('#photo-album-status')).toContainText('照片已上传');
    await a.phone.locator('#finish-photos').click();
    await expect(a.phone.locator('.photo-export-dialog')).toHaveCount(0);
    await expect(a.phone.locator('#photo-album-title')).toContainText('已上传 1 张');
    await expect(b.phone.locator('#photo-album-title')).toContainText('已上传 1 张', { timeout: 15000 });
    await expect(a.phone.locator(`[data-photo-id="${ids[0]}"] > small`)).toContainText('小李');
    await expect(a.phone.locator(`[data-photo-id="${ids[1]}"]`)).toContainText('尚未上传');
    await expect(a.phone.locator(`[data-photo-id="${ids[2]}"]`)).toContainText('尚未上传');

    // A later map edit invalidates navigation but must not strand the album.
    const current = await (await page.request.get('/api/maps/szu-3')).json();
    expect((await page.request.patch('/api/maps/szu-3', { data: { version: current.version, edits: current.edits } })).ok()).toBe(true);
    await a.phone.reload({ waitUntil: 'domcontentloaded' });
    await expect(a.phone.locator('#release-badge')).toContainText('课程已更新');
    await a.phone.locator('[data-open-album]').click();
    await expect(a.phone.locator('#photo-album-title')).toContainText('已上传 1 张');
    await a.phone.locator('[data-photo-close]').click();
    a.phone.once('dialog', dialog => dialog.accept());
    await a.phone.locator('[data-group-action="close"]').click();
    await expect(b.phone.locator('#group-name')).toBeVisible({ timeout: 15000 });
    await expect(b.phone.locator('.photo-dialog')).toHaveCount(0);
  } finally { await a.context.close(); await b.context.close(); }
});

test('queued photos recover a lost upload reply without overwriting the shared photo', async ({ page, browser }) => {
  test.setTimeout(120000);
  const id = await course(page);
  const context = await localOnly(await browser.newContext({ viewport: { width: 390, height: 844 } }));
  let releaseReply = () => {};
  try {
    const phone = await context.newPage();
    await phone.goto(`http://127.0.0.1:8768/?course=${id}`, { waitUntil: 'domcontentloaded' });
    await phone.locator('#mobile-panel').click(); await phone.locator('#group-section summary').click();
    await phone.locator('#group-name').fill('小周'); await phone.locator('[data-group-action="create"]').click();
    await phone.locator('[data-open-album]').click();
    await expect(phone.locator('.checkpoint-photo')).toHaveCount(5);
    const ids = await phone.locator('.checkpoint-photo').evaluateAll(rows => rows.map(row => (row as HTMLElement).dataset.photoId!));
    const buffer = Buffer.from(await phone.evaluate(() => {
      const canvas = document.createElement('canvas'); canvas.width = 640; canvas.height = 480;
      const ctx = canvas.getContext('2d')!; ctx.fillStyle = '#52a290'; ctx.fillRect(0, 0, 640, 480);
      return canvas.toDataURL('image/png').split(',')[1];
    }), 'base64');
    const upload = async (controlId: string) => {
      const chooser = phone.waitForEvent('filechooser');
      await phone.locator(`[data-photo-id="${controlId}"] [data-photo-action="choose"]`).click();
      await (await chooser).setFiles({ name: 'checkpoint.png', mimeType: 'image/png', buffer });
    };
    let storedPhoto: any;
    let uploaded!: () => void;
    const stored = new Promise<void>(resolve => { uploaded = resolve; });
    const held = new Promise<void>(resolve => { releaseReply = resolve; });
    await phone.route(`**/photos/${ids[0]}`, async route => {
      const response = await route.fetch(); expect(response.status()).toBe(200);
      storedPhoto = (await response.json()).controls.find((c: any) => c.id === ids[0]).photo;
      uploaded(); await held; await route.abort('connectionreset');
    }, { times: 1 });
    await upload(ids[0]); await stored;
    // Another checkpoint can be queued while the first request is pending.
    await upload(ids[1]);
    await expect(phone.locator(`[data-photo-id="${ids[1]}"]`)).toHaveClass(/pending/);
    releaseReply();
    await expect(phone.locator(`[data-photo-id="${ids[0]}"]`)).toContainText('上传未完成');
    await phone.locator(`[data-photo-id="${ids[0]}"] [data-photo-action="retry"]`).click();
    await expect(phone.locator('.checkpoint-photo.pending')).toHaveCount(0, { timeout: 15000 });
    await expect(phone.locator('#photo-album-title')).toContainText('已上传 2 张');
    const membership = await phone.evaluate(() => JSON.parse(localStorage.getItem('dirmap-group-v1')!));
    const album = await (await phone.request.get(`/api/groups/${membership.groupId}/photos`, {
      headers: { 'X-DirMap-Member': membership.token },
    })).json();
    expect(album.controls.find((c: any) => c.id === ids[0]).photo).toEqual(storedPhoto);
    // A successful but slow reply also drains newly queued checkpoints.
    let storedNext!: () => void;
    const savedNext = new Promise<void>(resolve => { storedNext = resolve; });
    const nextHeld = new Promise<void>(resolve => { releaseReply = resolve; });
    await phone.route(`**/photos/${ids[2]}`, async route => {
      const response = await route.fetch(); expect(response.status()).toBe(200);
      storedNext(); await nextHeld; await route.fulfill({ response });
    }, { times: 1 });
    await upload(ids[2]); await savedNext;
    await expect(phone.locator(`[data-photo-id="${ids[2]}"] [data-photo-action="choose"]`)).toBeDisabled();
    await upload(ids[3]); releaseReply();
    await expect(phone.locator('.checkpoint-photo.pending')).toHaveCount(0, { timeout: 15000 });
    await expect(phone.locator('#photo-album-title')).toContainText('已上传 4 张');
  } finally { releaseReply(); await context.close(); }
});
