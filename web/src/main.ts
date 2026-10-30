import 'ol/ol.css';
import './style.css';
import { transformExtent } from 'ol/proj.js';
import { CampusMap, teamColors } from './map';
import { MapPointPicker } from './map-picker';
import { NavigationSensors, type HeadingSample } from './heading';
import { PixelEditor } from './editor';
import { OffRouteMonitor, remainingPath } from './navigation';
import { installOffline, saveOfflineCourse } from './offline';
import { parseCustomRoutes, routeToken } from './course-routes';
import { teamComparisonHtml } from './team-comparison';
import { canRefine, refinementHtml } from './plan-refinement';
import { resultsCsv } from './class-results';
import { elevationSummary } from './elevation-profile';
import { printSheets } from './print';
import { fetchAccount, openSignIn, renderAccountChip, signInGate } from './account';
import { setWorkbench, watchAccountChanges, workbenchKey, workbenchScope } from './workbench';
import { GroupOverview, GroupPanel } from './groups';
import { escapeHtml as e, lngLatToPixel, pixelToLngLat, type AccountState, type Anchor, type AutoTeamRequest, type Catalog, type ClassProgress, type Control, type Job, type MapState, type Pixel, type Plan, type PlanRequest, type Portal, type Route, type Site, type Surface, type TerrainEdit } from './types';

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const number = (id: string) => Number($<HTMLInputElement>(id).value);
const value = (id: string) => $<HTMLInputElement>(id).value;
const minutes = (seconds: number) => `${Math.floor(Math.round(seconds) / 60)} 分 ${Math.round(seconds) % 60} 秒`;
const distance = (metres: number) => metres >= 1000 ? `${(metres / 1000).toFixed(2)} km` : `${Math.round(metres)} m`;
let catalog: Catalog;
let state: MapState | null = null;
let activeId = 'szu-1';
let plan: Plan | null = null;
let planRevision = 0;
let mandatory = new Set<string>();
let missingPresetLabels: string[] = [];
let customDrafts: Record<string, string> = {};
let restoredTeams: PlanRequest['teams'] | null = null;
let customMapTeam: string | null = null;
let startPixel: Pixel | null = null, finishPixel: Pixel | null = null;
let startSurfaceId: string | null = null, finishSurfaceId: string | null = null;
let anchors: Anchor[] = [];
let follow = false, northOnly = false;
let lastPosition: GeolocationPosition | null = null;
let locationFresh = false;
let selectedTeam: string | null = null;
let busy = false;
let courseId = new URLSearchParams(location.search).get('course');
// `?group=` without a course opens a page for joining a group only.
const groupCode = new URLSearchParams(location.search).get('group');
const groupMode = !courseId && groupCode !== null;
let account: AccountState | null = null;
let activeSite = 'szu-yuehai';
let groupPanel: GroupPanel | null = null;
let groupOverview: GroupOverview | null = null;
let completed = new Set<string>();
let departedMeetings = new Set<string>();
let checkInTimes: Record<string, number> = {};
let arrivedAt: number | null = null;
let classProgress: ClassProgress | null = null;
let syncingProgress = false;
let studentControls: Control[] = [];
let releasePoll: ReturnType<typeof setInterval> | null = null;
let courseSuperseded = false;
let navigationRoute: Route | null = null;
let navigationRevision = 0;
let navigating = false;
let surfaceSelectionPending = false;
let stateLoad = 0, overlayUpdate = 0;
let resumeSensors = false;
const offRoute = new OffRouteMonitor();

document.querySelector('#app')!.innerHTML = `
<header class="app-header"><a href="/" class="brand" aria-label="DirMap 首页"><span class="brand-mark"><svg viewBox="0 0 30 32" aria-hidden="true"><path d="M15 2 28 26 15 20 2 26Z" fill="none" stroke="currentColor" stroke-width="2.5"/><path d="M15 9v11" stroke="currentColor" stroke-width="2"/></svg></span><span>DirMap<small>定向越野课</small></span></a><div class="header-context"><span class="context-dot"></span><span id="site-name">深圳大学 · 粤海校区</span><span class="header-divider"></span><span id="header-mode">课程工作台</span></div><div class="header-actions"><span id="release-badge" class="status-tag">备课草稿</span><span id="account-slot"></span><button id="open-help" class="text-button">使用说明</button></div></header>
<div class="workspace"><aside class="sidebar" id="teacher-panel"><div class="sidebar-intro"><p class="eyebrow">ORIENTEERING CLASSROOM</p><h1>让每一次出发<br>都有方向<span>。</span></h1><p>校准地图，安排路线，一起探索校园与公园。</p></div><div id="workbench-notice"></div>
<section class="source-section"><div class="section-title"><h2>课程地图</h2><button id="upload-button" class="text-button">＋ 导入</button><input id="upload-input" type="file" accept="image/jpeg,image/png" hidden></div><div class="site-picker"><select id="site-select" aria-label="场地"></select><button id="add-site" class="text-button">＋ 场地</button></div><div id="site-status" class="site-status hidden"></div><div id="map-cards" class="map-cards"></div><div class="source-actions"><button id="process-current" class="primary-button">自动识别与配准</button><button id="process-all" class="soft-button">处理全部</button></div><div id="job-status" class="job-status hidden" role="status"><div><span id="job-message">准备中</span><span id="job-percent">0%</span></div><progress id="job-progress" max="100" value="0"></progress><button id="cancel-job" class="text-button hidden">取消任务</button></div><div id="map-summary" class="summary-strip">选择地图开始准备课程</div></section>
<nav class="tabs" aria-label="工作区域"><button data-tab="course" class="active">课程设置</button><button data-tab="registration">地图配准</button><button data-tab="terrain">通行核验</button></nav>
<section id="tab-course" class="tab-content"><div class="section-title"><h2>路线规则</h2><span class="step-number">01</span></div><label class="field-label" for="course-mode">访问方式</label><select id="course-mode"><option value="all_visit">每队访问全部目标</option><option value="split_coverage">全班分担全部目标</option><option value="fixed_order">按列表顺序访问</option></select><div class="form-grid"><label>总人数<input id="people" type="number" value="20" min="1" max="500"></label><label>队伍数<input id="teams" type="number" value="4" min="1" max="30"></label><label>步行 / 跑速 <span>m/s</span><input id="speed" type="number" value="1.4" step="0.1" min="0.2" max="10"></label><label>错峰间隔 <span>秒</span><input id="stagger" type="number" value="0" min="0" max="3600"></label></div><p id="group-preview" class="muted compact">每队 5 人 · 同时出发</p><label class="field-label" for="objective">优化目标</label><select id="objective"><option value="min_max_finish_then_sum">优先让最后一队更早完成</option><option value="min_total_time">全班总耗时最短</option></select>
<div class="section-title section-gap"><h2>起终点与目标</h2><span class="step-number">02</span></div><div class="endpoint-row"><button id="pick-start" class="endpoint-button"><i class="endpoint-dot start"></i><span>在地图选起点</span></button><button id="pick-finish" class="endpoint-button"><i class="endpoint-dot finish"></i><span>在地图选终点</span></button></div><label class="check-label"><input id="return-start" type="checkbox" checked>回到起点结束</label><div class="control-heading"><span id="control-count">尚未识别目标</span><button id="edit-controls" class="text-button">原图校核 ↗</button></div><p class="muted compact">勾选本次课程目标；固定顺序模式可上下调整。</p><div id="control-list" class="control-list"><div class="empty-small">识别后，控制圈会显示在这里。</div></div><button id="plan-button" class="primary-button wide plan-button">生成队伍路线 <span>→</span></button><div id="plan-errors" role="status"></div><div id="route-results"></div>
<div id="publish-section" class="publish-section hidden"><button id="review-button" class="soft-button wide">核验课程并发布</button><div id="share-result"></div></div></section>
<section id="tab-registration" class="tab-content hidden"><div class="section-title"><h2>地物匹配与局部校准</h2><span class="step-number">01</span></div><p class="muted">先通过水体和建筑匹配到标准地图；同一校园的图幅也会匹配共同地物。局部网格修正纸图的比例偏差。</p><div id="registration-quality" class="quality-card">完成自动处理后显示匹配结果。</div><div id="field-check-card" class="quality-card"><strong>现场 GPS 核查</strong><p class="muted compact">站在原图上能认出的位置（路口、建筑角或目标圈心），用手机当前定位记录核查点，再在原图上点出所站位置。核查点不参与拟合，也不会让路线或已发布课程失效。</p><div id="field-check-summary"></div><button id="add-field-check" class="soft-button wide">＋ 用当前位置添加核查点</button><div id="field-check-list" class="anchor-list"></div></div><button id="add-anchor" class="soft-button wide">＋ 在原图选择同名点</button><label class="check-label section-gap"><input id="validation-anchor" type="checkbox">下一点用于独立核查，不参与拟合</label><div id="anchor-list" class="anchor-list"></div><button id="fit-anchors" class="primary-button wide" disabled>用同名点重新校准</button><p class="muted">至少 3 个分散的非共线拟合点。建议加入 8 个以上拟合点，并另设核查点。</p><a id="export-map" class="text-link" href="#">导出配准和目标数据 ↗</a></section>
<section id="tab-terrain" class="tab-content hidden"><div class="section-title"><h2>核对可通行区域</h2><span class="step-number">01</span></div><p class="muted">红色区域为识别出的障碍。重点核对围墙、门洞、水体、禁区和上下层通道。</p><label class="check-label"><input id="show-terrain" type="checkbox">在地图上显示障碍候选</label><button id="edit-terrain" class="primary-button wide section-gap">打开原图修正通行区域</button><div id="terrain-summary" class="quality-card"></div><p class="muted">原图修正保存后需重新生成路线。二维相交不能证明桥上、桥下可以互通。</p><div class="section-title section-gap"><h2>随图图例</h2></div><div id="legend-links" class="legend-links"></div></section></aside>
<aside id="student-panel" class="sidebar student-panel hidden"><div id="group-only-heading" class="group-keep hidden"><p class="eyebrow">TEAM UP</p><h1>组队定位</h1><p class="muted">输入同伴告诉你的 6 位组队码，或创建新小组再把组队码告诉同伴。</p></div><p class="eyebrow">TODAY'S ADVENTURE</p><h1 id="course-title">定向越野课</h1><p class="muted">选择队伍，打开定位，出发探索。</p><label class="field-label" for="student-team">我的队伍</label><select id="student-team"></select><a id="print-team" class="text-link" target="_blank" rel="noreferrer">打印本队路线 ↗</a><p id="class-clock" class="class-clock" role="timer"></p><div id="student-overview" class="quality-card"></div><div class="section-title"><h2>下一目标</h2><span id="student-progress" class="muted"></span></div><div id="next-control" class="next-control"></div><button id="check-in" class="primary-button wide">已完成这个目标</button><button id="arrive-finish" class="primary-button wide hidden">已到达终点</button><button id="undo-check-in" class="text-button wide">撤销上一个打点</button><div id="student-stops" class="student-stops"></div><details id="class-board" class="class-board"><summary>全班进度</summary><div id="class-board-list" class="muted compact">联网后显示各队打点。</div></details><details id="group-section" class="group-section group-keep"><summary>组队与位置共享</summary><div id="group-panel" class="group-panel"></div></details><details id="group-overview-section" class="group-section hidden"><summary>各小组位置</summary><div id="group-overview" class="group-panel"></div></details><p class="muted">打点由本人确认；当前位置精度不足时，请结合原图和现场地物判断。</p></aside>
<main class="map-panel"><div id="map" role="application" aria-label="校园标准地图与定向路线"></div><div class="map-topbar"><div class="map-title"><span class="map-kicker">CAMPUS EXPLORER</span><strong id="map-title">深圳大学 · 粤海校区</strong><span id="map-subtitle">标准地图 / WGS84</span></div><div class="layer-tools"><label class="check-label"><input id="standard-map" type="checkbox" checked>标准底图</label><label class="check-label teacher-only"><input id="stitch-maps" type="checkbox">叠合图幅</label><label class="opacity-control">原图 <input id="overlay-opacity" type="range" min="0" max="100" value="70" aria-label="原图透明度"></label></div></div><div id="map-action-hint" class="map-action-hint hidden" role="status"></div><button id="north-button" class="compass-button" title="北向上" aria-label="切换北向上"><span>N</span><svg id="compass-arrow" viewBox="0 0 24 28" aria-hidden="true"><path d="M12 1 21 25 12 20 3 25Z" fill="#295c46"/><path d="M12 1v19l9 5Z" fill="#abbdb1"/></svg></button><div class="map-bottom"><div class="navigation-card"><button id="follow-button" class="follow-button"><span class="locate-icon">◎</span><span id="follow-label">开始定位与朝向跟随</span></button><div id="sensor-status" class="sensor-status" role="status">手机打开 HTTPS 链接即可启用</div></div><div class="map-key"><span><i class="key-control"></i>目标点</span><span><i class="key-route"></i>队伍路线</span><span id="location-status">位置尚未开启</span></div></div></main></div><div id="toast" class="toast hidden" role="status"></div>`;

