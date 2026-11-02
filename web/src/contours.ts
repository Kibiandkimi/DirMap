import { escapeHtml as e, type ContourHeightReference, type ContourReport, type MapState } from './types';
import { ImageViewport } from './image-viewport';
import { mapAssetUrl, workbenchKey } from './workbench';

type Selection = { id: string; heightM: number };
type Draft = { version: number; revision: number; interval: string; reference: ContourHeightReference;
  next: string; direction: string; values: Record<string, { height: string; use: boolean }> };

export function showContours(state: MapState, report: ContourReport,
  save: (contours: Selection[], intervalM: number | null, reference: ContourHeightReference) => Promise<void>) {
  const dialog = document.createElement('dialog'); dialog.className = 'contour-dialog';
  dialog.setAttribute('aria-label', '等高线辅助地形');
  const key = workbenchKey('contour-draft:'+state.assetId);
  let draft: Draft | null = null;
  try {
    const stored = JSON.parse(localStorage.getItem(key) ?? 'null');
    if (stored?.version === state.version && stored.revision === report.revision && stored.values) draft = stored;
  } catch { /* annotation still works without browser storage */ }
  const hints = report.contours.filter(c => c.calibrated && c.suggestedHeightM !== null).length;
  dialog.innerHTML = `<div class="dialog-heading"><h2>等高线辅助地形</h2><button class="icon-button" data-close aria-label="关闭等高线">×</button></div>
    <p class="muted compact">核对原图中的线条，再选择要贡献的等高线。<span data-draft-status>草稿保存在本浏览器。</span>已发布课程保留原高程快照。</p>
    ${report.contributionStale ? '<p class="notice-card">这张图的配准已改变。核对后重新贡献，会更新你此前的等高线位置。</p>' : ''}
    <div class="contour-layout"><div class="contour-map"><div class="sheet-photo-actions"><button class="soft-button" data-zoom="1.5">放大</button><button class="soft-button" data-zoom="0.667">缩小</button><button class="text-button" data-fit>全图</button></div><div class="editor-viewport" tabindex="0" aria-label="等高线原图，可拖动或双指缩放"><canvas aria-label="识别到的等高线"></canvas></div><div class="contour-sequence-indicator" hidden><strong data-sequence-label></strong><button class="text-button" data-map-undo>撤销</button><button class="text-button" data-stop-sequence>结束标高</button></div></div>
    <div class="contour-settings"><label>标高方式 <select data-height-reference aria-label="等高线标高方式"><option value="relative">按轨迹对齐相对标高</option><option value="reference">使用已知参考高程</option></select></label><p class="muted compact" data-reference-help></p>
    <label>图上等高距 / m <input data-interval type="number" min="0.1" max="100" step="any" placeholder="如 2" value="${e(draft?.interval ?? String(report.intervalM ?? ''))}"></label>
    <details class="contour-sequence-tools"><summary>连续标高</summary><p class="muted compact">在原图依次点线，自动填写高度并选中；同一条线不会重复递增。</p>
      <div class="contour-sequence-fields"><label>下一条 / m <input data-sequence-next type="number" min="-500" max="9000" step="any" value="${e(draft?.next ?? '')}" aria-label="下一条等高线高度"></label><label>标完以后 <select data-sequence-direction aria-label="连续标高方向"><option value="1">加一个等高距</option><option value="-1">减一个等高距</option><option value="0">保持同高</option></select></label></div>
      <label class="check-label"><input type="checkbox" data-sequence-mode>在原图点线标高</label><button class="text-button" data-sequence-undo disabled>撤销上次标高</button></details>
    <div class="contour-reference-tools"><span class="muted">${hints ? `${hints} 段有共享地形参考高度` : '当前服务尚无足够覆盖本图的高程参考，可先载入已有轨迹。'}</span><button class="text-button" data-suggestions ${hints ? '' : 'disabled'}>填入参考高度</button><button class="text-button" data-select-stable ${hints ? '' : 'disabled'}>选中参考较稳定的线</button></div>
    <label>筛选 <select data-filter aria-label="筛选等高线"><option value="all">全部线段</option><option value="reference">有参考高度</option><option value="stable">参考波动不超过等高距</option><option value="selected">已选线段</option></select></label><p class="muted compact" data-selection-count></p>
    <div class="contour-list">${report.contours.map((c, i) => {
      const stored = draft?.values[c.id], height = stored?.height ?? String(c.heightM ?? '');
      return `<div class="contour-row" data-contour="${e(c.id)}"><label><input type="checkbox" data-use ${(stored?.use ?? c.heightM !== null) && c.calibrated ? 'checked' : ''} ${c.calibrated ? '' : 'disabled'} aria-label="采用等高线 C${i+1}"><button class="text-button" data-focus-line="${e(c.id)}">C${i+1}</button></label><label><input type="number" data-height step="any" min="-500" max="9000" value="${e(height)}" placeholder="标高" aria-label="等高线 C${i+1} 高度"> m</label><small>${!c.calibrated ? '需补配准' : c.suggestedHeightM !== null ? `参考约 ${c.suggestedHeightM} m · 波动 ${c.suggestionSpreadM} m · 覆盖 ${Math.round(c.referenceCoverage*100)}%` : '请核对后标高'}</small></div>`;
    }).join('')}</div><p role="status" class="contour-status"></p><button class="primary-button wide" data-save>贡献已选等高线并重建</button></div></div>`;
  document.body.append(dialog); dialog.showModal();
  const field = <T extends HTMLElement>(selector: string) => dialog.querySelector<T>(selector)!;
  const rows = new Map(Array.from(dialog.querySelectorAll<HTMLElement>('[data-contour]')).map(row => [row.dataset.contour!, row]));
  const heightInput = (id: string) => rows.get(id)!.querySelector<HTMLInputElement>('[data-height]')!;
  const useInput = (id: string) => rows.get(id)!.querySelector<HTMLInputElement>('[data-use]')!;
  const interval = field<HTMLInputElement>('[data-interval]'), reference = field<HTMLSelectElement>('[data-height-reference]');
  const next = field<HTMLInputElement>('[data-sequence-next]'), direction = field<HTMLSelectElement>('[data-sequence-direction]');
  const enabled = field<HTMLInputElement>('[data-sequence-mode]'), filter = field<HTMLSelectElement>('[data-filter]');
  reference.value = draft?.reference ?? report.heightReference;
  direction.value = draft?.direction ?? '1';
  const canvas = field<HTMLCanvasElement>('canvas'), image = new Image(), context = canvas.getContext('2d')!;
  canvas.width = state.width; canvas.height = state.height;
  let selected: string | null = null, scale = 1, submitted = false, saving = false, lastWritten = '';
  const history: { id: string; height: string; use: boolean; next: string }[] = [];
  const tagged = new Set(report.contours.filter(c => draft?.values[c.id]?.use && draft.values[c.id].height !== '').map(c => c.id));
  const stable = (c: ContourReport['contours'][number]) => c.calibrated && c.suggestedHeightM !== null &&
    c.suggestionSpreadM !== null && Number(interval.value) > 0 && c.suggestionSpreadM <= Number(interval.value);
  function referenceOffset(): number | null {
    if (reference.value === 'reference') return 0;
    const marked = report.contours.filter(c => heightInput(c.id).value.trim() && heightInput(c.id).checkValidity());
    if (!marked.length) return 0;
    const anchors = marked.filter(c => c.calibrated && c.suggestedHeightM !== null);
    const focused = anchors.find(c => c.id === selected);
    if (focused) return focused.suggestedHeightM! - Number(heightInput(focused.id).value);
    if (!anchors.length) return null;
    const offsets = anchors.map(c => c.suggestedHeightM! - Number(heightInput(c.id).value)).sort((a, b) => a-b);
    if (offsets[offsets.length-1]-offsets[0] > 2*Math.max(2, Number(interval.value))) return null;
    return (offsets[Math.floor((offsets.length-1)/2)]+offsets[Math.floor(offsets.length/2)])/2;
  }
  function persist() {
    try { lastWritten = JSON.stringify({ version: state.version, revision: report.revision, interval: interval.value,
      reference: reference.value, next: next.value, direction: direction.value,
      values: Object.fromEntries(report.contours.map(c => [c.id, { height: heightInput(c.id).value, use: useInput(c.id).checked }])) }); localStorage.setItem(key, lastWritten);
      field('[data-draft-status]').textContent = '本机草稿已保存。'; }
    catch { field('[data-draft-status]').textContent = '浏览器未能保存草稿，请保持此页打开。'; }
  }
  function draw() {
    context.clearRect(0, 0, canvas.width, canvas.height);
    if (image.complete && image.naturalWidth) context.drawImage(image, 0, 0, canvas.width, canvas.height);
    for (const curve of report.contours) {
      context.strokeStyle = curve.id === selected ? '#f34d12' : useInput(curve.id).checked ? '#168456' : curve.calibrated ? '#0876c9' : '#8a8e86';
      context.lineWidth = (curve.id === selected ? 4 : 1.2)/scale;
      context.setLineDash(curve.calibrated ? [] : [5/scale, 4/scale]);
      context.beginPath(); curve.pixels.forEach(([x, y], i) => i ? context.lineTo(x, y) : context.moveTo(x, y)); context.stroke();
    }
    context.setLineDash([]);
  }
  function update() {
    const chosen = report.contours.filter(c => useInput(c.id).checked).length;
    field('[data-selection-count]').textContent = `已选 ${chosen} / ${report.contours.length} 段 · 绿色为已选线条`;
    field('[data-reference-help]').textContent = reference.value === 'relative'
      ? '只约束线与线之间的高差，整体高度随已有轨迹对齐；没有重叠参考时会等待，不生成零海拔地形。'
      : '这些数值直接约束高程。仅在已核对可靠标高时使用，不能把相对的零点当作海拔。';
    for (const c of report.contours) rows.get(c.id)!.hidden =
      filter.value === 'reference' ? c.suggestedHeightM === null : filter.value === 'stable' ? !stable(c)
        : filter.value === 'selected' ? !useInput(c.id).checked : false;
    field<HTMLButtonElement>('[data-sequence-undo]').disabled = !history.length;
    field<HTMLButtonElement>('[data-map-undo]').disabled = !history.length;
    field('.contour-sequence-indicator').hidden = !enabled.checked;
    field('[data-sequence-label]').textContent = next.value ? `下一条 ${next.value} m` : '请先填写下一条高度';
    draw();
  }
  function focus(id: string, zoom = false) {
    selected = id;
    for (const [key, row] of rows) row.classList.toggle('selected', key === id);
    const curve = report.contours.find(c => c.id === id)!;
    if (rows.get(id)!.hidden) filter.value = 'all';
    update();
    const list = field('.contour-list'), row = rows.get(id)!;
    const bounds = list.getBoundingClientRect(), at = row.getBoundingClientRect();
    if (at.top < bounds.top) list.scrollTop -= bounds.top-at.top;
    else if (at.bottom > bounds.bottom) list.scrollTop += at.bottom-bounds.bottom;
    if (!next.value) {
      const offset = referenceOffset();
      next.value = heightInput(id).value || (curve.suggestedHeightM === null || offset === null ? '' : String(Math.round((curve.suggestedHeightM-offset)*1e6)/1e6));
    }
    if (zoom) {
      const xs = curve.pixels.map(p => p[0]), ys = curve.pixels.map(p => p[1]);
      const left = Math.min(...xs), right = Math.max(...xs), top = Math.min(...ys), bottom = Math.max(...ys);
      viewport.focus([(left+right)/2, (top+bottom)/2], Math.max(24, (right-left)*.6, (bottom-top)*.6));
    }
    persist();
  }
  function annotate(id: string) {
    const status = field('[role="status"]');
    if (useInput(id).disabled) { status.textContent = '这条线需要先补配准'; return; }
    if (tagged.has(id)) { status.textContent = '本次已标过这条线；可撤销或直接编辑其高度。'; return; }
    const increment = Number(direction.value);
    if (!next.value.trim() || !next.checkValidity()) { status.textContent = '请先填写有效的下一条高度'; next.focus(); return; }
    if (increment && (!interval.value || !interval.checkValidity())) { status.textContent = '请先填写图上的等高距'; interval.focus(); return; }
    history.push({ id, height: heightInput(id).value, use: useInput(id).checked, next: next.value }); tagged.add(id);
    heightInput(id).value = next.value; useInput(id).checked = true;
    next.value = String(Math.round((Number(next.value)+increment*Number(interval.value))*1e6)/1e6);
    status.textContent = `已标高 ${heightInput(id).value} m；下一条 ${next.value} m。`;
    persist(); update();
  }
  const viewport = new ImageViewport(field('.editor-viewport'), canvas, {
    drawing: () => false, stroke: () => {}, change: value => { scale = value; draw(); },
    tap: point => {
      if (saving) return;
      let best: { id: string; distance: number } | null = null;
      for (const curve of report.contours) for (let i = 1; i < curve.pixels.length; i++) {
        const a = curve.pixels[i-1], b = curve.pixels[i], dx = b[0]-a[0], dy = b[1]-a[1];
        const t = Math.max(0, Math.min(1, ((point[0]-a[0])*dx+(point[1]-a[1])*dy)/Math.max(1e-9, dx*dx+dy*dy)));
        const distance = Math.hypot(point[0]-a[0]-t*dx, point[1]-a[1]-t*dy);
        if (distance < 14/scale && (!best || distance < best.distance)) best = { id: curve.id, distance };
      }
      if (best) { focus(best.id); if (enabled.checked) annotate(best.id); }
    },
  });
  image.onload = draw; image.src = mapAssetUrl(state, 'working.jpg');
  field<HTMLButtonElement>('[data-close]').onclick = () => dialog.close();
  dialog.addEventListener('close', () => { if (!submitted) persist(); viewport.destroy(); dialog.remove(); }, { once: true });
  field<HTMLButtonElement>('[data-fit]').onclick = () => viewport.fit();
  dialog.querySelectorAll<HTMLButtonElement>('[data-zoom]').forEach(button => button.onclick = () => viewport.zoom(Number(button.dataset.zoom)));
  dialog.querySelectorAll<HTMLButtonElement>('[data-focus-line]').forEach(button => button.onclick = event => { event.preventDefault(); focus(button.dataset.focusLine!, true); });
  dialog.addEventListener('input', event => {
    const input = event.target as HTMLElement;
    if (input.closest('[data-contour]') || input === next) { history.length = 0; tagged.clear(); }
    persist(); update();
  });
  dialog.addEventListener('change', event => {
    persist(); update();
    if (event.target === enabled && enabled.checked) field('.contour-map').scrollIntoView({ block: 'nearest' });
  });
  const undo = () => {
    const old = history.pop(); if (!old) return;
    heightInput(old.id).value = old.height; useInput(old.id).checked = old.use; next.value = old.next; tagged.delete(old.id);
    focus(old.id); persist(); update(); field('[role="status"]').textContent = '已撤销上次标高';
  };
  field<HTMLButtonElement>('[data-sequence-undo]').onclick = undo;
  field<HTMLButtonElement>('[data-map-undo]').onclick = undo;
  field<HTMLButtonElement>('[data-stop-sequence]').onclick = () => { enabled.checked = false; update(); };
  function fillReferences(choose = false) {
    if (choose && (!interval.value || !interval.checkValidity())) { field('[role="status"]').textContent = '先填写等高距，再按参考波动筛选'; interval.focus(); return; }
    const offset = referenceOffset();
    if (offset === null) { field('[role="status"]').textContent = '请先点选一条已有标高和参考高度的线，确定当前相对标高的零点。'; return; }
    let count = 0;
    for (const c of report.contours) {
      if (!c.calibrated || c.suggestedHeightM === null || (choose && !stable(c))) continue;
      if (!heightInput(c.id).value) heightInput(c.id).value = String(Math.round((c.suggestedHeightM-offset)*1e6)/1e6);
      if (choose) useInput(c.id).checked = true;
      count++;
    }
    history.length = 0; tagged.clear(); persist(); update();
    field('[role="status"]').textContent = choose ? `已选 ${count} 段参考较稳定的线，仍请对照原图核对线条与高差。` : `已填入 ${count} 段参考值，可按等高距连续校核。`;
  }
  field<HTMLButtonElement>('[data-suggestions]').onclick = () => fillReferences();
  field<HTMLButtonElement>('[data-select-stable]').onclick = () => fillReferences(true);
  field<HTMLButtonElement>('[data-save]').onclick = async event => {
    const button = event.currentTarget as HTMLButtonElement, status = field('[role="status"]');
    let locks: { element: HTMLInputElement | HTMLSelectElement | HTMLButtonElement; disabled: boolean }[] = [];
    try {
      const chosen: Selection[] = [];
      for (const c of report.contours) {
        if (!useInput(c.id).checked || useInput(c.id).disabled) continue;
        const input = heightInput(c.id);
        if (!input.value.trim() || !input.checkValidity()) { focus(c.id); input.focus(); throw new Error('请填写每条已选等高线的有效标高'); }
        chosen.push({ id: c.id, heightM: Number(input.value) });
      }
      if (!chosen.length) throw new Error('先勾选已核对的等高线');
      if (!interval.checkValidity()) throw new Error('请填写有效的等高距');
      saving = true;
      locks = Array.from(dialog.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLButtonElement>('input,select,button:not([data-close])')).map(element => ({ element, disabled: element.disabled }));
      for (const { element } of locks) element.disabled = true;
      status.textContent = '正在保存等高线并重建共享地形…';
      await save(chosen, interval.value.trim() ? Number(interval.value) : null, reference.value as ContourHeightReference);
      submitted = true; try { if (localStorage.getItem(key) === lastWritten) localStorage.removeItem(key); } catch { /* optional local draft */ }
      dialog.close();
    } catch (error) { status.textContent = (error as Error).message; button.disabled = false; }
    finally { saving = false; for (const { element, disabled } of locks) element.disabled = disabled; }
  };
  update();
}
