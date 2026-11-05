// Tools management screen — tabela com busca, toggle inline e edição via modal.
// Refresh imediato no backend (sem restart), atualiza via WebSocket.

import { h } from 'preact';
import { useEffect, useMemo, useState } from 'preact/hooks';
import htm from 'htm';
import { authHeaders, handleUnauthorized } from '../services/api.js';

const html = htm.bind(h);


function PencilIcon() {
  return html`
    <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
      <path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a.996.996 0 000-1.41l-2.34-2.34a.996.996 0 00-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/>
    </svg>
  `;
}


function EditModal({ tool, onClose, onSave, busy }) {
  const [description, setDescription] = useState(tool.current_description || '');
  const [label, setLabel] = useState(tool.current_label || '');

  const dirty =
    description.trim() !== (tool.current_description || '').trim() ||
    label.trim() !== (tool.current_label || '').trim();

  function save() {
    const body = {};
    if (description.trim() !== (tool.current_description || '').trim()) {
      if (!description.trim() || description.trim() === (tool.default_description || '').trim()) {
        body.description = null;
      } else {
        body.description = description.trim();
      }
    }
    if (label.trim() !== (tool.current_label || '').trim()) {
      const defaultLabel = (tool.default_label || '').trim();
      if (!label.trim() || label.trim() === defaultLabel) {
        body.display_label = null;
      } else {
        body.display_label = label.trim();
      }
    }
    onSave(tool.name, body);
  }

  function reset() {
    onSave(tool.name, { description: null, display_label: null });
  }

  const anyOverride = tool.has_override || tool.has_label_override;

  return html`
    <div class="fixed inset-0 bg-black/40 z-50 flex items-center justify-center" onClick=${onClose}>
      <div class="bg-wa-bg rounded-lg shadow-xl max-w-lg w-full mx-4 max-h-[85vh] overflow-y-auto"
           onClick=${(e) => e.stopPropagation()}>
        <div class="border-b border-wa-border px-4 py-3 flex items-center justify-between">
          <div>
            <div class="font-medium">Editar tool</div>
            <code class="text-[12px] text-wa-secondary">${tool.name}</code>
          </div>
          <button class="text-wa-secondary hover:text-wa-text text-xl leading-none" onClick=${onClose}>×</button>
        </div>
        <div class="p-4 space-y-4">
          <div>
            <label class="text-[12px] text-wa-secondary block mb-1">
              Rótulo (visível só na UI)
              ${tool.has_label_override ? html`<span class="text-[11px] text-blue-700"> · sobrescrito</span>` : null}
            </label>
            <input
              type="text"
              value=${label}
              onInput=${(e) => setLabel(e.target.value)}
              placeholder=${tool.default_label || tool.name}
              class="w-full wa-field text-[13px] border border-wa-border rounded px-2 py-1.5 focus:outline-none focus:border-wa-teal"
            />
            ${tool.has_label_override && tool.default_label ? html`
              <div class="text-[11px] text-wa-secondary mt-1">
                Padrão: <span class="italic">${tool.default_label}</span>
              </div>
            ` : null}
          </div>
          <div>
            <label class="text-[12px] text-wa-secondary block mb-1">
              Descrição enviada ao LLM
              ${tool.has_override ? html`<span class="text-[11px] text-blue-700"> · sobrescrita</span>` : null}
            </label>
            <textarea
              rows="6"
              value=${description}
              onInput=${(e) => setDescription(e.target.value)}
              placeholder=${tool.default_description}
              class="w-full wa-field text-[13px] border border-wa-border rounded px-2 py-1.5 focus:outline-none focus:border-wa-teal resize-y"
            />
            ${tool.has_override ? html`
              <div class="text-[11px] text-wa-secondary mt-1">
                Padrão: <span class="italic">${tool.default_description}</span>
              </div>
            ` : null}
          </div>
        </div>
        <div class="border-t border-wa-border px-4 py-3 flex items-center justify-between">
          <div>
            ${anyOverride ? html`
              <button
                onClick=${reset}
                disabled=${busy}
                class="px-3 py-1 text-[13px] rounded bg-wa-panel border border-wa-border disabled:opacity-50"
              >Restaurar padrão</button>
            ` : null}
          </div>
          <div class="flex gap-2">
            <button
              onClick=${onClose}
              class="px-3 py-1 text-[13px] rounded bg-wa-panel border border-wa-border"
            >Cancelar</button>
            <button
              onClick=${save}
              disabled=${!dirty || busy}
              class="px-3 py-1 text-[13px] rounded bg-wa-teal text-white disabled:opacity-50"
            >${busy ? 'Salvando…' : 'Salvar'}</button>
          </div>
        </div>
      </div>
    </div>
  `;
}


