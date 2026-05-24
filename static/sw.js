// Minimal service worker — required for PWA installability.
// No caching: the app always runs against the local Flask server.
self.addEventListener('fetch', event => {
  event.respondWith(fetch(event.request));
});