const campusMap = new CampusMap($('map'));
const sensors = new NavigationSensors();
const mapPicker = new MapPointPicker(campusMap, $('map-action-hint'), () => {
  follow = false; if (sensors.active) $('follow-label').textContent = '继续跟随';
  customMapTeam = null; setMobilePanel(false);
});
const phoneLayout = matchMedia('(max-width: 720px), (max-width: 1000px) and (max-height: 540px)');
document.querySelector('.workspace')!.insertAdjacentHTML('afterend', `<nav class="mobile-dock" aria-label="手机工作区域"><button id="mobile-map" class="active" aria-pressed="true">地图</button><button id="mobile-panel" aria-expanded="false" aria-controls="${courseId || groupMode ? 'student-panel' : 'teacher-panel'}">${courseId ? '本队路线' : groupMode ? '小组' : '课程设置'}</button></nav>`);
for (const id of ['teacher-panel', 'student-panel']) $(id).insertAdjacentHTML('afterbegin', '<button class="mobile-panel-close soft-button group-keep" aria-label="收起面板，查看地图">收起 · 查看地图 ↓</button>');
document.querySelector('.map-topbar')!.insertAdjacentHTML('beforeend', '<button id="mobile-layers" class="soft-button" aria-expanded="false" aria-controls="map-layers">图层</button>');
document.querySelector('.layer-tools')!.id = 'map-layers';
document.querySelector('.layer-tools')!.insertAdjacentHTML('beforeend', '<label class="check-label"><input id="route-arrows" type="checkbox" checked>路线箭头</label><label class="check-label"><input id="location-direction" type="checkbox" checked>我的方向</label><p class="muted compact layer-legend">蓝色光束：手机朝向；蓝色箭头：行进方向</p>');
for (const [id, apply] of [['route-arrows', (on: boolean) => campusMap.setRouteArrows(on)], ['location-direction', (on: boolean) => campusMap.setDirectionVisible(on)]] as const) {
  const checkbox = $<HTMLInputElement>(id);
  checkbox.checked = stored(`dirmap-${id}`) !== 'false'; apply(checkbox.checked);
  checkbox.onchange = () => { apply(checkbox.checked); try { localStorage.setItem(`dirmap-${id}`, String(checkbox.checked)); } catch { /* keep this session's preference */ } };
}
function setMobilePanel(open: boolean) {
  open = phoneLayout.matches && open;
  if (open && customMapTeam) { customMapTeam = null; $('map-action-hint').classList.add('hidden'); }
  document.querySelector('.workspace')!.classList.toggle('panel-open', open);
  $('mobile-panel').setAttribute('aria-expanded', String(open));
  $('mobile-panel').classList.toggle('active', open);
  $('mobile-map').classList.toggle('active', !open);
  $('mobile-map').setAttribute('aria-pressed', String(!open));
  for (const id of ['teacher-panel', 'student-panel']) $(id).inert = phoneLayout.matches && !open;
  requestAnimationFrame(() => campusMap.map.updateSize());
}
$('mobile-map').onclick = () => setMobilePanel(false);
$('mobile-panel').onclick = () => setMobilePanel($('mobile-panel').getAttribute('aria-expanded') !== 'true');
document.querySelectorAll<HTMLButtonElement>('.mobile-panel-close').forEach(button => button.onclick = () => { setMobilePanel(false); $('mobile-panel').focus(); });
$('mobile-layers').onclick = () => {
  const open = $('mobile-layers').getAttribute('aria-expanded') !== 'true';
  $('mobile-layers').setAttribute('aria-expanded', String(open));
  document.querySelector('.map-panel')!.classList.toggle('layers-open', open);
};
phoneLayout.addEventListener('change', () => setMobilePanel(false));
document.addEventListener('keydown', event => { if (event.key === 'Escape' && phoneLayout.matches) setMobilePanel(false); });
setMobilePanel(false);
$('objective').insertAdjacentHTML('beforeend', '<option value="min_distance">全班总距离最短</option>');
$('objective').insertAdjacentHTML('afterend', '<div class="elevation-option"><label class="check-label"><input id="use-elevation" type="checkbox" checked>考虑上下坡</label><a class="text-link" href="/elevation" target="_blank" rel="noreferrer">共享地形 ↗</a><small>使用共享轨迹高程；无覆盖的路段暂不计坡度。</small></div>');
if (!courseId && !groupCode) document.querySelector('.header-context')?.insertAdjacentHTML('beforeend', '<a class="text-link" href="/elevation">共享地形 ↗</a>');
$('group-preview').insertAdjacentHTML('afterend', '<details class="auto-team-settings"><summary>按分队收益自动选择队数</summary><div class="form-grid"><label>每队至少几人<input id="auto-min-people" type="number" min="1" max="500" value="2"></label><label>最多比较几队<input id="auto-max-teams" type="number" min="1" max="30" value="30"></label><label>保留最佳改善收益 <span>%</span><input id="auto-gain" type="number" min="50" max="100" step="5" value="90"></label></div><p class="muted compact">比较允许的全部队数，选择达到收益目标的最少队数。沿用总人数、速度、出发间隔与当前优化目标。</p><button id="recommend-teams" class="soft-button wide">比较收益并自动分队</button><p id="auto-team-hint" class="muted compact"></p></details>');
for (const id of ['auto-min-people', 'auto-max-teams', 'auto-gain']) $(id).oninput = () => { resetPlan(); persistRules(); };
$('map-summary').insertAdjacentHTML('afterend', '<details class="job-history"><summary>处理记录与恢复</summary><p class="muted compact">刷新页面或服务重启后，可在这里接续原任务。</p><button id="refresh-jobs" class="text-button">刷新记录</button><div id="job-history-list"></div></details>');
$('refresh-jobs').onclick = () => task(renderJobs);
$('map-summary').insertAdjacentHTML('afterend', '<details id="release-history" class="release-history"><summary>已发布课程</summary><p class="muted compact">本图最近发布的学生链接与全班进度。地图或通行修改后旧课程会失效。</p><div id="release-list"></div></details>');
$('release-history').addEventListener('toggle', () => { if ($<HTMLDetailsElement>('release-history').open) void renderReleases(); });
$('release-list').addEventListener('click', event => {
  const button = (event.target as HTMLElement).closest<HTMLButtonElement>('[data-session]');
  if (!button) return;
  if (button.dataset.session === 'reset' && !confirm('重置后各队手机的开课时间和出发倒计时将清除，确定吗？')) return;
  void task(async () => {
    await api(`/api/releases/${button.dataset.releaseId}/session`, { method: 'POST', body: JSON.stringify({ action: button.dataset.session }) });
    await renderReleases(); notify(button.dataset.session === 'start' ? '已开始计时，各队手机将显示出发倒计时' : '已重置计时');
  });
});
type ReleaseSummary = { id: string; title: string; createdAt: number; superseded: boolean; url: string; teams: number; startedTeams: number; finishedTeams: number; lastReportAt: number | null; startedAt: number | null };
let releaseRender = 0;
async function renderReleases() {
  if (courseId || !$<HTMLDetailsElement>('release-history').open) return;
  const render = ++releaseRender, assetId = activeId;
  let releases: ReleaseSummary[];
  try { releases = await api<ReleaseSummary[]>(`/api/releases?assetId=${encodeURIComponent(assetId)}`); }
  catch (error) { if (render === releaseRender) $('release-list').innerHTML = `<p class="muted compact">${e(error instanceof ApiError && error.status === 401 ? '登录或填写原工作台的教师口令后显示已发布课程。' : '暂时无法读取已发布课程。')}</p>`; return; }
  if (render !== releaseRender || assetId !== activeId) return;
  const clock = (seconds: number) => new Date(seconds * 1000).toLocaleString([], { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  $('release-list').innerHTML = releases.length ? releases.map(r => {
    const link = new URL(r.url, location.href).href;
    const progress = r.startedTeams ? `${r.startedTeams}/${r.teams} 队已打点 · ${r.finishedTeams} 队完成${r.lastReportAt ? ` · 最近上报 ${clock(r.lastReportAt)}` : ''}` : `${r.teams} 队 · 尚无打点`;
    const session = r.startedAt ? `<button class="text-button" data-session="reset" data-release-id="${e(r.id)}">重置计时</button>` : r.superseded ? '' : `<button class="soft-button" data-session="start" data-release-id="${e(r.id)}">开始计时</button>`;
    return `<div class="release-row" data-release="${e(r.id)}"><span>${e(r.title)}<small>${clock(r.createdAt)} 发布 · ${r.superseded ? '已失效' : '当前有效'}${r.startedAt ? ` · ${clock(r.startedAt)} 开课` : ''}</small><small>${progress}</small>${session}</span><a href="${e(link)}" target="_blank" rel="noreferrer" class="text-link">学生页 ↗</a><a href="${e(link)}#progress" target="_blank" rel="noreferrer" class="text-link">全班进度 ↗</a><a href="${e(link)}#groups" target="_blank" rel="noreferrer" class="text-link">小组位置 ↗</a><a href="${e(link)}&print=all" target="_blank" rel="noreferrer" class="text-link">打印 ↗</a><button class="text-button" data-course-qr="${e(r.id)}" data-course-title="${e(r.title)}">二维码</button></div>`;
  }).join('') : '<p class="muted compact">本图尚未发布课程。</p>';
}
$('course-mode').insertAdjacentHTML('beforeend', '<option value="mixed_coverage">全员必到 + 其余点全班分担</option>');
$('course-mode').insertAdjacentHTML('beforeend', '<option value="custom">自定义每队路线</option>');
$('plan-button').insertAdjacentHTML('beforebegin', '<section id="custom-routes" class="custom-routes hidden"><div class="section-title"><h2>每队目标点序</h2><button id="custom-fill" class="text-button">按列表填入各队</button></div><p class="muted compact">按到访顺序填写目标编号，用空格、逗号或 → 分隔。也可在地图上依次点选。各点之间自动沿可通行路段连接。</p><div id="custom-team-routes"></div><p id="custom-route-error" class="danger" role="status"></p></section>');
$('custom-fill').onclick = () => {
  const selected = state?.controls.filter(c => c.required && (c.role ?? 'control') === 'control' && c.state !== 'rejected') ?? [];
  const order = rulePayload().mandatoryControlIds;
  const ids = $<HTMLInputElement>('mandatory-order').checked ? [...order, ...selected.map(c => c.id).filter(id => !order.includes(id))] : selected.map(c => c.id);
  for (const team of courseTeams()) customDrafts[team.id] = ids.map(id => routeToken(id, selected)).join(' → ');
  resetPlan(); persistRules(); renderCustomRoutes();
};
$('custom-team-routes').oninput = event => {
  const input = event.target as HTMLTextAreaElement, team = input.dataset.customTeam;
  if (team) { customDrafts[team] = input.value; resetPlan(); persistRules(); $('custom-route-error').textContent = ''; }
};
$('custom-team-routes').onclick = event => {
  const button = (event.target as HTMLElement).closest<HTMLElement>('[data-custom-map]');
  if (!button || !state?.registration) return;
  mapPicker.cancel();
  customMapTeam = button.dataset.customMap!;
  $('map-action-hint').innerHTML = `${e(customMapTeam)}：依次点选目标 <button id="finish-map-route" class="soft-button">完成点选</button>`;
  $('finish-map-route').onclick = () => { customMapTeam = null; $('map-action-hint').classList.add('hidden'); setMobilePanel(true); };
  $('map-action-hint').classList.remove('hidden'); setMobilePanel(false);
};
$('edit-terrain').insertAdjacentHTML('beforebegin', '<p class="muted">原图紫色连线只表示课程关系，不是实际道路。路线会按其下方的地形绕开水体、建筑和围墙。</p><button id="refresh-terrain" class="soft-button wide">更新通行识别（保留教师校核）</button>');
$('refresh-terrain').onclick = () => task(async () => {
  if (!state) throw new Error('请先完成地图识别');
  state = await runJob<MapState>(`/api/maps/${activeId}/terrain`, { version: state.version });
  resetPlan(); renderState(); await updateOverlays(); notify('通行识别已更新，请重新生成路线');
});
$('map-cards').insertAdjacentHTML('afterend', '<div id="analysis-update" class="notice-card hidden"><strong>本图需要更新识别结果</strong><p>更新目标点与通行识别后即可规划。已有校准、手工点位和通行修正会保留。</p><button id="refresh-recognition" class="soft-button wide">更新识别与通行</button></div>');
$('refresh-recognition').onclick = () => task(async () => {
  if (!state?.registration || state.registrationStale) throw new Error('请先完成当前地图的校准');
  await runJob<MapState>(`/api/maps/${activeId}/process?preserveRegistration=true`);
  catalog = await api('/api/catalog'); await loadState(activeId, false);
  notify('识别结果已更新，已保留校准和手工修正，请核对新候选点');
});
$('course-mode').insertAdjacentHTML('afterend', '<div id="mixed-rules" class="quality-card hidden"><strong>全班集合规则</strong><p id="mixed-rule-summary"></p><p class="muted compact">“每队必到”是全班集合点：各队到达并完成打点后统一出发，早到队伍须等待。等待时间计入路线用时和优化目标。</p><label class="check-label"><input id="mandatory-order" type="checkbox">指定每队必到点的先后顺序</label><div id="mandatory-order-list"></div><p id="mandatory-rule-error" class="danger"></p></div>');
$<HTMLInputElement>('mandatory-order').onchange = () => { resetPlan(); persistRules(); renderState(); };
$('sensor-status').insertAdjacentHTML('afterend', '<button id="calibrate-compass" class="text-button compass-calibration">校准罗盘</button>');
$('check-in').insertAdjacentHTML('afterend', '<button id="meeting-departure" class="primary-button wide hidden">全班已到齐，统一出发</button>');
$('next-control').insertAdjacentHTML('beforebegin', '<div id="team-sync"></div>');
// Another phone of this team may have checked in first; adopting its record
// is the student's choice, so an undo here is not silently reverted.
$('team-sync').onclick = event => {
  if (!(event.target as HTMLElement).closest('#adopt-team-progress')) return;
  const route = studentRoute(), team = classProgress?.teams.find(t => t.teamId === route?.teamId);
  if (!route || !team || courseSuperseded || viewing()) return;
  for (const id of team.completed) if (!completed.has(id)) { completed.add(id); if (Number.isFinite(team.checkIns[id])) checkInTimes[id] = team.checkIns[id]; }
  for (const id of team.departed) if (completed.has(id)) departedMeetings.add(id);
  if (team.arrived && route.controlIds.every(id => completed.has(id)) && arrivedAt === null) arrivedAt = team.arrivedAt ?? Date.now();
  saveProgress(); resetNavigation(); renderStudent(); notify('已同步本队其他手机的打点');
};
$('objective').insertAdjacentHTML('afterend', '<label class="field-label" for="solver-budget">新方案计算预算</label><select id="solver-budget"><option value="5">快速规划 · 5 秒</option><option value="30">较长计算 · 30 秒</option></select>');
// A different time budget does not invalidate the current route. Continuing
// that route uses its own button and saved inputs below the result heading.
$('pick-finish').insertAdjacentHTML('afterend', '<div id="endpoint-surfaces" class="hidden"><label class="field-label">起点层级<select id="start-surface"></select></label><label class="field-label">终点层级<select id="finish-surface"></select></label></div>');
$('start-surface').onchange = () => { startSurfaceId = value('start-surface') || null; if ($<HTMLInputElement>('return-start').checked) finishSurfaceId = startSurfaceId; resetPlan(); renderEndpointSurfaces(); };
$('finish-surface').onchange = () => { finishSurfaceId = value('finish-surface') || null; if (finishSurfaceId !== startSurfaceId) $<HTMLInputElement>('return-start').checked = false; resetPlan(); };
$('student-team').insertAdjacentHTML('afterend', '<label id="student-surface-field" class="field-label hidden">当前位置层级<select id="student-surface"><option value="">重合位置需要确认</option></select></label>');
$('student-surface').onchange = () => { surfaceSelectionPending = true; resetNavigation(); if (lastPosition && locationFresh) void updateNavigation(lastPosition, true); };

function renderEndpointSurfaces() {
  const surfaces = [{ id: 'ground', label: '地面', accepted: true }, ...(state?.surfaces ?? [])];
  const html = '<option value="">未指定（重合位置须选择）</option>'+surfaces.map(s => `<option value="${e(s.id)}" ${s.accepted ? '' : 'disabled'}>${e(s.label)}${s.accepted ? '' : '（待核验）'}</option>`).join('');
  if (!surfaces.some(s => s.id === startSurfaceId)) startSurfaceId = null;
  if (!surfaces.some(s => s.id === finishSurfaceId)) finishSurfaceId = null;
  $('endpoint-surfaces').classList.toggle('hidden', !(state?.surfaces?.length));
  $('start-surface').innerHTML = html; $('finish-surface').innerHTML = html;
  $<HTMLSelectElement>('start-surface').value = startSurfaceId ?? '';
  $<HTMLSelectElement>('finish-surface').value = finishSurfaceId ?? '';
}
if (import.meta.env.PROD) {
  void installOffline();
  $('course-title').insertAdjacentHTML('afterend', '<p id="offline-status" class="muted" role="status"></p><button id="save-offline" class="text-button">保存本课供离线查看</button>');
  $('save-offline').onclick = () => { void cacheStudentCourse(); };
}
// Expose only in development for deterministic camera/sensor browser checks.
if (import.meta.env.DEV) (window as Window & { dirmap?: unknown }).dirmap = { campusMap, sensors };

function notify(message: string, error = false) { $('toast').textContent = message; $('toast').classList.remove('hidden'); $('toast').classList.toggle('error', error); setTimeout(() => $('toast').classList.add('hidden'), 7000); }
class ApiError extends Error { constructor(message: string, public status: number) { super(message); } }
const pendingJobKey = () => workbenchKey('pending-job');
type PendingJob = { id?: string; requestId?: string };
function rememberJob(pending: PendingJob | null) {
  try { if (pending) localStorage.setItem(pendingJobKey(), JSON.stringify(pending)); else localStorage.removeItem(pendingJobKey()); }
  catch { /* The server history remains available if local storage is denied. */ }
}
function pendingJob(): PendingJob | null {
  try { const saved = JSON.parse(localStorage.getItem(pendingJobKey()) ?? 'null'); return saved && (typeof saved.id === 'string' || typeof saved.requestId === 'string') ? saved : null; }
  catch { return null; }
}
async function api<T = unknown>(url: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  const scope = workbenchScope(); if (scope !== null) headers.set('X-DirMap-Workbench', scope);
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  const token = sessionStorage.getItem('dirmap-admin-token'); if (token) headers.set('X-DirMap-Token', token);
  const response = await fetch(url, { ...options, headers, cache: 'no-store' });
  if (!response.ok) {
    let message = `${response.status}`, login = false;
    try { const data = await response.json(); message = typeof data.detail === 'string' ? data.detail : '输入格式不正确，请检查人数、队伍及点位'; login = data.login === true; if (data.accountChanged) location.reload(); } catch { /* response had no JSON */ }
    // A session that ended while working: sign in again in place.
    if (login && account && catalog && !courseId) openSignIn({ ...account, user: null });
    throw new ApiError(message, response.status);
  }
  return response.json() as Promise<T>;
}
async function runJob<T>(url: string, body?: object): Promise<T> {
  const requestId = typeof crypto.randomUUID === 'function' ? crypto.randomUUID()
    : Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
  rememberJob({ requestId });
  let job: Job<T>;
  try { job = await api<Job<T>>(url, { method: 'POST', headers: { 'X-DirMap-Request-Id': requestId }, body: JSON.stringify(body ?? {}) }); }
  catch (error) {
    if (error instanceof ApiError && error.status < 500) rememberJob(null);
    else notify('提交结果尚未确认；刷新页面或打开处理记录可检查原任务。', true);
    throw error;
  }
  rememberJob({ id: job.id, requestId });
  return watchJob(job);
}
let watchedJob: string | null = null;
async function watchJob<T>(initial: Job<T>): Promise<T> {
  let job = initial;
  $('job-status').classList.remove('hidden');
  watchedJob = job.id;
  try {
    for (;;) {
      $('job-message').textContent = job.message ?? '排队处理中'; $('job-percent').textContent = `${job.progress}%`; $<HTMLProgressElement>('job-progress').value = job.progress;
      $('cancel-job').classList.toggle('hidden', !['queued', 'running'].includes(job.state) || !!job.cancelRequested);
      if (job.state === 'succeeded' && job.resultPruned) { rememberJob(null); throw new Error('这条记录的计算结果已按保留期限清理，请重新生成'); }
      if (job.state === 'succeeded') { rememberJob(null); $('job-status').classList.add('hidden'); return job.result as T; }
      if (job.state === 'failed') { rememberJob(null); throw new Error(job.cancelled ? '任务已取消，原有数据保持不变' : job.message ?? '处理失败'); }
      await new Promise(resolve => setTimeout(resolve, 700));
      // A connection loss doesn't mean the server job failed or should restart.
      try { job = await api(`/api/jobs/${job.id}`); }
      catch (error) { if (error instanceof ApiError && error.status === 404) { rememberJob(null); throw error; } $('job-message').textContent = '连接中断，正在重连原任务…'; await new Promise(resolve => setTimeout(resolve, 2500)); }
    }
  } finally {
    if (watchedJob === job.id) { watchedJob = null; $('cancel-job').classList.add('hidden'); }
  }
}
$('cancel-job').onclick = async () => {
  if (!watchedJob) return;
  const button = $<HTMLButtonElement>('cancel-job'); button.disabled = true;
  try { const job = await api<Job>(`/api/jobs/${encodeURIComponent(watchedJob)}/cancel`, { method: 'POST' }); $('job-message').textContent = job.message ?? '正在取消'; button.classList.toggle('hidden', !!job.cancelRequested || !['queued', 'running'].includes(job.state)); }
  catch (error) { notify(error instanceof Error ? error.message : String(error), true); }
  finally { button.disabled = false; }
};
async function renderJobs() {
  if (courseId) return;
  const jobs = await api<Job[]>('/api/jobs');
  const names: Record<string, string> = { process: '识别与配准', terrain: '通行识别', anchors: '同名点校准', plan: '路线规划', auto_teams: '分队收益比较', choose_teams: '采用分队方案', refine_plan: '继续优化路线', confirm_plan: '复核原路线', site_reference: '下载场地参照', elevation_rebuild: '合并轨迹高程' };
  const states: Record<string, string> = { queued: '排队中', running: '处理中', succeeded: '已完成', failed: '未完成' };
  const status = (job: Job) => job.cancelled ? '已取消' : job.cancelRequested ? '正在取消' : job.resultPruned ? '结果已清理' : states[job.state];
  $('job-history-list').innerHTML = jobs.length ? jobs.map(job => `<button class="job-history-row" data-resume-job="${e(job.id)}"><span>${e(job.kind === 'elevation_rebuild' ? '共享地形' : catalog?.maps.find(map => map.id === job.assetId)?.name ?? catalog?.sites?.find(site => site.id === job.assetId)?.name ?? job.assetId)} · ${e(names[job.kind] ?? job.kind)}<small>${e(new Date(job.createdAt * 1000).toLocaleString())}${job.recovered ? ' · 已接续' : ''}</small></span><b>${e(status(job))} ↗</b></button>`).join('') : '<p class="muted compact">尚无处理任务。</p>';
}
function restorePlanFields(request: PlanRequest) {
  if (!state) return;
  const positions = new Map(request.controlIds.map((id, index) => [id, index]));
  state.controls.sort((a, b) => (positions.get(a.id) ?? 1000) - (positions.get(b.id) ?? 1000));
  state.controls.forEach(control => { control.required = positions.has(control.id); });
  $<HTMLSelectElement>('course-mode').value = request.mode;
  $<HTMLSelectElement>('objective').value = request.objective;
  $<HTMLInputElement>('use-elevation').checked = request.useElevation !== false;
  mandatory = new Set(request.mandatoryControlIds ?? []); missingPresetLabels = [];
  $<HTMLInputElement>('mandatory-order').checked = request.mandatoryOrder ?? false;
  $<HTMLInputElement>('people').value = String(request.teams.reduce((total, team) => total + team.people, 0));
  $<HTMLInputElement>('teams').value = String(request.teams.length);
  $<HTMLInputElement>('speed').value = String(request.teams[0]?.speedMps ?? 1.4);
  $<HTMLInputElement>('stagger').value = String((request.teams[1]?.departureSec ?? 0) - (request.teams[0]?.departureSec ?? 0));
  $<HTMLSelectElement>('solver-budget').value = request.timeLimitSec > 5 ? '30' : '5';
  restoredTeams = request.teams;
  customDrafts = Object.fromEntries((request.customRoutes ?? []).map(r => [r.teamId, r.controlIds.map(id => routeToken(id, state!.controls)).join(' → ')]));
  startPixel = request.startPixel; finishPixel = request.finishPixel;
  startSurfaceId = request.startSurfaceId ?? null; finishSurfaceId = request.finishSurfaceId ?? null;
  $<HTMLInputElement>('return-start').checked = JSON.stringify(startPixel) === JSON.stringify(finishPixel) && startSurfaceId === finishSurfaceId;
  campusMap.setEndpoint('start', pixelToLngLat(startPixel, state)); campusMap.setEndpoint('finish', pixelToLngLat(finishPixel, state));
  $('pick-start').querySelector('span')!.textContent = '已恢复规划起点'; $('pick-finish').querySelector('span')!.textContent = '已恢复规划终点';
  groupPreview(); persistRules(); renderState();
}
async function resumeJob(pending: PendingJob) {
  let job: Job<Plan | MapState>;
  try { job = await api(pending.id ? `/api/jobs/${encodeURIComponent(pending.id)}` : `/api/jobs/request/${encodeURIComponent(pending.requestId!)}`); }
  catch (error) { if (error instanceof ApiError && error.status === 404) rememberJob(null); throw error; }
  rememberJob({ id: job.id });
  const result = await watchJob(job);
  if (job.kind === 'elevation_rebuild') { notify('共享地形已合并最新轨迹，可在“共享地形”页查看'); return; }
  catalog = await api('/api/catalog');
  if (job.kind === 'site_reference') { await selectSite(job.assetId); notify('已载入场地参照'); return; }
  const owner = catalog.maps.find(map => map.id === job.assetId);
  if (owner && owner.siteId !== activeSite) { activeSite = owner.siteId; renderCatalog(); void showSiteReference(); }
  if (!await loadState(job.assetId)) return;
  if (['plan', 'auto_teams', 'choose_teams', 'refine_plan', 'confirm_plan'].includes(job.kind)) {
    const restored = result as Plan;
    if (restored.version !== state?.version || restored.routingSchema !== __DIRMAP_ROUTING_SCHEMA__ || state.registrationStale) throw new Error('这份路线所用地图或算法已更新，请重新生成路线');
    if (restored.request?.teams?.length) restorePlanFields(restored.request);
    showPlan(restored);
  }
  notify(job.recovered ? '已恢复中断任务的结果' : '已载入原任务结果');
}
function needsRecognitionUpdate() {
  return !!state && (!!state.terrainStale || state.terrainSchema !== __DIRMAP_TERRAIN_SCHEMA__);
}
function syncTaskButtons() {
  // A place switch while another task runs would be ignored, leaving the
  // picker out of step with the listed maps.
  for (const id of ['process-current', 'process-all', 'upload-button', 'refresh-terrain', 'refresh-recognition', 'site-select', 'add-site']) $<HTMLButtonElement>(id).disabled = busy;
  $<HTMLButtonElement>('plan-button').disabled = busy || needsRecognitionUpdate();
  $<HTMLButtonElement>('recommend-teams').disabled = busy || needsRecognitionUpdate() || value('course-mode') === 'custom';
  $('auto-team-hint').textContent = value('course-mode') === 'custom' ? '自定义点序已确定队数；切换其他访问方式后可自动比较。' : '';
  document.querySelectorAll<HTMLButtonElement>('[data-map], [data-delete-map], [data-site-action]').forEach(button => { button.disabled = busy; });
  const refine = document.querySelector<HTMLButtonElement>('#refine-plan');
  if (refine) refine.disabled = busy || !canRefine(plan);
  for (const id of ['review-button', 'customize-plan']) {
    const button = document.getElementById(id) as HTMLButtonElement | null;
    if (button) button.disabled = busy;
  }
  document.querySelectorAll<HTMLButtonElement>('[data-choose-count]').forEach(button => {
    const rec = plan?.teamRecommendation;
    button.disabled = busy || !rec?.comparisonId || !rec.comparisonToken || Number(button.dataset.chooseCount) === (rec.selectedCount ?? rec.recommendedCount);
  });
}
async function task(action: () => Promise<void>) {
  if (busy) { notify('上一个操作仍在进行，请稍候'); return; }
  busy = true;
  syncTaskButtons();
  try { await action(); } catch (error) { notify(error instanceof Error ? error.message : String(error), true); $('job-status').classList.add('hidden'); }
  finally { busy = false; syncTaskButtons(); void renderJobs().catch(() => {}); }
}
function resetPlan() { planRevision++; plan = null; selectedTeam = null; campusMap.setRoutes([]); campusMap.setApproaches([]); $('route-results').innerHTML = ''; $('plan-errors').innerHTML = ''; $('publish-section').classList.add('hidden'); }
function rulesKey() { return workbenchKey('rules:' + (catalog.maps.find(m => m.id === activeId)?.sha256 ?? activeId)); }
function restoreAutoTeamFields(settings: Pick<AutoTeamRequest, 'minPeoplePerTeam' | 'maxTeams' | 'targetGainFraction'>) {
  if (Number.isInteger(settings.minPeoplePerTeam) && settings.minPeoplePerTeam >= 1 && settings.minPeoplePerTeam <= 500) $<HTMLInputElement>('auto-min-people').value = String(settings.minPeoplePerTeam);
  if (Number.isInteger(settings.maxTeams) && settings.maxTeams >= 1 && settings.maxTeams <= 30) $<HTMLInputElement>('auto-max-teams').value = String(settings.maxTeams);
  if (Number.isFinite(settings.targetGainFraction) && settings.targetGainFraction >= .5 && settings.targetGainFraction <= 1) $<HTMLInputElement>('auto-gain').value = String(settings.targetGainFraction * 100);
}
function persistRules() {
  try { localStorage.setItem(rulesKey(), JSON.stringify({ mode: value('course-mode'), objective: value('objective'), useElevation: $<HTMLInputElement>('use-elevation').checked, mandatory: [...mandatory], order: $<HTMLInputElement>('mandatory-order').checked, selected: state?.controls.filter(c => c.required).map(c => c.id), customDrafts, teamInputs: courseTeams(), autoTeams: { minPeoplePerTeam: number('auto-min-people'), maxTeams: number('auto-max-teams'), targetGainFraction: number('auto-gain') / 100 } })); }
  catch { /* Planning still works if private mode prevents local preferences. */ }
}
function initializeRules() {
  mandatory = new Set(); missingPresetLabels = [];
  customDrafts = {}; restoredTeams = null; customMapTeam = null;
  $<HTMLInputElement>('mandatory-order').checked = false;
  $<HTMLInputElement>('use-elevation').checked = true;
  restoreAutoTeamFields({ minPeoplePerTeam: 2, maxTeams: 30, targetGainFraction: .9 });
  const allowed = ['all_visit', 'split_coverage', 'fixed_order', 'mixed_coverage', 'custom'];
  try {
    const saved = JSON.parse(localStorage.getItem(rulesKey()) ?? 'null');
    if (saved && allowed.includes(saved.mode) && Array.isArray(saved.mandatory) && saved.mandatory.every((id: unknown) => typeof id === 'string')) {
      if (saved.autoTeams) restoreAutoTeamFields(saved.autoTeams);
      $<HTMLInputElement>('use-elevation').checked = saved.useElevation !== false;
      if (['min_max_finish_then_sum', 'min_total_time', 'min_distance'].includes(saved.objective)) $<HTMLSelectElement>('objective').value = saved.objective;
      $<HTMLSelectElement>('course-mode').value = saved.mode;
      mandatory = new Set(saved.mandatory.filter((id: string) => state?.controls.some(c => c.id === id && (c.role ?? 'control') === 'control')));
      $<HTMLInputElement>('mandatory-order').checked = saved.order === true;
      if (Array.isArray(saved.selected) && state) for (const c of state.controls) c.required = (c.role ?? 'control') === 'control' && (saved.selected.includes(c.id) || mandatory.has(c.id));
      if (saved.customDrafts && typeof saved.customDrafts === 'object') customDrafts = Object.fromEntries(Object.entries(saved.customDrafts).filter(([, draft]) => typeof draft === 'string')) as Record<string, string>;
      if (Array.isArray(saved.teamInputs) && saved.teamInputs.length && saved.teamInputs.every((t: PlanRequest['teams'][number]) => typeof t.id === 'string' && Number.isInteger(t.people) && t.people > 0 && Number.isFinite(t.speedMps))) {
        restoredTeams = saved.teamInputs;
        $<HTMLInputElement>('teams').value = String(restoredTeams!.length);
        $<HTMLInputElement>('people').value = String(restoredTeams!.reduce((sum, t) => sum+t.people, 0));
        $<HTMLInputElement>('speed').value = String(restoredTeams![0].speedMps);
        $<HTMLInputElement>('stagger').value = String((restoredTeams![1]?.departureSec ?? 0)-(restoredTeams![0].departureSec ?? 0));
      }
      return;
    }
  } catch { /* Use this sheet's explicit default rules. */ }
  const defaults = catalog.maps.find(m => m.id === activeId)?.courseDefaults;
  $<HTMLSelectElement>('course-mode').value = defaults?.mode ?? 'all_visit';
  if (defaults && state) {
    for (const label of defaults.mandatoryLabels) {
      const matches = state.controls.filter(c => c.label === label && (c.role ?? 'control') === 'control' && c.state !== 'rejected');
      if (matches.length === 1) mandatory.add(matches[0].id); else missingPresetLabels.push(label);
    }
    for (const c of state.controls) c.required = (c.role ?? 'control') === 'control' && c.state !== 'rejected';
    $<HTMLInputElement>('mandatory-order').checked = defaults.mandatoryOrder;
  }
}
function rulePayload() {
  const mixed = ['mixed_coverage', 'custom'].includes(value('course-mode'));
  const ids = [...mandatory].filter(id => state?.controls.some(c => c.id === id && c.required && c.state !== 'rejected'));
  return { mandatoryControlIds: mixed ? ids : [], mandatoryOrder: mixed && $<HTMLInputElement>('mandatory-order').checked };
}
function renderMixedRules() {
  const mixed = ['mixed_coverage', 'custom'].includes(value('course-mode')); $('mixed-rules').classList.toggle('hidden', !mixed);
  if (!mixed) return;
  const rules = rulePayload();
  const labels = rules.mandatoryControlIds.map(id => state?.controls.find(c => c.id === id)?.label ?? id);
  const total = state?.controls.filter(c => c.required && (c.role ?? 'control') === 'control' && c.state !== 'rejected').length ?? 0;
  $('mixed-rule-summary').textContent = `每队必到：${labels.join('、') || '尚未选择'}；其余 ${total - labels.length} 点${value('course-mode') === 'custom' ? '按自定义路线分配' : '全班分担'}。${rules.mandatoryOrder ? '集合点按下列顺序访问，可在其间插入其他目标。' : '各队集合顺序须一致，自动规划会联合选择顺序。'}`;
  $('mandatory-order-list').innerHTML = rules.mandatoryOrder ? rules.mandatoryControlIds.map((id, i) => `<div class="mandatory-order-row"><span>${i + 1}. ${e(labels[i])}</span><button class="text-button" data-mandatory-up="${e(id)}" aria-label="上移必到点 ${e(labels[i])}">↑</button><button class="text-button" data-mandatory-down="${e(id)}" aria-label="下移必到点 ${e(labels[i])}">↓</button></div>`).join('') : '';
  $('mandatory-rule-error').textContent = missingPresetLabels.length ? `尚未识别或编号重复：${missingPresetLabels.join('、')}。请校核后明确勾选每队必到点。` : '';
}
function courseTeams(): PlanRequest['teams'] {
  if (restoredTeams) return restoredTeams;
  const count = number('teams'), people = number('people');
  if (!Number.isInteger(count) || count < 1 || count > 30) return [];
  return Array.from({ length: count }, (_, i) => ({ id: `第 ${i+1} 队`, people: Math.floor(people/count)+(i < people%count ? 1 : 0), speedMps: number('speed'), departureSec: i*number('stagger') }));
}
function renderCustomRoutes() {
  const active = value('course-mode') === 'custom';
  $('custom-routes').classList.toggle('hidden', !active);
  if (!active) return;
  const open = new Set(Array.from(document.querySelectorAll<HTMLDetailsElement>('.custom-team[open]')).map(el => el.dataset.teamIndex));
  $('custom-team-routes').innerHTML = courseTeams().map((team, i) => `<details class="custom-team" data-team-index="${i}" ${open.has(String(i)) || (!open.size && i === 0) ? 'open' : ''}><summary>${e(team.id)} · ${team.people} 人</summary><label class="field-label" for="custom-team-${i}">目标点序</label><textarea id="custom-team-${i}" data-custom-team="${e(team.id)}" aria-label="${e(team.id)} 点序" rows="3" placeholder="例如：31 → 42 → 32 → 33">${e(customDrafts[team.id] ?? '')}</textarea><button class="soft-button" data-custom-map="${e(team.id)}">在地图上为${e(team.id)}点选</button></details>`).join('');
}
const currentSite = (): Site | undefined => catalog?.sites?.find(site => site.id === activeSite);
const siteMaps = () => catalog.maps.filter(map => (map.siteId ?? 'szu-yuehai') === activeSite);
function renderCatalog() {
  if (!catalog.sites.some(site => site.id === activeSite)) activeSite = catalog.sites[0]?.id ?? 'szu-yuehai';
  $<HTMLSelectElement>('site-select').innerHTML = catalog.sites.map(site => `<option value="${e(site.id)}">${e(site.name)}${site.status === 'ready' ? '' : site.status === 'failed' ? '（参照下载失败）' : '（正在下载参照）'}</option>`).join('');
  $<HTMLSelectElement>('site-select').value = activeSite;
  const site = currentSite();
  $('site-name').textContent = site?.name ?? '定向场地';
  const status = $('site-status');
  status.classList.toggle('hidden', !site || (site.builtin && site.status === 'ready'));
  if (site && !site.builtin) status.innerHTML = site.status === 'ready'
    ? `OpenStreetMap 参照 ${site.featureCount ?? 0} 个地物 · <button class="text-button" data-site-action="refresh" ${busy ? 'disabled' : ''}>重新下载</button> <button class="text-button" data-site-action="delete" ${busy ? 'disabled' : ''}>删除场地</button>`
    : site.status === 'failed' ? `<span class="danger">参照下载失败：${e(site.error ?? '请稍后重试')}</span> <button class="text-button" data-site-action="refresh" ${busy ? 'disabled' : ''}>重试</button> <button class="text-button" data-site-action="delete" ${busy ? 'disabled' : ''}>删除场地</button><br>没有参照时也可导入地图，用同名点手动配准。`
    : '正在下载这个场地的 OpenStreetMap 参照…';
  const maps = siteMaps();
  $('map-cards').innerHTML = maps.length ? maps.map((map, i) => `<div class="map-card-wrap"><button class="map-card ${map.id === activeId ? 'active' : ''}" data-map="${e(map.id)}" ${busy ? 'disabled' : ''}><span class="map-index">${String(i + 1).padStart(2, '0')}</span><span class="map-card-name">${e(map.name)}<small>${map.processed ? `${map.controls} 个候选目标` : '原图待处理'}</small></span><span class="map-card-state">${map.processed ? '✓' : '○'}</span></button>${map.builtin ? '' : `<button class="map-delete text-button" data-delete-map="${e(map.id)}" aria-label="删除地图 ${e(map.name)}" title="删除地图" ${busy ? 'disabled' : ''}>×</button>`}</div>`).join('')
    : `<div class="empty-small">${e(site?.name ?? '这个场地')}还没有地图。点右上角“＋ 导入”，上传定向地图的照片或扫描件（JPEG/PNG）。</div>`;
  $('legend-links').innerHTML = catalog.legends.map(l => `<a href="${e(l.url)}" target="_blank" rel="noreferrer">${e(l.name.includes('aigc') ? '随图定向运动图例（含 AIGC 标记）' : l.name)} <span>↗</span></a>`).join('');
}
async function showSiteReference() {
  const site = activeSite;
  try { const data = await api<object>(`/api/reference?site=${encodeURIComponent(site)}`); if (site === activeSite) campusMap.setReference(data); }
  catch { if (site === activeSite) campusMap.setReference({ type: 'FeatureCollection', features: [] }); }
}
/** Switch place: its reference, view and first map. */
async function selectSite(id: string, mapId?: string) {
  activeSite = id;
  try { localStorage.setItem(workbenchKey('site'), id); } catch { /* the choice lasts this page */ }
  renderCatalog();
  void showSiteReference();
  const site = currentSite();
  const target = mapId ?? siteMaps()[0]?.id;
  // A place chosen while this one was loading keeps the view.
  const current = await loadState(target ?? '');
  if (current && site && activeSite === id && !state?.extent) campusMap.fitBounds(site.bbox);
}
$('site-select').onchange = () => task(() => selectSite(value('site-select')));
$('add-site').onclick = () => {
  const dialog = simpleDialog('添加场地', `<p class="muted">先把地图移动、缩放到场地所在范围（边长 150 m 到 3 km），再填写名称。服务器会从 OpenStreetMap 下载这个范围内的建筑、水体、道路和绿地，用于自动配准和显示，约需 10–60 秒。</p><label class="field-label">场地名称<input id="site-name-input" maxlength="40" placeholder="例如：莲花山公园"></label><p id="site-add-error" class="danger" role="status"></p><button id="site-add-confirm" class="primary-button wide">用当前地图视野创建场地</button><p class="muted compact">没有 OpenStreetMap 地物的地方，也可以导入地图后用同名点手动配准。</p>`);
  dialog.querySelector<HTMLButtonElement>('#site-add-confirm')!.onclick = () => {
    const name = dialog.querySelector<HTMLInputElement>('#site-name-input')!.value.trim();
    if (!name) { dialog.querySelector('#site-add-error')!.textContent = '请填写场地名称'; return; }
    const extent = campusMap.map.getView().calculateExtent(campusMap.map.getSize());
    const bbox = transformExtent(extent, 'EPSG:3857', 'EPSG:4326');
    dialog.close();
    void task(async () => {
      const added = await api<{ site: Site; job: Job<Site> }>('/api/sites', { method: 'POST', body: JSON.stringify({ name, bbox }) });
      catalog = await api('/api/catalog'); activeSite = added.site.id; renderCatalog();
      try { await watchJob(added.job); notify(`已下载${name}的参照，可以导入这个场地的地图`); }
      finally { catalog = await api('/api/catalog'); await selectSite(added.site.id); }
    });
  };
};
$('site-status').onclick = event => {
  const action = (event.target as HTMLElement).closest<HTMLElement>('[data-site-action]')?.dataset.siteAction;
  const site = currentSite();
  if (!action || !site || site.builtin) return;
  if (action === 'delete' && !confirm(`删除场地“${site.name}”？场地中没有地图时才能删除。`)) return;
  void task(async () => {
    if (action === 'delete') { await api(`/api/sites/${encodeURIComponent(site.id)}`, { method: 'DELETE' }); catalog = await api('/api/catalog'); await selectSite(catalog.sites[0].id); return; }
    const job = await api<Job<Site>>(`/api/sites/${encodeURIComponent(site.id)}/reference`, { method: 'POST' });
    try { await watchJob(job); notify('已更新场地参照'); }
    finally { catalog = await api('/api/catalog'); renderCatalog(); void showSiteReference(); }
  });
};
async function loadState(id: string, fit = true) {
  // The latest map choice wins. A slower earlier response must not pair one
  // map's controls with another map's ID, so both change together here.
  const load = ++stateLoad;
  // A place without maps yet has no map to select.
  const next = id ? await api<MapState & { processed: boolean }>(`/api/maps/${id}`) : { processed: false } as MapState & { processed: boolean };
  if (load !== stateLoad) return false;
  activeId = id; state = next.processed ? next : null; fieldCheckPoints = []; campusMap.setFieldChecks([]); anchors = []; renderAnchors(); resetPlan(); startPixel = finishPixel = null; startSurfaceId = finishSurfaceId = null;
  initializeRules();
  campusMap.setEndpoint('start', null); campusMap.setEndpoint('finish', null); mapPicker.cancel(); $('map-action-hint').classList.add('hidden');
  $('pick-start').querySelector('span')!.textContent = '在地图选起点'; $('pick-finish').querySelector('span')!.textContent = '在地图选终点';
  const starts = state?.controls.filter(c => ['start', 'start_finish'].includes(c.role ?? '') && c.calibrated && c.state !== 'rejected') ?? [];
  const finishes = state?.controls.filter(c => ['finish', 'start_finish'].includes(c.role ?? '') && c.calibrated && c.state !== 'rejected') ?? [];
  if (state && starts.length === 1) {
    startPixel = starts[0].approachPixel ?? starts[0].pixel;
    startSurfaceId = starts[0].surfaceId ?? null;
    campusMap.setEndpoint('start', pixelToLngLat(startPixel, state));
    $('pick-start').querySelector('span')!.textContent = '已识别原图起点';
  }
  if (state && finishes.length === 1) {
    finishPixel = finishes[0].approachPixel ?? finishes[0].pixel;
    finishSurfaceId = finishes[0].surfaceId ?? null;
    campusMap.setEndpoint('finish', pixelToLngLat(finishPixel, state));
    $('pick-finish').querySelector('span')!.textContent = '已识别原图终点';
    $<HTMLInputElement>('return-start').checked = JSON.stringify(startPixel) === JSON.stringify(finishPixel) && startSurfaceId === finishSurfaceId;
  }
  renderCatalog(); renderState(); if (id) void renderReleases(); void renderFieldChecks(); await updateOverlays(); if (fit && state?.extent && load === stateLoad) campusMap.fit(state.extent);
  return load === stateLoad;
}
async function updateOverlays() {
  const update = ++overlayUpdate;
  let states = state ? [state] : [];
  if ($<HTMLInputElement>('stitch-maps').checked) {
    const all = await Promise.all(siteMaps().filter(m => m.processed && m.id !== activeId).map(m => api<MapState>(`/api/maps/${m.id}`)));
    if (update !== overlayUpdate) return;
    states = [...all, ...states];
  }
  campusMap.setOverlays(states, number('overlay-opacity') / 100, activeId);
  campusMap.setTerrain(state, $<HTMLInputElement>('show-terrain').checked);
}
function renderState() {
  syncTaskButtons();
  renderEndpointSurfaces();
  $('analysis-update').classList.toggle('hidden', !needsRecognitionUpdate() || !state?.registration || !!state?.registrationStale);
  renderMixedRules();
  renderCustomRoutes();
  groupPreview();
  campusMap.setControls(state?.controls ?? [], state?.surfaces ?? []);
  $('export-map').setAttribute('href', activeId ? `/api/maps/${activeId}/export` : '#');
  $('release-badge').textContent = state?.reviewed ? '已核验' : '备课草稿';
  $('map-title').textContent = state?.name ?? catalog.maps.find(map => map.id === activeId)?.name ?? currentSite()?.name ?? '定向场地';
  if (!state) {
    $('map-subtitle').textContent = activeId ? '原图待处理' : '尚未导入地图';
    $('map-summary').textContent = activeId ? '原图尚未处理' : '导入这个场地的定向地图后开始准备课程';
    $('control-list').innerHTML = `<div class="empty-small">${activeId ? '点击“自动识别与配准”开始。' : '先导入地图。'}</div>`;
    $('control-count').textContent = '尚未识别目标';
    $('registration-quality').textContent = '完成自动处理后显示匹配结果。';
    $('terrain-summary').textContent = '完成自动处理后显示通行候选。';
    return;
  }
  $('map-subtitle').textContent = state.registrationStale ? '基准图已更新，请重新配准' : state.registration ? '已叠合至标准地图 · 待现场核对' : '地理参照待补充';
  $('map-summary').innerHTML = `<span><b>${state.controls.length}</b> 候选目标</span><span><b>${state.quality?.fitCount ?? 0}</b> 配准点</span><span class="review-dot">待复核</span>`;
  $('control-count').textContent = `${state.controls.filter(c => c.required).length} / ${state.controls.length} 个目标已选`;
  const numbered = state.controls.filter(c => (c.role ?? 'control') === 'control').map(c => c.label);
  $('control-list').innerHTML = state.controls.map((c, index) => `<div class="control-row" data-id="${e(c.id)}"><label class="control-toggle"><input type="checkbox" data-required="${e(c.id)}" ${c.role && c.role !== 'control' ? 'disabled' : ''} ${c.required ? 'checked' : ''}><span class="control-number ${c.calibrated ? '' : 'uncalibrated'}">${e(c.label)}</span></label>${['mixed_coverage', 'custom'].includes(value('course-mode')) && (c.role ?? 'control') === 'control' ? `<label class="every-team-toggle"><input type="checkbox" data-mandatory="${e(c.id)}" aria-label="${e(c.label)} 每队必到" ${mandatory.has(c.id) ? 'checked' : ''}>每队必到</label>` : ''}<span class="control-state">${(c.role ?? 'control') === 'control' && numbered.filter(label => label === c.label).length > 1 ? '<b class="danger">编号重复</b> · ' : ''}${!c.calibrated ? '待补配准' : c.state === 'accepted' ? '已确认' : '待核对'}</span><button class="text-button" data-control-edit="${e(c.id)}" aria-label="编辑目标 ${e(c.label)}">↗</button>${value('course-mode') === 'fixed_order' ? `<button class="text-button" data-up="${index}" aria-label="上移 ${e(c.label)}">↑</button><button class="text-button" data-down="${index}" aria-label="下移 ${e(c.label)}">↓</button>` : ''}</div>`).join('');
  const q = state.quality;
  $('registration-quality').innerHTML = q ? `<span class="quality-label">${q.baseAssetId ? '共同地物对齐' : '标准地图地物匹配'}</span><div class="quality-values"><div><strong>${q.fitCount}</strong><span>拟合同名点</span></div><div><strong>${q.p95ErrorM?.toFixed(1) ?? '—'}<small> m</small></strong><span>留出误差 P95</span></div></div><p>${e(q.validationScope)}</p>${q.inheritedGeographicP95M ? `<p>基准图地理匹配 P95：${q.inheritedGeographicP95M.toFixed(1)} m</p>` : ''}` : '自动匹配证据不足，可添加同名点完成校准。';
  $('terrain-summary').innerHTML = `<strong>${Math.round(state.statistics.forbiddenPixelFraction * 100)}%</strong> 像素被识别为障碍候选<p>${state.edits.length} 处教师通行修正</p><p>${state.statistics.staircaseCandidates ?? 0} 处楼梯候选，可在原图中勾选查看；楼梯按较慢速度估计。</p><p>路线会检查每条边经过的工作图像素。未知层级及模糊的门洞仍需核验。</p>`;
}
async function saveEdits(controls: Control[], edits: TerrainEdit[], surfaces?: Surface[], portals?: Portal[]) {
  if (!state) return;
  // Send mutable control fields only; server-derived positions are recomputed.
  const bodyControls = controls.map(c => ({ id: c.id, label: c.label, pixel: c.pixel, approachPixel: c.approachPixel ?? null,
    surfaceId: c.surfaceId ?? null, role: c.role ?? 'control', radiusPx: c.radiusPx, required: c.required, state: c.state, serviceBaseSec: c.serviceBaseSec, servicePerPersonSec: c.servicePerPersonSec }));
  state = await api<MapState>(`/api/maps/${activeId}`, { method: 'PATCH', body: JSON.stringify({ version: state.version, controls: bodyControls, edits, surfaces, portals }) });
  resetPlan(); renderState(); await updateOverlays(); notify('修改已保存，请重新生成路线');
}
function openEditor(mode: 'inspect' | 'close' | 'anchor' = 'inspect', id: string | null = null) {
  if (!state) { notify('请先识别原图'); return; }
  mapPicker.cancel();
  const source = state;
  new PixelEditor(source, saveEdits, pixel => { void (async () => {
    if (state !== source) return;
    const purpose = $<HTMLInputElement>('validation-anchor').checked ? 'validation' : 'fit';
    campusMap.setOpacity(.1);
    const lnglat = await mapPicker.choose('在标准地图选择同一建筑角或路口', { back: () => openEditor('anchor') });
    campusMap.setOpacity(number('overlay-opacity') / 100);
    if (!lnglat || state !== source) return;
    anchors.push({ pixel, lnglat, purpose }); renderAnchors(); notify('同名点已添加，地图上保留编号标记');
  })();
  }, plan?.approaches ?? []).open(mode, id);
}
type FieldCheckReport = { checks: { id: string; errorM: number | null; accuracyM: number; usable: boolean; insideRegistration: boolean; recordedAt: number; lnglat: Pixel; predictedLnglat: Pixel | null }[];
  summary: { count: number; usable: number; medianM?: number; p95M?: number; maxM?: number; accuracyMedianM?: number; meetsThresholds?: boolean;
    thresholds: { medianM: number; p95M: number; maxM: number }; maxAccuracyM: number; minimumForVerdict: number } };
let fieldRender = 0;
let fieldCheckPoints: FieldCheckReport['checks'] = [];
const registrationTabOpen = () => !$('tab-registration').classList.contains('hidden');
async function renderFieldChecks() {
  if (courseId || !state) return;
  const render = ++fieldRender, assetId = activeId;
  let result: FieldCheckReport;
  try { result = await api<FieldCheckReport>(`/api/maps/${encodeURIComponent(assetId)}/field-checks`); } catch { return; }
  if (render !== fieldRender || assetId !== activeId) return;
  fieldCheckPoints = result.checks; campusMap.setFieldChecks(registrationTabOpen() ? fieldCheckPoints : []);
  const s = result.summary, t = s.thresholds;
  const verdict = s.meetsThresholds === undefined ? `至少 ${s.minimumForVerdict} 个可用点后对照门槛。` : s.meetsThresholds ? '达到独立精度门槛。' : '未达到独立精度门槛，可补同名点重新校准后复查。';
  $('field-check-summary').innerHTML = s.usable ? `<p><strong>${s.usable}</strong> 个可用点（共 ${s.count} 个）：中位 ${s.medianM} m · P95 ${s.p95M} m · 最大 ${s.maxM} m。门槛为中位 ≤${t.medianM}、P95 ≤${t.p95M}、最大 ≤${t.maxM} m；${verdict}</p><p class="muted compact">误差含手机定位误差（本组精度中位 ${s.accuracyMedianM} m）；精度差于 ${s.maxAccuracyM} m 或在配准范围外的点不计入。</p>`
    : s.count ? `<p>已记录 ${s.count} 个点，暂无可用点：定位精度需在 ${s.maxAccuracyM} m 以内且位于配准范围内。</p>` : '';
  $('field-check-list').innerHTML = result.checks.map((c, i) => `<div><span>${i + 1}. ${c.errorM === null ? '配准范围外' : `误差 ${c.errorM.toFixed(1)} m`}</span><small>定位精度 ${Math.round(c.accuracyM)} m${c.usable ? '' : ' · 不计入'}</small><button data-remove-field-check="${e(c.id)}" class="text-button" aria-label="删除核查点 ${i + 1}">×</button></div>`).join('');
}
$('field-check-list').addEventListener('click', event => {
  const id = (event.target as HTMLElement).closest<HTMLElement>('[data-remove-field-check]')?.dataset.removeFieldCheck;
  if (id) void task(async () => { await api(`/api/maps/${encodeURIComponent(activeId)}/field-checks/${encodeURIComponent(id)}`, { method: 'DELETE' }); await renderFieldChecks(); });
});
$('add-field-check').onclick = () => task(async () => {
  mapPicker.cancel();
  if (!state?.registration || state.registrationStale) throw new Error('请先完成当前地图的配准');
  if (!navigator.geolocation) throw new Error('此浏览器不能定位');
  $('field-check-summary').textContent = '正在获取当前位置…';
  const fix = await new Promise<GeolocationPosition>((resolve, reject) => navigator.geolocation.getCurrentPosition(resolve, reject, { enableHighAccuracy: true, maximumAge: 0, timeout: 20000 }))
    .catch(() => { void renderFieldChecks(); throw new Error('没有取得当前位置，请允许定位后到开阔处重试'); });
  notify(fix.coords.accuracy > 10 ? `定位精度约 ${Math.round(fix.coords.accuracy)} m，会保存但不计入统计；可到开阔处重试` : `定位精度约 ${Math.round(fix.coords.accuracy)} m，请在原图上点出你所站的位置`);
  const pixel = await new Promise<Pixel | null>(resolve => new PixelEditor(state!, saveEdits, resolve, [], () => resolve(null)).open('field'));
  if (!pixel) { void renderFieldChecks(); return; }
  await api(`/api/maps/${encodeURIComponent(activeId)}/field-checks`, { method: 'POST', body: JSON.stringify({ pixel, lnglat: [fix.coords.longitude, fix.coords.latitude], accuracyM: fix.coords.accuracy, measuredAt: Date.now() }) });
  await renderFieldChecks(); notify('已添加核查点');
});
function renderAnchors() {
  campusMap.setAnchors(anchors);
  $('anchor-list').innerHTML = anchors.map((a, i) => `<div><span>${i + 1}. ${a.purpose === 'fit' ? '拟合点' : '核查点'}</span><small>${a.lnglat[0].toFixed(6)}, ${a.lnglat[1].toFixed(6)}</small><button data-remove-anchor="${i}" class="text-button" aria-label="删除同名点 ${i + 1}">×</button></div>`).join('');
  $<HTMLButtonElement>('fit-anchors').disabled = anchors.filter(a => a.purpose === 'fit').length < 3;
}
function groupPreview() {
  const people = number('people'), teams = number('teams');
  $('group-preview').textContent = Number.isInteger(people) && Number.isInteger(teams) && teams > 0 && teams <= people ? `${teams} 队，每队 ${Math.floor(people / teams)}${people % teams ? `–${Math.ceil(people / teams)}` : ''} 人 · ${number('stagger') ? `间隔 ${number('stagger')} 秒出发` : '同时出发'}` : '队数不能超过人数，均须为正整数';
}
function showPlan(result: Plan) {
  plan = result;
  campusMap.setApproaches(result.approaches ?? []);
  if (result.status === 'NEEDS_REVIEW') {
    $('plan-errors').innerHTML = `<div class="notice-card"><strong>这些位置需要核对后再规划</strong>${result.unresolved!.map(item => `<p>${e(state?.controls.find(c => c.id === item.id)?.label ?? ({ start: '起点', finish: '终点' }[item.id] ?? item.id))}：${e(item.message)}${state?.controls.some(c => c.id === item.id) ? ` <button data-control-edit="${e(item.id)}" class="text-button">原图修正 ↗</button>` : ''}</p>`).join('')}</div>`;
    return;
  }
  campusMap.setRoutes(result.routes);
  $('route-results').innerHTML = `<div class="result-heading"><h2>队伍路线</h2><span class="status-tag ${result.status === 'OPTIMAL' ? 'success' : ''}">${result.status === 'FIXED' ? '按自定义点序' : result.status === 'OPTIMAL' ? '模型内已证明最优' : '当前最佳可行'}</span></div><p class="muted compact">基于当前通行图；${result.draft ? '核验后可发布给学生。' : '可以发布给学生。'}</p>${result.routes.map((r, i) => `<button class="route-card" data-team="${e(r.teamId)}" style="--team-color:${teamColors[i % teamColors.length]}"><span class="route-name"><i></i>${e(r.teamId)}<small>${r.people} 人</small></span><span class="route-metrics"><strong>${distance(r.distanceM)}</strong><span>${minutes(r.durationSec)}</span></span><span class="route-stops">${r.controlIds.map(id => e(state?.controls.find(c => c.id === id)?.label ?? id)).join(' → ')}</span><span class="route-detail">${r.departureSec ? `${Math.round(r.departureSec)} 秒后出发 · ` : ''}打点 ${Math.round(r.serviceSec)} 秒 · 集合等待 ${minutes(r.waitSec ?? 0)} · 开课后 ${minutes(r.finishSec)} 完成</span></button>`).join('')}<button id="export-route" class="text-button wide">下载路线 GeoJSON</button>`;
  $('publish-section').classList.remove('hidden');
  $('route-results').querySelector('.result-heading')?.insertAdjacentHTML('afterend', refinementHtml(result));
  if (result.coverage?.mode === 'mixed_coverage') {
    const ids = result.coverage.mandatoryControlIds;
    $('route-results').insertAdjacentHTML('afterbegin', `<div id="mixed-coverage-result" class="notice-card"><strong>已验证混合覆盖</strong><p>每队均到访 ${ids.map(id => e(state?.controls.find(c => c.id === id)?.label ?? id)).join('、')}；其余 ${result.coverage.sharedControlIds.length} 个点均已分配。${result.coverage.mandatoryOrder ? '必到点按指定顺序访问。' : '集合顺序由规划统一安排。'}</p></div>`);
  }
  if (result.proof?.scope) $('route-results').insertAdjacentHTML('afterbegin', `<p class="muted compact">${e(result.proof.scope)}</p>`);
  const transitions = result.routes.flatMap(r => r.transitions ?? []);
  if (transitions.length) {
    const names = Object.fromEntries((result.surfaces ?? []).map(s => [s.id, s.label]));
    const unique = [...new Map(transitions.map(t => [t.portalId+':'+t.fromSurface, t])).values()];
    $('route-results').insertAdjacentHTML('afterbegin', `<div class="notice-card"><strong>路线含换层入口</strong>${unique.map(t => `<p>${e(t.label)}：${e(names[t.fromSurface])} → ${e(names[t.toSurface])}</p>`).join('')}<p class="muted compact">平面相交处不会直接换层。发布前请核对入口及各目标的到访层级。</p></div>`);
  }
  if (result.teamRecommendation) {
    restoreAutoTeamFields(result.teamRecommendation.sourceRequest);
    persistRules();
    $('route-results').insertAdjacentHTML('afterbegin', teamComparisonHtml(result.teamRecommendation));
  }
  const suggestions = result.approaches?.filter(a => a.automatic) ?? [];
  if (suggestions.length) {
    $('route-results').insertAdjacentHTML('afterbegin', `<div class="notice-card"><strong>${suggestions.length} 处到访位置建议</strong><p>路线停在可通行的地物外沿；紫色圈心保持原位。请核对橙色到访标记。</p>${suggestions.map(a => `<p>${e(state?.controls.find(c => c.id === a.controlId)?.label)}：距圈心 ${a.offsetM.toFixed(1)} m <button class="text-button" data-approach-review="${e(a.controlId)}">在原图核对 ↗</button></p>`).join('')}</div>`);
    document.querySelectorAll<HTMLButtonElement>('[data-approach-review]').forEach(button => { button.onclick = () => openEditor('inspect', button.dataset.approachReview!); });
  }
  $('export-route').onclick = () => download('dirmap-routes.geojson', { type: 'FeatureCollection', features: result.routes.map(r => ({ type: 'Feature', properties: { team: r.teamId, distanceM: r.distanceM, durationSec: r.durationSec, graphHash: result.graphHash }, geometry: { type: 'LineString', coordinates: r.geometry } })) });
  for (const card of Array.from($('route-results').querySelectorAll<HTMLElement>('.route-card'))) {
    const route = result.routes.find(r => r.teamId === card.dataset.team);
    if (route) card.insertAdjacentHTML('beforeend', elevationSummary(route));
  }
  $('route-results').insertAdjacentHTML('beforeend', '<button id="customize-plan" class="soft-button wide">以这些路线为基础，自定义各队点序</button>');
  $('customize-plan').onclick = () => {
    if (result.request) restorePlanFields(result.request);
    $<HTMLSelectElement>('course-mode').value = 'custom';
    customDrafts = Object.fromEntries(result.routes.map(r => [r.teamId, r.controlIds.map(id => routeToken(id, state!.controls)).join(' → ')]));
    resetPlan(); persistRules(); renderState();
    $('custom-routes').scrollIntoView({ block: 'nearest' });
  };
  if (result.rendezvous?.length) {
    $('route-results').insertAdjacentHTML('beforeend', `<details class="meeting-schedule"><summary>查看各集合点的到达、等待与出发时间</summary><p class="muted compact">以下均从开课计时；早到队伍完成打点后等待，全部队伍准备好才统一出发。</p><div class="schedule-scroll"><table><thead><tr><th>点位</th><th>队伍</th><th>到达</th><th>等待</th><th>统一出发</th></tr></thead><tbody>${result.rendezvous.map(m => result.routes.map(r => { const stop = r.stops?.find(s => s.controlId === m.controlId); return stop ? `<tr><td>${e(state?.controls.find(c => c.id === m.controlId)?.label ?? m.controlId)}</td><td>${e(r.teamId)}</td><td>${minutes(stop.arrivalSec)}</td><td>${minutes(stop.waitSec)}</td><td>${minutes(stop.departureSec)}</td></tr>` : ''; }).join('')).join('')}</tbody></table></div></details>`);
  }
}
function download(name: string, data: unknown) { const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })); const link = document.createElement('a'); link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }

