import type { Pixel } from './types.ts';

/** Even spacing along the travelled polyline, with a bounded number of
 * markers. Rotation is clockwise from north, like the phone's bearing. */
export function routeArrows(points: number[][], spacing: number, limit = 400): { point: Pixel; rotation: number }[] {
  if (!(spacing > 0) || !Number.isFinite(spacing) || limit < 1) return [];
  const lengths = points.slice(1).map((p, i) => Math.hypot(p[0] - points[i][0], p[1] - points[i][1]));
  const total = lengths.reduce((a, b) => a + b, 0);
  if (!Number.isFinite(total) || total < spacing * .6) return [];
  spacing = Math.max(spacing, total / limit);
  let next = Math.min(spacing / 2, total / 2), travelled = 0;
  const result: { point: Pixel; rotation: number }[] = [];
  for (let i = 0; i < lengths.length; i++) {
    const length = lengths[i], a = points[i], b = points[i + 1];
    if (length <= 0) continue;
    while (next <= travelled + length && next < total) {
      const fraction = (next - travelled) / length;
      result.push({ point: [a[0] + fraction * (b[0] - a[0]), a[1] + fraction * (b[1] - a[1])], rotation: Math.atan2(b[0] - a[0], b[1] - a[1]) });
      next += spacing;
    }
    travelled += length;
  }
  return result;
}
