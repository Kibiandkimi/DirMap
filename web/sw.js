/* Only this app's shell and at most three released course packages.
 * Standard-map tiles are neither prefetched nor stored by this worker. */
// The build inserts a content fingerprint so every new application refreshes
// its offline shell, even when the worker's logic itself has not changed.
const SHELL = 'dirmap-shell-__DIRMAP_BUILD_ID__';
const COURSES = 'dirmap-courses-v1';
const RELEASE = /^\/api\/releases\/([0-9a-f]{32})$/;
const OVERLAY = /^\/api\/releases\/[0-9a-f]{32}\/overlay\.png$/;

self.addEventListener('install', event => event.waitUntil((async () => {
  const response = await fetch('/', { cache: 'reload' });
  if (!response.ok) throw new Error('Cannot download application shell');
  const html = await response.clone().text();
  const assets = [...html.matchAll(/(?:src|href)=["'](\/assets\/[^"']+\.(?:js|css))["']/g)].map(match => match[1]);
  const cache = await caches.open(SHELL);
  await cache.put('/index.html', response);
  await cache.addAll(assets);
  await self.skipWaiting();
})()));

self.addEventListener('activate', event => event.waitUntil((async () => {
  for (const name of await caches.keys()) {
    if ((name.startsWith('dirmap-shell-') || name.startsWith('dirmap-courses-')) && name !== SHELL && name !== COURSES) await caches.delete(name);
  }
  await self.clients.claim();
})()));

async function limitedFetch(request, limitMs = 4000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), limitMs);
  try { return await fetch(request, { signal: controller.signal }); }
  finally { clearTimeout(timer); }
}

async function saveJSON(cache, request, response) {
  const headers = new Headers(response.headers);
  // The stored body is already decoded; keeping these would misdescribe it.
  headers.delete('Content-Encoding'); headers.delete('Content-Length');
  headers.set('X-DirMap-Saved-At', new Date().toISOString());
  await cache.put(request, new Response(await response.clone().arrayBuffer(), { status: response.status, headers }));
}

async function trimCourses(cache) {
  const entries = [];
  for (const request of await cache.keys()) {
    const match = new URL(request.url).pathname.match(RELEASE);
    if (match) entries.push({ id: match[1], request, saved: (await cache.match(request)).headers.get('X-DirMap-Saved-At') || '' });
  }
  entries.sort((a, b) => b.saved.localeCompare(a.saved));
  for (const entry of entries.slice(3)) {
    await cache.delete(entry.request);
    await cache.delete(`/api/releases/${entry.id}/overlay.png`);
  }
  const kept = new Set(entries.slice(0, 3).map(entry => entry.id));
  for (const request of await cache.keys()) {
    const path = new URL(request.url).pathname;
    if (OVERLAY.test(path) && !kept.has(path.split('/')[3])) await cache.delete(request);
  }
}

async function freshJSON(request) {
  const cache = await caches.open(COURSES), saved = await cache.match(request);
  try {
    // Fall back to a saved copy quickly; without one, a slow first download
    // is better than no course at all.
    const response = await limitedFetch(request, saved ? 4000 : 30000);
    // An explicit server 404/409 must not resurrect a deleted/stale course.
    if (response.ok) { await saveJSON(cache, request, response); await trimCourses(cache); }
    return response;
  } catch (error) {
    const cached = saved ?? await cache.match(request);
    if (!cached) throw error;
    return Response.json({ ...await cached.json(), offline: true, savedAt: cached.headers.get('X-DirMap-Saved-At') },
      { headers: { 'X-DirMap-Offline': '1' } });
  }
}

self.addEventListener('fetch', event => {
  const request = event.request, url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (request.mode === 'navigate') {
    event.respondWith(limitedFetch(request).catch(async () => (await caches.open(SHELL)).match('/index.html')));
  } else if (RELEASE.test(url.pathname) || ['/api/health', '/api/reference'].includes(url.pathname)) {
    event.respondWith(freshJSON(request));
  } else if (OVERLAY.test(url.pathname)) {
    event.respondWith((async () => {
      const cache = await caches.open(COURSES), cached = await cache.match(request);
      if (cached) return cached;
      const response = await fetch(request);
      if (response.ok) { await cache.put(request, response.clone()); await trimCourses(cache); }
      return response;
    })());
  } else if (/^\/assets\/.*\.(?:js|css)$/.test(url.pathname)) {
    event.respondWith((async () => {
      const cache = await caches.open(SHELL);
      return await cache.match(request) || fetch(request);
    })());
  }
});

self.addEventListener('message', event => {
  if (event.data?.type !== 'CACHE_COURSE' || !/^[0-9a-f]{32}$/.test(event.data.id)) return;
  event.waitUntil((async () => {
    try {
      const id = event.data.id, cache = await caches.open(COURSES);
      const url = `/api/releases/${id}`, course = await limitedFetch(url, 30000);
      if (!course.ok || (await course.clone().json()).superseded) throw new Error('课程已更新或不可用');
      const overlay = await limitedFetch(url + '/overlay.png', 60000);
      if (!overlay.ok) throw new Error('原图尚未下载完整');
      await cache.put(url + '/overlay.png', overlay);
      await saveJSON(cache, url, course);
      for (const path of ['/api/health', '/api/reference']) {
        try { const response = await limitedFetch(path); if (response.ok) await saveJSON(cache, path, response); } catch { /* course overlay and routes are already complete */ }
      }
      await trimCourses(cache);
      event.ports[0]?.postMessage({ ok: true });
    } catch (error) { event.ports[0]?.postMessage({ ok: false, message: error instanceof Error ? error.message : String(error) }); }
  })());
});
