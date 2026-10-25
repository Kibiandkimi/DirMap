import type { Control, PlanRequest } from './types';

export function routeToken(id: string, controls: Control[]): string {
  const control = controls.find(c => c.id === id);
  return control && control.label && !/[\s,，、;；→>]/u.test(control.label) &&
    controls.filter(c => c.label === control.label).length === 1 &&
    !controls.some(c => c.id === control.label && c.id !== id) ? control.label : id;
}

export function parseCustomRoutes(drafts: Record<string, string>, teams: PlanRequest['teams'], controls: Control[], mandatory: string[], ordered: boolean) {
  const selected = controls.filter(c => c.required && c.state !== 'rejected' && (c.role ?? 'control') === 'control');
  const covered = new Set<string>();
  let commonOrder: string[] | null = null;
  const routes = teams.map(team => {
    const tokens = (drafts[team.id] ?? '').trim().split(/[\s,，、;；→>]+/u).filter(Boolean);
    if (!tokens.length) throw new Error(`请填写${team.id}的目标点序`);
    const controlIds = tokens.map(token => {
      const byId = selected.find(c => c.id === token);
      const matches = byId ? [byId] : selected.filter(c => c.label === token);
      if (matches.length !== 1) throw new Error(`${team.id}：目标 ${token} ${matches.length ? '编号重复，请用地图点选' : '不存在或未勾选'}`);
      return matches[0].id;
    });
    if (new Set(controlIds).size !== controlIds.length) throw new Error(`${team.id}有重复目标，请调整点序`);
    if (mandatory.some(id => !controlIds.includes(id))) throw new Error(`${team.id}遗漏了集合点，请补齐每队必到点`);
    const order = controlIds.filter(id => mandatory.includes(id));
    if (ordered && order.join('\0') !== mandatory.join('\0')) throw new Error(`${team.id}的集合点顺序与指定顺序不一致`);
    if (commonOrder && order.join('\0') !== commonOrder.join('\0')) throw new Error('各队的集合点顺序必须一致，避免互相等待');
    commonOrder = order;
    controlIds.forEach(id => covered.add(id));
    return { teamId: team.id, controlIds };
  });
  const missing = selected.filter(c => !covered.has(c.id));
  if (missing.length) throw new Error(`尚未分配目标 ${missing.map(c => c.label).join('、')}，请分配或取消勾选`);
  return routes;
}
