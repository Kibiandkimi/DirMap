import { escapeHtml as e, type Plan } from './types';

const supported = (plan: Plan) => ['all_visit', 'split_coverage', 'mixed_coverage'].includes(plan.request?.mode ?? '');
const time = (seconds: number) => `${(seconds / 60).toFixed(1)} 分钟`;
const distance = (metres: number) => metres >= 1000 ? `${(metres / 1000).toFixed(2)} km` : `${metres.toFixed(1)} m`;
const distanceGain = (metres: number) => metres < .1 ? '不足 0.1 m' : distance(metres);
const timeGain = (seconds: number) => seconds < .1 ? '不足 0.1 秒' : seconds < 60 ? `${seconds.toFixed(1)} 秒` : time(seconds);

export function canRefine(plan: Plan | null): boolean {
  return !!plan?.routes.length && supported(plan) && !!plan.continuation && !plan.proof?.globalOptimalityProven;
}

export function refinementHtml(plan: Plan): string {
  if (!supported(plan) || (plan.proof?.globalOptimalityProven && !plan.refinement)) return '';
  const refinement = plan.refinement;
  let title = '继续优化当前方案';
  let summary = `保持当前 ${plan.routes.length} 队和课程规则，在这份路线基础上继续寻找更好的安排。`;
  let detail = '';
  if (refinement) {
    const { before, after } = refinement;
    title = refinement.improved ? '本轮路线已改善' : '保留原有路线';
    summary = '本轮没有找到更好的路线，已保留原方案。';
    if (!refinement.attempted) summary = '当前方案已在模型内证明最优。';
    if (plan.request?.objective === 'min_distance') {
      detail = `总路线长度：${distance(before.totalDistanceM)} → ${distance(after.totalDistanceM)}`;
      if (refinement.improved) summary = `总路线长度减少 ${distanceGain(before.totalDistanceM - after.totalDistanceM)}。`;
    } else if (plan.request?.objective === 'min_total_time') {
      detail = `各队完成时间合计：${time(before.totalFinishSec)} → ${time(after.totalFinishSec)}`;
      if (refinement.improved) summary = `各队完成时间合计减少 ${timeGain(before.totalFinishSec - after.totalFinishSec)}。`;
    } else {
      detail = `最晚完成：${time(before.latestFinishSec)} → ${time(after.latestFinishSec)}；各队完成时间合计：${time(before.totalFinishSec)} → ${time(after.totalFinishSec)}`;
      if (refinement.improved) summary = before.latestFinishSec - after.latestFinishSec > 1e-8
        ? `最晚完成时间减少 ${timeGain(before.latestFinishSec - after.latestFinishSec)}。`
        : `最晚完成时间保持，各队完成时间合计减少 ${timeGain(before.totalFinishSec - after.totalFinishSec)}。`;
    }
  }
  const reason = !plan.continuation ? '这份旧记录需重新生成后才能继续优化。' : plan.proof?.globalOptimalityProven ? '当前模型已证明最优，无需继续计算。' : '';
  return `<section class="notice-card refinement-card" aria-label="继续优化路线"><strong>${e(title)}</strong><p>${e(summary)}</p>${detail ? `<p class="muted compact refinement-metrics">${e(detail)}</p>` : ''}<button id="refine-plan" data-refine-plan type="button" class="soft-button wide" ${canRefine(plan) ? '' : 'disabled'}>继续优化，最多 30 秒</button>${reason ? `<p class="muted compact">${e(reason)}</p>` : ''}</section>`;
}
