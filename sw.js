const CACHE = 'oltedal-v18';
const ASSETS = [
  '/',
  '/index.html',
  '/profile.html',
  '/manifest.json',
  'https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;800&display=swap'
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll(ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  // Ikke cache vær-API – alltid hent ferske data
  if (e.request.url.includes('api.met.no')) {
    e.respondWith(fetch(e.request).catch(() => new Response('{}', { headers: { 'Content-Type': 'application/json' }})));
    return;
  }

  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
