/** All consumers (map, heading and routing) use the same Unix-ms timestamp. */
export const LOCATION_VERSION = '2026-10-25.1';
export const LOCATION_STALE_MS = 10000;
export type PositionClock = 'unix-ms' | 'unix-seconds' | 'received';
export type PositionClockPolicy = 'provider' | 'received';
export type LocationFix = GeolocationPosition & { receivedAt: number; rawTimestamp: number | null; clock: PositionClock };
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);

export function positionClockPolicy(userAgent: string): PositionClockPolicy {
  // The reported OpenHarmony/ArkWeb WeChat provider works when freshness is
  // based on callbacks, but its timestamp is not a reliable Unix/page clock.
  // Keep this policy explicit: ordinary browsers still reject old Unix fixes.
  // All native requests using this policy must ask for maximumAge: 0.
  return /\b(?:OpenHarmony|ArkWeb)\b/i.test(userAgent) ? 'received' : 'provider';
}

export function normalizePosition(position: GeolocationPosition, receivedAt: number, policy: PositionClockPolicy = 'provider'): LocationFix | null {
  const coords = position?.coords;
  if (!finite(receivedAt) || !coords || !finite(coords.latitude) || !finite(coords.longitude) ||
      Math.abs(coords.latitude) > 90 || Math.abs(coords.longitude) > 180 ||
      !finite(coords.accuracy) || coords.accuracy < 0) return null;
  const raw = position.timestamp;
  let timestamp = receivedAt, clock: PositionClock = 'received';
  if (policy === 'provider' && finite(raw) && raw < 0) return null;
  if (policy === 'provider') {
    if (finite(raw) && raw >= 1e12) { timestamp = raw; clock = 'unix-ms'; }
    else if (finite(raw) && raw >= 1e9) { timestamp = raw * 1000; clock = 'unix-seconds'; }
  }
  // A small timestamp does NOT establish performance.timeOrigin as its origin.
  // It may be measured since device boot, in another unit, or in another process.
  // Adding the page origin made otherwise live streams go stale as the page aged.
  if (timestamp > receivedAt + 250 || receivedAt - timestamp > LOCATION_STALE_MS) return null;
  return { coords, timestamp, receivedAt, rawTimestamp: finite(raw) ? raw : null, clock,
    toJSON: () => ({ coords: typeof coords.toJSON === 'function' ? coords.toJSON() : coords, timestamp }) };
}
