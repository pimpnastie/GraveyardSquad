// Idea #248: minimal service worker so the dashboard qualifies as an
// installable PWA (add-to-home-screen). Deliberately does NOT cache or serve
// stale responses — this is a live clan dashboard (roster/war status changes
// every harvest cycle), so an offline-first cache would show outdated data
// instead of a normal network error. install/activate just take control
// immediately; fetch is left unhandled so every request goes straight to the
// network exactly as if no service worker were present at all.
self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});
