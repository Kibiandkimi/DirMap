import type { ClassProgress, Control, Route } from './types';

const cell = (value: string | number) => {
  let text = String(value);
  // A spreadsheet must not evaluate a team name or label as a formula.
  if (/^[=+\-@\t\r]/.test(text)) text = "'" + text;
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
};

const clock = (ms: number) => {
  const t = new Date(ms), two = (n: number) => String(n).padStart(2, '0');
  return `${t.getFullYear()}-${two(t.getMonth() + 1)}-${two(t.getDate())} ${two(t.getHours())}:${two(t.getMinutes())}:${two(t.getSeconds())}`;
};

/** One row per team and target in published order, for after-class records.
 * Times are what each phone recorded; a blank time with "已完成" means the
 * phone's clock was rejected or the check-in predates time sharing. */
export function resultsCsv(routes: Route[], progress: ClassProgress, controls: Control[], meetings: string[] = []) {
  const labels = new Map(controls.map(c => [c.id, c.label]));
  const rows = [['队伍', '人数', '顺序', '目标', '类型', '状态', '打点时间（手机记录）']];
  for (const route of routes) {
    const team = progress.teams.find(t => t.teamId === route.teamId);
    route.controlIds.forEach((id, index) => {
      const done = team?.completed.includes(id) ?? false, at = team?.checkIns[id];
      rows.push([route.teamId, String(route.people), String(index + 1), labels.get(id) ?? id,
        meetings.includes(id) ? '全班必到' : '本队分担', done ? '已完成' : '未完成', done && at !== undefined ? clock(at) : '']);
    });
  }
  return '\uFEFF' + rows.map(row => row.map(cell).join(',')).join('\r\n') + '\r\n';
}
