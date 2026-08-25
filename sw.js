/* FinCom OS service worker - pass-through only (no caching; portal data stays network-fresh). */
self.addEventListener("install", function (e) {
  self.skipWaiting();
});
self.addEventListener("activate", function (e) {
  e.waitUntil(self.clients.claim());
});
self.addEventListener("fetch", function (e) {
  e.respondWith(fetch(e.request));
});