$('map-cards').onclick = event => {
  const remove = (event.target as HTMLElement).closest<HTMLElement>('[data-delete-map]');
  if (remove) {
    const map = catalog.maps.find(m => m.id === remove.dataset.deleteMap);
    if (!map || busy || !confirm(`删除地图“${map.name}”？原图、识别结果和路线将一并删除，用它发布的课程会失效。`)) return;
    void task(async () => { await api(`/api/maps/${encodeURIComponent(map.id)}`, { method: 'DELETE' }); catalog = await api('/api/catalog'); await selectSite(activeSite, map.id === activeId ? undefined : activeId); notify('地图已删除'); });
    return;
  }
  const button = (event.target as HTMLElement).closest<HTMLElement>('[data-map]'); if (button && !busy) task(async () => { await loadState(button.dataset.map!); });
};
$('process-current').onclick = () => task(async () => { if (!activeId) throw new Error('请先导入这个场地的地图'); await runJob(`/api/maps/${activeId}/process`); catalog = await api('/api/catalog'); await loadState(activeId); notify('识别与配准完成，请核对候选点和通行区域'); });
$('process-all').onclick = () => task(async () => { const maps = siteMaps(); if (!maps.length) throw new Error('请先导入这个场地的地图'); for (const map of maps) { await runJob(`/api/maps/${map.id}/process`); } catalog = await api('/api/catalog'); $<HTMLInputElement>('stitch-maps').checked = true; await loadState(activeId || maps[0].id); });
$('upload-button').onclick = () => $<HTMLInputElement>('upload-input').click();
$<HTMLInputElement>('upload-input').onchange = event => task(async () => { const file = (event.target as HTMLInputElement).files?.[0]; if (!file) return; const form = new FormData(); form.append('file', file); (event.target as HTMLInputElement).value = ''; const asset = await api<{ id: string }>(`/api/maps/upload?siteId=${encodeURIComponent(activeSite)}`, { method: 'POST', body: form }); catalog = await api('/api/catalog'); renderCatalog(); await loadState(asset.id); });
document.querySelectorAll<HTMLButtonElement>('[data-tab]').forEach(button => button.onclick = () => { document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('active', b === button)); document.querySelectorAll('.tab-content').forEach(tab => tab.classList.toggle('hidden', tab.id !== `tab-${button.dataset.tab}`)); campusMap.setFieldChecks(registrationTabOpen() ? fieldCheckPoints : []); });
for (const id of ['people', 'teams', 'stagger', 'speed']) $<HTMLInputElement>(id).oninput = () => { restoredTeams = null; groupPreview(); renderCustomRoutes(); resetPlan(); persistRules(); };
$<HTMLSelectElement>('objective').onchange = () => { resetPlan(); persistRules(); };
$<HTMLInputElement>('use-elevation').onchange = () => { resetPlan(); persistRules(); };
$<HTMLSelectElement>('course-mode').onchange = () => { missingPresetLabels = []; customMapTeam = null; mapPicker.cancel(); $('map-action-hint').classList.add('hidden'); resetPlan(); persistRules(); renderState(); };
for (const id of ['pick-start', 'pick-finish']) $(id).onclick = async () => {
  if (!state?.registration || state.registrationStale) { notify('请先完成配准'); return; }
  const source = state, start = id === 'pick-start';
  const lnglat = await mapPicker.choose(`设置${start ? '起点' : '终点'}`, {
    validate: point => lngLatToPixel(point, source) ? null : '超出已校准范围，请补充同名点',
  });
  if (!lnglat || state !== source) return;
  const pixel = lngLatToPixel(lnglat, state)!;
  if (start) {
    startPixel = pixel; campusMap.setEndpoint('start', lnglat); $('pick-start').querySelector('span')!.textContent = '起点已设置';
    if ($<HTMLInputElement>('return-start').checked) {
      finishPixel = pixel; finishSurfaceId = startSurfaceId; campusMap.setEndpoint('finish', lnglat);
      $('pick-finish').querySelector('span')!.textContent = '回到起点结束';
    }
  } else {
    finishPixel = pixel; campusMap.setEndpoint('finish', lnglat); $('pick-finish').querySelector('span')!.textContent = '终点已设置';
    $<HTMLInputElement>('return-start').checked = false;
  }
  resetPlan(); renderEndpointSurfaces();
};
$<HTMLInputElement>('return-start').onchange = () => { if ($<HTMLInputElement>('return-start').checked) { finishPixel = startPixel; finishSurfaceId = startSurfaceId; if (startPixel && state) campusMap.setEndpoint('finish', pixelToLngLat(startPixel, state)); } resetPlan(); renderEndpointSurfaces(); };
campusMap.onClick = (lnglat, id) => {
  if (mapPicker.pick(lnglat)) return;
  if (customMapTeam && state) {
    const control = state.controls.find(c => c.id === id && c.state !== 'rejected' && (c.role ?? 'control') === 'control');
    if (!control) { notify('请点击地图上的目标圆圈'); return; }
    const token = routeToken(control.id, state.controls), previous = customDrafts[customMapTeam] ?? '';
    if (previous.split(/[\s,，、;；→>]+/u).includes(token)) { notify('本队已包含这个目标'); return; }
    customDrafts[customMapTeam] = previous.trim() ? previous.trim()+' → '+token : token;
    control.required = true; resetPlan(); persistRules(); renderState(); notify(`已为${customMapTeam}添加目标 ${control.label}`); return;
  }
  if (id && !courseId) openEditor('inspect', id);
};
campusMap.onDrag = () => { follow = false; if (sensors.active) $('follow-label').textContent = '继续跟随'; };
document.body.addEventListener('click', event => {
  const job = (event.target as HTMLElement).closest<HTMLElement>('[data-resume-job]');
  if (job) { void task(() => resumeJob({ id: job.dataset.resumeJob })); return; }
  const refine = (event.target as HTMLElement).closest<HTMLElement>('[data-refine-plan]');
  if (refine) {
    void task(async () => {
      if (!state || !plan || !canRefine(plan)) throw new Error('这份路线不能继续优化，请先重新生成可行方案');
      const source = plan, revision = planRevision;
      const result = await runJob<Plan>('/api/plans/refine', { assetId: activeId, version: state.version,
        planId: source.id, timeLimitSec: 30 });
      if (revision !== planRevision) throw new Error('计算期间课程设置已改变，继续优化的结果已保留在处理记录中');
      if (!result.request?.teams.length) throw new Error('无法恢复继续优化的方案');
      resetPlan(); restorePlanFields(result.request); showPlan(result);
      notify(result.refinement?.improved ? '本轮已找到更好的路线，队数保持不变' : '本轮未找到更好的路线，已保留原方案');
    });
    return;
  }
  const choice = (event.target as HTMLElement).closest<HTMLElement>('[data-choose-count]');
  if (choice) {
    void task(async () => {
      const rec = plan?.teamRecommendation;
      if (!state || !rec?.comparisonId || !rec.comparisonToken) throw new Error('请先重新比较队数');
      const count = Number(choice.dataset.chooseCount), revision = planRevision;
      const result = await runJob<Plan>('/api/plans/choose-team-count', { assetId: activeId, version: state.version,
        comparisonId: rec.comparisonId, comparisonToken: rec.comparisonToken, teamCount: count });
      if (revision !== planRevision) throw new Error('选择期间课程设置已改变，请重新选择方案');
      if (!result.request?.teams?.length) throw new Error('无法恢复该方案，请重新比较');
      resetPlan(); restorePlanFields(result.request); showPlan(result);
      const details = document.querySelector<HTMLDetailsElement>('.team-comparison');
      if (details) details.open = true;
      notify(`已采用 ${count} 队方案，保留原比较中的点序`);
    });
    return;
  }
  const reorder = (event.target as HTMLElement).closest<HTMLElement>('[data-mandatory-up],[data-mandatory-down]');
  if (reorder) {
    const ids = [...mandatory], id = reorder.dataset.mandatoryUp ?? reorder.dataset.mandatoryDown!;
    const from = ids.indexOf(id), to = from + (reorder.dataset.mandatoryUp !== undefined ? -1 : 1);
    if (from >= 0 && to >= 0 && to < ids.length) { [ids[from], ids[to]] = [ids[to], ids[from]]; mandatory = new Set(ids); resetPlan(); persistRules(); renderMixedRules(); }
    return;
  }
  const el = (event.target as HTMLElement).closest<HTMLElement>('[data-control-edit],[data-up],[data-down],[data-remove-anchor],[data-team]'); if (!el) return;
  if (el.dataset.controlEdit) openEditor('inspect', el.dataset.controlEdit);
  else if (el.dataset.removeAnchor !== undefined) { anchors.splice(Number(el.dataset.removeAnchor), 1); renderAnchors(); }
  else if (el.dataset.team) { selectedTeam = selectedTeam === el.dataset.team ? null : el.dataset.team; campusMap.setRoutes(plan?.routes ?? [], selectedTeam); document.querySelectorAll('[data-team]').forEach(card => card.classList.toggle('selected', (card as HTMLElement).dataset.team === selectedTeam)); }
  else if (state) { const from = Number(el.dataset.up ?? el.dataset.down), to = from + (el.dataset.up !== undefined ? -1 : 1); if (to >= 0 && to < state.controls.length) { [state.controls[from], state.controls[to]] = [state.controls[to], state.controls[from]]; resetPlan(); renderState(); } }
});
$('control-list').onchange = event => {
  const input = event.target as HTMLInputElement, id = input.dataset.mandatory ?? input.dataset.required;
  const c = state?.controls.find(c => c.id === id); if (!c) return;
  if (input.dataset.mandatory) { if (input.checked) { mandatory.add(c.id); c.required = true; } else mandatory.delete(c.id); missingPresetLabels = []; }
  else { c.required = input.checked; if (!c.required) mandatory.delete(c.id); }
  resetPlan(); persistRules(); renderState();
};
$('edit-controls').onclick = () => openEditor(); $('edit-terrain').onclick = () => openEditor('close'); $('add-anchor').onclick = () => openEditor('anchor');
$('fit-anchors').onclick = () => task(async () => { if (!state) return; state = await runJob<MapState>(`/api/maps/${activeId}/anchors`, { version: state.version, anchors }); resetPlan(); renderState(); await updateOverlays(); if (state.extent) campusMap.fit(state.extent); });
$<HTMLInputElement>('standard-map').onchange = event => campusMap.setStandardMap((event.target as HTMLInputElement).checked);
$<HTMLInputElement>('overlay-opacity').oninput = () => campusMap.setOpacity(number('overlay-opacity') / 100);
$<HTMLInputElement>('stitch-maps').onchange = () => task(updateOverlays);
$<HTMLInputElement>('show-terrain').onchange = () => campusMap.setTerrain(state, $<HTMLInputElement>('show-terrain').checked);
function planInputs(): Omit<PlanRequest, 'teams' | 'customRoutes'> {
  if (!state?.registration) throw new Error('请先识别并校准地图'); if (!startPixel) throw new Error('请在地图上选择起点');
  const finish = $<HTMLInputElement>('return-start').checked ? startPixel : finishPixel; if (!finish) throw new Error('请在地图上选择终点');
  const speed = number('speed'), stagger = number('stagger');
  if (!Number.isFinite(speed) || speed < .2 || speed > 10 || !Number.isFinite(stagger) || stagger < 0) throw new Error('请填写有效速度和出发间隔');
  const controlIds = state.controls.filter(c => c.required && c.state !== 'rejected').map(c => c.id); if (!controlIds.length) throw new Error('请至少选择一个目标点');
  const rules = rulePayload();
  if (value('course-mode') === 'mixed_coverage' && (missingPresetLabels.length || !rules.mandatoryControlIds.length)) throw new Error('请明确选择所有每队必到点后再规划');
  return { assetId: activeId, version: state.version, controlIds, startPixel, finishPixel: finish, startSurfaceId,
    finishSurfaceId: $<HTMLInputElement>('return-start').checked ? startSurfaceId : finishSurfaceId,
    mode: value('course-mode'), objective: value('objective'), ...rules, timeLimitSec: number('solver-budget'), useElevation: $<HTMLInputElement>('use-elevation').checked };
}
$('plan-button').onclick = () => task(async () => {
  const input = planInputs();
  const people = number('people'), teams = number('teams');
  if (![people, teams].every(Number.isInteger) || people < 1 || teams < 1 || teams > people || teams > 30) throw new Error('请填写有效人数与队数，队数不能超过人数');
  const rules = rulePayload();
  let customRoutes: PlanRequest['customRoutes'] = [];
  if (value('course-mode') === 'custom') {
    try { customRoutes = parseCustomRoutes(customDrafts, courseTeams(), state!.controls, rules.mandatoryControlIds, rules.mandatoryOrder); }
    catch (error) { $('custom-route-error').textContent = error instanceof Error ? error.message : String(error); throw error; }
  }
  resetPlan();
  const revision = planRevision;
  const result = await runJob<Plan>('/api/plans', { ...input, customRoutes, teams: courseTeams() });
  if (revision !== planRevision) throw new Error('计算期间规则已改变，请按新规则重新生成路线');
  showPlan(result);
});
$('recommend-teams').onclick = () => task(async () => {
  const input = planInputs();
  if (input.mode === 'custom') throw new Error('自定义点序已确定队数，请切换其他访问方式');
  const totalPeople = number('people'), minPeoplePerTeam = number('auto-min-people'), maxTeams = number('auto-max-teams');
  const targetGainFraction = number('auto-gain') / 100;
  if (![totalPeople, minPeoplePerTeam, maxTeams].every(Number.isInteger) || totalPeople < 1 || totalPeople > 500 || minPeoplePerTeam < 1 || minPeoplePerTeam > totalPeople || maxTeams < 1 || maxTeams > 30) throw new Error('请检查总人数、每队最少人数和最多队数');
  if (!Number.isFinite(targetGainFraction) || targetGainFraction < .5 || targetGainFraction > 1) throw new Error('收益保留比例应为 50%–100%');
  const request: AutoTeamRequest = { ...input, totalPeople, minPeoplePerTeam, maxTeams, targetGainFraction, speedMps: number('speed'), staggerSec: number('stagger') };
  resetPlan(); persistRules();
  const revision = planRevision;
  const result = await runJob<Plan>('/api/plans/auto-teams', request);
  if (revision !== planRevision) throw new Error('比较期间规则已改变，请按新规则重新计算');
  if (result.request?.teams?.length) restorePlanFields(result.request);
  showPlan(result);
});

