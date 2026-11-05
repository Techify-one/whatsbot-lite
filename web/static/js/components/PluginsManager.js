// Plugins management screen — list cards, toggle, settings drawer,
// import (zip upload), export and delete. The Toggle/Delete actions
// trigger a server-side restart; we show a brief "reiniciando…" overlay.

import { h } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';
import htm from 'htm';
import { PluginSettingsForm } from './PluginSettingsForm.js';
import { PluginScreen } from './PluginScreen.js';
import { authHeaders, handleUnauthorized } from '../services/api.js';

const html = htm.bind(h);


function StatusBadge({ plugin }) {
  if (plugin.error) {
    return html`<span class="plug-badge plug-badge--err">Manifest inválido</span>`;
  }
  if (plugin.load_error) {
    return html`<span class="plug-badge plug-badge--err" title=${plugin.load_error}>Erro ao carregar</span>`;
  }
  if (plugin.enabled && plugin.loaded) {
    return html`<span class="plug-badge plug-badge--ok">Ativo</span>`;
  }
  if (plugin.enabled && !plugin.loaded) {
    return html`<span class="plug-badge plug-badge--warn">Aguardando restart</span>`;
  }
  return html`<span class="plug-badge">Desativado</span>`;
}


function RestartBanner() {
  return html`
    <div class="fixed inset-0 bg-black/40 z-[60] flex items-center justify-center">
      <div class="bg-wa-bg rounded-lg shadow-xl p-6 max-w-sm">
        <div class="flex items-center gap-3">
          <div class="w-6 h-6 border-2 border-wa-teal border-t-transparent rounded-full animate-spin"></div>
          <div>
            <div class="font-medium">Reiniciando o servidor…</div>
            <div class="text-[12px] text-wa-secondary mt-1">A página será recarregada em alguns segundos.</div>
          </div>
        </div>
      </div>
    </div>
  `;
}


