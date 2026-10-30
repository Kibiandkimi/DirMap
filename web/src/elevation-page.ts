// Same stylesheet order as the course app, so the shared CSS chunk keeps
// OpenLayers' defaults before this app's overrides.
import 'ol/ol.css';
import './style.css';
import './elevation.css';
import { fetchAccount, openSignIn, renderAccountChip } from './account';
import { mapAssetUrl, setWorkbench, watchAccountChanges, workbenchKey, workbenchScope } from './workbench';
import { escapeHtml as e, type AccountState, type MapState } from './types';
import type { HeightGrid, HeightRegion, HeightStatus, HeightTrack } from './elevation-types';
import { ElevationView } from './elevation-view';

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
let account: AccountState | null = null, status: HeightStatus | null = null, selected = '', loadedFingerprint = '';
let loading = 0, pendingFingerprint = '', jobWatching = '', mode: '3d' | 'map' = '3d';
document.body.classList.add('elevation-page'); document.documentElement.classList.add('elevation-document');
document.querySelector('#app')!.innerHTML = `
  <header class="app-header height-header"><a href="/" class="brand"><span class="brand-mark"><svg viewBox="0 0 30 32" aria-hidden="true"><path d="M15 2 28 26 15 20 2 26Z" fill="none" stroke="currentColor" stroke-width="2.5"/></svg></span>DirMap</a><nav><a href="/">课程工作台</a><span aria-current="page">共享地形</span></nav><div id="height-account" class="header-actions"></div></header>
  <main class="height-page"><div class="height-title"><div><p class="eyebrow">共同记录 · 持续重建</p><h1>共享地形</h1><p>把每一次行走的高程，汇成更完整的地形。</p></div><span id="height-status" class="height-state" role="status">正在载入…</span></div>
  <div class="height-layout"><section class="height-main" aria-label="重建地形">
    <div class="height-toolbar"><label class="height-region-label">地区<select id="height-region" aria-label="选择地形地区"><option>载入中…</option></select></label>
      <div class="height-view-modes" role="group" aria-label="绘制方式"><button id="height-3d" class="active" aria-pressed="true">三维</button><button id="height-map" aria-pressed="false">高程图</button></div>
      <button id="height-reset" class="soft-button">复位</button><a id="height-download" class="soft-button" aria-disabled="true">下载 GeoTIFF</a></div>
    <div class="height-viewport"><canvas id="height-canvas" tabindex="0" aria-label="三维地形；方向键旋转，加减键缩放"></canvas><canvas id="height-map-canvas" hidden aria-label="北向上的高程图；点击可查看高程"></canvas>
      <div id="height-empty" class="height-empty">正在读取共享地形…</div><div id="height-gesture" class="height-gesture">拖动旋转 · 双指或滚轮缩放</div>
      <div class="height-legend"><span id="height-low">—</span><i></i><span id="height-high">—</span><small>轨迹参考高程 · 米</small></div></div>
    <div class="height-display-tools"><label>垂直夸张 <input id="height-exaggeration" type="range" min="1" max="8" value="4" step=".5"><span id="height-factor">4×</span><small>仅影响三维显示</small></label><label class="check-label"><input id="height-observations" type="checkbox">显示采样位置</label><label id="height-overlay-field" class="height-overlay hidden">叠加定向图 <select id="height-overlay-map" aria-label="叠加的定向图"><option value="">不叠加</option></select></label><label id="height-overlay-opacity-field" class="hidden">定向图 <input id="height-overlay-opacity" type="range" min="0" max="100" value="60" aria-label="定向图透明度"><span id="height-overlay-value">60%</span></label></div>
    <div id="height-statistics" class="height-statistics"></div><div id="height-inspect" class="height-inspect">切换“高程图”，点击或移动指针查看某处高程。</div><div id="height-context-credit" class="height-context-credit hidden">位置参照 © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap contributors</a> · 未着色区域暂无高程支持</div>
    <div class="height-explanation"><strong>从轨迹重建的参考地形</strong><p>手机记录的高度会随时间漂移，不同日子的记录也有整体偏差。重建时利用交叉和重叠路段，同时估计地面高度和每条记录的漂移，再在有观测支持的区域平滑插值。留白处没有足够的数据；三维夸张不影响路线中的爬升与耗时计算。</p><p>高程来自记录设备，未建立测量级海拔基准。新贡献会更新后续规划使用的地形，已发布课程保留发布时的高程快照。</p><p id="height-alignment"></p></div>
  </section><aside class="height-sidebar"><div class="height-contribute"><h2>贡献轨迹</h2><p>大家共同构建、共同查看。你可以管理自己上传的轨迹。</p><button id="height-upload" class="primary-button wide">＋ 添加 KML 轨迹</button><input id="height-files" type="file" accept=".kml,application/vnd.google-earth.kml+xml" multiple hidden><small>可多选；相同轨迹不会重复计入。</small>
    <div id="height-job" class="height-job hidden" role="status"><progress id="height-job-progress" max="100"></progress><p id="height-job-message"></p></div>
    <div id="height-import-errors" class="height-import-errors"></div><button id="height-rebuild" class="soft-button wide hidden">合并最新轨迹</button></div>
    <div class="height-track-title"><h2>已贡献轨迹 <span id="height-track-count"></span></h2><label class="check-label"><input id="height-mine" type="checkbox">只看我的</label></div><div id="height-tracks" class="height-tracks"></div>
  </aside></div></main><div id="height-toast" class="toast hidden" role="status"></div>`;

