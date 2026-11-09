/* WebSocket client: heartbeat, silent-death detection, reconnect on wake. Run: node --test tests/websocket.test.cjs */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL} = require('node:url');

const load = () => import(pathToFileURL(path.join(__dirname, '../web/static/js/services/websocket.js')).href);

// Fake clock: timers run only when advance() moves time past them.
function fakeClock() {
    let now = 0, seq = 0;
    const timers = new Map();
    return {
        setTimeout: (fn, ms) => { const id = ++seq; timers.set(id, {at: now + ms, fn}); return id; },
        clearTimeout: (id) => timers.delete(id),
        advance(ms) {
            const end = now + ms;
            for (;;) {
                let next = null;
                for (const [id, t] of timers) if (t.at <= end && (!next || t.at < next.t.at)) next = {id, t};
                if (!next) break;
                timers.delete(next.id);
                now = next.t.at;
                next.t.fn();
            }
            now = end;
        },
        pending: () => timers.size,
    };
}

function harness() {
    const clock = fakeClock();
    const sockets = [];
    class FakeWS {
        constructor(url) { this.url = url; this.readyState = 0; this.sent = []; this.closeCalls = 0; sockets.push(this); }
        send(d) { if (this.readyState !== 1) throw new Error('not open'); this.sent.push(JSON.parse(d)); }
        close() { this.closeCalls++; this.readyState = 3; }
        open() { this.readyState = 1; this.onopen && this.onopen(); }
        recv(msg) { this.onmessage && this.onmessage({data: JSON.stringify(msg)}); }
        drop() { this.readyState = 3; this.onclose && this.onclose(); }
    }
    const listeners = {doc: {}, win: {}};
    const doc = {hidden: false,
        addEventListener: (n, f) => { listeners.doc[n] = f; }, removeEventListener: (n) => { delete listeners.doc[n]; }};
    const win = {
        addEventListener: (n, f) => { listeners.win[n] = f; }, removeEventListener: (n) => { delete listeners.win[n]; }};
    const events = [];
    const handlers = {
        onConnect: () => events.push('connect'), onDisconnect: () => events.push('disconnect'),
        status: (d) => events.push(['status', d]),
    };
    const deps = {WebSocket: FakeWS, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, document: doc, window: win,
        location: {protocol: 'https:', host: 'x.test'}, localStorage: {getItem: () => 'tok'}};
    return {clock, sockets, listeners, doc, events, handlers, deps};
}

test('connects with the token and fires onConnect on every (re)connect', async () => {
    const {createWebSocket} = await load();
    const h = harness();
    createWebSocket(h.handlers, h.deps);
    assert.equal(h.sockets[0].url, 'wss://x.test/ws?token=tok');
    h.sockets[0].open();
    assert.deepEqual(h.events, ['connect']);
    h.sockets[0].drop();
    h.clock.advance(3000);
    h.sockets[1].open();
    assert.deepEqual(h.events, ['connect', 'disconnect', 'connect']);
});

test('sends a ping every 25 s while connected', async () => {
    const {createWebSocket, PING_INTERVAL_MS} = await load();
    const h = harness();
    createWebSocket(h.handlers, h.deps);
    const s = h.sockets[0]; s.open();
    h.clock.advance(PING_INTERVAL_MS);
    assert.deepEqual(s.sent, [{action: 'ping'}]);
    s.recv({event: 'pong', data: {}});
    h.clock.advance(PING_INTERVAL_MS);
    assert.equal(s.sent.length, 2);
});

test('a missing pong drops the socket and reconnects (connection died silently)', async () => {
    const {createWebSocket, PING_INTERVAL_MS, PONG_TIMEOUT_MS} = await load();
    const h = harness();
    createWebSocket(h.handlers, h.deps);
    const s = h.sockets[0]; s.open();
    h.clock.advance(PING_INTERVAL_MS);            // ping out, server never answers
    h.clock.advance(PONG_TIMEOUT_MS);
    assert.equal(s.closeCalls, 1);
    assert.ok(h.events.includes('disconnect'));
    assert.equal(h.sockets.length, 2);            // reconnect attempted (0 ms delay)
    h.sockets[1].open();
    assert.equal(h.events.at(-1), 'connect');
});

