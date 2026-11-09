/**
 * WebSocket client with auto-reconnect for Whatsbot-lite real-time events.
 *
 * On a phone the socket is dropped silently whenever the app goes to the background or the
 * network changes, and the browser may never fire `onclose`. Without a heartbeat the UI would
 * keep showing stale data believing it is still connected. So this client:
 *   - pings the server every PING_INTERVAL_MS and treats a missing pong (or any message) within
 *     PONG_TIMEOUT_MS as a dead connection: it closes the socket and reconnects;
 *   - reconnects right away (no 3 s wait) when the app comes back to the foreground or the
 *     network returns, and also checks a socket that looks open but may be dead.
 * `handlers.onConnect` fires on EVERY successful (re)connect, so callers can refresh whatever they
 * missed while the socket was down.
 *
 * `deps` exists only so the timing/visibility logic can be unit-tested without a browser.
 */

export const PING_INTERVAL_MS = 25000;
export const PONG_TIMEOUT_MS = 10000;
export const RECONNECT_DELAY_MS = 3000;

export function createWebSocket(handlers, deps = {}) {
  const WS = deps.WebSocket || WebSocket;
  const setT = deps.setTimeout || setTimeout;
  const clearT = deps.clearTimeout || clearTimeout;
  const doc = deps.document || (typeof document !== 'undefined' ? document : null);
  const win = deps.window || (typeof window !== 'undefined' ? window : null);
  const loc = deps.location || (typeof location !== 'undefined' ? location : { protocol: 'http:', host: '' });
  const storage = deps.localStorage || (typeof localStorage !== 'undefined' ? localStorage : null);

  let ws = null;
  let reconnectTimer = null;
  let pingTimer = null;
  let pongTimer = null;
  let closed = false;

  function clearHeartbeat() {
    if (pingTimer) { clearT(pingTimer); pingTimer = null; }
    if (pongTimer) { clearT(pongTimer); pongTimer = null; }
  }

  // Anything received proves the connection is alive, so it cancels the pending pong deadline.
  function heardFromServer() {
    if (pongTimer) { clearT(pongTimer); pongTimer = null; }
  }

  function scheduleReconnect(delay = RECONNECT_DELAY_MS) {
    if (closed || reconnectTimer) return;
    reconnectTimer = setT(() => { reconnectTimer = null; connect(); }, delay);
  }

  function sendPing() {
    if (!ws || ws.readyState !== 1) return;
    try {
      ws.send(JSON.stringify({ action: 'ping' }));
    } catch (_) {
      dropSocket();
      return;
    }
    if (!pongTimer) {
      // No reply (not even another event) in time: the socket is dead even if `onclose` never fired.
      pongTimer = setT(() => { pongTimer = null; dropSocket(); }, PONG_TIMEOUT_MS);
    }
  }

  function startHeartbeat() {
    clearHeartbeat();
    const tick = () => {
      sendPing();
      pingTimer = setT(tick, PING_INTERVAL_MS);
    };
    pingTimer = setT(tick, PING_INTERVAL_MS);
  }

  // Close the current socket and reconnect. `onclose` may be delayed or never arrive on a dead
  // socket, so detach it and run the disconnect path ourselves.
  function dropSocket() {
    clearHeartbeat();
    const old = ws;
    ws = null;
    if (old) {
      old.onopen = old.onmessage = old.onclose = old.onerror = null;
      try { old.close(); } catch (_) { /* already gone */ }
      if (handlers.onDisconnect) handlers.onDisconnect();
    }
    scheduleReconnect(0);
  }

  function connect() {
    if (closed) return;

    const protocol = loc.protocol === 'https:' ? 'wss:' : 'ws:';
    const token = (storage && storage.getItem('whatsbot_token')) || '';
    const qs = token ? `?token=${encodeURIComponent(token)}` : '';
    const socket = new WS(`${protocol}//${loc.host}/ws${qs}`);
    ws = socket;

    socket.onopen = () => {
      if (ws !== socket) return;
      startHeartbeat();
      if (handlers.onConnect) handlers.onConnect();
    };

    socket.onmessage = (event) => {
      if (ws !== socket) return;
      heardFromServer();
      try {
        const msg = JSON.parse(event.data);
        const handler = handlers[msg.event];
        if (handler) handler(msg.data);
      } catch (e) {
        console.error('WS parse error:', e);
      }
    };

    socket.onclose = () => {
      if (ws !== socket) return;
      ws = null;
      clearHeartbeat();
      if (handlers.onDisconnect) handlers.onDisconnect();
      scheduleReconnect();
    };

    socket.onerror = () => {
      try { socket.close(); } catch (_) { /* ignore */ }
    };
  }

  // Back to the foreground / network is back: don't wait for the timers. If there is no live socket
  // reconnect now; if it looks open it may have died while frozen in the background, so probe it
  // with a ping (the pong deadline then drops it if it is dead).
  function wake() {
    if (closed) return;
    if (!ws || ws.readyState > 1) {
      if (reconnectTimer) { clearT(reconnectTimer); reconnectTimer = null; }
      ws = null;
      connect();
    } else if (ws.readyState === 1) {
      sendPing();
    }
  }
  const onVisible = () => { if (doc && !doc.hidden) wake(); };
  if (doc) doc.addEventListener('visibilitychange', onVisible);
  if (win) win.addEventListener('online', wake);

  connect();

  return {
    close() {
      closed = true;
      clearHeartbeat();
      if (reconnectTimer) clearT(reconnectTimer);
      if (doc) doc.removeEventListener('visibilitychange', onVisible);
      if (win) win.removeEventListener('online', wake);
      if (ws) {
        const old = ws;
        ws = null;
        old.onopen = old.onmessage = old.onclose = old.onerror = null;
        try { old.close(); } catch (_) { /* ignore */ }
      }
    },
  };
}
