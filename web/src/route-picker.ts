import { draftControlIds } from './course-routes';
import { sortedControls } from './control-order';
import { escapeHtml as e, type Control } from './types';

export function routePickerHtml(team: string, draft: string, controls: Control[], mandatory: Set<string>) {
  let ids: string[];
  try { ids = draftControlIds(draft, controls); }
  catch (error) { return `<p class="danger" role="status">${e((error as Error).message)}</p>`; }
  const available = sortedControls(controls.filter(c => c.state !== 'rejected' && (c.role ?? 'control') === 'control'));
  return `<div class="route-picker" data-picker-team="${e(team)}"><p class="muted compact">依次点编号加入，再点可移除；顺序条可前移、后移。</p>
    <div class="route-number-grid" aria-label="${e(team)} 可选目标">${available.map(c => `<button class="route-number" data-route-add="${e(c.id)}" aria-label="${e(team)} 选择目标 ${e(c.label)}" aria-pressed="${ids.includes(c.id)}">${e(c.label)}${mandatory.has(c.id) ? '<small>必到</small>' : ''}</button>`).join('')}</div>
    <ol class="route-sequence" aria-label="${e(team)} 已选点序">${ids.map((id, i) => {
      const label = controls.find(c => c.id === id)?.label ?? id;
      return `<li><span>${i + 1}. <b>${e(label)}</b></span><button class="text-button" data-route-move="${i}" data-direction="-1" aria-label="前移目标 ${e(label)}" ${i === 0 ? 'disabled' : ''}>←</button><button class="text-button" data-route-move="${i}" data-direction="1" aria-label="后移目标 ${e(label)}" ${i === ids.length - 1 ? 'disabled' : ''}>→</button><button class="text-button" data-route-remove="${i}" aria-label="移除目标 ${e(label)}">×</button></li>`;
    }).join('')}</ol><div class="route-picker-actions"><button class="text-button" data-route-undo ${ids.length ? '' : 'disabled'}>撤销末点</button><button class="text-button" data-route-clear ${ids.length ? '' : 'disabled'}>清空本队</button><span class="muted">已选 ${ids.length} 点</span></div></div>`;
}