test('any message from the server counts as proof of life (no false drop)', async () => {
    const {createWebSocket, PING_INTERVAL_MS, PONG_TIMEOUT_MS} = await load();
    const h = harness();
    createWebSocket(h.handlers, h.deps);
    const s = h.sockets[0]; s.open();
    h.clock.advance(PING_INTERVAL_MS);
    s.recv({event: 'status', data: {connected: true}});   // not a pong, still alive
    h.clock.advance(PONG_TIMEOUT_MS);
    assert.equal(s.closeCalls, 0);
    assert.equal(h.sockets.length, 1);
});

test('returning to the foreground reconnects immediately when there is no socket', async () => {
    const {createWebSocket} = await load();
    const h = harness();
    createWebSocket(h.handlers, h.deps);
    h.sockets[0].open();
    h.sockets[0].drop();                          // reconnect timer (3 s) is pending
    assert.equal(h.sockets.length, 1);
    h.listeners.doc.visibilitychange();           // app comes back to the front
    assert.equal(h.sockets.length, 2);            // no 3 s wait
});

test('returning to the foreground probes a socket that looks open', async () => {
    const {createWebSocket} = await load();
    const h = harness();
    createWebSocket(h.handlers, h.deps);
    const s = h.sockets[0]; s.open();
    h.listeners.doc.visibilitychange();
    assert.deepEqual(s.sent, [{action: 'ping'}]);
});

test('hidden app / background does not reconnect', async () => {
    const {createWebSocket} = await load();
    const h = harness();
    createWebSocket(h.handlers, h.deps);
    h.sockets[0].open(); h.sockets[0].drop();
    h.doc.hidden = true;
    h.listeners.doc.visibilitychange();
    assert.equal(h.sockets.length, 1);
});

test('network coming back (online) reconnects', async () => {
    const {createWebSocket} = await load();
    const h = harness();
    createWebSocket(h.handlers, h.deps);
    h.sockets[0].open(); h.sockets[0].drop();
    h.listeners.win.online();
    assert.equal(h.sockets.length, 2);
});

test('only one reconnect is queued however many times it drops', async () => {
    const {createWebSocket} = await load();
    const h = harness();
    createWebSocket(h.handlers, h.deps);
    h.sockets[0].open(); h.sockets[0].drop();
    h.clock.advance(3000);
    assert.equal(h.sockets.length, 2);
    h.sockets[1].drop();                          // never opened
    h.clock.advance(3000);
    assert.equal(h.sockets.length, 3);
});

test('a late event from a replaced socket is ignored', async () => {
    const {createWebSocket} = await load();
    const h = harness();
    createWebSocket(h.handlers, h.deps);
    const old = h.sockets[0]; old.open();
    h.listeners.doc.visibilitychange();
    old.drop();
    h.clock.advance(3000);
    const fresh = h.sockets.at(-1); fresh.open();
    old.recv({event: 'status', data: {stale: true}});
    assert.equal(h.events.some(e => Array.isArray(e) && e[1].stale), false);
});

test('close() stops everything: no reconnect, no listeners, no timers', async () => {
    const {createWebSocket} = await load();
    const h = harness();
    const ctl = createWebSocket(h.handlers, h.deps);
    const s = h.sockets[0]; s.open();
    ctl.close();
    assert.equal(s.closeCalls, 1);
    assert.equal(Object.keys(h.listeners.doc).length + Object.keys(h.listeners.win).length, 0);
    h.clock.advance(120000);
    assert.equal(h.sockets.length, 1);
    assert.equal(h.clock.pending(), 0);
});