$('follow-button').onclick = async () => { if (courseSuperseded) { notify('旧路线已失效，请使用教师提供的新课程链接', true); return; } northOnly = false; follow = true; await sensors.start(); if (sensors.active) $('follow-label').textContent = '跟随已开启'; };
$('north-button').onclick = () => { northOnly = true; campusMap.northUp(); $('compass-arrow').style.transform = 'rotate(0deg)'; $('sensor-status').textContent = '北向上 · 点击跟随按钮恢复朝向跟随'; };
sensors.onHeading = (sample: HeadingSample | null) => {
  campusMap.setLocationHeading(sample);
  if (!sample) { if (sensors.active && !northOnly) $('sensor-status').textContent = '等待可靠朝向 · 可缓慢走动或校准罗盘'; return; }
  if (follow && !northOnly) { campusMap.setHeading(sample.degrees); $('compass-arrow').style.transform = `rotate(${-sample.degrees}deg)`; }
  if (!northOnly) $('sensor-status').textContent = `${sample.source === 'compass' ? sample.reference === 'true' ? '罗盘方向' : '罗盘方向（北向待校准）' : '行进方向'} ${Math.round(sample.degrees)}°${follow ? ' · 跟随中' : ' · 已暂停跟随'}`;
};
sensors.onPosition = position => {
  lastPosition = position; campusMap.setLocation(position, follow); groupPanel?.draw();
  $('location-status').textContent = `定位精度 ±${Math.round(position.coords.accuracy)} m`;
  if (courseId) { renderStudent(); void updateNavigation(position); }
};
sensors.onStatus = message => { $('sensor-status').textContent = message; notify(message); };
sensors.onLocationStatus = message => { $('location-status').textContent = message; };
sensors.onLocationFreshness = fresh => {
  locationFresh = fresh;
  if (!fresh && lastPosition) campusMap.setLocation(lastPosition, false, false);
  if (courseId) renderStudent();
};
window.addEventListener('pagehide', () => { resumeSensors = sensors.active; sensors.stop(); });
window.addEventListener('pageshow', event => { if (event.persisted && resumeSensors && !courseSuperseded) void sensors.start(); });

