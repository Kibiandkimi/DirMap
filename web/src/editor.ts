import { escapeHtml, type Approach, type Control, type MapState, type Pixel, type Portal, type Surface, type TerrainEdit } from './types';
import { ImageViewport } from './image-viewport';
import { mapAssetUrl } from './workbench';

type Mode = 'inspect' | 'control' | 'approach' | 'open' | 'close' | 'anchor' | 'portal' | 'field';
export class PixelEditor {
  private dialog = document.createElement('dialog');
  private canvas!: HTMLCanvasElement;
  private image = new Image();
  private terrain = new Image();
  private controls: Control[];
  private edits: TerrainEdit[];
  private surfaces: Surface[];
  private portals: Portal[];
  private surfaceId = 'ground';
  private referencePaths: { id: string; label: string; level: number; pixels: Pixel[]; suggestedRadiusPx: number; usable: boolean; scope: string }[] = [];
  private referenceIndex = -1;
  private mode: Mode = 'inspect';
  private selected: string | null = null;
  private stroke: Pixel[] | null = null;
  private viewport!: ImageViewport;
  private viewScale = 1;
  private panOnly = false;
  private pendingPixel: Pixel | null = null;
  private showTerrain = false;
  private showStairs = false;
  private history: { controls: Control[]; edits: TerrainEdit[]; surfaces: Surface[]; portals: Portal[] }[] = [];
  private picked = false;

