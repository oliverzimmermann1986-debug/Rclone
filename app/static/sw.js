/* Minimal Service Worker for rclone-sync-container PWA support.
   Network-first for API, no aggressive caching of dynamic content.
*/
self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
  // Intentionally minimal – do not cache API or authenticated responses.
  // Can be extended later with a static asset cache if desired.
});