function showCourseQr(id: string, title: string) {
  const link = new URL(`/?course=${id}`, location.href).href;
  simpleDialog('扫码进入课程', `<p class="course-qr-title">${e(title)}</p><img class="course-qr" src="/api/releases/${e(id)}/qr.png?origin=${encodeURIComponent(location.origin)}" alt="课程二维码"><p class="muted compact course-qr-link">${e(link)}</p><p class="muted compact">学生用微信或相机扫码打开，选择本队后开始。投屏时可放大浏览器窗口。</p>`);
}
document.addEventListener('click', event => {
  const button = (event.target as HTMLElement).closest<HTMLElement>('[data-course-qr]');
  if (button) showCourseQr(button.dataset.courseQr!, button.dataset.courseTitle ?? '定向越野课');
});
function simpleDialog(title: string, body: string): HTMLDialogElement {
  const dialog = document.createElement('dialog'); dialog.className = 'info-dialog'; dialog.innerHTML = `<div class="dialog-heading"><h2>${e(title)}</h2><button class="icon-button" data-close aria-label="关闭">×</button></div>${body}`;
  document.body.append(dialog); dialog.querySelector<HTMLButtonElement>('[data-close]')!.onclick = () => dialog.close(); dialog.addEventListener('close', () => dialog.remove(), { once: true }); dialog.showModal(); return dialog;
}
$('calibrate-compass').onclick = () => {
  const dialog = simpleDialog('校准手机朝向', `<p>在开阔处平放手机，将屏幕上沿对准已知的真北方向。默认真北为 0°；也可以输入已知地物的方位角。</p><label class="field-label">已知方位角（顺时针，度）<input id="known-bearing" type="number" min="0" max="359" value="0"></label><p id="compass-calibration-error" class="danger" role="status"></p><button id="confirm-compass" class="primary-button wide">已对准，校准当前方向</button><button id="clear-compass-calibration" class="text-button wide">恢复设备原始方向</button><p class="muted">校准只影响罗盘；行进方向仍由定位数据计算。远离金属和磁铁后可重新校准。</p>`);
  dialog.querySelector<HTMLButtonElement>('#confirm-compass')!.onclick = () => {
    try { sensors.calibrate(number('known-bearing')); dialog.close(); dialog.remove(); notify('已校准罗盘方向'); }
    catch (error) { $('compass-calibration-error').textContent = error instanceof Error ? error.message : String(error); }
  };
  dialog.querySelector<HTMLButtonElement>('#clear-compass-calibration')!.onclick = () => { sensors.clearCalibration(); dialog.close(); dialog.remove(); notify('已恢复设备方向'); };
};
$('open-help').onclick = () => {
  const dialog = simpleDialog('使用 DirMap', `<ol class="help-list"><li>选择场地（深大粤海校区、大沙河公园，或用“＋ 场地”添加其他地点），导入或选择课程地图，点击“自动识别与配准”。同一场地的图幅可以叠合。</li><li>在“原图校核”中核对目标编号和到访位置。配准偏差较大时，在原图与标准地图选择分散的同名点。</li><li>核对围墙、门洞、禁区与桥隧。自动识别是候选结果，未核验时路线保留为草稿。</li><li>填写人数、队数与课程规则，选择起终点并生成路线。“全班分担”与“每队全点”有不同要求。</li><li>发布前确认课程，学生通过课程链接选队、定位并打点。手机传感器需 HTTPS 和浏览器权限。</li></ol><p class="muted">标准地图及校园参照 © OpenStreetMap contributors。三张扫描图无 GPS 元数据；自动匹配误差不等价于现场精度。</p><button id="location-diagnostics" class="soft-button">定位诊断与重试</button>${account?.user ? '' : '<label class="field-label section-gap">原工作台的教师口令（未登录时使用）<input id="admin-token" type="password" autocomplete="off"></label><button id="save-token" class="soft-button">保存到本次会话</button>'}<p class="muted section-gap">注册账户后，每人有独立的工作台，在自己的工作台中识别地图、生成路线和发布课程不需要教师口令。学生使用课程链接或组队码，无需注册。</p>`);
  const saveToken = dialog.querySelector<HTMLButtonElement>('#save-token');
  if (saveToken) saveToken.onclick = () => { sessionStorage.setItem('dirmap-admin-token', value('admin-token')); notify('已保存本次会话口令'); };
  dialog.querySelector<HTMLButtonElement>('#location-diagnostics')!.onclick = () => { dialog.close(); openLocationDiagnostics(); };
};

