const CACHE_NAME = 'transitlab-maroc-v24-20261023';
const STATIC_ASSETS = [
  './',
  './index.html',
  './carte-interactive-maroc.html',
  './assets/leaflet.js',
  './assets/leaflet.css',
  './assets/map-boot.js',
  './assets/map-app.js',
  './assets/population-choropleth.mjs',
  './assets/line-index-worker.js',
  './assets/data-catalog.json',
  './assets/transitlab-favicon-32.png',
  './assets/transitlab-favicon-48.png',
  './assets/transitlab-favicon-180.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;

  const isData = /\/assets\/(network-data|network-optional|communes-fast)\.json$/.test(url.pathname);
  if (isData) {
    event.respondWith(
      fetch(request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy)).catch(() => {});
        }
        return response;
      }).catch(() => caches.match(request))
    );
    return;
  }

  // The HTML shell and versioned JavaScript must not be trapped behind an old
  // cache entry. Prefer a revalidated network response, retaining a cache
  // fallback for offline use.
  const isShell = request.mode === 'navigate'
    || /\/(?:index\.html|carte-interactive-maroc\.html)$/.test(url.pathname)
    || /\/assets\/(?:map-boot\.js|map-app\.js|population-choropleth\.mjs|line-index-worker\.js|leaflet\.js|leaflet\.css)$/.test(url.pathname);

  if (isShell) {
    event.respondWith(
      fetch(request, { cache: 'no-cache' }).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy)).catch(() => {});
        }
        return response;
      }).catch(async () => {
        const exact = await caches.match(request);
        const fallback = exact || await caches.match(request, { ignoreSearch: true });
        if (fallback) return fallback;
        return new Response('Hors ligne : la carte n’est pas disponible dans le cache.', {
          status: 503,
          headers: { 'Content-Type': 'text/plain; charset=utf-8' }
        });
      })
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => cached || fetch(request).then((response) => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(request, copy)).catch(() => {});
      }
      return response;
    }))
  );
});
