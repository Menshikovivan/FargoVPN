const CACHE = '__FARGOVPN_CACHE__';
const BASE = '__FARGOVPN_BASE__';
const VERSION = '__FARGOVPN_VERSION__';
const ASSETS = [
  `${BASE}static/panel.css?v=${VERSION}`,
  `${BASE}static/panel.js?v=${VERSION}`,
  `${BASE}manifest.webmanifest?v=${VERSION}`,
  `${BASE}static/icons/icon-192.png`,
  `${BASE}static/icons/icon-512.png`,
];

self.addEventListener('message', event => {
  if (event.data && event.data.type === 'SKIP_WAITING') self.skipWaiting();
});

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => Promise.allSettled(ASSETS.map(asset => cache.add(asset)))).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('fargovpn-static-v') && key !== CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('push', event => {
  event.waitUntil((async () => {
    let data = {};
    try { data = event.data ? event.data.json() : {}; } catch (_) {
      try { data = { body: event.data ? event.data.text() : '' }; } catch (__) { data = {}; }
    }
    const title = String(data.title || 'FargoVPN').slice(0, 120);
    const receivedAt = Date.now();
    const serverSentAt = Number(data.server_sent_at_ms || 0);
    if (serverSentAt > 0) {
      const providerToDeviceMs = Math.max(0, receivedAt - serverSentAt);
      console.info('[FargoVPN Push] received', { serverSentAt, receivedAt, providerToDeviceMs, version: VERSION });
    }
    const options = {
      body: String(data.body || '').slice(0, 500),
      icon: `${BASE}static/icons/icon-192.png`,
      badge: `${BASE}static/icons/icon-192.png`,
      tag: String(data.tag || 'fargovpn').slice(0, 100),
      renotify: true,
      data: { url: String(data.url || `${BASE}cabinet`) },
    };
    await self.registration.showNotification(title, options);
  })());
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const raw = event.notification && event.notification.data && event.notification.data.url;
  event.waitUntil((async () => {
    let targetUrl;
    try { targetUrl = new URL(String(raw || `${BASE}cabinet`), self.location.origin); }
    catch (_) { targetUrl = new URL(`${BASE}cabinet`, self.location.origin); }
    if (targetUrl.origin !== self.location.origin || !targetUrl.pathname.startsWith(BASE)) {
      targetUrl = new URL(`${BASE}cabinet`, self.location.origin);
    }
    const target = targetUrl.href;
    const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of clients) {
      if ('focus' in client && new URL(client.url).pathname.startsWith(BASE)) {
        try { await client.navigate(target); } catch (_) {}
        return client.focus();
      }
    }
    return self.clients.openWindow(target);
  })());
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith(BASE)) return;
  if (url.pathname === new URL('service-worker.js', self.location).pathname) return;
  event.respondWith(
    caches.match(request).then(cached => cached || fetch(request).then(response => {
      const copy = response.clone();
      if (response.ok && url.pathname.startsWith(BASE + 'static/')) {
        caches.open(CACHE).then(cache => cache.put(request, copy)).catch(() => {});
      }
      return response;
    }).catch(() => cached || new Response('Сеть недоступна. Повторите запрос после восстановления соединения.', {status: 503, headers: {'Content-Type': 'text/plain; charset=utf-8'}})))
  );
});