function openLocationDiagnostics() {
  const dialog = simpleDialog('定位诊断', `<p class="muted">这里显示定位是否持续更新。灰色位置标记表示上次位置，正在等待新定位。</p><textarea id="location-diagnostic-text" rows="12" readonly aria-label="定位诊断信息"></textarea><p class="muted">诊断仅显示在本机，不含经纬度，可复制发回排查。</p><button id="retry-location" class="soft-button">重新定位</button> <button id="copy-location-diagnostics" class="soft-button">复制诊断</button><p id="location-diagnostic-copy-status" role="status"></p>`);
  const output = dialog.querySelector<HTMLTextAreaElement>('#location-diagnostic-text')!;
  const refresh = () => { output.value = JSON.stringify({ ...sensors.locationDiagnostics(),
    secureContext: window.isSecureContext, visibility: document.visibilityState,
    deviceTime: Date.now(), pageTimeOrigin: performance.timeOrigin,
    origin: location.origin, userAgent: navigator.userAgent }, null, 2); };
  refresh();
  const timer = setInterval(refresh, 1000);
  dialog.addEventListener('close', () => clearInterval(timer), { once: true });
  dialog.querySelector<HTMLButtonElement>('#retry-location')!.onclick = () => {
    if (courseSuperseded) { notify('旧路线已失效，请使用教师提供的新课程链接', true); return; }
    if (sensors.active) sensors.retryLocation(); else void sensors.start();
    refresh();
  };
  dialog.querySelector<HTMLButtonElement>('#copy-location-diagnostics')!.onclick = async () => {
    refresh();
    try { await navigator.clipboard.writeText(output.value); $('location-diagnostic-copy-status').textContent = '诊断已复制'; }
    catch { clearInterval(timer); output.focus(); output.select(); $('location-diagnostic-copy-status').textContent = '请长按选中的文字复制'; }
  };
}
$('review-button').onclick = () => {
  if (!state || !plan?.routes.length) return;
  const suggested = plan.approaches?.filter(a => a.automatic) ?? [];
  const dialog = simpleDialog('发布前核验', `<p class="muted">请结合标准地图、原图和现场情况核验本次课程。配准或通行修改后需要重新规划。</p><label class="check-label"><input id="review-registration" type="checkbox">配准位置与实际地物一致</label><label class="check-label"><input id="review-controls" type="checkbox">本次目标编号、圈心与到访位置已核对</label><label class="check-label"><input id="review-passages" type="checkbox">路线不穿越禁区、围墙及不可通行的层级</label><label class="field-label">核验备注<textarea id="review-notes" rows="3" placeholder="现场核验日期、临时封闭路段等"></textarea></label><p id="review-error" class="danger" role="status"></p><button id="publish-confirm" class="primary-button wide">生成学生课程链接</button>`);
  if (suggested.length) dialog.querySelector('#review-error')!.insertAdjacentHTML('beforebegin', `<label class="check-label"><input id="review-approaches" type="checkbox">已核对并采用 ${suggested.length} 处橙色标记的建议到访位置</label>`);
  dialog.querySelector<HTMLButtonElement>('#publish-confirm')!.onclick = async () => {
    const button = dialog.querySelector<HTMLButtonElement>('#publish-confirm')!; button.disabled = true;
    try {
      if (!['registration', 'controls', 'passages'].every(name => $<HTMLInputElement>(`review-${name}`).checked)) throw new Error('请完成三项核验后发布');
      if (suggested.length && !$<HTMLInputElement>('review-approaches').checked) throw new Error('请先在原图核对并确认建议的到访位置');
      // Explicit teacher action confirms the selected targets and creates a
      // new version. Saved calculations can revalidate the exact route.
      const previousPlan = plan!;
      const controls = state!.controls.map(c => {
        if (!c.required) return c;
        const approach = suggested.find(a => a.controlId === c.id);
        return { ...c, state: 'accepted' as const, ...(approach ? { approachPixel: approach.pixel } : {}) };
      });
      await saveEdits(controls, state!.edits);
      state = await api(`/api/maps/${activeId}/review`, { method: 'POST', body: JSON.stringify({ version: state!.version, registrationChecked: true, controlsChecked: true, passagesChecked: true, notes: value('review-notes') }) });
      // Use the exact existing team inputs and selected controls, not stale
      // geometry from the previous version after confirmation.
      const automatic = previousPlan.teamRecommendation?.sourceRequest;
      const choice = previousPlan.teamRecommendation;
      const preserveChoice = choice?.selectionMode === 'teacher' && choice.comparisonId && choice.comparisonToken;
      const result = previousPlan.continuation
        ? await runJob<Plan>('/api/plans/confirm', { assetId: activeId, version: state!.version, planId: previousPlan.id })
        : preserveChoice
        ? await runJob<Plan>('/api/plans/choose-team-count', { assetId: activeId, version: state!.version,
            comparisonId: choice.comparisonId, comparisonToken: choice.comparisonToken,
            teamCount: choice.selectedCount, confirmPlanId: previousPlan.id })
        : await runJob<Plan>(automatic ? '/api/plans/auto-teams' : '/api/plans', { ...(automatic ?? previousPlan.request!), version: state!.version });
      if (result.request?.teams?.length) restorePlanFields(result.request);
      showPlan(result); if (!result.routes.length) throw new Error('核验后有点位不可达，请重新检查');
      const title = `${currentSite()?.shortName ?? currentSite()?.name ?? '校园'}定向越野课`;
      const release = await api<{ url: string }>('/api/releases', { method: 'POST', body: JSON.stringify({ assetId: activeId, planId: result.id, title }) });
      const link = new URL(release.url, location.href).href; $('share-result').innerHTML = `<label class="field-label">学生课程链接<input readonly value="${e(link)}" aria-label="学生课程链接"></label><a href="${e(link)}" target="_blank" rel="noreferrer" class="text-link">打开学生页面 ↗</a><a href="${e(link)}#progress" target="_blank" rel="noreferrer" class="text-link">查看全班进度 ↗</a><a href="${e(link)}#groups" target="_blank" rel="noreferrer" class="text-link">各小组实时位置 ↗</a><a href="${e(link)}&print=all" target="_blank" rel="noreferrer" class="text-link">打印各队路线 ↗</a><button class="text-button" data-course-qr="${e(new URL(link).searchParams.get('course') ?? '')}" data-course-title="${e(title)}">课程二维码</button>`;
      dialog.close(); dialog.remove(); renderState(); void renderReleases(); notify('已生成学生课程链接');
    } catch (error) { $('review-error').textContent = error instanceof Error ? error.message : String(error); button.disabled = false; }
  };
};

