import type { Route } from './types';

export function speedSummary(route: Route) {
  const data = route.speedLearning;
  if (!data?.enabled) return '';
  const mode = data.mode === 'run' ? '跑步' : '步行';
  if (data.status === 'insufficient_data') return `<div class="route-speed muted">${mode}轨迹不足，沿用基础耗时估计；速度按本队设置。</div>`;
  return `<div class="route-speed"><span>${mode}耗时学习${data.gradeLearned ? ' · 已学习上下坡影响' : ''}</span><small>重复路段覆盖 ${Math.round(data.coverageFraction * 100)}%${data.fallbackDistanceM > 1 ? ` · 其余 ${Math.round(data.fallbackDistanceM)} m 按基础通行估计` : ''}</small><small>按本队平地速度计算移动时间；打卡与集合等待另计。</small></div>`;
}
