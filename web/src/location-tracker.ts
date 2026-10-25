import { LOCATION_STALE_MS, LOCATION_VERSION, normalizePosition, positionClockPolicy, type LocationFix, type PositionClockPolicy } from './geolocation.ts';

const PROBE_AFTER_MS = 5000, REQUEST_TIMEOUT_MS = 15000, RESTART_AFTER_MS = 30000;
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
type PositionSource = 'watch' | 'request';

/** Keep a watch, but also recover after the FIRST fix if the stream stalls. */
export class LocationTracker {
  private active = false;
  private generation = 0;
  private watchGeneration = 0;
  private watch: number | null = null;
  private watchHighAccuracy = true;
  private timer: ReturnType<typeof setInterval> | null = null;
  private requestSequence = 0;
  private request: { id: number; startedAt: number; highAccuracy: boolean; acceptedAtStart: number } | null = null;
  private nextRequestAt = Infinity;
  private nextHighAccuracy = true;
  private startedAt = 0;
  private lastRestartAt = 0;
  private lastReceivedAt: number | null = null;
  private lastAcceptedAt: number | null = null;
  private lastFix: LocationFix | null = null;
  private lastSignature = '';
  private fresh = false;
  private denied = false;
  private policy: PositionClockPolicy = 'provider';
  private status = '';
  private receivedCount = 0;
  private acceptedCount = 0;
  private rejectedCount = 0;
  private requestCount = 0;
  private restartCount = 0;
  private failureCount = 0;
  private lastRawTimestamp: number | null = null;
  private lastRawAccuracy: number | null = null;
  private lastRejection: string | null = null;
  private lastError: number | null = null;
  onPosition: (position: LocationFix) => void = () => {};
  onStatus: (message: string) => void = () => {};
  onFreshness: (fresh: boolean) => void = () => {};

  start() {
    this.stop();
    this.active = true; this.denied = false;
    this.policy = positionClockPolicy(navigator.userAgent);
    this.startedAt = this.lastRestartAt = performance.now();
    this.lastAcceptedAt = this.lastReceivedAt = null;
    this.lastFix = null; this.lastSignature = '';
    this.receivedCount = this.acceptedCount = this.rejectedCount = this.requestCount = this.restartCount = 0;
    this.failureCount = 0;
    this.lastRawTimestamp = this.lastRawAccuracy = this.lastError = null;
    this.lastRejection = null;
    this.nextRequestAt = this.startedAt + PROBE_AFTER_MS;
    this.nextHighAccuracy = true;
    if (!navigator.geolocation) { this.setStatus('此浏览器没有定位接口，请使用支持定位的浏览器打开'); return; }
    this.setStatus('正在获取位置…');
    this.startWatch(true);
    this.timer = setInterval(() => this.tick(), 1000);
  }

  retry() { if (!this.active || this.denied || !this.hasFreshFix()) this.start(); }

  stop() {
    this.active = false; this.generation++;
    this.clearWatch();
    this.request = null;
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
    this.setFresh(false);
  }

  private current(generation: number) { return this.active && !document.hidden && !this.denied && generation === this.generation; }
  private fixAge() {
    return this.lastFix && this.lastAcceptedAt !== null
      ? Math.max(0, this.lastFix.receivedAt - this.lastFix.timestamp) + Math.max(0, performance.now() - this.lastAcceptedAt)
      : Infinity;
  }
  private hasFreshFix() { return this.fixAge() < LOCATION_STALE_MS; }
  private setFresh(fresh: boolean) { if (this.fresh !== fresh) { this.fresh = fresh; this.onFreshness(fresh); } }
  private setStatus(message: string) { if (this.status !== message) { this.status = message; this.onStatus(message); } }
  private clearWatch() {
    this.watchGeneration++;
    if (this.watch !== null) navigator.geolocation?.clearWatch(this.watch);
    this.watch = null;
  }

