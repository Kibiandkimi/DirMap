import { normalizePosition, positionClockPolicy, type LocationFix } from './geolocation.ts';

/** One deliberate survey capture; its deadline does not depend on the WebView. */
export function captureFieldPosition(geolocation: Pick<Geolocation, 'getCurrentPosition'>,
  userAgent: string, signal: AbortSignal, timeoutMs = 20000): Promise<LocationFix> {
  return new Promise((resolve, reject) => {
    let settled = false;
    const expiresAt = performance.now()+timeoutMs;
    const finish = (fix: LocationFix | null, error?: Error) => {
      if (settled) return;
      settled = true; clearTimeout(deadline); signal.removeEventListener('abort', cancel);
      if (fix) resolve(fix); else reject(error);
    };
    const cancel = () => finish(null, new DOMException('已取消定位采集', 'AbortError'));
    const deadline = setTimeout(() => finish(null, new Error('定位超时，请到开阔处重试')), timeoutMs);
    signal.addEventListener('abort', cancel, { once: true });
    if (signal.aborted) { cancel(); return; }
    try {
      geolocation.getCurrentPosition(position => {
        if (settled) return;
        // Timers may be suspended while a page is in the background. A
        // native callback delivered first on resume must still expire.
        if (performance.now() >= expiresAt) {
          finish(null, new Error('定位超时，请到开阔处重试')); return;
        }
        const fix = normalizePosition(position, Date.now(), positionClockPolicy(userAgent));
        if (!fix || fix.coords.accuracy <= 0) {
          finish(null, new Error('位置数据已过期或无效，请重新采集')); return;
        }
        finish(fix);
      }, error => finish(null, new Error(error.code === 1 ? '请允许浏览器定位后重试'
        : error.code === 3 ? '定位超时，请到开阔处重试' : '未取得当前位置，请重试')),
      { enableHighAccuracy: true, maximumAge: 0, timeout: timeoutMs });
    } catch { finish(null, new Error('浏览器未能启动定位，请重试')); }
  });
}
