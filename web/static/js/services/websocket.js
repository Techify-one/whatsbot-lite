/**
 * WebSocket client with auto-reconnect for Whatsbot-lite real-time events.
 */

export function createWebSocket(handlers) {
  let ws = null;
  let reconnectTimer = null;
  let closed = false;

  function connect() {
    if (closed) return;

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const token = localStorage.getItem('whatsbot_token') || '';
    const qs = token ? `?token=${encodeURIComponent(token)}` : '';
    ws = new WebSocket(`${protocol}//${location.host}/ws${qs}`);

    ws.onopen = () => {
      if (handlers.onConnect) handlers.onConnect();
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        const handler = handlers[msg.event];
        if (handler) handler(msg.data);
      } catch (e) {
        console.error('WS parse error:', e);
      }
    };

    ws.onclose = () => {
      if (handlers.onDisconnect) handlers.onDisconnect();
      if (!closed) {
        reconnectTimer = setTimeout(connect, 3000);
      }
    };

    ws.onerror = () => {
      ws.close();
    };
  }

  connect();

  return {
    close() {
      closed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (ws) ws.close();
    }
  };
}
