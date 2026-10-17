// Minimal extension-slot registry for plugins (plugin API 1.3).
//
// A plugin that declares `frontend_extends` in its manifest ships an ES module
// whose default export is called ONCE at boot with `{ pluginId, apiBase, addSlot }`.
// It can then fill a named UI slot; the core renders the slot with <Slot name=...>.
//
// Slots today:
//   - `chat.header.actions` — the conversation header bar (ContactDetail). The
//     component receives `{ phone, contact, info, isGroup, rawName }` as props.
//
// An empty slot renders nothing, so an install without plugins is unchanged.
// A plugin whose module fails to load, or whose component throws while
// rendering, is dropped silently — it must never take the chat down with it.

import { h } from 'preact';
import { useEffect, useState, useErrorBoundary } from 'preact/hooks';
import htm from 'htm';

const html = htm.bind(h);

const _slots = new Map();      // slot name -> [{ pluginId, component }]
const _listeners = new Set();
const _loaded = new Set();     // plugin ids whose frontend_extends already ran

function notify() {
  _listeners.forEach((fn) => { try { fn(); } catch (_) { /* a listener never breaks the registry */ } });
}

export function addSlot(name, component, pluginId = 'core') {
  if (typeof component !== 'function') return;
  const list = _slots.get(name) || [];
  list.push({ pluginId, component });
  _slots.set(name, list);
  notify();
}

export function getSlot(name) {
  return _slots.get(name) || [];
}

function subscribe(fn) {
  _listeners.add(fn);
  return () => _listeners.delete(fn);
}

// Wrapper with its own error boundary: one broken plugin component renders nothing
// instead of unmounting the whole conversation.
function SlotEntry({ component, ctx }) {
  const [error] = useErrorBoundary();
  if (error) return null;
  return h(component, ctx);
}

export function Slot({ name, ctx = {}, class: cls = 'flex items-center gap-1 shrink-0', style = undefined }) {
  // Plugin modules load asynchronously after boot — re-render when one registers.
  const [, force] = useState(0);
  useEffect(() => subscribe(() => force((n) => n + 1)), []);

  const entries = getSlot(name);
  if (!entries.length) return null;
  return html`
    <div class=${cls} style=${style}>
      ${entries.map((e, i) => html`<${SlotEntry} key=${`${e.pluginId}:${i}`} component=${e.component} ctx=${ctx} />`)}
    </div>
  `;
}

// Imports each loaded plugin's `frontend_extends` module (once per page load).
export function loadPluginExtensions(plugins) {
  (plugins || []).forEach((p) => {
    const url = p && p.frontend_extends;
    if (!url || _loaded.has(p.id)) return;
    _loaded.add(p.id);
    import(url)
      .then((mod) => {
        const register = mod && (mod.default || mod.register);
        if (typeof register !== 'function') throw new Error('frontend_extends must export a default function');
        register({
          pluginId: p.id,
          apiBase: `/api/plugins/${p.id}`,
          addSlot: (name, component) => addSlot(name, component, p.id),
        });
      })
      .catch((e) => console.warn(`[plugins] frontend_extends of "${p.id}" failed:`, e));
  });
}
