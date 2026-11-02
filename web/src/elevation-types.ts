export interface HeightTrack {
  id: string; kind?: 'contours'; heightReference?: 'reference' | 'relative' | 'shape'; contourCount?: number; name: string; fileName: string; ownerId: string; ownerName: string; enabled: boolean;
  pointCount: number; minHeightM: number; maxHeightM: number; createdAt: number; recordedAt?: number | null;
  activity?: 'auto' | 'foot' | 'walk' | 'run' | 'bicycle' | 'exclude';
  speedLearning?: { inferredActivity: 'foot' | 'walk' | 'run' | 'bicycle' | 'unknown' | 'excluded'; confidence: number;
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
  travelModels?: { regionId: string; profiles: Record<'foot' | 'bicycle', {
    tracks: number; observations: number; gradeLearned: boolean; corridorCount: number; status: string;
  }> }[];
}

export interface TrackDetail {
  track: HeightTrack; distanceM: number;
  regions: { id: string; name: string; fingerprint: string }[];
  alignment: (HeightRegion['alignment']['tracks'][number] & { regionId: string })[];
  samples: { lng: number; lat: number; heightM: number | null; surfaceHeightM: number | null; distanceM: number; breakBefore: boolean }[];
  speedLearning: NonNullable<HeightTrack['speedLearning']> & {
    episodes: number; timedPairs: number; rejectedPairs: number; speedP10Mps?: number; speedP90Mps?: number;
  };
  speedWindows: { elapsedSec: number; durationSec: number; speedMps: number; grade: number | null; episode: string }[];
}
export interface TravelDiagnostics {
  regionId: string; fingerprint?: string; selectedTrackId?: string;
  tracks: (NonNullable<HeightTrack['speedLearning']> & { id: string; regionObservations: number; episodes: number; rejectedPairs: number })[];
  profiles: Record<string, {
    mode: string; tracks: number; observations: number; gradeLearned: boolean; corridorCount: number; status: string;
    gradeRange: [number, number]; residualLogMad: number | null;
    gradeCurve: { grade: number; timeFactor: number; baselineFactor: number; learned: boolean }[];
    episodes: { id: string; trackId: string; paceMps: number }[];
    corridors: { coordinates: [number, number][]; efficiency: number; tracks: number; observations: number; scatterM: number; trackIds: string[] }[];
  }>;
}
