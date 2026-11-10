// beets-ui service worker. It exists so browsers offer to install beets-ui as an
// app; it caches nothing and lets every request go to the server as usual.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
