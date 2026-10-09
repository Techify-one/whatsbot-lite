/* Deterministic service-worker policy tests. Run: node --test tests/pwa-worker.test.cjs */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/sw.js'), 'utf8');
const origin = 'https://whatsbot.example';

function worker(fetchImpl = async () => new Response('generic offline', {headers: {'content-type': 'text/html'}})) {
    const handlers = {};
    const stores = new Map();
    const writes = [];
    const deleted = [];
    const requests = [];
    const caches = {
        async open(name) {
            if (!stores.has(name)) stores.set(name, new Map());
            return {
                async put(key, value) { writes.push(key); stores.get(name).set(key, value); },
                async match(key) { return stores.get(name).get(key)?.clone(); },
            };
        },
        async keys() { return [...stores.keys()]; },
        async delete(name) { deleted.push(name); return stores.delete(name); },
    };
    class WorkerRequest extends Request {
        constructor(url, opts) { super(new URL(url, origin), opts); }
    }
    vm.runInNewContext(source, {
        self: {location: {origin}, addEventListener: (name, handler) => { handlers[name] = handler; }},
        caches, Request: WorkerRequest, Response, URL,
        fetch: async (request) => { requests.push(request); return fetchImpl(request); },
    });
    return {
        stores, writes, deleted, requests,
        async lifecycle(name) { let pending; handlers[name]({waitUntil: (p) => { pending = p; }}); await pending; },
        async request(pathname, {method = 'GET', mode = 'navigate', authorization = false} = {}) {
            let pending;
            const request = {url: new URL(pathname, origin).href, method, mode,
                headers: new Headers(authorization ? {Authorization: 'Bearer secret'} : {})};
            handlers.fetch({request, respondWith: (p) => { pending = p; }});
            return pending ? await pending : undefined;
        },
    };
}

test('install caches only generic offline HTML and fetches without credentials', async () => {
    const sw = worker();
    await sw.lifecycle('install');
    assert.deepEqual(sw.writes, ['/offline.html']);
    assert.equal(sw.requests[0].url, `${origin}/offline.html`);
    assert.equal(sw.requests[0].credentials, 'omit');
    assert.equal(sw.requests[0].redirect, 'error');
    assert.equal(sw.requests[0].cache, 'reload');
});

test('failed/incorrect offline response prevents installation without caching', async () => {
    for (const response of [new Response('login', {status: 401}), new Response('{}', {headers: {'content-type': 'application/json'}})]) {
        const sw = worker(async () => response);
        await assert.rejects(sw.lifecycle('install'), /Offline page unavailable/);
        assert.deepEqual(sw.writes, []);
    }
});

test('activation only cleans this worker own outdated caches', async () => {
    const sw = worker();
    sw.stores.set('whatsbot-pwa-offline-v0', new Map());
    sw.stores.set('other-application', new Map());
    await sw.lifecycle('install');
    await sw.lifecycle('activate');
    assert.deepEqual(sw.deleted, ['whatsbot-pwa-offline-v0']);
    assert.equal(sw.stores.has('other-application'), true);
    assert.equal(sw.stores.has('whatsbot-pwa-offline-v1'), true);
});

test('API/auth/media/assets/non-GET/cross-origin/Authorization bypass interception', async () => {
    const sw = worker(() => { throw new Error('must not fetch'); });
    for (const pathname of ['/api/contacts', '/api/auth/login', '/auth/login', '/statics/media/a.jpg', '/static/js/app.js', '/plugins/example/data', 'https://other.example/']) {
        assert.equal(await sw.request(pathname), undefined, pathname);
    }
    assert.equal(await sw.request('/', {authorization: true}), undefined);
    assert.equal(await sw.request('/chat', {method: 'POST'}), undefined);
    assert.equal(await sw.request('/chat', {mode: 'cors'}), undefined);
    assert.deepEqual(sw.requests, []);
    assert.deepEqual(sw.writes, []);
});

test('navigation stays network-only, including errors, without storing private content', async () => {
    for (const status of [200, 401, 403, 404, 500]) {
        const sw = worker(async () => new Response('private content', {status}));
        const response = await sw.request('/chat/projects/private?secret=value');
        assert.equal(response.status, status);
        assert.equal(await response.text(), 'private content');
        assert.deepEqual(sw.writes, []);
        assert.equal(sw.stores.size, 0);
    }
});

test('network failure returns only cached generic offline page', async () => {
    let online = true;
    const sw = worker(async () => {
        if (!online) throw new TypeError('offline');
        return new Response('generic offline', {headers: {'content-type': 'text/html'}});
    });
    await sw.lifecycle('install');
    online = false;
    assert.equal(await (await sw.request('/chat/projects/private')).text(), 'generic offline');
    assert.deepEqual(sw.writes, ['/offline.html']);
});

test('missing offline cache degrades to generic 503', async () => {
    const sw = worker(async () => { throw new TypeError('offline'); });
    const response = await sw.request('/');
    assert.equal(response.status, 503);
    assert.match(await response.text(), /Sem conexão/);
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.deepEqual(sw.writes, []);
});
