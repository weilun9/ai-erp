// Service worker: lets the app open and the till keep selling with no connection.
//
// - Page loads: network first, the cached app shell when offline.
// - Built files (/assets/*, hashed names, so they never change): cache first.
// - Fonts: cached after the first load.
// - /api/*: never cached. Business data and login tokens stay on the server; the till
//   keeps its own small offline copy (catalog, shift, unsent sales) in the page.

const SHELL = "ai-erp-shell-v1";
const RUNTIME = "ai-erp-runtime-v1";
const PRECACHE = ["/", "/manifest.webmanifest", "/icon.svg", "/icon-192.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((c) => c.addAll(PRECACHE)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== SHELL && k !== RUNTIME).map((k) => caches.delete(k))),
    ),
  );
  self.clients.claim();
});

// After a deploy the page points at new hashed files: drop the cached old ones.
async function pruneAssets(html) {
  const keep = new Set([...html.matchAll(/\/assets\/[^"'\s>]+/g)].map((m) => m[0]));
  if (!keep.size) return;
  const cache = await caches.open(RUNTIME);
  for (const req of await cache.keys()) {
    const path = new URL(req.url).pathname;
    if (path.startsWith("/assets/") && !keep.has(path)) await cache.delete(req);
  }
}

async function networkFirstPage(request) {
  try {
    const fresh = await fetch(request);
    if (fresh.ok) {
      const cache = await caches.open(SHELL);
      await cache.put("/", fresh.clone()); // every route is the same single-page app
      fresh.clone().text().then(pruneAssets).catch(() => undefined);
    }
    return fresh;
  } catch {
    return (await caches.match("/")) || Response.error();
  }
}

async function cacheFirst(request) {
  const hit = await caches.match(request);
  if (hit) return hit;
  const fresh = await fetch(request);
  if (fresh.ok || fresh.type === "opaque") {
    const cache = await caches.open(RUNTIME);
    cache.put(request, fresh.clone());
  }
  return fresh;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin === self.location.origin && url.pathname.startsWith("/api/")) return;
  if (request.mode === "navigate") {
    event.respondWith(networkFirstPage(request));
    return;
  }
  const fonts = url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com";
  const own = url.origin === self.location.origin;
  if (fonts || own) event.respondWith(cacheFirst(request));
});