  private startWatch(highAccuracy: boolean) {
    this.clearWatch();
    this.watchHighAccuracy = highAccuracy;
    this.lastRestartAt = performance.now();
    const generation = this.generation, watchGeneration = this.watchGeneration;
    const current = () => this.current(generation) && watchGeneration === this.watchGeneration;
    try {
      const watch = navigator.geolocation.watchPosition(
        position => { if (current()) this.accept(position, 'watch'); },
        error => { if (current()) this.fail(error.code, 'watch', highAccuracy); },
        { enableHighAccuracy: highAccuracy, maximumAge: 0, timeout: REQUEST_TIMEOUT_MS });
      // Also handle implementations/test doubles which invoke a callback inline.
      if (current()) this.watch = watch;
      else navigator.geolocation.clearWatch(watch);
    } catch {
      this.lastError = 2;
      this.setStatus('浏览器暂未启动定位，正在自动重试…');
      this.nextRequestAt = performance.now() + 1000;
    }
  }

  private accept(raw: GeolocationPosition, source: PositionSource) {
    const now = Date.now(), monotonicNow = performance.now();
    this.receivedCount++; this.lastReceivedAt = monotonicNow;
    this.lastRawTimestamp = finite(raw?.timestamp) ? raw.timestamp : null;
    this.lastRawAccuracy = finite(raw?.coords?.accuracy) ? raw.coords.accuracy : null;
    const fix = normalizePosition(raw, now, this.policy);
    if (!fix) {
      this.rejectedCount++; this.lastRejection = 'invalid-coordinates-or-timestamp';
      if (!this.hasFreshFix()) this.setStatus('位置数据暂不可用，正在重新获取…');
      return;
    }
    const signature = JSON.stringify([fix.rawTimestamp, fix.coords.longitude, fix.coords.latitude,
      fix.coords.accuracy, fix.coords.heading, fix.coords.speed]);
    const outOfOrder = this.lastFix && fix.clock !== 'received' && this.lastFix.clock !== 'received' && fix.timestamp < this.lastFix.timestamp;
    // An unchanged positive-timestamp watch sample is not proof of a live sensor.
    // A new maximumAge:0 request can confirm a stationary fix for a broken-clock
    // provider. Zero/missing clocks cannot be ordered and use callback receipt.
    const repeated = source === 'watch' && fix.rawTimestamp !== null && fix.rawTimestamp > 0 && signature === this.lastSignature;
    if (outOfOrder || repeated) {
      this.rejectedCount++; this.lastRejection = outOfOrder ? 'out-of-order' : 'repeated-watch-fix';
      return;
    }
    this.lastFix = fix; this.lastSignature = signature; this.lastAcceptedAt = monotonicNow;
    this.acceptedCount++; this.failureCount = 0; this.nextHighAccuracy = true;
    this.nextRequestAt = monotonicNow + PROBE_AFTER_MS;
    this.setFresh(true);
    this.setStatus(`定位精度 ±${Math.round(fix.coords.accuracy)} m`);
    this.onPosition(fix);
  }

  private fail(code: number, source: PositionSource, highAccuracy: boolean) {
    this.lastError = code;
    if (code === 1) {
      this.denied = true;
      this.clearWatch(); this.request = null;
      this.setFresh(false);
      this.setStatus('定位权限未开启，请在浏览器设置中允许后点击跟随重试');
      return;
    }
    if (!this.hasFreshFix()) {
      this.setFresh(false);
      this.setStatus(code === 3 ? '定位超时，正在自动重试；请尽量到开阔处' : '暂未获取位置，正在自动重试；请检查系统定位');
    }
    this.nextHighAccuracy = !highAccuracy;
    this.failureCount++;
    // Try the other provider promptly once, then back off on persistent errors
    // instead of repeatedly waking GPS/network every second while unavailable.
    const delay = this.failureCount === 1 ? 1000 : Math.min(RESTART_AFTER_MS, PROBE_AFTER_MS * 2 ** Math.min(3, this.failureCount - 2));
    this.nextRequestAt = performance.now() + delay;
    if (source === 'watch' && highAccuracy) this.startWatch(false);
  }

