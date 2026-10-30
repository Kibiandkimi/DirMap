import type { Route } from './types';

/** Unknown spans break the profile; never draw an invented ramp across them.
 * Students and printed sheets omit the note when no shared height covers the route. */
export function elevationSummary(route: Route, chart = true, noteMissing = true) {
  const data = route.elevation;
  if (!data) return '';
  if (data.ascentM === null) return noteMissing ? '<div class="route-elevation muted">暂无高程覆盖 · 预计用时未计上下坡</div>' : '';
  let svg = '';
  const samples = data.profile.filter(p => p.heightM !== null && Number.isFinite(p.heightM));
  if (chart && samples.length > 1) {
    const low = Math.min(...samples.map(p => p.heightM!)), high = Math.max(low + 2, ...samples.map(p => p.heightM!));
    const length = Math.max(1, ...data.profile.map(p => p.distanceM));
    const segments: string[][] = [[]];
    for (const point of data.profile) {
      if (point.heightM === null || !Number.isFinite(point.heightM)) { if (segments.at(-1)!.length) segments.push([]); continue; }
      segments.at(-1)!.push(`${(34 + point.distanceM / length * 270).toFixed(1)},${(65 - (point.heightM - low) / (high - low) * 53).toFixed(1)}`);
    }
    svg = `<svg class="route-height-profile" viewBox="0 0 320 84" role="img" aria-label="路线参考高程，空白段没有高程覆盖"><path d="M34 12V65H304" fill="none" stroke="#dce5d6"/>${segments.filter(s => s.length > 1).map(s => `<polyline points="${s.join(' ')}" fill="none" stroke="#4f8063" stroke-width="2"/>`).join('')}<g fill="#84927b" font-size="9"><text x="2" y="17">${high.toFixed(0)} m</text><text x="2" y="68">${low.toFixed(0)} m</text><text x="34" y="80">起点</text><text x="304" y="80" text-anchor="end">${Math.round(length)} m</text></g></svg>`;
  }
  return `<div class="route-elevation"><span>预计爬升 <b>${Math.round(data.ascentM)} m</b> · 下降 <b>${Math.round(data.descentM ?? 0)} m</b></span><small>高程覆盖 ${Math.round(data.coverageFraction * 100)}%</small>${svg}${data.unknownDistanceM > 1 ? `<small class="elevation-gap">其余约 ${Math.round(data.unknownDistanceM)} m 缺少高程，未计该部分上下坡。</small>` : ''}</div>`;
}