export function PluginsManager({ onPluginsChanged }) {
  const [plugins, setPlugins] = useState([]);
  const [apiVersion, setApiVersion] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [settingsOpen, setSettingsOpen] = useState(null); // plugin id
  const [importing, setImporting] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [exporting, setExporting] = useState({}); // { [pluginId]: pct (0-100) }
  const [updating, setUpdating] = useState(null); // pid sendo atualizado
  const [menuOpen, setMenuOpen] = useState(null); // pid com o menu "⋯" aberto
  const fileRef = useRef(null);
  const updateFileRef = useRef(null);
  const pendingUpdatePid = useRef(null); // pid alvo do <input> de atualização

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch('/api/plugins', { headers: authHeaders() });
      if (r.status === 401) { handleUnauthorized(); throw new Error('Não autenticado.'); }
      const data = await r.json();
      if (!data.ok) throw new Error(data.error || 'failed');
      setPlugins(data.data.plugins || []);
      setApiVersion(data.data.whatsbot_api_version || '');
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  // Close the "⋯" menu on any outside tap / Escape.
  useEffect(() => {
    if (!menuOpen) return undefined;
    const onDown = e => { if (!e.target.closest || !e.target.closest('.plug-menu-wrap')) setMenuOpen(null); };
    const onKey = e => { if (e.key === 'Escape') setMenuOpen(null); };
    document.addEventListener('pointerdown', onDown);
    document.addEventListener('keydown', onKey);
    return () => { document.removeEventListener('pointerdown', onDown); document.removeEventListener('keydown', onKey); };
  }, [menuOpen]);

  // After we trigger a restart, poll /health until it responds again, then reload.
  useEffect(() => {
    if (!restarting) return;
    let cancelled = false;
    const startedAt = Date.now();
    async function poll() {
      while (!cancelled && Date.now() - startedAt < 60_000) {
        try {
          await new Promise(r => setTimeout(r, 1500));
          const r = await fetch('/health', { cache: 'no-store' });
          if (r.ok) { window.location.reload(); return; }
        } catch (_) { /* still down */ }
      }
      if (!cancelled) setRestarting(false);
    }
    poll();
    return () => { cancelled = true; };
  }, [restarting]);

  async function toggle(pid, enable) {
    const action = enable ? 'enable' : 'disable';
    try {
      const r = await fetch(`/api/plugins/${pid}/${action}`, { method: 'POST', headers: authHeaders() });
      if (r.status === 401) { handleUnauthorized(); throw new Error('Não autenticado.'); }
      const data = await r.json();
      if (!data.ok) throw new Error(data.error || 'falha');
      setRestarting(true);
    } catch (e) {
      alert(`Erro ao ${enable ? 'ativar' : 'desativar'}: ${e.message || e}`);
    }
  }

  async function deletePlugin(pid) {
    if (!confirm(`Remover plugin '${pid}'? A pasta e as tabelas dele serão apagadas.`)) return;
    try {
      const r = await fetch(`/api/plugins/${pid}`, { method: 'DELETE', headers: authHeaders() });
      if (r.status === 401) { handleUnauthorized(); throw new Error('Não autenticado.'); }
      const data = await r.json();
      if (!data.ok) throw new Error(data.error || 'falha');
      setRestarting(true);
    } catch (e) {
      alert(`Erro ao deletar: ${e.message || e}`);
    }
  }

  function clearExporting(pid) {
    setExporting(s => { const n = { ...s }; delete n[pid]; return n; });
  }

  async function exportPlugin(pid) {
    if (exporting[pid] != null) return; // already running
    setExporting(s => ({ ...s, [pid]: 0 }));
    try {
      const r = await fetch(`/api/plugins/${pid}/export`, { headers: authHeaders() });
      if (r.status === 401) { handleUnauthorized(); throw new Error('Não autenticado.'); }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);

      // Stream the body so we can report real download progress. Falls back to
      // a plain blob if the browser can't expose the stream or size.
      const total = Number(r.headers.get('content-length')) || 0;
      let blob;
      if (r.body && total > 0) {
        const reader = r.body.getReader();
        const chunks = [];
        let received = 0;
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          chunks.push(value);
          received += value.length;
          setExporting(s => ({ ...s, [pid]: Math.min(99, Math.round((received / total) * 100)) }));
        }
        blob = new Blob(chunks, { type: 'application/zip' });
      } else {
        blob = await r.blob();
      }
      setExporting(s => ({ ...s, [pid]: 100 }));

      const cd = r.headers.get('content-disposition') || '';
      const m = cd.match(/filename="?([^";]+)"?/i);
      const filename = (m && m[1]) || `${pid}.zip`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);

      // Hold the full bar briefly so it's visible even for tiny plugins.
      setTimeout(() => clearExporting(pid), 800);
    } catch (e) {
      clearExporting(pid);
      alert(`Erro ao exportar: ${e.message || e}`);
    }
  }

  async function importPlugin(file) {
    if (!file) return;
    setImporting(true);
    try {
      const fd = new FormData();
      fd.append('file', file);
      const r = await fetch('/api/plugins/import', { method: 'POST', body: fd, headers: authHeaders() });
      if (r.status === 401) { handleUnauthorized(); throw new Error('Não autenticado.'); }
      const data = await r.json();
      if (!data.ok) throw new Error(data.error || 'falha');
      await load();
      alert(`Plugin '${data.data.id}' importado. Ative-o quando estiver pronto.`);
    } catch (e) {
      alert(`Erro ao importar: ${e.message || e}`);
    } finally {
      setImporting(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  }

  // Atualizar: troca o código do plugin por um novo .zip SEM apagar tabelas/dados
  // (diferente de Deletar + Importar). O backend preserva o banco e roda só as
  // migrations novas no restart.
  function pickUpdate(pid) {
    if (!confirm(
      `Atualizar o plugin '${pid}' com um novo .zip?\n\n` +
      `Os dados e tabelas dele são PRESERVADOS — apenas o código é trocado e ` +
      `as migrations novas rodam no reinício.`
    )) return;
    pendingUpdatePid.current = pid;
    if (updateFileRef.current) updateFileRef.current.click();
  }

  async function updatePlugin(pid, file) {
    if (!file || !pid) return;
    setUpdating(pid);
    try {
      const fd = new FormData();
      fd.append('file', file);
      const r = await fetch(`/api/plugins/${pid}/update`, { method: 'POST', body: fd, headers: authHeaders() });
      if (r.status === 401) { handleUnauthorized(); throw new Error('Não autenticado.'); }
      const data = await r.json();
      if (!data.ok) throw new Error(data.error || 'falha');
      if (data.data && data.data.warning) alert(`Atenção — ${data.data.warning}`);
      // Limpa o estado por-card: o RestartBanner (overlay) assume a UI a partir
      // daqui, e se o poll de restart estourar o timeout o botão não fica preso
      // em "Atualizando…".
      setUpdating(null);
      setRestarting(true);
    } catch (e) {
      alert(`Erro ao atualizar: ${e.message || e}`);
      setUpdating(null);
    } finally {
      pendingUpdatePid.current = null;
      if (updateFileRef.current) updateFileRef.current.value = '';
    }
  }

  if (loading) return html`<div class="text-wa-secondary">Carregando plugins…</div>`;
  if (error) return html`<div class="text-red-600">Erro: ${error}</div>`;

  // A plugin may ship a custom config UI as a screen flagged `config: true`
  // (rendered in the modal below instead of the auto-generated settings form).
  const cfgPlugin = settingsOpen ? plugins.find(p => p.id === settingsOpen) : null;
  const cfgScreen = cfgPlugin ? (cfgPlugin.screens || []).find(s => s.config) : null;

  return html`
    <div>
      ${restarting ? html`<${RestartBanner} />` : null}

      <div class="plug-toolbar">
        <div class="plug-count">
          <span class="plug-count-n">${plugins.length}</span>
          plugin${plugins.length === 1 ? '' : 's'}
          <span class="plug-api">API do core ${apiVersion}</span>
        </div>
        <div class="plug-toolbar-actions">
          <input type="file" ref=${fileRef} accept=".zip" class="hidden"
            onChange=${e => importPlugin(e.target.files && e.target.files[0])} />
          <input type="file" ref=${updateFileRef} accept=".zip" class="hidden"
            onChange=${e => updatePlugin(pendingUpdatePid.current, e.target.files && e.target.files[0])} />
          <a href="https://techify.one/whatsbot/plugins" target="_blank" rel="noopener noreferrer"
             class="plug-btn plug-btn--outline">Loja de Plugins</a>
          <button disabled=${importing} onClick=${() => fileRef.current && fileRef.current.click()}
            class="plug-btn plug-btn--primary">${importing ? 'Importando…' : 'Importar (.zip)'}</button>
        </div>
      </div>

      ${plugins.length === 0
        ? html`
          <div class="plug-empty">
            <div class="font-medium mb-1">Nenhum plugin instalado</div>
            <div class="text-sm text-wa-secondary">
              Coloque um plugin em <code>storages/plugins/&lt;id&gt;/</code> ou importe um arquivo .zip.
            </div>
          </div>`
        : html`
          <div class="plug-grid">
            ${plugins.map(p => html`
              <div key=${p.id} class="plug-card">
                <div class="plug-card-head">
                  <div class="plug-title">
                    <div class="plug-name">${p.name || p.id}</div>
                    <div class="plug-meta"><code>${p.id}</code>${p.version ? html` · v${p.version}` : null}${p.author ? html` · ${p.author}` : null}</div>
                  </div>
                  <${StatusBadge} plugin=${p} />
                </div>
                ${p.description ? html`<p class="plug-desc">${p.description}</p>` : null}
                ${(p.dependencies && p.dependencies.length) ? html`
                  <div class="plug-note">📦 Instala ao ativar: ${p.dependencies.join(', ')}</div>` : null}
                ${p.load_error ? html`<div class="plug-error">${p.load_error}</div>` : null}
                <div class="plug-actions">
                  <label class="plug-switch" title=${p.enabled ? 'Desativar plugin' : 'Ativar plugin'}>
                    <input type="checkbox" checked=${!!p.enabled} onChange=${() => toggle(p.id, !p.enabled)} />
                    <span class="plug-switch-track" aria-hidden="true"></span>
                    <span class="plug-switch-label">${p.enabled ? 'Ativado' : 'Desativado'}</span>
                  </label>
                  <button onClick=${() => setSettingsOpen(p.id)} disabled=${!p.loaded}
                    class="plug-btn plug-btn--outline plug-btn--sm">Configurar</button>
                  <div class="plug-menu-wrap">
                    <button type="button" class="plug-btn plug-btn--ghost plug-btn--icon" aria-label="Mais ações"
                      aria-haspopup="menu" aria-expanded=${menuOpen === p.id}
                      onClick=${() => setMenuOpen(menuOpen === p.id ? null : p.id)}>⋯</button>
                    ${menuOpen === p.id ? html`
                      <div class="plug-menu" role="menu">
                        <button role="menuitem" disabled=${exporting[p.id] != null}
                          onClick=${() => { setMenuOpen(null); exportPlugin(p.id); }}>${exporting[p.id] != null ? 'Exportando…' : 'Exportar'}</button>
                        <button role="menuitem" disabled=${updating === p.id}
                          title="Enviar um novo .zip preservando os dados/tabelas do plugin"
                          onClick=${() => { setMenuOpen(null); pickUpdate(p.id); }}>${updating === p.id ? 'Atualizando…' : 'Atualizar'}</button>
                        <button role="menuitem" class="is-danger"
                          onClick=${() => { setMenuOpen(null); deletePlugin(p.id); }}>Deletar</button>
                      </div>` : null}
                  </div>
                </div>
                ${exporting[p.id] != null ? html`
                  <div class="plug-progress">
                    <div class="plug-progress-row"><span>Exportando…</span><span>${exporting[p.id]}%</span></div>
                    <div class="plug-progress-track"><div class="plug-progress-bar" style=${`width:${exporting[p.id]}%`}></div></div>
                  </div>` : null}
              </div>
            `)}
          </div>`
      }

      ${settingsOpen ? html`
        <div class="fixed inset-0 bg-black/40 z-50 flex items-center justify-center">
          <div class="bg-wa-bg rounded-lg shadow-xl ${cfgScreen ? 'max-w-2xl' : 'max-w-lg'} w-full mx-4 max-h-[85dvh] overflow-y-auto">
            <div class="border-b border-wa-border px-4 py-3 flex items-center justify-between">
              <div class="font-medium">Configurações — ${(cfgPlugin && cfgPlugin.name) || settingsOpen}</div>
              <button class="text-wa-secondary hover:text-wa-text"
                      onClick=${() => setSettingsOpen(null)}>×</button>
            </div>
            <div class="p-4">
              ${cfgScreen
                ? html`<${PluginScreen} screen=${{ ...cfgScreen, pluginId: cfgPlugin.id }} />`
                : html`<${PluginSettingsForm}
                    pluginId=${settingsOpen}
                    onSaved=${() => onPluginsChanged && onPluginsChanged()}
                  />`}
            </div>
          </div>
        </div>
      ` : null}
    </div>
  `;
}

export default PluginsManager;