  private requestPosition() {
    if (this.request || typeof navigator.geolocation.getCurrentPosition !== 'function') return;
    const request = { id: ++this.requestSequence, startedAt: performance.now(), highAccuracy: this.nextHighAccuracy, acceptedAtStart: this.acceptedCount };
    this.request = request; this.requestCount++;
    const generation = this.generation;
    const current = () => this.current(generation) && this.request?.id === request.id;
    const finish = () => { this.request = null; this.nextRequestAt = performance.now() + PROBE_AFTER_MS; };
    try {
      navigator.geolocation.getCurrentPosition(position => {
        if (!current()) return;
        finish();
        // A recovering watch can beat a pending one-shot. With an opaque clock
        // we cannot prove the delayed request is newer than that live watch.
        if (request.acceptedAtStart !== this.acceptedCount && this.hasFreshFix()) return;
        this.accept(position, 'request');
      }, error => {
        if (!current()) return;
        finish(); this.fail(error.code, 'request', request.highAccuracy);
      }, { enableHighAccuracy: request.highAccuracy, maximumAge: 0, timeout: REQUEST_TIMEOUT_MS });
    } catch {
      if (current()) { finish(); this.fail(2, 'request', request.highAccuracy); }
    }
  }

  private tick() {
    if (!this.current(this.generation)) return;
    const now = performance.now();
    if (!this.hasFreshFix() && now - this.startedAt >= LOCATION_STALE_MS) {
      this.setFresh(false);
      this.setStatus(this.lastFix ? '位置暂未更新，正在重新定位…' : '仍在获取位置，正在自动重试…');
    }
    // Some WebViews omit even the timeout callback. Retire the request id so
    // its eventual response cannot overwrite a newer request or resumed session.
    if (this.request && now - this.request.startedAt >= REQUEST_TIMEOUT_MS + 1000) {
      const highAccuracy = this.request.highAccuracy;
      this.request = null;
      this.fail(3, 'request', highAccuracy);
    }
    const quietFor = this.lastAcceptedAt === null ? now - this.startedAt : now - this.lastAcceptedAt;
    if (quietFor >= PROBE_AFTER_MS && now >= this.nextRequestAt) this.requestPosition();
    if (quietFor >= RESTART_AFTER_MS && now - this.lastRestartAt >= RESTART_AFTER_MS) {
      this.restartCount++;
      this.startWatch(!this.watchHighAccuracy);
    }
  }

  /** No coordinates are included in this user-copyable diagnostic snapshot. */
  diagnostics() {
    const now = performance.now(), age = this.fixAge();
    return { version: LOCATION_VERSION, active: this.active, clockPolicy: this.policy, status: this.status,
      watchMode: this.watch === null ? null : this.watchHighAccuracy ? 'high-accuracy' : 'balanced',
      requestPending: this.request !== null, permissionDenied: this.denied,
      receivedCount: this.receivedCount, acceptedCount: this.acceptedCount, rejectedCount: this.rejectedCount,
      requestCount: this.requestCount, restartCount: this.restartCount, consecutiveErrors: this.failureCount,
      lastRawTimestamp: this.lastRawTimestamp, lastRawAccuracy: this.lastRawAccuracy,
      lastClock: this.lastFix?.clock ?? null, lastNormalizedTimestamp: this.lastFix?.timestamp ?? null,
      lastReceivedAgoMs: this.lastReceivedAt === null ? null : Math.round(now - this.lastReceivedAt),
      lastFixAgeMs: Number.isFinite(age) ? Math.round(age) : null,
      lastRejection: this.lastRejection, lastErrorCode: this.lastError };
  }
}
