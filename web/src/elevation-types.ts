export interface HeightTrack {
  id: string; kind?: 'contours'; heightReference?: 'reference' | 'relative'; contourCount?: number; name: string; fileName: string; ownerId: string; ownerName: string; enabled: boolean;
  pointCount: number; minHeightM: number; maxHeightM: number; createdAt: number; recordedAt?: number | null;
  activity?: 'auto' | 'walk' | 'run' | 'bicycle' | 'exclude';
  speedLearning?: { inferredActivity: 'walk' | 'run' | 'bicycle' | 'unknown' | 'excluded'; confidence: number;
    observations: number; movingSec: number; stoppedSec: number; medianSpeedMps: number | null; reason: string } | null;
}
export interface HeightRegion {
  id: string; name: string; fingerprint: string; frame: { origin: [number, number] };
  origin: [number, number]; spacingM: number; columns: number; rows: number; bbox: number[];
  trackIds: string[]; pointCount: number; observationCells: number; supportedCells: number; coverageFraction: number;
  minHeightM: number | null; maxHeightM: number | null;
  datum?: string; relativeContourSources?: number;
  alignment: { alignedComponents: number; unanchoredComponents?: number; overlapPairs: number; overlapResidualM: number | null; residualM?: number | null;
    tracks: { id: string; correctionM: number | null; driftRangeM?: number | null; observationCells: number; component: number; anchored?: boolean }[] };
}
export interface HeightGrid extends HeightRegion {
  height: (number | null)[]; support: (number | null)[]; scatter: (number | null)[]; observations: number[][];
  context?: { features: { properties: { kind: string; highway?: string }; geometry: { type: string; coordinates: number[][] | number[][][] } }[] };
}
export interface HeightStatus {
  version: number; modelVersion: number; needsRebuild: boolean; builtAt?: number;
  tracks: HeightTrack[]; regions: HeightRegion[]; pointCount: number;
  travelModels?: { regionId: string; profiles: Record<'walk' | 'run' | 'bicycle', {
    tracks: number; observations: number; gradeLearned: boolean; corridorCount: number; status: string;
  }> }[];
}
