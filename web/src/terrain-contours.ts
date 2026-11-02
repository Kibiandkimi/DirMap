import { contourStep } from './elevation-layer.ts';

type Point = [number, number];
export interface TerrainContour { height: number; a: Point; b: Point }
interface Grid { rows: number; columns: number; height: (number | null)[]; minHeightM?: number | null; maxHeightM?: number | null }

/** Intersections with the same two triangles used by the 3D ground mesh. */
export function terrainContours(grid: Grid, stride = 1): TerrainContour[] {
  const low = grid.minHeightM ?? 0, high = grid.maxHeightM ?? 0, step = contourStep(low, high);
  const result: TerrainContour[] = [];
  for (let y = 0; y + stride < grid.rows; y += stride) for (let x = 0; x + stride < grid.columns; x += stride) {
    let valid = true;
    for (let yy = y; yy <= y + stride && valid; yy++) for (let xx = x; xx <= x + stride; xx++) {
      const h = grid.height[yy * grid.columns + xx];
      if (h === null || !Number.isFinite(h)) { valid = false; break; }
    }
    if (!valid) continue;
    const points: Point[] = [[x, y], [x + stride, y], [x, y + stride], [x + stride, y + stride]];
    for (const triangle of [[0, 2, 1], [1, 2, 3]]) {
      const corners = triangle.map(i => points[i]), heights = corners.map(([xx, yy]) => grid.height[yy * grid.columns + xx]!);
      const first = Math.ceil(Math.min(...heights) / step) * step, last = Math.max(...heights);
      for (let height = first; height <= last; height += step) {
        const intersections: Point[] = [];
        for (let i = 0; i < 3; i++) {
          const j = (i + 1) % 3, a = heights[i], b = heights[j];
          if ((a < height) === (b < height)) continue;
          const t = (height - a) / (b - a);
          intersections.push([corners[i][0] + t * (corners[j][0] - corners[i][0]), corners[i][1] + t * (corners[j][1] - corners[i][1])]);
        }
        if (intersections.length === 2 && Math.hypot(intersections[0][0] - intersections[1][0], intersections[0][1] - intersections[1][1]) > 1e-8)
          result.push({ height, a: intersections[0], b: intersections[1] });
      }
    }
  }
  return result;
}
