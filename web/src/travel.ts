import type { Route } from './types';

export function speedSummary(route: Route) {
  const data = route.speedLearning;
  if (!data?.enabled) return '';
  const percent = Math.round(data.coverageFraction * 100);
  const learning = data.mode === 'foot' ? '跑走共同学习' : data.mode === 'run' ? '历史跑步模型' : '历史步行模型';
  const parts = [{ label: '移动', seconds: route.travelSec, colour: '#477d66' },
    { label: '打卡', seconds: route.serviceSec, colour: '#b9975a' }, { label: '集合等待', seconds: route.waitSec ?? 0, colour: '#a3ac98' }];
  const total = parts.reduce((sum, p) => sum + p.seconds, 0);
  return `<div class="route-speed"><strong>通行效率与预计耗时</strong><small>${data.status === 'insufficient_data' ? '徒步轨迹不足，沿用基础耗时估计' : `${learning}${data.gradeLearned ? ' · 已学习上下坡影响' : ''}`}；速度按本队设置。</small>
    <div class="time-composition" role="img" aria-label="${parts.map(p => `${p.label} ${(p.seconds / 60).toFixed(1)} 分钟`).join('，')}">${parts.filter(p => p.seconds > 0).map(p => `<span style="width:${total ? p.seconds / total * 100 : 0}%;background:${p.colour}"></span>`).join('')}</div>
    <div class="time-composition-labels">${parts.map(p => `<span><i style="background:${p.colour}"></i>${p.label} <b>${(p.seconds / 60).toFixed(1)}</b> min</span>`).join('')}</div>
    <small>路段效率覆盖 ${percent}% · ${Math.round(data.learnedDistanceM)} m；基础通行估计 ${Math.round(data.fallbackDistanceM)} m</small>
    <div class="learning-coverage" role="img" aria-label="路段效率覆盖 ${percent}%"><i style="width:${percent}%"></i></div></div>`;
}
