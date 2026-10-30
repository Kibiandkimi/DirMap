import { escapeHtml as e, type Control, type Plan } from './types';
import { elevationSummary } from './elevation-profile';

const minutes = (seconds: number) => `${Math.round(seconds / 60)} 分钟`;
const distance = (metres: number) => metres >= 1000 ? `${(metres / 1000).toFixed(2)} km` : `${Math.round(metres)} m`;

/** Paper sheets for one team or the whole class, one A4 page per team.
 * The server draws the route over the frozen course map; text stays here so
 * the browser lays out Chinese labels and the page prints as a document. */
export function printSheets(course: { id: string; title: string; plan: Plan; controls: Control[]; superseded?: boolean }, team: string) {
  const routes = team === 'all' ? course.plan.routes : course.plan.routes.filter(r => r.teamId === team);
  if (!routes.length) throw new Error('课程中没有这个队伍');
  const labels = new Map(course.controls.map(c => [c.id, c.label]));
  const meetings = new Set(course.plan.coverage?.mandatoryControlIds ?? []);
  const sheets = routes.map(route => `<section class="print-sheet">
    <header><div><span class="eyebrow">DirMap · 定向越野课</span><h1>${e(course.title)} · ${e(route.teamId)}</h1></div>
      <p>${route.people} 人 · 全程 ${distance(route.distanceM)} · 预计 ${minutes(route.durationSec)}${route.departureSec ? ` · 开课后 ${minutes(route.departureSec)}出发` : ' · 统一出发'}</p>${elevationSummary(route, false, false)}</header>
    <img class="print-map" src="/api/releases/${e(course.id)}/route-map?team=${encodeURIComponent(route.teamId)}" alt="${e(route.teamId)} 的路线图">
    <table><thead><tr><th>顺序</th><th>目标</th><th>类型</th><th>计划到达</th><th>打点时间</th></tr></thead><tbody>${route.controlIds.map((id, index) => {
      const stop = route.stops?.find(s => s.controlId === id);
      return `<tr><td>${index + 1}</td><td>${e(labels.get(id) ?? id)}</td><td>${meetings.has(id) ? '全班必到，到齐后统一出发' : meetings.size ? '本队分担' : '目标'}</td><td>${stop ? `开课后 ${minutes(stop.arrivalSec)}` : ''}</td><td></td></tr>`;
    }).join('')}</tbody></table>
    <p class="print-note">图中彩色线为建议路线，数字为本队到访顺序，三角形为起点、双圈为终点。路线依据教师核验的通行图，现场以实际地物和安全为准。</p>
  </section>`).join('');
  return `<div class="print-toolbar"><button id="print-now" class="primary-button">打印${routes.length > 1 ? `全部 ${routes.length} 队` : '本队路线'}</button>${course.superseded ? '<span class="danger">这份课程已被教师更新，请确认是否仍要使用旧路线。</span>' : '<span class="muted">每队一页，建议 A4 纵向。</span>'}</div>${sheets}`;
}
