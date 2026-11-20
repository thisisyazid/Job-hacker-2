// Minimal service worker: makes the app installable and keeps the shell fast.
// API calls always go to the network — job data is never cached here.
const CACHE = "jh-shell-v3";
const SHELL = ["/", "/assets/app.css", "/assets/app.js", "/assets/icons/icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  // Network first, cache as fallback, so new deploys show up immediately.
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res.ok && (url.pathname === "/" || url.pathname.startsWith("/assets/"))) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(url.pathname === "/" ? "/" : e.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(url.pathname.startsWith("/assets/") ? e.request : "/"))
  );
});
