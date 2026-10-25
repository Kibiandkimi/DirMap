import { escapeHtml as e, type TeamRecommendation } from './types';

const duration = (seconds: number) => `${(seconds / 60).toFixed(1)} 分`;
const length = (metres: number) => metres >= 1000 ? `${(metres / 1000).toFixed(2)} km` : `${Math.round(metres)} m`;

export function teamComparisonHtml(result: TeamRecommendation): string {
  const rows = result.candidates;
  if (!rows.length) return '';
  const title = { latestFinishSec: '最晚完成时间', totalFinishSec: '各队完成时间合计', totalDistanceM: '总路线长度' }[result.metric];
  const format = result.metric === 'totalDistanceM' ? length : duration;
  const values = rows.map(row => row[result.metric]), low = Math.min(...values), high = Math.max(...values);
  const points = rows.map((row, i) => [24 + 290 * i / Math.max(1, rows.length - 1), 22 + (high === low ? 30 : 65 * (high - row[result.metric]) / (high - low))]);
  const summary = result.noMeaningfulGain ? '本次计算中，增加队数没有改善当前优化目标。' :
    `相对 1 队，${title}减少 ${result.gainPercent.toFixed(1)}%；保留已计算最佳收益的 ${(result.retainedGainFraction * 100).toFixed(1)}%。`;
  return `<section class="team-recommendation" aria-label="分队收益比较"><div class="notice-card"><strong>自动建议 ${result.recommendedCount} 队</strong><p>${e(summary)}</p><p class="muted compact">按 ${e(title)} 比较 ${rows.length} 种队数；计算中指标最好的方案为 ${result.bestMetricCount} 队。${result.allCountsOptimalityProven ? '各队数方案均有模型内最优证明。' : '部分方案为限时可行解，推荐依据本次求解结果。'}</p></div>
  <details class="team-comparison"><summary>查看队数、收益与路线长度</summary>
  <p class="muted compact">目标：${e(title)}越小越好；选择达到 ${(result.targetGainFraction * 100).toFixed(0)}% 最佳改善收益的最少队数。</p>
  <svg viewBox="0 0 340 122" role="img" aria-label="${e(title)}随队数的变化，建议 ${result.recommendedCount} 队"><path d="M24 93H320" fill="none" stroke="#cbd5c8"/><path d="${points.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(2)} ${y.toFixed(2)}`).join(' ')}" fill="none" stroke="#437b5f" stroke-width="2"/>${rows.map((row, i) => `<g><circle cx="${points[i][0]}" cy="${points[i][1]}" r="${row.recommended ? 5 : 3}" fill="${row.recommended ? '#e27637' : '#437b5f'}"><title>${row.teamCount} 队：${e(format(row[result.metric]))}</title></circle>${i === 0 || i === rows.length - 1 || row.recommended || (rows.length <= 10) ? `<text x="${points[i][0]}" y="111" text-anchor="middle">${row.teamCount}</text>` : ''}</g>`).join('')}</svg>
  <div class="comparison-scroll"><table><thead><tr><th>队数</th><th>每队人数</th><th>最晚完成</th>${result.metric === 'totalFinishSec' ? '<th>完成时间合计</th>' : ''}<th>总路线</th><th>改善</th><th>解的状态</th></tr></thead><tbody>${rows.map(row => `<tr data-comparison-count="${row.teamCount}" class="${row.recommended ? 'recommended' : ''}"><td>${row.teamCount}${row.recommended ? ' ✓' : ''}</td><td>${Math.min(...row.teamSizes)}${Math.min(...row.teamSizes) !== Math.max(...row.teamSizes) ? `–${Math.max(...row.teamSizes)}` : ''}</td><td>${duration(row.latestFinishSec)}</td>${result.metric === 'totalFinishSec' ? `<td>${duration(row.totalFinishSec)}</td>` : ''}<td>${length(row.totalDistanceM)}</td><td>${row.gainPercent.toFixed(1)}%</td><td>${row.optimalityProven ? '模型最优' : '可行解'}</td></tr>`).join('')}</tbody></table></div>
  <p class="muted compact">“改善”相对 1 队，按当前优化目标计算。所有队数均遵守选定的目标覆盖、集合顺序及出发间隔。</p></details></section>`;
}