export function ToolsManager() {
  const [tools, setTools] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null); // tool name being saved
  const [editing, setEditing] = useState(null); // tool name being edited
  const [query, setQuery] = useState('');

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch('/api/tools', { headers: authHeaders() });
      if (r.status === 401) { handleUnauthorized(); throw new Error('Não autenticado.'); }
      const data = await r.json();
      if (!data.ok) throw new Error(data.error || 'failed');
      setTools(data.data.tools || []);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  useEffect(() => {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.onmessage = (msg) => {
      try {
        const ev = JSON.parse(msg.data);
        if (ev.event === 'tools_changed') load();
      } catch {}
    };
    return () => ws.close();
  }, []);

  async function save(name, body) {
    setBusy(name);
    try {
      const r = await fetch(`/api/tools/${encodeURIComponent(name)}`, {
        method: 'PUT',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body),
      });
      if (r.status === 401) { handleUnauthorized(); throw new Error('Não autenticado.'); }
      const data = await r.json();
      if (!data.ok) throw new Error(data.error || 'failed');
      setTools((prev) => prev.map((t) => (t.name === name ? { ...t, ...data.data } : t)));
      // Close modal only when the save came from the modal (description/label)
      if ('description' in body || 'display_label' in body) setEditing(null);
    } catch (e) {
      alert('Erro ao salvar: ' + (e.message || e));
    } finally {
      setBusy(null);
    }
  }

  function toggle(tool) {
    save(tool.name, { enabled: !tool.enabled });
  }

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return tools;
    return tools.filter((t) => {
      const label = (t.current_label || '').toLowerCase();
      const name = t.name.toLowerCase();
      const plugin = (t.plugin_id || 'core').toLowerCase();
      return label.includes(q) || name.includes(q) || plugin.includes(q);
    });
  }, [tools, query]);

  const editingTool = editing ? tools.find((t) => t.name === editing) : null;

  return html`
    <div>
      <div class="text-sm text-wa-secondary mb-4">
        Tools são as ações que a IA pode executar (salvar contato, transferir pra humano, plugins…).
        Aqui você liga/desliga cada uma e ajusta a descrição que vai pro modelo decidir quando usá-la.
      </div>

      <div class="mb-3">
        <input
          type="search"
          value=${query}
          onInput=${(e) => setQuery(e.target.value)}
          placeholder="Buscar por nome, slug ou plugin…"
          class="w-full md:w-80 wa-field text-[13px] border border-wa-border rounded px-3 py-2 focus:outline-none focus:border-wa-teal"
        />
      </div>

      ${error && html`<div class="text-red-600 mb-3 text-sm">${error}</div>`}

      ${loading
        ? html`<div class="text-wa-secondary">Carregando…</div>`
        : tools.length === 0
          ? html`<div class="text-wa-secondary">Nenhuma tool registrada.</div>`
          : html`
            <div class="bg-wa-bg border border-wa-border rounded-lg overflow-x-auto">
              <table class="tools-table w-full text-[13px]">
                <thead class="bg-wa-panel border-b border-wa-border text-wa-secondary text-[12px] uppercase tracking-wide">
                  <tr>
                    <th class="text-left px-3 py-2 font-medium">Nome</th>
                    <th class="text-left px-3 py-2 font-medium">Slug</th>
                    <th class="text-left px-3 py-2 font-medium">Plugin</th>
                    <th class="text-center px-3 py-2 font-medium w-20">Ativa</th>
                    <th class="text-center px-3 py-2 font-medium w-12"></th>
                  </tr>
                </thead>
                <tbody>
                  ${filtered.length === 0
                    ? html`<tr><td colspan="5" class="px-3 py-6 text-center text-wa-secondary">Nenhuma tool encontrada.</td></tr>`
                    : filtered.map((t) => html`
                      <tr key=${t.name} class="border-b border-wa-border last:border-b-0 hover:bg-wa-hover">
                        <td class="px-3 py-2">
                          <div class="font-medium">${t.current_label || t.name}</div>
                          ${t.has_override || t.has_label_override ? html`<div class="text-[11px] text-blue-700">customizada</div>` : null}
                        </td>
                        <td class="px-3 py-2">
                          <code class="text-[12px] bg-wa-panel px-1.5 py-0.5 rounded">${t.name}</code>
                        </td>
                        <td class="px-3 py-2 text-wa-secondary">
                          ${t.plugin_id ? html`<code class="text-[12px]">${t.plugin_id}</code>` : html`<span class="text-[12px]">core</span>`}
                        </td>
                        <td class="px-3 py-2 text-center">
                          <label class="inline-flex items-center cursor-pointer align-middle">
                            <input
                              type="checkbox"
                              class="sr-only peer"
                              checked=${t.enabled}
                              disabled=${busy === t.name}
                              onChange=${() => toggle(t)}
                            />
                            <div class="relative w-9 h-5 bg-wa-border rounded-full peer peer-checked:bg-green-600 transition-colors after:content-[''] after:absolute after:top-0.5 after:left-0.5 after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-transform peer-checked:after:translate-x-4"></div>
                          </label>
                        </td>
                        <td class="px-3 py-2 text-center">
                          <button
                            onClick=${() => setEditing(t.name)}
                            class="text-wa-secondary hover:text-wa-teal p-1 rounded hover:bg-wa-panel"
                            title="Editar descrição e rótulo"
                          ><${PencilIcon} /></button>
                        </td>
                      </tr>
                    `)
                  }
                </tbody>
              </table>
            </div>
          `
      }

      ${editingTool ? html`
        <${EditModal}
          tool=${editingTool}
          onClose=${() => setEditing(null)}
          onSave=${save}
          busy=${busy === editingTool.name}
        />
      ` : null}
    </div>
  `;
}
