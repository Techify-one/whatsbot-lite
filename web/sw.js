/* Deliberately not an offline app: only a generic, public offline page is stored.
 * Bump the cache version whenever offline.html changes. Never cache index.html,
 * API/auth responses, chats, media, plugins, or requests carrying credentials.
 */
const CACHE_PREFIX = 'whatsbot-pwa-offline-';
const CACHE_NAME = `${CACHE_PREFIX}v1`;
const OFFLINE_URL = '/offline.html';

self.addEventListener('install', (event) => {
    event.waitUntil((async () => {
        const response = await fetch(new Request(OFFLINE_URL, {
            cache: 'reload', credentials: 'omit', redirect: 'error',
        }));
        if (!response.ok || !response.headers.get('content-type')?.includes('text/html')) {
            throw new Error('Offline page unavailable');
        }
        const cache = await caches.open(CACHE_NAME);
        await cache.put(OFFLINE_URL, response);
    })());
    // Do not skipWaiting: an update must never interrupt an active conversation.
});

self.addEventListener('activate', (event) => {
    event.waitUntil((async () => {
        const names = await caches.keys();
        await Promise.all(names
            .filter((name) => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME)
            .map((name) => caches.delete(name)));
    })());
    // No clients.claim(), forced reload, or takeover of an already-open page.
});

self.addEventListener('fetch', (event) => {
    const request = event.request;
    const url = new URL(request.url);
    if (request.method !== 'GET' || request.mode !== 'navigate'
        || url.origin !== self.location.origin || request.headers.has('authorization')
        || /^\/(?:api|auth|static|statics|plugins)(?:\/|$)/.test(url.pathname)) {
        return;
    }
    event.respondWith((async () => {
        try {
            // HTTP failures (including 401/403/404/500) stay untouched. No cache write.
            return await fetch(request);
        } catch (error) {
            const cache = await caches.open(CACHE_NAME);
            const fallback = await cache.match(OFFLINE_URL);
            if (fallback) return fallback;
            return new Response('Sem conexão. Reconecte-se e tente novamente.', {
                status: 503,
                headers: {'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store'},
            });
        }
    })());
});
