import { escapeHtml as e } from './types';
import type { TrackDetail, TravelDiagnostics } from './elevation-types';

type Series = { name: string; colour: string; points: (readonly [number, number] | null)[]; dashed?: boolean };
export function lineChart(label: string, series: Series[], xLabel: string, yLabel: string, formatX = (v: number) => v.toFixed(0)) {
  const points = series.flatMap(s => s.points.filter((p): p is readonly [number, number] => !!p && p.every(Number.isFinite)));
  if (!points.length) return `<p class="chart-empty">${e(label)}：暂无可用数据</p>`;
  const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
  const lowX = Math.min(...xs), highX = Math.max(...xs), lowY = Math.min(...ys), highY = Math.max(...ys);
  const spanX = Math.max(1e-6, highX - lowX), pad = Math.max(.05, (highY - lowY) * .12);
  const minY = lowY - pad, maxY = highY + pad;
  const x = (v: number) => 46 + (v - lowX) / spanX * 304, y = (v: number) => 160 - (v - minY) / (maxY - minY) * 132;
  const ticks = [0, .5, 1].map(t => {
    const yy = minY + t * (maxY - minY);
    return `<path d="M46 ${y(yy)}H350" stroke="#dbe4d6"/><text x="40" y="${y(yy) + 4}" text-anchor="end">${yy.toFixed(1)}</text>`;
  }).join('');
  const lines = series.map(s => {
    let pen = false;
    const path = s.points.map(p => {
      if (!p || !p.every(Number.isFinite)) { pen = false; return ''; }
      const result = `${pen ? 'L' : 'M'}${x(p[0]).toFixed(2)},${y(p[1]).toFixed(2)}`; pen = true; return result;
    }).join(' ');
    return `<path d="${path}" fill="none" stroke="${s.colour}" stroke-width="2.3" ${s.dashed ? 'stroke-dasharray="5 4"' : ''}/>`;
  }).join('');
  return `<figure class="learning-chart"><figcaption>${e(label)}</figcaption><svg role="img" aria-label="${e(label)}；横轴${e(xLabel)}，纵轴${e(yLabel)}" viewBox="0 0 370 202"><g font-size="10" fill="#62715d"><text x="46" y="14">${e(yLabel)}</text>${ticks}${lines}<text x="46" y="180">${e(formatX(lowX))}</text><text x="350" y="180" text-anchor="end">${e(formatX(highX))}</text><text x="198" y="197" text-anchor="middle">${e(xLabel)}</text></g></svg><div class="chart-key">${series.map(s => `<span><i style="background:${s.colour}"></i>${e(s.name)}</span>`).join('')}</div></figure>`;
}

export function trackCharts(detail: TrackDetail) {
  const original: Series['points'] = [], surface: Series['points'] = [];
  for (const p of detail.samples) {
    if (p.breakBefore) { original.push(null); surface.push(null); }
    original.push(p.heightM === null ? null : [p.distanceM, p.heightM]);
    surface.push(p.surfaceHeightM === null ? null : [p.distanceM, p.surfaceHeightM]);
  }
  const height = lineChart('沿轨迹的高程', [{ name: '原始记录', colour: '#b38751', points: original },
    { name: '共同重建地形', colour: '#397c68', points: surface }], '沿记录距离 / m', '高程 / m');
  let episode = '', end = -Infinity;
  const speed: Series['points'] = [];
  for (const p of detail.speedWindows) {
    if (p.episode !== episode || p.elapsedSec - end > 30) speed.push(null);
    speed.push([p.elapsedSec / 60, p.speedMps]); episode = p.episode; end = p.elapsedSec + p.durationSec;
  }
  return `<div class="learning-charts">${height}${lineChart('移动速度随时间变化', [{ name: '有效移动片段', colour: '#568575', points: speed }], '记录开始后 / min', '速度 / m/s')}</div>`;
}

export function travelCharts(data: TravelDiagnostics, mode: string) {
  const p = data.profiles[mode]; if (!p) return '';
  const slope = lineChart('坡度对耗时的影响', [
    { name: '基础估计', colour: '#9ba78d', dashed: true, points: p.gradeCurve.map(c => [c.grade * 100, c.baselineFactor]) },
    { name: '已学范围', colour: '#267866', points: p.gradeCurve.map(c => c.learned ? [c.grade * 100, c.timeFactor] : null) },
  ], '坡度 / %（负值下坡）', '相对平地耗时 / 倍');
  let map = '<figure class="learning-chart"><figcaption>已学习路段的位置与效率</figcaption><p class="chart-empty">尚无路段达到 3 条独立记录。更多重复经过可补充这里。</p></figure>';
  if (p.corridors.length) {
    const coords = p.corridors.flatMap(c => c.coordinates), lngs = coords.map(c => c[0]), lats = coords.map(c => c[1]);
    const west = Math.min(...lngs), east = Math.max(...lngs), south = Math.min(...lats), north = Math.max(...lats);
    const eastScale = Math.cos((north + south) / 2 * Math.PI / 180);
    const scale = Math.min(310 / Math.max((east - west) * eastScale, 1e-7), 145 / Math.max(north - south, 1e-7));
    const project = ([lng, lat]: number[]) => [185 + (lng - (west + east) / 2) * eastScale * scale, 100 - (lat - (south + north) / 2) * scale];
    map = `<figure class="learning-chart"><figcaption>已学习路段的位置与效率</figcaption><svg class="efficiency-map" role="img" aria-label="${p.corridors.length} 条已学习路段，绿色较快，橙色较慢" viewBox="0 0 370 202"><rect x="16" y="16" width="338" height="168" rx="8" fill="#eef3e8"/><text x="320" y="32" font-size="11" fill="#647c61">北 ↑</text>${p.corridors.map(c => `<polyline points="${c.coordinates.map(point => project(point).join(',')).join(' ')}" fill="none" stroke="${c.efficiency >= 1 ? '#277c69' : '#bd7b38'}" stroke-width="3" stroke-linecap="round"><title>相对效率 ${c.efficiency.toFixed(2)} 倍 · ${c.tracks} 条记录 · ${c.observations} 个片段</title></polyline>`).join('')}</svg><div class="chart-key"><span><i style="background:#277c69"></i>效率 ≥ 1</span><span><i style="background:#bd7b38"></i>效率 &lt; 1</span></div></figure>`;
  }
  return `<div class="learning-charts">${slope}${map}</div>`;
}

export const minutes = (seconds: number) => (seconds / 60).toFixed(1) + ' min';