  constructor(private state: MapState, private onSave: (controls: Control[], edits: TerrainEdit[], surfaces?: Surface[], portals?: Portal[]) => Promise<void>, private onAnchor: (pixel: Pixel) => void, private suggestions: Approach[] = [], private onDismiss?: () => void) {
    this.controls = structuredClone(state.controls);
    this.edits = structuredClone(state.edits);
    this.surfaces = structuredClone(state.surfaces ?? []);
    this.portals = structuredClone(state.portals ?? []);
  }
  open(mode: Mode = 'inspect', selected: string | null = null) {
    this.mode = mode; this.selected = selected;
    this.surfaceId = this.controls.find(c => c.id === selected)?.surfaceId ?? 'ground';
    this.dialog.className = 'editor-dialog';
    this.dialog.innerHTML = `<div class="dialog-heading"><div><span class="eyebrow">原图校核</span><h2>${escapeHtml(this.state.name)}</h2></div><button data-close class="icon-button" aria-label="关闭原图">×</button></div>
      <div class="editor-toolbar"><select id="edit-mode" aria-label="编辑操作"><option value="inspect">检查 / 选择目标</option><option value="control">新增目标</option><option value="approach">选择目标到访位置</option><option value="close">标记封闭路段</option><option value="open">确认开放通道</option><option value="portal">标记换层入口</option><option value="anchor">添加配准同名点</option><option value="field" hidden>标记所站位置</option></select>
      <label class="brush-label">笔刷 <input id="brush-size" type="number" value="6" min="1" max="100" aria-label="笔刷半径"></label><button id="undo-edit" class="soft-button">撤销</button><button id="pan-image" class="soft-button" aria-pressed="false">移动地图</button><button id="zoom-out" class="icon-button" aria-label="缩小原图">−</button><button id="zoom-in" class="icon-button" aria-label="放大原图">＋</button><button id="fit-image" class="text-button">全图</button><span id="image-zoom" class="muted"></span><label class="check-label"><input id="editor-terrain" type="checkbox">识别障碍</label></div>
      <details class="layer-settings"><summary>桥隧与层级</summary><div id="layer-controls"></div></details><p id="editor-hint" class="muted editor-hint" role="status"></p><div class="editor-viewport" tabindex="0" aria-label="原图视野，可拖动、滚轮或双指缩放"><canvas aria-label="原图及目标点编辑画布"></canvas></div>
      <div id="point-confirmation" class="point-confirmation hidden"><span id="point-preview" role="status"></span><button id="confirm-point" class="primary-button" disabled>确认选点</button><button id="cancel-point" class="text-button">取消选点</button></div>
      <div class="editor-footer"><span id="edit-selection" class="muted">点击目标可修改编号、删除或确认</span><button id="save-edits" class="primary-button">保存修改</button></div>`;
    document.body.append(this.dialog); this.dialog.showModal();
    if (this.state.sourceImage?.resized) {
      const source = this.state.sourceImage;
      this.dialog.querySelector('.editor-toolbar')!.insertAdjacentHTML('afterend',
        `<p class="muted compact">以 ${source.workingSize.join(' × ')} 像素校核；<a href="${escapeHtml(mapAssetUrl(this.state, 'original', false))}" target="_blank" rel="noreferrer">查看 ${source.orientedSourceSize.join(' × ')} 像素原文件</a>。</p>`);
    }
    if (this.state.terrainFeatures?.length) this.dialog.querySelector('.editor-toolbar')!.insertAdjacentHTML('beforeend',
      '<label class="check-label"><input id="editor-stairs" type="checkbox">楼梯与下层候选</label>');
    this.canvas = this.dialog.querySelector('canvas')!;
    this.canvas.width = this.state.width; this.canvas.height = this.state.height;
    this.viewport = new ImageViewport(this.dialog.querySelector('.editor-viewport')!, this.canvas, {
      tap: point => { if (!this.panOnly) this.pickPoint(point); },
      drawing: () => !this.panOnly && ['open', 'close'].includes(this.mode),
      stroke: (phase, point) => {
        if (phase === 'start') this.stroke = [point!];
        else if (phase === 'cancel') this.stroke = null;
        else if (this.stroke && point) this.stroke.push(point);
        if (phase === 'end') this.endStroke();
        this.draw();
      },
      change: scale => {
        if (this.viewScale === scale) return; // A pan only moves the existing canvas.
        this.viewScale = scale; this.dialog.querySelector('#image-zoom')!.textContent = `${Math.round(scale * 100)}%`; this.draw();
      },
    });
    const select = this.dialog.querySelector<HTMLSelectElement>('#edit-mode')!; select.value = mode;
    select.onchange = () => { this.mode = select.value as Mode; this.pendingPixel = null; this.panOnly = false; this.hint(); this.renderPick(); this.draw(); };
    if (mode === 'field') { select.disabled = true; this.dialog.querySelector('.layer-settings')!.classList.add('hidden'); }
    this.dialog.querySelector<HTMLButtonElement>('[data-close]')!.onclick = () => this.close();
    this.dialog.addEventListener('cancel', event => { event.preventDefault(); this.close(); });
    this.dialog.querySelector<HTMLButtonElement>('#zoom-in')!.onclick = () => this.viewport.zoom(1.4);
    this.dialog.querySelector<HTMLButtonElement>('#zoom-out')!.onclick = () => this.viewport.zoom(1 / 1.4);
    this.dialog.querySelector<HTMLButtonElement>('#fit-image')!.onclick = () => this.viewport.fit();
    this.dialog.querySelector<HTMLButtonElement>('#pan-image')!.onclick = () => { this.panOnly = !this.panOnly; this.hint(); };
    this.dialog.querySelector<HTMLButtonElement>('#confirm-point')!.onclick = () => this.confirmPoint();
    this.dialog.querySelector<HTMLButtonElement>('#cancel-point')!.onclick = () => { this.pendingPixel = null; this.renderPick(); this.draw(); };
    this.dialog.querySelector<HTMLButtonElement>('#undo-edit')!.onclick = () => { const previous = this.history.pop(); if (previous) { this.controls = previous.controls; this.edits = previous.edits; this.surfaces = previous.surfaces; this.portals = previous.portals; this.renderLayers(); this.draw(); this.selection(); } };
    this.dialog.querySelector<HTMLInputElement>('#editor-terrain')!.onchange = event => { this.showTerrain = (event.target as HTMLInputElement).checked; this.draw(); };
    const stairToggle = this.dialog.querySelector<HTMLInputElement>('#editor-stairs');
    if (stairToggle) stairToggle.onchange = () => { this.showStairs = stairToggle.checked; this.draw(); };
    const source = this.state.sourceImage;
    const transformed = source && (source.resized || source.exifOrientation !== 1);
    this.image.onload = () => this.draw();
    this.image.src = mapAssetUrl(this.state, transformed ? 'working.jpg' : 'original', !!transformed);
    this.terrain.onload = () => this.draw(); this.terrain.src = mapAssetUrl(this.state, 'terrain.png');
    this.dialog.querySelector<HTMLButtonElement>('#save-edits')!.onclick = async () => {
      const button = this.dialog.querySelector<HTMLButtonElement>('#save-edits')!; button.disabled = true;
      try { await this.onSave(this.controls, this.edits, this.surfaces, this.portals); this.close(); }
      catch (error) { this.dialog.querySelector('#editor-hint')!.textContent = String(error); button.disabled = false; }
    };
    this.renderLayers(); this.hint(); this.selection(); this.renderPick();
  }
  private remember() { this.history.push({ controls: structuredClone(this.controls), edits: structuredClone(this.edits), surfaces: structuredClone(this.surfaces), portals: structuredClone(this.portals) }); }
  private catalogue(): Surface[] { return [{ id: 'ground', label: '地面', level: 0, accepted: true }, ...this.surfaces]; }
  private renderLayers() {
    const catalogue = this.catalogue();
    if (!catalogue.some(s => s.id === this.surfaceId)) this.surfaceId = 'ground';
    const options = catalogue.map(s => `<option value="${escapeHtml(s.id)}">${escapeHtml(s.label)}${s.accepted ? '' : '（待核验）'}</option>`).join('');
    const container = this.dialog.querySelector('#layer-controls')!;
    container.innerHTML = `<p class="muted compact">分别绘制可行走的层级，再在真实入口连接。层级在平面上重叠不会自动互通；待核验的范围和入口不加入路网。</p><div class="editor-toolbar"><label>当前层级 <select id="edit-surface" aria-label="当前编辑层级">${options}</select></label><label>通行耗时系数 <input id="surface-cost" type="number" min="1" max="10" step="0.1" value="1"></label><label class="check-label"><input id="separate-ground" type="checkbox" checked>从地面路网分离</label></div><p class="muted compact">若桥下仍可走，切回地面绘制下方通道。入口只连接选定两层；零长度表示已经相接的入口，楼梯可填写额外长度与耗时系数。</p><div class="editor-toolbar"><input id="new-surface-label" aria-label="新层级名称" placeholder="例如：桥上、地下通道" maxlength="50"><label>相对层级 <input id="new-surface-level" type="number" value="1" min="-20" max="20" step="1"></label><button id="add-surface" class="soft-button">新增层级</button></div><div id="surface-list">${this.surfaces.map(s => `<div class="surface-row"><label class="check-label"><input type="checkbox" data-confirm-surface="${escapeHtml(s.id)}" ${s.accepted ? 'checked' : ''}>已核验 ${escapeHtml(s.label)} 的可通行范围</label><button type="button" class="text-button danger" data-remove-surface="${escapeHtml(s.id)}">删除层级</button></div>`).join('')}</div><div class="editor-toolbar"><label>入口连接到 <select id="portal-target" aria-label="入口目标层级">${catalogue.filter(s => s.id !== this.surfaceId).map(s => `<option value="${escapeHtml(s.id)}">${escapeHtml(s.label)}</option>`).join('')}</select></label><label>额外长度（米）<input id="portal-length" type="number" value="0" min="0" max="1000" step="0.5"></label><label>耗时系数 <input id="portal-cost" type="number" value="1" min="1" max="10" step="0.1"></label><label class="check-label"><input id="portal-accepted" type="checkbox">已核对两层在此相连</label></div><button id="load-layer-reference" class="soft-button">查看校园参照的桥隧位置</button><div id="layer-reference-list"></div>`;
    const select = container.querySelector<HTMLSelectElement>('#edit-surface')!;
    select.value = this.surfaceId;
    container.querySelector<HTMLInputElement>('#separate-ground')!.disabled = this.surfaceId === 'ground';
    select.onchange = () => { this.surfaceId = select.value; this.renderLayers(); this.hint(); this.draw(); };
    container.querySelector<HTMLButtonElement>('#add-surface')!.onclick = () => {
      const label = container.querySelector<HTMLInputElement>('#new-surface-label')!.value.trim();
      const level = Number(container.querySelector<HTMLInputElement>('#new-surface-level')!.value);
      if (!label || !Number.isInteger(level) || level < -20 || level > 20) { this.dialog.querySelector('#editor-hint')!.textContent = '请填写层级名称与 -20 到 20 的整数层级。'; return; }
      this.remember(); this.surfaceId = 'surface-'+crypto.randomUUID().slice(0, 8);
      this.surfaces.push({ id: this.surfaceId, label, level, accepted: false }); this.renderLayers(); this.hint(); this.draw();
    };
    container.querySelectorAll<HTMLInputElement>('[data-confirm-surface]').forEach(box => box.onchange = () => {
      this.remember(); const id = box.dataset.confirmSurface!;
      this.surfaces.find(s => s.id === id)!.accepted = box.checked;
      if (!box.checked) this.portals.forEach(p => { if (p.fromSurface === id || p.toSurface === id) p.accepted = false; });
      this.draw(); this.selection();
    });
    container.querySelectorAll<HTMLButtonElement>('[data-remove-surface]').forEach(button => button.onclick = () => {
      this.remember(); const id = button.dataset.removeSurface!;
      this.surfaces = this.surfaces.filter(s => s.id !== id);
      this.edits = this.edits.filter(e => e.surfaceId !== id);
      this.portals = this.portals.filter(p => p.fromSurface !== id && p.toSurface !== id);
      this.controls.forEach(c => { if (c.surfaceId === id) c.surfaceId = null; });
      this.renderLayers(); this.draw(); this.selection();
    });
    container.querySelector<HTMLButtonElement>('#load-layer-reference')!.onclick = async () => {
      try {
        const response = await fetch(`/api/maps/${this.state.assetId}/layer-candidates`);
        if (!response.ok) throw new Error('无法加载参照桥隧位置');
        this.referencePaths = await response.json();
        const target = container.querySelector('#layer-reference-list')!;
        target.innerHTML = `<p class="muted compact">参照可能存在年代和位置误差，不能直接当作通行许可。载入的范围仍为待核验草稿。</p>${this.referencePaths.map((p, i) => `<div class="surface-row"><button class="text-button" data-preview-passage="${i}">${escapeHtml(p.label)} · ${escapeHtml(p.id)}</button><button class="soft-button" data-import-passage="${i}" ${p.usable ? '' : 'disabled'}>${p.usable ? '载入草稿' : '参照标为不可通行'}</button></div>`).join('') || '<p>当前范围没有可用参照。</p>'}`;
        target.querySelectorAll<HTMLButtonElement>('[data-preview-passage]').forEach(button => button.onclick = () => { this.referenceIndex = Number(button.dataset.previewPassage); this.draw(); });
        target.querySelectorAll<HTMLButtonElement>('[data-import-passage]').forEach(button => button.onclick = () => {
          const p = this.referencePaths[Number(button.dataset.importPassage)]; this.remember();
          this.surfaceId = 'surface-'+crypto.randomUUID().slice(0, 8);
          this.surfaces.push({ id: this.surfaceId, label: p.label, level: p.level, accepted: false });
          this.edits.push({ kind: 'open', surfaceId: this.surfaceId, pixels: structuredClone(p.pixels), radiusPx: p.suggestedRadiusPx, cost: 1, separateGround: p.level > 0 });
          this.renderLayers(); this.draw(); this.hint();
        });
      } catch (error) { this.dialog.querySelector('#editor-hint')!.textContent = String(error); }
    };
  }
  private pickPoint(point: Pixel) {
    if (this.mode !== 'inspect') {
      if (this.mode === 'approach' && !this.controls.some(c => c.id === this.selected)) { this.dialog.querySelector('#editor-hint')!.textContent = '请先切到“检查”，点击需要设置到访位置的目标。'; return; }
      this.pendingPixel = point; this.renderPick(); this.draw(); return;
    }
    this.applyPoint(point);
  }
  private renderPick() {
    const pickOnly = this.mode === 'anchor' || this.mode === 'field';
    const picking = !!this.pendingPixel || pickOnly;
    this.dialog.querySelector('#point-confirmation')!.classList.toggle('hidden', !picking);
    this.dialog.querySelector('#edit-selection')!.classList.toggle('hidden', picking);
    const save = this.dialog.querySelector<HTMLButtonElement>('#save-edits')!;
    save.classList.toggle('hidden', pickOnly); save.disabled = !!this.pendingPixel;
    this.dialog.querySelector<HTMLButtonElement>('#confirm-point')!.disabled = !this.pendingPixel;
    this.dialog.querySelector('#point-preview')!.textContent = this.pendingPixel
      ? `已选原图位置 ${this.pendingPixel.map(n => n.toFixed(1)).join(', ')}。可继续点选调整，再确认。`
      : '点选后显示标记，确认前可反复调整。';
  }
  private confirmPoint() {
    if (!this.pendingPixel) return;
    const point = this.pendingPixel;
    this.pendingPixel = null; this.applyPoint(point);
    if (this.dialog.isConnected) { this.renderPick(); this.draw(); }
  }
  private applyPoint(point: Pixel) {
    if (this.mode === 'anchor' || this.mode === 'field') { this.picked = true; this.onAnchor(point); this.close(); return; }
    if (this.mode === 'portal') {
      const target = this.dialog.querySelector<HTMLSelectElement>('#portal-target')!.value;
      const length = Number(this.dialog.querySelector<HTMLInputElement>('#portal-length')!.value);
      const cost = Number(this.dialog.querySelector<HTMLInputElement>('#portal-cost')!.value);
      if (!target || target === this.surfaceId || !Number.isFinite(length) || length < 0 || !Number.isFinite(cost) || cost < 1) { this.dialog.querySelector('#editor-hint')!.textContent = '请在层级设置中选择入口连接的另一层及有效长度。'; return; }
      this.remember(); const id = 'portal-'+crypto.randomUUID().slice(0, 8);
      this.portals.push({ id, label: '换层入口', fromSurface: this.surfaceId, toSurface: target, pixel: point,
        radiusPx: Math.min(100, Math.max(1, Number(this.dialog.querySelector<HTMLInputElement>('#brush-size')!.value) || 6)+2),
        lengthM: length, costFactor: cost, accepted: this.dialog.querySelector<HTMLInputElement>('#portal-accepted')!.checked });
      this.selected = id; this.draw(); this.selection(); return;
    }
    if (this.mode === 'control') {
      this.remember(); const id = `manual-${crypto.randomUUID().slice(0, 8)}`;
      this.controls.push({ id, label: `新点${this.controls.length + 1}`, pixel: point, surfaceId: this.surfaceId, required: true, radiusPx: this.state.width / 60, state: 'accepted', serviceBaseSec: 5, servicePerPersonSec: 1 });
      this.selected = id;
    } else if (this.mode === 'approach') {
      const control = this.controls.find(c => c.id === this.selected);
      if (control) { this.remember(); control.approachPixel = point; control.surfaceId = this.surfaceId; control.state = 'accepted'; }
      else this.dialog.querySelector('#editor-hint')!.textContent = '请先切到“检查”，点击需要设置到访位置的目标。';
    } else {
      const portal = this.portals.find(p => Math.hypot(p.pixel[0]-point[0], p.pixel[1]-point[1]) < Math.max(8, this.state.width/150));
      if (portal) { this.selected = portal.id; this.draw(); this.selection(); return; }
      let nearest = this.controls[0], distance = Infinity;
      for (const c of this.controls) { const d = Math.hypot(c.pixel[0] - point[0], c.pixel[1] - point[1]); if (d < distance) { nearest = c; distance = d; } }
      this.selected = nearest && distance < Math.max(nearest.radiusPx * 1.7, 25) ? nearest.id : null;
    }
    this.draw(); this.selection();
  }
  private endStroke() {
    if (!this.stroke) return;
    this.remember();
    this.edits.push({ kind: this.mode as 'open' | 'close', pixels: this.stroke, radiusPx: Math.max(1, Math.min(100, Number(this.dialog.querySelector<HTMLInputElement>('#brush-size')!.value) || 6)),
      cost: Math.max(1, Math.min(10, Number(this.dialog.querySelector<HTMLInputElement>('#surface-cost')!.value) || 1)), surfaceId: this.surfaceId,
      separateGround: this.mode === 'open' && this.surfaceId !== 'ground' && this.dialog.querySelector<HTMLInputElement>('#separate-ground')!.checked });
    this.stroke = null; this.draw();
  }
  private hint() {
    const messages: Record<Mode, string> = { inspect: '点击圈心或入口选择对象。', control: '点选新增目标的位置，再确认。', approach: '点选实际可以到访的位置，再确认；保留原圈心。', open: '沿当前层级可行走的区域描画；“移动地图”可拖动视野。', close: '沿当前层级的禁行线描画；“移动地图”可拖动视野。', anchor: '点选原图中的建筑角或路口，确认后再到标准地图选同一位置。', field: '点选你此刻所站位置在原图上的对应点，核对标记后确认。', portal: '在层级设置中选择另一层，再点选入口位置并确认。' };
    this.dialog.querySelector('#editor-hint')!.textContent = this.panOnly ? '移动模式：拖动原图；滚轮或双指缩放。再次点击“移动地图”继续编辑。' : `${messages[this.mode]}${['open', 'close'].includes(this.mode) ? '' : '拖动可平移；'}滚轮或双指可缩放。`;
    this.dialog.querySelector('#pan-image')!.setAttribute('aria-pressed', String(this.panOnly));
    this.canvas.style.cursor = this.panOnly ? 'grab' : 'crosshair';
  }
  private selection() {
    const el = this.dialog.querySelector('#edit-selection')!;
    const portal = this.portals.find(p => p.id === this.selected);
    if (portal) {
      const names = new Map(this.catalogue().map(s => [s.id, s.label]));
      el.innerHTML = `<label>入口名称 <input id="portal-label" value="${escapeHtml(portal.label)}" maxlength="50"></label><span>${escapeHtml(names.get(portal.fromSurface))} ↔ ${escapeHtml(names.get(portal.toSurface))}</span><label class="check-label"><input id="confirm-portal" type="checkbox" ${portal.accepted ? 'checked' : ''}>已核对两层确实相连</label><button id="delete-portal" class="text-button danger">删除入口</button>`;
      el.querySelector<HTMLInputElement>('#portal-label')!.onchange = event => { portal.label = (event.target as HTMLInputElement).value.trim() || '换层入口'; this.draw(); };
      el.querySelector<HTMLInputElement>('#confirm-portal')!.onchange = event => { this.remember(); portal.accepted = (event.target as HTMLInputElement).checked; this.draw(); };
      el.querySelector<HTMLButtonElement>('#delete-portal')!.onclick = () => { this.remember(); this.portals = this.portals.filter(p => p.id !== portal.id); this.selected = null; this.draw(); this.selection(); };
      return;
    }
    const control = this.controls.find(c => c.id === this.selected);
    if (!control) { el.textContent = '点击目标可修改编号、删除或确认'; return; }
    const suggestion = this.suggestions.find(a => a.controlId === control.id && a.automatic);
    el.innerHTML = `<label>编号 <input id="control-label" value="${escapeHtml(control.label)}" maxlength="40"></label><label>类型 <select id="control-role"><option value="control">目标</option><option value="start">起点</option><option value="finish">终点</option><option value="start_finish">起终点</option></select></label><button id="confirm-control" class="soft-button">确认目标</button>${suggestion ? `<button id="accept-approach" class="soft-button">采用建议到访位置（距圈心 ${suggestion.offsetM.toFixed(1)} m）</button>` : ''}<button id="delete-control" class="text-button danger">删除</button>`;
    const role = el.querySelector<HTMLSelectElement>('#control-role')!; role.value = control.role ?? 'control';
    el.insertAdjacentHTML('beforeend', `<label>到访层级 <select id="control-surface"><option value="">未指定</option>${this.catalogue().map(s => `<option value="${escapeHtml(s.id)}">${escapeHtml(s.label)}</option>`).join('')}</select></label>`);
    const surface = el.querySelector<HTMLSelectElement>('#control-surface')!; surface.value = control.surfaceId ?? '';
    surface.onchange = () => { this.remember(); control.surfaceId = surface.value || null; this.draw(); };
    role.onchange = () => { this.remember(); control.role = role.value as Control['role']; control.required = control.role === 'control'; this.draw(); };
    el.querySelector<HTMLButtonElement>('#accept-approach')?.addEventListener('click', () => {
      this.remember(); control.approachPixel = [...suggestion!.pixel]; control.surfaceId = suggestion!.surfaceId ?? null; control.state = 'accepted'; this.draw();
      this.dialog.querySelector('#editor-hint')!.textContent = '已采用到访位置，原圈心保持原位。保存后重新规划。';
    });
    el.querySelector<HTMLInputElement>('#control-label')!.onchange = event => { control.label = (event.target as HTMLInputElement).value.trim() || control.label; this.draw(); };
    el.querySelector<HTMLButtonElement>('#confirm-control')!.onclick = () => { this.remember(); control.state = 'accepted'; this.draw(); this.dialog.querySelector('#editor-hint')!.textContent = '目标已确认，点击“保存修改”提交。'; };
    el.querySelector<HTMLButtonElement>('#delete-control')!.onclick = () => { this.remember(); this.controls = this.controls.filter(c => c.id !== control.id); this.selected = null; this.draw(); this.selection(); };
  }
  private draw() {
    const ctx = this.canvas.getContext('2d')!; ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    if (this.image.complete && this.image.naturalWidth) ctx.drawImage(this.image, 0, 0, this.state.width, this.state.height);
    if (this.showTerrain && this.terrain.complete && this.terrain.naturalWidth) ctx.drawImage(this.terrain, 0, 0, this.state.width, this.state.height);
    if (this.showStairs) {
      for (const feature of this.state.terrainFeatures ?? []) {
        ctx.strokeStyle = feature.kind === 'stairs' ? '#bd7208' : '#287ac0'; ctx.fillStyle = feature.kind === 'stairs' ? '#efb84c55' : '#509adb22'; ctx.lineWidth = Math.max(2, this.state.width / 650);
        ctx.beginPath(); feature.polygon.forEach(([x, y], i) => i ? ctx.lineTo(x, y) : ctx.moveTo(x, y));
        ctx.closePath(); ctx.fill(); ctx.stroke();
      }
    }
    const strokes = [...this.edits, ...(this.stroke ? [{ kind: this.mode, pixels: this.stroke, surfaceId: this.surfaceId, radiusPx: Number(this.dialog.querySelector<HTMLInputElement>('#brush-size')!.value) || 6 }] : [])];
    for (const edit of strokes) { if ((edit.surfaceId ?? 'ground') !== this.surfaceId && edit.surfaceId !== '*') continue; ctx.strokeStyle = edit.kind === 'close' ? '#ef4444aa' : this.surfaceId === 'ground' ? '#16a077aa' : '#2779caaa'; ctx.fillStyle = ctx.strokeStyle; ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.lineWidth = edit.radiusPx * 2;
      if (edit.pixels.length === 1) { ctx.beginPath(); ctx.arc(...edit.pixels[0], edit.radiusPx, 0, Math.PI * 2); ctx.fill(); }
      else { ctx.beginPath(); edit.pixels.forEach(([x, y], i) => i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)); ctx.stroke(); } }
    for (const c of this.controls) {
      ctx.strokeStyle = c.id === this.selected ? '#ed6c35' : c.state === 'accepted' ? '#11815f' : '#a22bbe'; ctx.lineWidth = Math.max(2, this.state.width / 350); ctx.beginPath(); ctx.arc(...c.pixel, c.radiusPx, 0, Math.PI * 2); ctx.stroke();
      ctx.font = `bold ${Math.max(18, this.state.width / 60)}px sans-serif`; ctx.strokeStyle = '#fff'; ctx.lineWidth = 4; ctx.strokeText(c.label, c.pixel[0] + c.radiusPx, c.pixel[1]); ctx.fillStyle = '#7d238b'; ctx.fillText(c.label, c.pixel[0] + c.radiusPx, c.pixel[1]);
      if (c.approachPixel) { ctx.strokeStyle = '#0a8966'; ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(...c.pixel); ctx.lineTo(...c.approachPixel); ctx.stroke(); ctx.fillStyle = '#0a8966'; ctx.beginPath(); ctx.arc(...c.approachPixel, 6, 0, Math.PI * 2); ctx.fill(); }
    }
    for (const portal of this.portals) {
      ctx.strokeStyle = portal.accepted ? '#126a85' : '#dc851c'; ctx.fillStyle = portal.accepted ? '#ffffffdd' : '#fff0cfaa'; ctx.lineWidth = Math.max(2, this.state.width/600);
      ctx.beginPath(); ctx.arc(...portal.pixel, Math.max(7, this.state.width/170), 0, Math.PI*2); ctx.fill(); ctx.stroke();
    }
    const reference = this.referencePaths[this.referenceIndex];
    if (reference) { ctx.strokeStyle = '#18a246'; ctx.lineWidth = Math.max(2, this.state.width/400); ctx.setLineDash([10, 7]); ctx.beginPath(); reference.pixels.forEach(([x, y], i) => i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)); ctx.stroke(); ctx.setLineDash([]); }
    for (const a of this.suggestions) {
      if (!a.automatic || this.controls.find(c => c.id === a.controlId)?.approachPixel) continue;
      ctx.strokeStyle = '#e58824'; ctx.lineWidth = 3; ctx.beginPath(); ctx.arc(...a.pixel, 6, 0, Math.PI * 2); ctx.stroke();
    }
    if (this.pendingPixel) {
      const [x, y] = this.pendingPixel, radius = 11 / this.viewScale;
      ctx.save(); ctx.strokeStyle = '#fff'; ctx.lineWidth = 5 / this.viewScale;
      for (const color of ['#fff', '#e35e22']) {
        ctx.strokeStyle = color; ctx.beginPath(); ctx.arc(x, y, radius, 0, Math.PI * 2);
        ctx.moveTo(x - radius * 1.6, y); ctx.lineTo(x + radius * 1.6, y);
        ctx.moveTo(x, y - radius * 1.6); ctx.lineTo(x, y + radius * 1.6); ctx.stroke(); ctx.lineWidth = 2 / this.viewScale;
      }
      ctx.restore();
    }
  }
  close() { this.viewport.destroy(); this.dialog.close(); this.dialog.remove(); if (!this.picked) this.onDismiss?.(); }
}
