import { expect, type Page } from './fixtures';

export function editableControl(control: Record<string, unknown>) {
  return Object.fromEntries(Object.entries(control).filter(([key]) => ['id', 'label', 'pixel', 'approachPixel', 'surfaceId', 'radiusPx',
    'required', 'role', 'serviceBaseSec', 'servicePerPersonSec', 'state'].includes(key)));
}

export async function finished(page: Page, job: { id: string } | null) {
  if (!job) return;
  let last: any;
  await expect.poll(async () => {
    last = await (await page.request.get(`/api/jobs/${job.id}`)).json();
    return ['succeeded', 'failed'].includes(last.state);
  }, { timeout: 90000 }).toBe(true);
  expect(last.state, last.message).toBe('succeeded');
  return last.result;
}

export async function teachingSheet(page: Page, obstacle = true, photographs = true) {
  const buffer = Buffer.from(await page.evaluate(({ obstacle, photographs }) => {
    const canvas = document.createElement('canvas'); canvas.width = 1000; canvas.height = 700;
    const ctx = canvas.getContext('2d')!;
    ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 1000, 700);
    ctx.fillStyle = '#bee8b0'; ctx.fillRect(40, 40, 630, 610);
    ctx.strokeStyle = '#f5decf'; ctx.lineWidth = 12;
    ctx.beginPath(); ctx.moveTo(100, 100); ctx.lineTo(600, 600); ctx.stroke();
    ctx.strokeStyle = '#b87636'; ctx.lineWidth = 3;
    for (const radius of [80, 130, 180]) { ctx.beginPath(); ctx.ellipse(400, 320, radius, radius*.8, .2, 0, Math.PI*2); ctx.stroke(); }
    if (obstacle) {
      ctx.fillStyle = '#777'; ctx.fillRect(382, 302, 36, 36);
      ctx.strokeStyle = '#111'; ctx.lineWidth = 3; ctx.strokeRect(382, 302, 36, 36);
      ctx.fillStyle = '#ddd'; ctx.fillRect(395, 315, 10, 10);
    }
    const purple = '#bd31ca'; ctx.strokeStyle = purple; ctx.fillStyle = purple; ctx.lineWidth = 3; ctx.font = 'bold 24px sans-serif';
    for (const [i, [x, y]] of [[180, 180], [400, 320], [250, 520]].entries()) {
      ctx.beginPath(); ctx.arc(x, y, 22, 0, Math.PI*2); ctx.stroke(); ctx.fillText(String(31+i), x+27, y-20);
    }
    let random = 71;
    if (photographs) for (let i = 0; i < 3; i++) {
      const x = 740, y = 60+i*160, w = 200, h = 150, picture = ctx.createImageData(w, h);
      for (let p = 0; p < w*h; p++) {
        random = (Math.imul(random, 1664525)+1013904223) >>> 0;
        const value = 40+random % 165;
        picture.data.set([value, value, value, 255], p*4);
      }
      ctx.putImageData(picture, x, y); ctx.fillStyle = purple; ctx.font = 'bold 26px sans-serif'; ctx.fillText(String(31+i), x+w-42, y+h-10);
    }
    return canvas.toDataURL('image/png').split(',')[1];
  }, { obstacle, photographs }), 'base64');
  const uploaded = await page.request.post('/api/maps/upload', { multipart: { file: { name: '教学核对.png', mimeType: 'image/png', buffer } } });
  expect(uploaded.ok()).toBe(true); const asset = await uploaded.json();
  const processed = await finished(page, await (await page.request.post(`/api/maps/${asset.id}/process`)).json());
  expect(processed.controls.filter((c: any) => c.role === 'control').map((c: any) => c.label).sort()).toEqual(['31', '32', '33']);
  if (photographs) expect(processed.sheetPhotos.map((p: any) => p.label).sort()).toEqual(['31', '32', '33']);
  let state = await finished(page, await (await page.request.post(`/api/maps/${asset.id}/anchors`, { data: {
    version: processed.version,
    anchors: [[40, 40], [670, 40], [670, 650], [40, 650]].map(([x, y]) => ({ pixel: [x, y], lnglat: [113.93+x*.3/103000, 22.53+(700-y)*.3/111000] })),
  } })).json());
  const controls = state.controls.map(editableControl);
  controls.push({ id: 'manual-start', label: '起终点', role: 'start_finish', required: false, state: 'accepted', pixel: [100, 620], radiusPx: 22, serviceBaseSec: 0, servicePerPersonSec: 0 });
  const response = await page.request.patch(`/api/maps/${asset.id}`, { data: { version: state.version, controls } });
  expect(response.ok(), await response.text()).toBe(true); state = await response.json();
  return { asset, state };
}

export async function openSheet(page: Page, id: string, mobile = false) {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  if (mobile) await page.locator('#mobile-panel').click();
  await page.locator(`[data-map="${id}"]`).click();
  await expect(page.locator('#map-title')).toHaveText('教学核对');
}