const viewer = new ElevationView($<HTMLCanvasElement>('height-canvas'), $<HTMLCanvasElement>('height-map-canvas'));
function notify(message: string, error = false) {
  $('height-toast').textContent = message; $('height-toast').classList.remove('hidden'); $('height-toast').classList.toggle('error', error);
  setTimeout(() => $('height-toast').classList.add('hidden'), 6500);
}
async function api(path: string, options: RequestInit = {}, timeoutMs = 60000) {
  const headers = new Headers(options.headers);
  if (workbenchScope()) headers.set('X-DirMap-Workbench', workbenchScope()!);
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  const response = await fetch(path, { ...options, headers, cache: 'no-store', signal: AbortSignal.timeout(timeoutMs) });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (result.accountChanged) location.reload();
    throw new Error(typeof result.detail === 'string' ? result.detail : '操作未完成，请稍后重试');
  }
  return result;
}
function requireAccount() {
  if (account?.user) return true;
  if (account) openSignIn(account); else notify('暂时无法读取账户，请联网后刷新页面', true);
  return false;
}
function jobKey() { return workbenchKey('height-job'); }
async function watchJob(job: { id: string } | null) {
  if (!job) { await refresh(); return; }
  jobWatching = job.id;
  try { localStorage.setItem(jobKey(), job.id); } catch { /* tracking still works in this tab */ }
  $('height-job').classList.remove('hidden');
  try {
    while (jobWatching === job.id) {
      const current = await api('/api/jobs/' + job.id);
      if (jobWatching !== job.id) break;
      $<HTMLProgressElement>('height-job-progress').value = current.progress ?? 0;
      $('height-job-message').textContent = current.message || '正在合并共享高程…';
      if (current.state === 'succeeded' || current.state === 'failed') {
        try { localStorage.removeItem(jobKey()); } catch { /* no persistent storage */ }
        await refresh();
        if (current.state === 'failed' && status?.needsRebuild) throw new Error(current.message || '地形重建未完成，可重新合并最新轨迹');
        notify('共享地形已更新'); break;
      }
      await new Promise(resolve => setTimeout(resolve, 700));
    }
  } catch (error) { notify((error as Error).message, true); await refresh().catch(() => {}); }
  finally { if (jobWatching === job.id) { jobWatching = ''; $('height-job').classList.add('hidden'); } }
}
function renderTracks() {
  const onlyMine = $<HTMLInputElement>('height-mine').checked;
  const tracks = [...(status?.tracks ?? [])].filter(t => !onlyMine || t.ownerId === account?.user?.id).sort((a, b) => b.createdAt - a.createdAt);
  $('height-track-count').textContent = `${status?.tracks.length ?? 0}`;
  $('height-tracks').innerHTML = tracks.map(track => {
    const mine = track.ownerId === account?.user?.id;
    const region = status?.regions.find(r => r.trackIds.includes(track.id));
    const aligned = region?.alignment.tracks.find(t => t.id === track.id), correction = aligned?.correctionM, drift = aligned?.driftRangeM ?? 0;
    const recorded = track.recordedAt ? new Date(track.recordedAt * 1000).toLocaleString('zh-CN', { year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
    return `<article class="height-track ${track.enabled ? '' : 'disabled'}" data-track="${e(track.id)}"><div class="height-track-heading"><strong>${e(track.name)}</strong><span>${mine ? '我的' : e(track.ownerName)}</span></div><p>${e(region?.name ?? '等待合并')} · ${track.pointCount.toLocaleString()} 点${recorded ? ` · 记录于 ${e(recorded)}` : ''}</p><p>记录高程 ${track.minHeightM.toFixed(1)}–${track.maxHeightM.toFixed(1)} m${correction !== undefined && Math.abs(correction) >= .1 ? ` · 平均校正 ${correction > 0 ? '+' : ''}${correction.toFixed(1)} m` : ''}${drift >= 1 ? ` · 期间漂移约 ${drift.toFixed(1)} m` : ''}</p>${mine ? `<div class="height-track-actions"><label class="check-label"><input type="checkbox" data-track-enable="${e(track.id)}" ${track.enabled ? 'checked' : ''}>参与重建</label><button data-track-name="${e(track.id)}" class="text-button">改名</button><button data-track-delete="${e(track.id)}" class="text-button danger">删除</button></div>` : `<span class="height-readonly">${track.enabled ? '已分享，参与共同重建' : '贡献者已停用'}</span>`}</article>`;
  }).join('') || `<p class="muted">${onlyMine ? '你还没有贡献轨迹。' : '还没有高程轨迹。添加带海拔的 KML 即可开始。'}</p>`;
}
function renderRegion(region: HeightRegion) {
  $('height-low').textContent = region.minHeightM === null ? '—' : region.minHeightM.toFixed(1);
  $('height-high').textContent = region.maxHeightM === null ? '—' : region.maxHeightM.toFixed(1);
  $('height-statistics').innerHTML = `<div><strong>${region.spacingM}<small> m</small></strong><span>网格间距</span></div><div><strong>${(region.supportedCells * region.spacingM ** 2 / 10000).toFixed(1)}<small> 公顷</small></strong><span>有观测支持的范围</span></div><div><strong>${region.trackIds.length}<small> 条</small></strong><span>参与本区的轨迹</span></div><div><strong>${region.pointCount.toLocaleString()}</strong><span>轨迹采样点</span></div>`;
  const agreement = region.alignment.overlapResidualM !== null ? `，校正后交叉处高度差中位 ${region.alignment.overlapResidualM.toFixed(1)} m` : '';
  $('height-alignment').textContent = region.alignment.alignedComponents > 1 ? '部分轨迹缺少足够重叠，暂时保留不同高程参考之间的空白；后续交叉经过这些位置的轨迹有助于补全。' : region.alignment.overlapPairs ? `已利用 ${region.alignment.overlapPairs} 对重叠记录校正高程${agreement}。` : '本区目前只有一条记录，无法与其他记录互相校核；更多重复和交叉轨迹能让地形更可靠。';
  const download = $<HTMLAnchorElement>('height-download');
  if (region.supportedCells) { download.href = `/api/elevation/regions/${region.id}/height.tif`; download.setAttribute('aria-disabled', 'false'); }
  else { download.removeAttribute('href'); download.setAttribute('aria-disabled', 'true'); }
}
async function loadRegion() {
  const region = status?.regions.find(r => r.id === selected);
  if (!region) {
    ++loading; pendingFingerprint = '';
    viewer.setGrid(null); loadedFingerprint = ''; $('height-empty').textContent = '还没有重建地形。登录后添加带高程的 KML 轨迹，即可共同构建。'; $('height-empty').classList.remove('hidden');
    $('height-statistics').innerHTML = ''; $<HTMLAnchorElement>('height-download').removeAttribute('href'); return;
  }
  if (loadedFingerprint === region.fingerprint) { renderRegion(region); return; }
  if (pendingFingerprint === region.fingerprint) return;
  const revision = ++loading; pendingFingerprint = region.fingerprint;
  viewer.setGrid(null); $('height-empty').textContent = `正在读取${region.name}…`; $('height-empty').classList.remove('hidden');
  try {
    const grid: HeightGrid = await api('/api/elevation/regions/' + region.id);
    if (revision !== loading || selected !== grid.id) return;
    viewer.setGrid(grid); loadedFingerprint = grid.fingerprint; renderRegion(grid); renderOverlayChoices();
    $('height-context-credit').classList.toggle('hidden', !grid.context?.features.length);
    $('height-empty').classList.toggle('hidden', !!grid.supportedCells);
    if (!grid.supportedCells) { $('height-empty').textContent = '目前的观测尚不足以形成地形面；继续补充交叉轨迹后可重建。'; viewer.setPoints(true); }
    const url = new URL(location.href); url.searchParams.set('region', selected); history.replaceState({}, '', url);
  } catch (error) { if (revision === loading) { $('height-empty').textContent = (error as Error).message; notify((error as Error).message, true); } }
  finally { if (revision === loading) pendingFingerprint = ''; }
}
async function refresh() {
  const next: HeightStatus = await api('/api/elevation');
  if (status && next.version < status.version) return;
  status = next;
  const known = status.regions.some(r => r.id === selected);
  if (!known) {
    const requested = new URLSearchParams(location.search).get('region');
    selected = status.regions.find(r => r.id === requested)?.id ?? status.regions.find(r => r.name.includes('粤海'))?.id ?? status.regions[0]?.id ?? '';
  }
  $<HTMLSelectElement>('height-region').innerHTML = status.regions.map(r => `<option value="${e(r.id)}" ${r.id === selected ? 'selected' : ''}>${e(r.name)}</option>`).join('') || '<option value="">尚无地形地区</option>';
  $('height-status').textContent = status.needsRebuild ? '有新轨迹待合并' : status.builtAt ? `最近重建 ${new Date(status.builtAt * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}` : '等待第一份贡献';
  $('height-status').classList.toggle('pending', status.needsRebuild);
  $('height-rebuild').classList.toggle('hidden', !status.needsRebuild || !account?.user);
  renderTracks(); await loadRegion();
}
function switchMode(next: '3d' | 'map') {
  mode = next === '3d' && !viewer.supports3D ? 'map' : next; viewer.setMode(mode);
  for (const value of ['3d', 'map']) { $('height-' + value).classList.toggle('active', mode === value); $('height-' + value).setAttribute('aria-pressed', String(mode === value)); }
  $<HTMLInputElement>('height-exaggeration').disabled = mode !== '3d';
  $('height-gesture').textContent = mode === '3d' ? '拖动旋转 · 双指或滚轮缩放' : '北向上 · 点击查看高程 · 等高线间隔 2 m（大起伏区域 10 m）';
}
viewer.onInspect = value => { $('height-inspect').textContent = value ? `${value.lng.toFixed(6)}, ${value.lat.toFixed(6)} · ${value.height === null ? '此处没有足够的观测支持' : `网格参考高程 ${value.height.toFixed(1)} m${value.support !== null ? ` · 距最近观测约 ${Math.round(value.support)} m` : ''}`}` : '切换“高程图”，点击或移动指针查看某处高程。'; };
$('height-3d').onclick = () => switchMode('3d'); $('height-map').onclick = () => switchMode('map'); $('height-reset').onclick = () => viewer.reset();
$('height-exaggeration').oninput = event => { const value = Number((event.target as HTMLInputElement).value); $('height-factor').textContent = `${value}×`; viewer.setExaggeration(value); };
$('height-observations').onchange = event => viewer.setPoints((event.target as HTMLInputElement).checked);
$('height-region').onchange = event => { selected = (event.target as HTMLSelectElement).value; void loadRegion(); };
$('height-mine').onchange = renderTracks;
$('height-upload').onclick = () => { if (requireAccount()) $<HTMLInputElement>('height-files').click(); };
$<HTMLInputElement>('height-files').onchange = async event => {
  const input = event.target as HTMLInputElement, files = Array.from(input.files ?? []); if (!files.length || !requireAccount()) return;
  const button = $<HTMLButtonElement>('height-upload'); button.disabled = true; button.textContent = '正在导入…';
  try {
    const form = new FormData(); for (const file of files) form.append('files', file);
    // Up to 64 MiB of recordings may take minutes on a phone connection.
    const result = await api('/api/elevation/tracks', { method: 'POST', body: form }, 600000);
    $('height-import-errors').innerHTML = (result.errors ?? []).map((error: { fileName: string; message: string }) => `<p>${e(error.fileName)}：${e(error.message)}</p>`).join('');
    notify(`已加入 ${result.added.length} 份轨迹${result.duplicates.length ? `，${result.duplicates.length} 份相同轨迹未重复计入` : ''}`);
    input.value = ''; await refresh(); void watchJob(result.job);
  } catch (error) { notify((error as Error).message, true); }
  finally { button.disabled = false; button.textContent = '＋ 添加 KML 轨迹'; input.value = ''; }
};
$('height-rebuild').onclick = async () => { if (!requireAccount()) return; try { void watchJob((await api('/api/elevation/rebuild', { method: 'POST' })).job); } catch (error) { notify((error as Error).message, true); } };
async function editTrack(track: HeightTrack, body: object, remove = false) {
  const result = await api('/api/elevation/tracks/' + track.id, { method: remove ? 'DELETE' : 'PATCH', body: JSON.stringify({ version: status!.version, ...body }) });
  await refresh(); void watchJob(result.job);
}
$('height-tracks').addEventListener('change', async event => {
  const input = event.target as HTMLInputElement, id = input.dataset.trackEnable; if (!id) return;
  const track = status?.tracks.find(t => t.id === id); if (!track || !requireAccount()) return;
  try { await editTrack(track, { enabled: input.checked }); }
  catch (error) { notify((error as Error).message, true); await refresh().catch(() => {}); }
});
$('height-tracks').addEventListener('click', event => {
  const target = (event.target as HTMLElement).closest<HTMLElement>('[data-track-name],[data-track-delete]');
  const track = status?.tracks.find(t => t.id === (target?.dataset.trackName ?? target?.dataset.trackDelete));
  if (!track || !requireAccount()) return;
  const remove = !!target!.dataset.trackDelete, box = document.createElement('dialog'); box.className = 'info-dialog';
  box.innerHTML = `<form><div class="dialog-heading"><h2>${remove ? '删除自己的轨迹' : '修改轨迹名称'}</h2><button type="button" data-close class="icon-button" aria-label="关闭">×</button></div>${remove ? `<p>删除“${e(track.name)}”后，将重新构建共享地形。已发布课程保留原来的高程快照。</p>` : `<label class="field-label">名称<input id="height-track-new-name" maxlength="120" required value="${e(track.name)}"></label>`}<p class="danger" data-error></p><button class="primary-button wide" type="submit">${remove ? '删除并重建' : '保存名称'}</button></form>`;
  document.body.append(box); box.querySelector('[data-close]')!.addEventListener('click', () => box.close()); box.addEventListener('close', () => box.remove(), { once: true });
  box.querySelector('form')!.onsubmit = async event => {
    event.preventDefault(); const submit = box.querySelector<HTMLButtonElement>('[type=submit]')!; submit.disabled = true;
    try { await editTrack(track, remove ? {} : { name: box.querySelector<HTMLInputElement>('input')!.value }, remove); box.close(); }
    catch (error) { box.querySelector('[data-error]')!.textContent = (error as Error).message; await refresh().catch(() => {}); }
    finally { submit.disabled = false; }
  };
  box.showModal();
});

// The signed-in workbench's registered maps that cover this terrain.
let workbenchMaps: { id: string; name: string; extent: number[] }[] = [];
let overlayRequest = 0;
const EARTH = 6378137;
const degrees = (x: number, y: number) => [x / EARTH * 180 / Math.PI, (2 * Math.atan(Math.exp(y / EARTH)) - Math.PI / 2) * 180 / Math.PI];
function remembered(name: string) { try { return localStorage.getItem(workbenchKey(name)); } catch { return null; } }
function remember(name: string, value: string) { try { localStorage.setItem(workbenchKey(name), value); } catch { /* this page keeps it */ } }
async function loadWorkbenchMaps() {
  try {
    const catalog: { maps: { id: string; name: string; processed: boolean; extent?: number[] | null }[] } = await api('/api/catalog');
    workbenchMaps = catalog.maps.filter(m => m.processed && m.extent?.length === 4).map(m => ({ id: m.id, name: m.name, extent: m.extent! }));
  } catch { workbenchMaps = []; }  // a visitor without a workbench sees only the terrain
  renderOverlayChoices();
}
function renderOverlayChoices() {
  const region = status?.regions.find(r => r.id === selected), select = $<HTMLSelectElement>('height-overlay-map');
  const candidates = region ? workbenchMaps.filter(m => {
    const [west, south] = degrees(m.extent[0], m.extent[1]), [east, north] = degrees(m.extent[2], m.extent[3]);
    return west <= region.bbox[2] && east >= region.bbox[0] && south <= region.bbox[3] && north >= region.bbox[1];
  }) : [];
  const chosen = select.value || remembered('height-overlay') || '';
  select.innerHTML = '<option value="">不叠加</option>' + candidates.map(m => `<option value="${e(m.id)}">${e(m.name)}</option>`).join('');
  select.value = candidates.some(m => m.id === chosen) ? chosen : '';
  $('height-overlay-field').classList.toggle('hidden', !candidates.length);
  void applyOverlay();
}
async function applyOverlay() {
  const id = $<HTMLSelectElement>('height-overlay-map').value, request = ++overlayRequest;
  $('height-overlay-opacity-field').classList.toggle('hidden', !id);
  if (!id) { viewer.setOverlay(null); return; }
  try {
    const state: MapState = await api('/api/maps/' + encodeURIComponent(id));
    if (!state.extent || state.registrationStale) throw new Error('这张图的配准已过期，请先在课程工作台重新配准');
    const image = new Image(); image.src = mapAssetUrl(state, 'overlay.png'); await image.decode();
    if (request === overlayRequest) viewer.setOverlay({ image, extent: state.extent });
  } catch (error) {
    if (request !== overlayRequest) return;
    viewer.setOverlay(null); $('height-overlay-opacity-field').classList.add('hidden');
    notify(error instanceof Error && error.message ? error.message : '暂时无法叠加这张定向图', true);
  }
}
$('height-overlay-map').onchange = () => { remember('height-overlay', $<HTMLSelectElement>('height-overlay-map').value); void applyOverlay(); };
$('height-overlay-opacity').oninput = event => {
  const value = Number((event.target as HTMLInputElement).value);
  $('height-overlay-value').textContent = `${value}%`; viewer.setOverlayOpacity(value / 100); remember('height-overlay-opacity', String(value));
};
async function start() {
  account = await fetchAccount(); if (account) setWorkbench(account);
  renderAccountChip($('height-account'), account, notify); watchAccountChanges(() => location.reload());
  if (!viewer.supports3D) { $<HTMLButtonElement>('height-3d').disabled = true; switchMode('map'); }
  if (account) {
    const opacity = Number(remembered('height-overlay-opacity'));
    if (opacity >= 0 && opacity <= 100 && remembered('height-overlay-opacity') !== null) {
      $<HTMLInputElement>('height-overlay-opacity').value = String(opacity); $('height-overlay-value').textContent = `${opacity}%`; viewer.setOverlayOpacity(opacity / 100);
    }
    void loadWorkbenchMaps();
  }
  try { await refresh(); }
  catch (error) { $('height-empty').textContent = '暂时无法读取共享地形，请联网后刷新。'; notify((error as Error).message, true); }
  if (account?.user) { try { const id = localStorage.getItem(jobKey()); if (id) void watchJob({ id }); } catch { /* storage unavailable */ } }
  setInterval(() => { if (!document.hidden && !jobWatching) void refresh().catch(() => {}); }, 15000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) void refresh().catch(() => {}); });
}
void start();
