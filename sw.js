const CACHE_NAME = 'transitlab-maroc-v19-20261023';
const STATIC_ASSETS = [
  './',
  './index.html',
  './carte-interactive-maroc.html',
  './assets/leaflet.js',
  './assets/leaflet.css',
  './assets/map-boot.js',
  './assets/map-app.js',
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
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        }
        return response;
      }).catch(() => caches.match(request))
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => cached || fetch(request).then((response) => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
      }
      return response;
    }))
  );
});
