/** Anonymous height samples a phone sends while its course collects them. */
export interface HeightSample { t: number; lng: number; lat: number; h: number; hacc: number; vacc: number | null }
interface Coordinates { latitude: number; longitude: number; accuracy: number; altitude: number | null; altitudeAccuracy: number | null }
interface Receipt { enabled: boolean; accepted: number; full?: boolean }

export const SAMPLE_GAP_MS = 3000, SAMPLE_MOVE_M = 4, MAX_PENDING = 3000, BATCH = 600;

/** A fix worth sending: a real height and usable horizontal/vertical accuracy. */
export function heightSample(coords: Coordinates, at: number): HeightSample | null {
  const { altitude, altitudeAccuracy } = coords;
  if (typeof altitude !== 'number' || !Number.isFinite(altitude)) return null;
  // Some WebViews report exactly zero when they have no height at all.
  if (altitude === 0 && !altitudeAccuracy) return null;
  if (!(coords.accuracy <= 25)) return null;
  const vertical = typeof altitudeAccuracy === 'number' && Number.isFinite(altitudeAccuracy) ? altitudeAccuracy : null;
  if (vertical !== null && vertical > 30) return null;
  return { t: Math.round(at), lng: coords.longitude, lat: coords.latitude, h: altitude, hacc: coords.accuracy, vacc: vertical };
}

function metres(a: HeightSample, b: HeightSample) {
  const north = (b.lat - a.lat) * 110540, east = (b.lng - a.lng) * 111320 * Math.cos(a.lat * Math.PI / 180);
  return Math.hypot(east, north);
}

function randomId() {
  const bytes = new Uint8Array(12); crypto.getRandomValues(bytes);
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
}

/**
 * A random identifier per phone and course (never the team or the progress
 * device), a bounded queue that survives reloads and offline stretches, and
 * at most one sample every few seconds or metres.
 */
export class HeightSampler {
  enabled = false;
  sent = 0;
  /** After every change that a status line would show. */
  onChange: () => void = () => {};
  private pending: HeightSample[] = [];
  private last: HeightSample | null = null;
  private sending = false;
  readonly deviceId: string;
  private courseId: string;
  private post: (deviceId: string, batch: HeightSample[]) => Promise<Receipt>;
  private storage: Storage | null;

  constructor(courseId: string, post: (deviceId: string, batch: HeightSample[]) => Promise<Receipt>, storage: Storage | null = null) {
    this.courseId = courseId; this.post = post; this.storage = storage;
    let device = this.read('device');
    if (!device || !/^[A-Za-z0-9_-]{16,64}$/.test(device)) { device = randomId(); this.write('device', device); }
    this.deviceId = device;
    try { const saved = JSON.parse(this.read('pending') ?? '[]'); if (Array.isArray(saved)) this.pending = saved.slice(-MAX_PENDING); }
    catch { this.pending = []; }
    this.sent = Number(this.read('sent')) || 0;
  }
  private read(name: string) { try { return this.storage?.getItem(`dirmap-live-heights:${this.courseId}:${name}`) ?? null; } catch { return null; } }
  private write(name: string, value: string | null) {
    try { if (value === null) this.storage?.removeItem(`dirmap-live-heights:${this.courseId}:${name}`); else this.storage?.setItem(`dirmap-live-heights:${this.courseId}:${name}`, value); }
    catch { /* a private window keeps samples for this page only */ }
  }
  get optedOut() { return this.read('opt-out') === 'true'; }
  set optedOut(value: boolean) { this.write('opt-out', value ? 'true' : null); if (value) this.discard(); this.onChange(); }
  get waiting() { return this.pending.length; }
  get collecting() { return this.enabled && !this.optedOut; }

  add(coords: Coordinates, at = Date.now()) {
    if (!this.collecting) return false;
    const sample = heightSample(coords, at);
    if (!sample) return false;
    if (this.last && sample.t - this.last.t < SAMPLE_GAP_MS && metres(this.last, sample) < SAMPLE_MOVE_M) return false;
    this.last = sample; this.pending.push(sample);
    if (this.pending.length > MAX_PENDING) this.pending.splice(0, this.pending.length - MAX_PENDING);
    this.write('pending', JSON.stringify(this.pending));
    this.onChange();
    return true;
  }
  discard() { this.pending = []; this.last = null; this.write('pending', null); }
  async flush() {
    if (this.sending || !this.pending.length || !this.collecting) return;
    this.sending = true;
    const batch = this.pending.slice(0, BATCH);
    try {
      const receipt = await this.post(this.deviceId, batch);
      if (!receipt.enabled) { this.enabled = false; this.discard(); return; }
      this.pending = this.pending.slice(batch.length); this.sent += receipt.accepted;
      this.write('pending', JSON.stringify(this.pending)); this.write('sent', String(this.sent));
      // A phone at its limit stops collecting rather than retrying forever.
      if (receipt.full) { this.enabled = false; this.discard(); }
    } catch { /* kept for the next attempt, e.g. after reconnecting */ }
    finally { this.sending = false; this.onChange(); }
  }
}