function studentRoute(): Route | undefined { return plan?.routes.find(r => r.teamId === value('student-team')); }
let classStart: { startedAt: number | null; offset: number } | null = null;
function rememberClassStart(startedAt: number | null, serverTime: number) {
  // Phone clocks drift; count from the server's start using its own time.
  classStart = { startedAt, offset: serverTime - Date.now() / 1000 };
  try { localStorage.setItem(`dirmap-class-start:${courseId}`, JSON.stringify(classStart)); } catch { /* the clock still runs this session */ }
  renderClassClock();
}
function loadClassStart() {
  try { const saved = JSON.parse(stored(`dirmap-class-start:${courseId}`) ?? 'null'); if (saved && Number.isFinite(saved.offset)) classStart = saved; } catch { /* no saved start */ }
  renderClassClock();
}
const clockText = (seconds: number) => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
function renderClassClock() {
  const route = studentRoute(); if (!route || !courseId) return;
  if (!classStart?.startedAt) { $('class-clock').textContent = '等待教师开始计时'; $('class-clock').classList.remove('due'); return; }
  const elapsed = Math.max(0, Date.now() / 1000 + classStart.offset - classStart.startedAt), wait = route.departureSec - elapsed;
  const together = !plan?.routes.some(r => r.departureSec > 0);
  $('class-clock').textContent = `开课 ${clockText(elapsed)} · ${together ? '统一出发' : wait > 0 ? `本队还有 ${clockText(wait)} 出发` : '本队已可出发'}`;
  $('class-clock').classList.toggle('due', route.departureSec > 0 && wait > 0 && wait <= 60);
}
let fallbackDevice: string | null = null;
function deviceId() {
  // A random per-browser label lets the server order this phone's reports;
  // it identifies no person and is never combined with a location.
  try {
    let id = localStorage.getItem('dirmap-device-id');
    if (!id || !/^[A-Za-z0-9_-]{8,64}$/.test(id)) { id = crypto.randomUUID().replaceAll('-', ''); localStorage.setItem('dirmap-device-id', id); }
    return id;
  } catch { return fallbackDevice ??= crypto.randomUUID().replaceAll('-', ''); }
}
function saveProgress() {
  const key = progressKey();
  try {
    localStorage.setItem(key, JSON.stringify([...completed]));
    localStorage.setItem(key + ':departed-meetings', JSON.stringify([...departedMeetings]));
    localStorage.setItem(key + ':times', JSON.stringify(checkInTimes));
    localStorage.setItem(key + ':arrived', JSON.stringify(arrivedAt));
    localStorage.setItem(key + ':revision', String(Number(localStorage.getItem(key + ':revision') ?? '0') + 1));
    localStorage.setItem(key + ':unsent', '1');
  } catch { /* progress still works on this page; sharing waits for storage */ }
  void syncProgress();
}
function stored(key: string) { try { return localStorage.getItem(key); } catch { return null; } }
async function syncProgress() {
  const route = studentRoute(), key = progressKey();
  if (!route || !courseId || courseSuperseded || syncingProgress || stored(key + ':unsent') !== '1') return;
  const revision = Number(stored(key + ':revision') ?? '0');
  const done = route.controlIds.filter(id => completed.has(id));
  syncingProgress = true;
  try {
    classProgress = await api<ClassProgress>(`/api/releases/${courseId}/progress`, { method: 'POST', signal: AbortSignal.timeout(15000), body: JSON.stringify({
      teamId: route.teamId, deviceId: deviceId(), revision, completed: done, departed: [...departedMeetings].filter(id => done.includes(id)),
      arrived: arrivedAt !== null && done.length === route.controlIds.length, arrivedAt,
      checkIns: Object.fromEntries(done.filter(id => Number.isFinite(checkInTimes[id])).map(id => [id, checkInTimes[id]])) }) });
    if (stored(key + ':revision') === String(revision)) localStorage.removeItem(key + ':unsent');
    renderStudent();
  } catch (error) {
    // Offline or unreachable: keep it for the next poll. A rejected report
    // would be rejected again, so stop retrying it.
    if (error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408 && error.status !== 429) localStorage.removeItem(key + ':unsent');
  } finally {
    syncingProgress = false;
    // A check-in made while this report was in flight is sent right away,
    // not at the next poll; a failed report waits for that poll instead.
    if (stored(key + ':unsent') === '1' && stored(key + ':revision') !== String(revision)) void syncProgress();
  }
}
async function refreshClassProgress() {
  if (!courseId || courseSuperseded) return;
  try { classProgress = await api<ClassProgress>(`/api/releases/${courseId}/progress`, { signal: AbortSignal.timeout(15000) }); renderStudent(); }
  catch { /* the class view keeps its last known state while offline */ }
}
function renderClassBoard() {
  if (!plan || !classProgress) return;
  const label = (id: string) => e(studentControls.find(c => c.id === id)?.label ?? id);
  const clock = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  $('class-board-list').innerHTML = classProgress.teams.map(team => {
    const route = plan!.routes.find(r => r.teamId === team.teamId), last = team.completed.at(-1);
    const time = last && team.checkIns[last] ? `（${clock(team.checkIns[last])}）` : '';
    const state = team.arrived ? `已到终点${team.arrivedAt ? `（${clock(team.arrivedAt)}）` : ''}` : team.finished ? `全部目标完成${time}` : last ? `最近 ${label(last)}${time}` : '尚未打点';
    return `<div class="class-board-row ${team.finished ? 'done' : ''}" data-team-progress="${e(team.teamId)}"><strong>${e(team.teamId)}</strong><span>${team.completed.length} / ${route?.controlIds.length ?? 0}</span><small>${state}</small></div>`;
  }).join('') + `<p class="muted compact">按各队手机上报的打点汇总，断网的队伍联网后补传。</p><button id="export-results" class="text-button">导出成绩表（CSV）</button>`;
}
$('class-board-list').addEventListener('click', event => {
  if (!(event.target as HTMLElement).closest('#export-results') || !plan || !classProgress) return;
  const start = classStart?.startedAt ? (classStart.startedAt - classStart.offset) * 1000 : null;
  const url = URL.createObjectURL(new Blob([resultsCsv(plan.routes, classProgress, studentControls, plan.coverage?.mandatoryControlIds ?? [], start)], { type: 'text/csv;charset=utf-8' }));
  const link = document.createElement('a'); link.href = url; link.download = `dirmap-results-${courseId}.csv`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
function progressKey() { return `dirmap-progress:${courseId}:${value('student-team')}`; }
function waitingMeeting(): Control | undefined {
  const last = studentRoute()?.controlIds.filter(id => completed.has(id)).at(-1);
  return last && plan?.coverage?.mandatoryControlIds.includes(last) && !departedMeetings.has(last)
    ? studentControls.find(c => c.id === last) : undefined;
}
function renderStudent() {
  const route = studentRoute(); if (!route) return;
  const team = classProgress?.teams.find(t => t.teamId === route.teamId);
  const ahead = team && !viewing() ? team.completed.filter(id => !completed.has(id)) : [];
  const teamArrived = !!team?.arrived && arrivedAt === null && route.controlIds.every(id => completed.has(id) || team.completed.includes(id));
  $('team-sync').innerHTML = ahead.length || (teamArrived && !viewing()) ? `<div class="notice-card team-sync"><strong>队友的手机已${ahead.length ? `打点：${ahead.map(id => e(studentControls.find(c => c.id === id)?.label ?? id)).join('、')}` : '记录到达终点'}</strong><button id="adopt-team-progress" class="soft-button wide">同步到本机</button></div>` : '';
  $('student-overview').innerHTML = `<div class="quality-values"><div><strong>${distance(route.distanceM)}</strong><span>全程距离</span></div><div><strong>${Math.round(route.durationSec / 60)}<small> 分钟</small></strong><span>预计用时（含打点与等待）</span></div></div><p>${route.people} 人同行${route.departureSec ? ` · 开课后 ${minutes(route.departureSec)}出发` : plan?.routes.some(r => r.departureSec > 0) ? ' · 开课即出发，其他队错峰' : ' · 统一出发'}</p>`;
  const surfaceNames = Object.fromEntries((plan?.surfaces ?? []).map(s => [s.id, s.label]));
  $('student-overview').insertAdjacentHTML('beforeend', elevationSummary(route, true, false));
  if (route.transitions?.length) $('student-overview').insertAdjacentHTML('beforeend', `<div id="student-transitions" class="notice-card"><strong>按标记换层</strong>${route.transitions.map(t => `<p>${e(t.label)}：${e(surfaceNames[t.fromSurface])} → ${e(surfaceNames[t.toSurface])}</p>`).join('')}<p class="muted compact">道路在屏幕上重合不代表互通。换层后请确认当前位置层级。</p></div>`);
  const requiredByAll = new Set(plan?.coverage?.mandatoryControlIds ?? []);
  if (plan?.coverage?.mandatoryControlIds.length) {
    const labels = [...requiredByAll].map(id => e(studentControls.find(c => c.id === id)?.label ?? id));
    $('student-overview').insertAdjacentHTML('beforeend', `<p id="student-coverage">全员必到：${labels.join('、')}；其余为本队分担点。${plan.coverage.mandatoryOrder ? '必到点按教师指定顺序经过。' : '按全班统一集合顺序前往。'}</p>`);
  }
  const index = route.controlIds.findIndex(id => !completed.has(id)), next = index < 0 ? null : studentControls.find(c => c.id === route.controlIds[index]);
  $('student-progress').textContent = `${route.controlIds.filter(id => completed.has(id)).length} / ${route.controlIds.length}`;
  let metres: number | null = null;
  if (next?.lnglat && locationFresh && lastPosition && Date.now() - lastPosition.timestamp < 10000) { const lat = next.lnglat[1] * Math.PI / 180; metres = Math.hypot((next.lnglat[0] - lastPosition.coords.longitude) * 111320 * Math.cos(lat), (next.lnglat[1] - lastPosition.coords.latitude) * 110700); }
  const waiting = waitingMeeting();
  const nextSurface = plan?.targetSurfaces?.[waiting?.id ?? next?.id ?? 'finish'];
  const plannedWait = waiting ? route.stops?.find(s => s.controlId === waiting.id)?.waitSec : null;
  const arrived = waiting && classProgress ? classProgress.teams.filter(t => t.completed.includes(waiting.id)) : null;
  const missing = arrived ? (plan?.routes ?? []).filter(r => !arrived.some(t => t.teamId === r.teamId)).map(r => e(r.teamId)) : [];
  const arrivals = !arrived ? '<p id="meeting-arrivals" class="muted compact">联网后显示各队到达情况。</p>'
    : `<p id="meeting-arrivals">${missing.length ? `已到 ${arrived.length} / ${plan!.routes.length} 队 · 未到：${missing.join('、')}` : '各队均已上报到达，可以统一出发。'}</p>`;
  $('next-control').innerHTML = waiting ? `<span class="next-label">集合点 ${e(waiting.label)}</span><strong>等待全班集合</strong><p>${plannedWait ? `计划等待 ${minutes(plannedWait)}。` : ''}请现场确认所有队伍到齐，再统一出发。</p>${arrivals}` : next ? `<span class="next-label">目标 ${e(next.label)}</span><strong>${metres === null ? '等待定位' : `直线 ${distance(metres)}`}</strong><p>${requiredByAll.has(next.id) ? '这是全班集合点，到达后须等待所有队伍。' : '沿地图中的队伍路线前往'}</p>`
    : arrivedAt !== null ? `<span class="next-label">课程终点</span><strong>已到达终点</strong><p>${new Date(arrivedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} 到达${classStart?.startedAt ? `，开课后 ${clockText(Math.max(0, arrivedAt / 1000 + classStart.offset - classStart.startedAt))}` : ''}。</p>`
    : '<span class="next-label">所有目标已完成</span><strong>前往课程终点</strong><p>仍需按路线返回终点，到达后点“已到达终点”。</p>';
  if ((plan?.surfaces?.length ?? 0) > 1 && nextSurface) $('next-control').insertAdjacentHTML('beforeend', `<p class="target-surface">到访层级：${e(surfaceNames[nextSurface] ?? '请核对')}</p>`);
  $<HTMLButtonElement>('check-in').disabled = !next || !!waiting || courseSuperseded;
  $('check-in').classList.toggle('hidden', !!waiting);
  $('meeting-departure').classList.toggle('hidden', !waiting);
  $('arrive-finish').classList.toggle('hidden', !!next || !!waiting || arrivedAt !== null);
  $<HTMLButtonElement>('arrive-finish').disabled = courseSuperseded;
  $<HTMLButtonElement>('meeting-departure').disabled = courseSuperseded;
  renderClassBoard();
  $('student-stops').innerHTML = route.controlIds.map((id, i) => `<div class="student-stop ${completed.has(id) ? 'done' : ''}"><i>${completed.has(id) ? '✓' : i + 1}</i><span>目标 ${e(studentControls.find(c => c.id === id)?.label ?? id)}${plan?.coverage?.mandatoryControlIds.length ? `<small> · ${requiredByAll.has(id) ? '全员必到' : '本队分担'}</small>` : ''}</span><small>${completed.has(id) ? `已完成${checkInTimes[id] ? ' ' + new Date(checkInTimes[id]).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''}` : '待到访'}</small></div>`).join('');
  const photoTarget = waiting ?? next;
  if (!viewing()) {
    $('next-control').insertAdjacentHTML('beforeend', `<button class="soft-button student-photo-button" data-student-photo="${e(photoTarget?.id ?? '')}">${photoTarget ? `为 ${e(photoTarget.label)} 拍照 / 查看小组照片` : '整理并导出小组照片'}</button>`);
    $('student-stops').querySelectorAll<HTMLElement>('.student-stop').forEach((row, i) => {
      row.insertAdjacentHTML('beforeend', `<button class="text-button" data-student-photo="${e(route.controlIds[i])}" aria-label="目标 ${e(studentControls.find(c => c.id === route.controlIds[i])?.label)} 的小组照片">照片</button>`);
    });
  }
}
function resetNavigation() {
  navigationRevision++; navigationRoute = null; offRoute.reset();
  campusMap.setRoutes(plan?.routes ?? [], value('student-team'));
}
async function updateNavigation(position: GeolocationPosition, force = false) {
  const route = studentRoute();
  force ||= surfaceSelectionPending;
  if (!route || !courseId || courseSuperseded || (!follow && !force) || navigating || waitingMeeting()) return;
  const lnglat: Pixel = [position.coords.longitude, position.coords.latitude];
  if (!force && !offRoute.update({ lnglat, accuracy: position.coords.accuracy, timestamp: position.timestamp }, remainingPath(navigationRoute ?? route, completed), Date.now())) return;
  const revision = navigationRevision, teamId = route.teamId;
  const completedControlIds = route.controlIds.filter(id => completed.has(id));
  surfaceSelectionPending = false;
  navigating = true; $('location-status').textContent = '已偏离路线，正在计算返回路径…';
  try {
    const result = await api<{ status: string; route: Route | null; unresolved?: { message: string }[] }>(`/api/releases/${courseId}/navigate`, {
      method: 'POST', signal: AbortSignal.timeout(30000), body: JSON.stringify({ teamId, lnglat, accuracyM: position.coords.accuracy, timestampMs: position.timestamp, completedControlIds, surfaceId: value('student-surface') || null }),
    });
    if (revision !== navigationRevision || courseSuperseded || teamId !== studentRoute()?.teamId) return;
    if (result.status !== 'READY' || !result.route) throw new Error(result.unresolved?.[0]?.message ?? '当前位置没有可确认的合法连接');
    navigationRoute = result.route;
    campusMap.setRoutes(plan!.routes.map(r => r.teamId === teamId ? navigationRoute! : r), teamId);
    $('location-status').textContent = '已更新返回路线 · 保持本队剩余点顺序';
  } catch (error) {
    if (revision === navigationRevision) $('location-status').textContent = navigator.onLine ?
      `暂不能重规划：${error instanceof Error ? error.message : String(error)}` : '当前离线，保留已下载路线';
  } finally { navigating = false; }
}
$<HTMLSelectElement>('student-team').onchange = () => {
  try { completed = new Set(JSON.parse(localStorage.getItem(progressKey()) ?? '[]')); } catch { completed = new Set(); }
  try { departedMeetings = new Set(JSON.parse(localStorage.getItem(progressKey() + ':departed-meetings') ?? '[]')); } catch { departedMeetings = new Set(); }
  // Discard malformed/outdated local progress instead of skipping targets.
  const ids = studentRoute()?.controlIds ?? [];
  const count = ids.findIndex(id => !completed.has(id)); completed = new Set(ids.slice(0, count < 0 ? ids.length : count));
  departedMeetings = new Set([...departedMeetings].filter(id => completed.has(id) && plan?.coverage?.mandatoryControlIds.includes(id)));
  try { checkInTimes = JSON.parse(localStorage.getItem(progressKey() + ':times') ?? '{}') ?? {}; } catch { checkInTimes = {}; }
  checkInTimes = Object.fromEntries(Object.entries(checkInTimes).filter(([id, at]) => completed.has(id) && Number.isFinite(at)));
  try { arrivedAt = JSON.parse(localStorage.getItem(progressKey() + ':arrived') ?? 'null'); } catch { arrivedAt = null; }
  if (!Number.isFinite(arrivedAt) || ids.some(id => !completed.has(id))) arrivedAt = null;
  $('print-team').setAttribute('href', `/?course=${courseId}&print=${encodeURIComponent(value('student-team'))}`);
  resetNavigation(); renderStudent(); renderClassClock(); void syncProgress();
};
const viewing = () => $('student-panel').classList.contains('viewer');
$('check-in').onclick = () => { if (courseSuperseded || waitingMeeting() || viewing()) return; const next = studentRoute()?.controlIds.find(id => !completed.has(id)); if (next) { completed.add(next); checkInTimes[next] = Date.now(); saveProgress(); resetNavigation(); renderStudent(); } };
$('meeting-departure').onclick = () => {
  const waiting = waitingMeeting(); if (!waiting || courseSuperseded || viewing()) return;
  departedMeetings.add(waiting.id); saveProgress();
  resetNavigation(); renderStudent();
};
$('arrive-finish').onclick = () => {
  const route = studentRoute(); if (!route || courseSuperseded || viewing() || route.controlIds.some(id => !completed.has(id))) return;
  arrivedAt = Date.now(); saveProgress(); resetNavigation(); renderStudent();
};
$('undo-check-in').onclick = () => { if (courseSuperseded || viewing()) return; if (arrivedAt !== null) { arrivedAt = null; saveProgress(); renderStudent(); return; } const last = [...completed].at(-1); if (last) { completed.delete(last); departedMeetings.delete(last); delete checkInTimes[last]; saveProgress(); resetNavigation(); renderStudent(); } };
addEventListener('online', () => { void syncProgress(); void refreshClassProgress(); });

async function applyDeclination(origin?: Pixel) {
  // The compass corrects magnetic north where the course takes place.
  const query = origin ? `?lng=${origin[0]}&lat=${origin[1]}` : '';
  const health = await api<{ magneticDeclinationDeg?: number; modelDate?: string }>('/api/health' + query).catch(() => ({} as { magneticDeclinationDeg?: number; modelDate?: string }));
  sensors.declination = health.modelDate && Math.abs(Date.now() - Date.parse(health.modelDate)) < 31 * 86400000 ? health.magneticDeclinationDeg ?? null : null;
}
function startGroupPanel() {
  groupPanel = new GroupPanel({ root: $('group-panel'), map: campusMap, notify, courseId, initialCode: groupCode,
    teamId: () => courseId ? value('student-team') || null : null, ownPosition: () => locationFresh ? lastPosition : null,
    pickPoint: title => mapPicker.choose(title) });
  if (groupCode !== null || groupPanel.active) $<HTMLDetailsElement>('group-section').open = true;
}
async function boot() {
  const printTeam = courseId ? new URLSearchParams(location.search).get('print') : null;
  if (courseId && printTeam) {
    const course = await api<{ id: string; title: string; plan: Plan; controls: Control[]; superseded: boolean }>(`/api/releases/${courseId}`);
    document.documentElement.classList.add('print-page');
    document.body.innerHTML = printSheets(course, printTeam);
    document.getElementById('print-now')!.onclick = () => print();
    return;
  }
  if (courseId) {
    $('teacher-panel').classList.add('hidden'); $('student-panel').classList.remove('hidden'); document.querySelectorAll('.teacher-only').forEach(el => el.classList.add('hidden')); $('header-mode').textContent = '学生导航';
    const course = await api<{ id: string; title: string; plan: Plan; controls: Control[]; extent: number[]; superseded: boolean; routingSchema?: string; offline?: boolean; siteName?: string; frame?: { origin: Pixel } }>(`/api/releases/${courseId}`);
    $('site-name').textContent = course.siteName ?? '深圳大学粤海校区';
    $('map-title').textContent = course.title; $('map-subtitle').textContent = course.siteName ?? '标准地图 / WGS84';
    void applyDeclination(course.frame?.origin);
    api<object>(`/api/releases/${courseId}/reference`).then(data => campusMap.setReference(data)).catch(() => notify('场地参照暂不可用，正在显示在线底图'));
    if (course.superseded || course.routingSchema !== __DIRMAP_ROUTING_SCHEMA__) {
      courseSuperseded = true; $('release-badge').textContent = '课程已更新'; $('sensor-status').textContent = '旧路线已失效，请使用教师提供的新链接';
      $<HTMLButtonElement>('check-in').disabled = true; $<HTMLButtonElement>('undo-check-in').disabled = true; $<HTMLButtonElement>('meeting-departure').disabled = true;
      // A changed route must not strand the group's already captured photos.
      if (!['#progress', '#groups'].includes(location.hash)) {
        startGroupPanel(); $<HTMLDetailsElement>('group-section').open = true; setMobilePanel(true);
      }
      throw new Error('这份课程的地图或通行状态已更新，请向教师获取新链接');
    }
    plan = course.plan; studentControls = course.controls; $('course-title').textContent = course.title;
    const surfaces = plan.surfaces ?? [{ id: 'ground', label: '地面', level: 0 }];
    $('student-surface-field').classList.toggle('hidden', surfaces.length < 2);
    $('student-surface').innerHTML = '<option value="">重合位置需要确认</option>'+surfaces.map(s => `<option value="${e(s.id)}">${e(s.label)}</option>`).join('');
    $('release-badge').textContent = course.offline ? '离线 · 状态未更新' : '课堂导航';
    $('student-team').innerHTML = plan.routes.map(r => `<option value="${e(r.teamId)}">${e(r.teamId)} · ${r.people} 人</option>`).join('');
    campusMap.setReleaseOverlay(courseId, course.extent, .65); campusMap.setControls(course.controls, plan.surfaces); campusMap.setApproaches(plan.approaches ?? []); $<HTMLSelectElement>('student-team').dispatchEvent(new Event('change'));
    if (import.meta.env.PROD) {
      if (course.offline) $('offline-status').textContent = '正在使用已保存课程；联网后检查教师更新。';
      else void cacheStudentCourse();
    }
    const checkStatus = async () => {
      if (document.hidden || courseSuperseded) return;
      try {
        // A few bytes per check instead of the whole course package.
        const current = await api<{ superseded: boolean; routingSchema?: string; offline?: boolean; startedAt?: number | null; serverTime?: number }>(`/api/releases/${courseId}/status`);
        if (typeof current.serverTime === 'number') rememberClassStart(current.startedAt ?? null, current.serverTime);
        if (current.superseded || current.routingSchema !== __DIRMAP_ROUTING_SCHEMA__) {
          courseSuperseded = true; navigationRevision++; follow = false; sensors.stop(); campusMap.setRoutes([]);
          $('release-badge').textContent = '课程已更新'; $('sensor-status').textContent = '旧路线已失效，请使用教师提供的新链接';
          $<HTMLButtonElement>('check-in').disabled = true; $<HTMLButtonElement>('undo-check-in').disabled = true; $<HTMLButtonElement>('meeting-departure').disabled = true; notify('教师已更新地图或通行状态，请使用新课程链接', true);
        } else {
          $('release-badge').textContent = current.offline ? '离线 · 状态未更新' : '课堂导航';
          if (!current.offline) { await syncProgress(); await refreshClassProgress(); }
        }
      } catch { $('release-badge').textContent = '离线 · 状态未更新'; }
    };
    releasePoll = setInterval(checkStatus, 15000);
    loadClassStart(); setInterval(renderClassClock, 1000);
    if (!course.offline) void checkStatus();
    if (['#progress', '#groups'].includes(location.hash)) {
      // The teacher's progress link only views the class; a stray tap must
      // not report a check-in for whichever team happens to be selected.
      $('student-panel').classList.add('viewer'); $<HTMLDetailsElement>('class-board').open = location.hash === '#progress';
      $('student-progress').insertAdjacentHTML('afterend', '<p id="viewer-note" class="muted compact">查看模式：不记录打点。学生请使用不带 # 后缀的课程链接。</p>');
      // The teacher watches every group of the course instead of joining one.
      $('group-section').classList.add('hidden'); $('group-overview-section').classList.remove('hidden');
      $<HTMLDetailsElement>('group-overview-section').open = location.hash === '#groups';
      groupOverview = new GroupOverview(courseId, $('group-overview'), campusMap); groupOverview.start();
    } else startGroupPanel();
    if (!course.offline) void refreshClassProgress();
  } else if (groupMode) {
    $('teacher-panel').classList.add('hidden'); $('student-panel').classList.remove('hidden'); $('student-panel').classList.add('group-only');
    $('group-only-heading').classList.remove('hidden'); $('header-mode').textContent = '组队定位'; $('release-badge').textContent = '小组';
    document.querySelectorAll('.teacher-only').forEach(el => el.classList.add('hidden'));
    $('map-title').textContent = '组队定位'; $('map-subtitle').textContent = '组员共享的位置与集合点'; $('site-name').textContent = '组队定位';
    void applyDeclination();
    startGroupPanel();
    setMobilePanel(true);
  } else {
    // Resume only what was pending when this page opened. A submission made
    // here in the meantime reports its own unconfirmed state instead.
    void applyDeclination();
    account = await fetchAccount();
    if (!account) throw new Error('暂时无法确认登录状态，请检查连接后刷新页面');
    setWorkbench(account);
    activeSite = stored(workbenchKey('site')) ?? 'szu-yuehai';
    const pending = pendingJob();
    renderAccountChip($('account-slot'), account, notify);
    try { catalog = await api<Catalog>('/api/catalog'); }
    catch (error) {
      if (!(error instanceof ApiError && error.status === 401 && account)) throw error;
      // This server's workbenches belong to accounts: sign in first.
      $('teacher-panel').innerHTML = signInGate(account); $('release-badge').textContent = '未登录'; $('header-mode').textContent = '登录';
      $('teacher-panel').querySelectorAll<HTMLButtonElement>('[data-sign-in]').forEach(button => button.onclick = () => openSignIn(account!, button.dataset.signIn as 'login' | 'register'));
      setMobilePanel(true);
      return;
    }
    if (account && !account.user) $('workbench-notice').innerHTML = `<div class="notice-card workbench-notice"><strong>正在使用服务器原工作台</strong><p>${account.original.tokenRequired ? '修改需要原教师口令。' : ''}注册账户即可获得自己的独立工作台：导入地图、识别、生成路线和发布课程都不需要教师口令。</p><button class="soft-button" data-sign-in="register">注册或登录</button></div>`;
    $('workbench-notice').querySelector<HTMLButtonElement>('[data-sign-in]')?.addEventListener('click', () => openSignIn(account!, 'register'));
    await selectSite(catalog.sites.some(site => site.id === activeSite) ? activeSite : catalog.sites[0].id);
    await renderJobs();
    if (pending && JSON.stringify(pendingJob()) === JSON.stringify(pending)) await task(() => resumeJob(pending));
  }
}
document.addEventListener('click', event => {
  const button = (event.target as HTMLElement).closest<HTMLElement>('[data-student-photo]');
  if (!button || viewing()) return;
  if (!groupPanel?.active) { $<HTMLDetailsElement>('group-section').open = true; setMobilePanel(true); }
  groupPanel?.openPhotos(button.dataset.studentPhoto || undefined);
});
async function cacheStudentCourse() {
  if (!courseId || courseSuperseded) return;
  $('offline-status').textContent = '正在保存原图、路线与目标…';
  try { await saveOfflineCourse(courseId); $('offline-status').textContent = '本课已保存；断网后可查看原图、路线并记录打点。'; }
  catch (error) { $('offline-status').textContent = error instanceof Error ? error.message : String(error); }
}
if ((!courseId && !groupMode) || location.hash === '#groups') watchAccountChanges(() => location.reload());
boot().catch(error => notify(error instanceof Error ? error.message : String(error), true));
