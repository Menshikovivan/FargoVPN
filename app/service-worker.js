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
      data: { source: 'fargovpn' },
    };
    await self.registration.showNotification(title, options);
  })());
});

self.addEventListener('notificationclick', event => {
  // Never navigate to a URL received in push content. Push payloads can be
  // supplied by remote infrastructure; opening a fixed same-origin panel path
  // avoids redirect-style navigation and keeps this worker's trust boundary small.
  event.notification.close();
  event.waitUntil((async () => {
    const appPrefix = `${self.location.origin}${BASE}`;
    const target = appPrefix;
    const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of clients) {
      if ('focus' in client && String(client.url || '').startsWith(appPrefix)) {
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

  // Dynamic API responses must never be served from CacheStorage. In particular,
  // a cached empty chat-feed response repeats the same after_id cursor forever,
  // hiding new Telegram messages until the page/dialog is reopened. Older
  // workers used global caches.match(), which could also read legacy API entries.
  const relativePath = url.pathname.slice(BASE.length);
  const isApi = /(?:^|\/)api(?:\/|$)/.test(relativePath);
  const accept = String(request.headers.get('Accept') || '').toLowerCase();
  const wantsJson = accept.includes('application/json');
  const isStaticAsset = relativePath.startsWith('static/');
  const isManifest = relativePath === 'manifest.webmanifest';

  if (isApi || wantsJson || request.cache === 'no-store' || (!isStaticAsset && !isManifest)) {
    event.respondWith(fetch(request).catch(() => {
      if (isApi || wantsJson) {
        return new Response(JSON.stringify({ok: false, detail: 'Сеть недоступна. Проверьте соединение и повторите запрос.'}), {
          status: 503,
          headers: {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store'},
        });
      }
      return new Response('Сеть недоступна. Повторите запрос после восстановления соединения.', {
        status: 503,
        headers: {'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store'},
      });
    }));
    return;
  }

  // Cache only versioned static assets from this worker's own cache. Do not use
  // the global caches.match(), because that searches every old cache namespace.
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const cached = await cache.match(request);
    if (cached) return cached;
    const response = await fetch(request);
    if (response.ok && isStaticAsset) {
      try { await cache.put(request, response.clone()); } catch (_) {}
    }
    return response;
  })().catch(() => new Response('Сеть недоступна. Повторите запрос после восстановления соединения.', {
    status: 503,
    headers: {'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store'},
  })));
});
