import { localScales, type Pixel } from './types.ts';

/** What a map needs to place a height model's relief image. */
export interface HeightModelRef {
  artifact: string; frame: { origin: Pixel }; origin: Pixel; spacingM: number; columns: number; rows: number;
  bbox?: number[]; minHeightM?: number | null; maxHeightM?: number | null;
}
const EARTH = 6378137;

export function mercator([lng, lat]: Pixel): Pixel {
  return [lng * Math.PI / 180 * EARTH, Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360)) * EARTH];
}

/** Web Mercator extent of a relief image whose pixels centre on grid nodes. */
export function modelExtent(model: HeightModelRef): number[] {
  const [east, north] = localScales(model.frame.origin), half = model.spacingM / 2;
  const [lng, lat] = model.frame.origin;
  const low = mercator([lng + (model.origin[0] - half) / east, lat + (model.origin[1] - half) / north]);
  const high = mercator([lng + (model.origin[0] + (model.columns - .5) * model.spacingM) / east, lat + (model.origin[1] + (model.rows - .5) * model.spacingM) / north]);
  return [low[0], low[1], high[0], high[1]];
}

export function sharedReliefUrl(model: HeightModelRef) {
  return `/api/elevation/artifacts/${model.artifact.replace(/^models\//, '').replace(/\.npz$/, '')}/relief.png`;
}

export function overlapping<T extends HeightModelRef>(models: T[], bbox: number[]) {
  return models.filter(m => !m.bbox || (m.bbox[0] <= bbox[2] && m.bbox[2] >= bbox[0] && m.bbox[1] <= bbox[3] && m.bbox[3] >= bbox[1]));
}

/** Matches the server's relief: contours every 2, 5 or 10 m with the span. */
export function contourStep(low: number, high: number) {
  const span = high - low;
  return span > 60 ? 10 : span > 25 ? 5 : 2;
}

export function legend(models: HeightModelRef[]) {
  const lows = models.map(m => m.minHeightM).filter((v): v is number => typeof v === 'number' && Number.isFinite(v));
  const highs = models.map(m => m.maxHeightM).filter((v): v is number => typeof v === 'number' && Number.isFinite(v));
  if (!lows.length || !highs.length) return '';
  const low = Math.min(...lows), high = Math.max(...highs);
  return `${low.toFixed(0)}–${high.toFixed(0)} m · 等高线 ${contourStep(low, high)} m`;
}
