export interface HeightTrack {
  id: string; name: string; fileName: string; ownerId: string; ownerName: string; enabled: boolean;
  pointCount: number; minHeightM: number; maxHeightM: number; createdAt: number; recordedAt?: number | null;
}
export interface HeightRegion {
  id: string; name: string; fingerprint: string; frame: { origin: [number, number] };
  origin: [number, number]; spacingM: number; columns: number; rows: number; bbox: number[];
  trackIds: string[]; pointCount: number; observationCells: number; supportedCells: number; coverageFraction: number;
  minHeightM: number | null; maxHeightM: number | null;
  alignment: { alignedComponents: number; overlapPairs: number; overlapResidualM: number | null; residualM?: number;
    tracks: { id: string; correctionM: number; driftRangeM?: number; observationCells: number; component: number }[] };
}
export interface HeightGrid extends HeightRegion {
  height: (number | null)[]; support: (number | null)[]; scatter: (number | null)[]; observations: number[][];
  context?: { features: { properties: { kind: string; highway?: string }; geometry: { type: string; coordinates: number[][] | number[][][] } }[] };
}
export interface HeightStatus {
  version: number; modelVersion: number; needsRebuild: boolean; builtAt?: number;
  tracks: HeightTrack[]; regions: HeightRegion[]; pointCount: number;
}
