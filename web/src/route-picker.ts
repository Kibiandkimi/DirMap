import { draftControlIds } from './course-routes';
import { sortedControls } from './control-order';
import { escapeHtml as e, type Control } from './types';

export function routePickerHtml(team: string, draft: string, controls: Control[], mandatory: Set<string>) {
  let ids: string[];
  try { ids = draftControlIds(draft, controls); }
  catch (error) { return `<p class="danger" role="status">${e((error as Error).message)}</p>`; }
  const available = sortedControls(controls.filter(c => c.state !== 'rejected' && (c.role ?? 'control') === 'control'));
  return `<div class="route-picker" data-picker-team="${e(team)}"><p class="muted compact">点下面的编号加入路线；已选编号再次点击可移除。</p>
    <div class="route-number-grid" aria-label="${e(team)} 可选目标">${available.map(c => `<button class="route-number" data-route-add="${e(c.id)}" aria-label="${e(team)} 选择目标 ${e(c.label)}" aria-pressed="${ids.includes(c.id)}">${e(c.label)}${mandatory.has(c.id) ? '<small>必到</small>' : ''}</button>`).join('')}</div>
    <div class="route-order-heading"><strong>到访顺序</strong><span>从上往下走 · ${ids.length} 个目标</span></div>
    <div class="route-endpoint">起点 <span>从地图上设置的位置出发</span></div>
    <ol class="route-sequence" aria-label="${e(team)} 已选点序">${ids.map((id, i) => {
      const label = controls.find(c => c.id === id)?.label ?? id;
      return `<li><span class="route-stop">第 ${i + 1} 站</span><strong class="route-target"><small>目标</small> ${e(label)}</strong><div class="route-stop-actions"><button class="text-button" data-route-move="${i}" data-direction="-1" aria-label="上移目标 ${e(label)}" ${i === 0 ? 'disabled' : ''}>↑ 上移</button><button class="text-button" data-route-move="${i}" data-direction="1" aria-label="下移目标 ${e(label)}" ${i === ids.length - 1 ? 'disabled' : ''}>↓ 下移</button><button class="text-button" data-route-remove="${i}" aria-label="移除目标 ${e(label)}">移除</button></div></li>`;
    }).join('')}</ol>${ids.length ? '' : '<p class="route-empty">还没有目标，点击上方编号添加第一站。</p>'}<div class="route-endpoint">终点 <span>完成最后一站后前往地图上设置的终点</span></div><div class="route-picker-actions"><button class="text-button" data-route-undo ${ids.length ? '' : 'disabled'}>撤销末点</button><button class="text-button" data-route-clear ${ids.length ? '' : 'disabled'}>清空本队</button></div></div>`;
}
