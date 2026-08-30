/* Statischer App-Shell-Cache. Authentifizierte und dynamische Antworten
   werden bewusst nie vom Service Worker verarbeitet oder gespeichert. */
const CACHE_PREFIX = 'rclone-sync-static-';
const CACHE_NAME = `${CACHE_PREFIX}v3`;
const STATIC_ASSETS = new Set([
  '/static/style.css',
  '/static/alpine.min.js',
  '/static/ui-helpers.js',
  '/static/app.js',
  '/static/manifest.json',
  '/static/app-icon-192.png',
  '/static/app-icon-512.png',
  '/static/app-icon-1024.png',
]);

function staticPath(requestUrl) {
  const url = new URL(requestUrl);
  if (url.origin !== self.location.origin) return null;
  if (!STATIC_ASSETS.has(url.pathname)) return null;
  return url.pathname;
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(
      [...STATIC_ASSETS].map((path) => new Request(path, { cache: 'reload' })),
    )),
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names
          .filter((name) => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME)
          .map((name) => caches.delete(name)),
      ))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  const path = staticPath(event.request.url);
  if (!path) return;

  event.respondWith(
    caches.open(CACHE_NAME).then(async (cache) => {
      try {
        const request = new Request(event.request, { cache: 'no-cache' });
        const response = await fetch(request);
        if (!response.ok) {
          return (await cache.match(path)) || response;
        }
        if (response.type === 'basic') {
          await cache.put(path, response.clone());
        }
        return response;
      } catch (error) {
        const cached = await cache.match(path);
        if (cached) return cached;
        throw error;
      }
    }),
  );
});
