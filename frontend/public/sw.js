/*
 * App-shell service worker. It only ever answers same-origin GETs for the page itself
 * and its hashed build assets; API calls, the chat stream and the identity provider
 * always go straight to the network, so nothing user-specific is cached here.
 */
const CACHE = 'kb-shell-v1';
const SHELL = '/';

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.add(new Request(SHELL, { cache: 'reload' })))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

// Network first, so a new deployment is picked up on the next launch; the cached shell
// is only the offline fallback. Every route, /callback included, is the same document.
async function shell(request) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    if (response.ok) await cache.put(SHELL, response.clone());
    return response;
  } catch (cause) {
    const cached = await cache.match(SHELL);
    if (cached !== undefined) return cached;
    throw cause;
  }
}

// Build assets carry a content hash in their name, so a cached copy never goes stale.
async function asset(request) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);
  if (cached !== undefined) return cached;
  const response = await fetch(request);
  if (response.ok) await cache.put(request, response.clone());
  return response;
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // The local deployment serves the API from this origin.
  if (url.pathname.startsWith('/v1/') || url.pathname === '/healthz') return;
  if (request.mode === 'navigate') {
    event.respondWith(shell(request));
  } else if (url.pathname.startsWith('/assets/')) {
    event.respondWith(asset(request));
  }
});
