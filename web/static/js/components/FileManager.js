// Arquivos — see what takes space and delete received/sent media safely.
// The server only ever receives opaque ids and only lists media folders, so
// nothing here can reach the database, the WhatsApp session or the code.

import { h } from 'preact';
import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks';
import htm from 'htm';
import { getFilesUsage, listFiles, deleteFiles, authHeaders } from '../services/api.js';

const html = htm.bind(h);

const PAGE = 100;

const ROOTS = [
  { id: 'received', label: 'Recebidos', hint: 'Fotos, vídeos, áudios e documentos que seus contatos enviaram.' },
  { id: 'sent', label: 'Enviados por você', hint: 'Arquivos que você enviou pelo painel.' },
];

const SORTS = [
  { id: 'date:desc', label: 'Mais recentes' },
  { id: 'date:asc', label: 'Mais antigos' },
  { id: 'size:desc', label: 'Mais pesados' },
  { id: 'size:asc', label: 'Mais leves' },
  { id: 'kind:asc', label: 'Por tipo' },
  { id: 'name:asc', label: 'Por nome' },
];

const KINDS = [
  { id: '', label: 'Todos os tipos' },
  { id: 'image', label: 'Fotos' },
  { id: 'video', label: 'Vídeos' },
  { id: 'audio', label: 'Áudios' },
  { id: 'document', label: 'Documentos e outros' },
];

const KIND_LABEL = { image: 'Foto', video: 'Vídeo', audio: 'Áudio', document: 'Documento', dir: 'Pasta' };
const KIND_ICON = { image: '🖼️', video: '🎬', audio: '🎧', document: '📄', dir: '📁' };

function fmtBytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  const digits = v >= 100 || i === 0 ? 0 : v >= 10 ? 1 : 2;
  return `${v.toFixed(digits).replace('.', ',')} ${units[i]}`;
}

function fmtDate(ts) {
  if (!ts) return '';
  return new Date(ts * 1000).toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', year: 'numeric' });
}

function plural(n, one, many) {
  return `${n.toLocaleString('pt-BR')} ${n === 1 ? one : many}`;
}

function Origin({ item }) {
  if (item.type === 'dir') return html`<span class="text-wa-secondary">${plural(item.count || 0, 'arquivo', 'arquivos')}</span>`;
  if (item.origin) {
    return html`<span class="text-wa-text">${item.origin.is_group ? '👥 ' : ''}${item.origin.name}</span>`;
  }
  return html`<span class="text-wa-secondary" title="Nenhuma conversa usa este arquivo">Sem conversa</span>`;
}

function UsageBar({ usage, onLimit }) {
  if (!usage) {
    return html`<div class="h-24 rounded-xl bg-wa-hover animate-pulse-slow"></div>`;
  }
  const limit = usage.limit_bytes || 0;
  const total = usage.total_bytes || 0;
  const pct = limit ? Math.min(100, (total * 100) / limit) : null;
  const over = limit && total >= limit;
  const warn = limit && !over && total >= limit * 0.8;
  const barColor = over ? 'bg-red-500' : warn ? 'bg-amber-500' : 'bg-wa-teal';
  const recv = usage.roots?.received || { files: 0, bytes: 0 };
  const sent = usage.roots?.sent || { files: 0, bytes: 0 };

  return html`
    <section class="bg-wa-bg rounded-xl p-4 border border-wa-border shadow-sm" aria-label="Uso de espaço">
      <div class="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <div class="text-[12px] font-semibold uppercase tracking-wider text-wa-secondary">Espaço usado</div>
          <div class="text-[22px] font-semibold text-wa-text leading-tight">
            ${fmtBytes(total)}${limit ? html`<span class="text-[14px] font-normal text-wa-secondary"> de ${fmtBytes(limit)}</span>` : null}
          </div>
        </div>
        ${limit ? html`<div class="text-[13px] font-medium ${over ? 'text-red-600' : warn ? 'text-amber-700' : 'text-wa-secondary'}">${Math.round(pct)}%</div>` : null}
      </div>
      ${limit ? html`
        <div class="mt-3 h-2.5 rounded-full bg-wa-hover overflow-hidden" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow=${Math.round(pct)}>
          <div class="h-full ${barColor} transition-all" style="width:${pct}%"></div>
        </div>
        ${over ? html`<p class="mt-2 text-[12.5px] text-red-600">O limite de espaço foi atingido. Apague arquivos abaixo para liberar espaço.</p>` : null}
        ${warn ? html`<p class="mt-2 text-[12.5px] text-amber-700">Seu espaço está acabando. Apague arquivos que não precisa mais.</p>` : null}
      ` : null}
      <dl class="mt-3 grid grid-cols-1 sm:grid-cols-3 gap-2 text-[13px]">
        <div class="rounded-lg bg-wa-panel border border-wa-border px-3 py-2">
          <dt class="text-wa-secondary text-[12px]">Recebidos</dt>
          <dd class="text-wa-text font-medium">${fmtBytes(recv.bytes)} <span class="text-wa-secondary font-normal">· ${plural(recv.files, 'arquivo', 'arquivos')}</span></dd>
        </div>
        <div class="rounded-lg bg-wa-panel border border-wa-border px-3 py-2">
          <dt class="text-wa-secondary text-[12px]">Enviados por você</dt>
          <dd class="text-wa-text font-medium">${fmtBytes(sent.bytes)} <span class="text-wa-secondary font-normal">· ${plural(sent.files, 'arquivo', 'arquivos')}</span></dd>
        </div>
        <div class="rounded-lg bg-wa-panel border border-wa-border px-3 py-2">
          <dt class="text-wa-secondary text-[12px]">Sistema e dados do app</dt>
          <dd class="text-wa-text font-medium">${fmtBytes(usage.system_bytes)} <span class="text-wa-secondary font-normal">· não pode ser apagado aqui</span></dd>
        </div>
      </dl>
      ${!limit || !usage.limit_locked ? html`
        <div class="mt-3"><${LimitEditor} usage=${usage} onSaved=${onLimit} /></div>
      ` : html`<p class="mt-3 text-[12px] text-wa-secondary">O limite de espaço é definido pela sua hospedagem.</p>`}
    </section>
  `;
}

function LimitEditor({ usage, onSaved }) {
  const current = usage.limit_bytes ? String(+(usage.limit_bytes / 1024 ** 3).toFixed(2)).replace('.', ',') : '';
  const [value, setValue] = useState(current);
  const [saving, setSaving] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => { setValue(current); }, [current]);

  async function save() {
    const gb = parseFloat(String(value).replace(',', '.'));
    setSaving(true);
    try {
      const res = await fetch('/api/config', {
        method: 'PUT',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ disk_limit_gb: Number.isFinite(gb) && gb > 0 ? gb : 0 }),
      });
      if (res.ok) { setOpen(false); onSaved && onSaved(); }
    } finally {
      setSaving(false);
    }
  }

  if (!open) {
    return html`
      <button type="button" onClick=${() => setOpen(true)} class="text-[12.5px] text-wa-teal underline">
        ${usage.limit_bytes ? 'Mudar limite de espaço' : 'Definir um limite de espaço'}
      </button>
    `;
  }
  return html`
    <div class="flex flex-wrap items-end gap-2">
      <label class="flex flex-col gap-1 text-[12px] text-wa-secondary">
        Limite (em GB, vazio = sem limite)
        <input
          type="text" inputmode="decimal" value=${value}
          onInput=${(e) => setValue(e.target.value)}
          class="wa-field rounded-lg px-3 py-2 text-[14px] w-36 outline-none focus:ring-2 focus:ring-wa-teal"
          placeholder="Ex.: 5"
        />
      </label>
      <button type="button" onClick=${save} disabled=${saving}
        class="px-3 py-2 rounded-lg bg-wa-teal text-white text-[13px] font-medium disabled:opacity-60">Salvar</button>
      <button type="button" onClick=${() => setOpen(false)}
        class="px-3 py-2 rounded-lg text-[13px] text-wa-secondary hover:bg-wa-hover">Cancelar</button>
    </div>
  `;
}

function Preview({ item, onClose }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  const src = `/api/files/${encodeURIComponent(item.id)}/preview`;
  // Preview goes through the authenticated endpoint; use a blob when a password is set.
  const [blobUrl, setBlobUrl] = useState('');
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let revoked = false;
    let url = '';
    fetch(src, { headers: authHeaders() })
      .then((r) => (r.ok ? r.blob() : Promise.reject(new Error('bad'))))
      .then((b) => { if (!revoked) { url = URL.createObjectURL(b); setBlobUrl(url); } })
      .catch(() => { if (!revoked) setFailed(true); });
    return () => { revoked = true; if (url) URL.revokeObjectURL(url); };
  }, [item.id]);

  return html`
    <div class="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4"
         role="dialog" aria-modal="true" aria-label=${item.name}
         onClick=${(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div class="bg-wa-bg rounded-xl max-w-3xl w-full max-h-[90vh] flex flex-col overflow-hidden border border-wa-border">
        <div class="flex items-center justify-between gap-3 px-4 py-3 border-b border-wa-border">
          <div class="min-w-0">
            <div class="text-[14px] font-medium text-wa-text truncate">${item.name}</div>
            <div class="text-[12px] text-wa-secondary">${fmtBytes(item.size)} · ${fmtDate(item.mtime)}</div>
          </div>
          <button type="button" onClick=${onClose} class="px-3 py-1.5 rounded-lg text-[13px] text-wa-secondary hover:bg-wa-hover">Fechar</button>
        </div>
        <div class="flex-1 min-h-0 overflow-auto p-4 flex items-center justify-center bg-wa-panel">
          ${failed ? html`<p class="text-[13px] text-wa-secondary">Não foi possível abrir este arquivo.</p>`
            : !blobUrl ? html`<p class="text-[13px] text-wa-secondary animate-pulse-slow">Carregando...</p>`
            : item.kind === 'image' ? html`<img src=${blobUrl} alt=${item.name} class="max-w-full max-h-[70vh] rounded" />`
            : item.kind === 'video' ? html`<video src=${blobUrl} controls class="max-w-full max-h-[70vh] rounded"></video>`
            : item.kind === 'audio' ? html`<audio src=${blobUrl} controls class="w-full"></audio>`
            : item.name.toLowerCase().endsWith('.pdf') ? html`<iframe src=${blobUrl} title=${item.name} class="w-full h-[70vh] rounded bg-white"></iframe>`
            : html`<a href=${blobUrl} download=${item.name} class="text-wa-teal underline text-[14px]">Baixar ${item.name}</a>`}
        </div>
      </div>
    </div>
  `;
}

function ConfirmDelete({ summary, busy, onCancel, onConfirm }) {
  const ref = useRef(null);
  useEffect(() => {
    if (ref.current) ref.current.focus();
    const onKey = (e) => { if (e.key === 'Escape' && !busy) onCancel(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [busy]);
  return html`
    <div class="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4" role="alertdialog" aria-modal="true" aria-labelledby="del-title"
         onClick=${(e) => { if (e.target === e.currentTarget && !busy) onCancel(); }}>
      <div class="bg-wa-bg rounded-xl max-w-md w-full p-5 border border-wa-border shadow-xl">
        <h2 id="del-title" class="text-[17px] font-semibold text-wa-text">Apagar ${summary.label}?</h2>
        <p class="mt-2 text-[14px] text-wa-text">
          Isso libera <strong>${fmtBytes(summary.bytes)}</strong> e <strong>não pode ser desfeito</strong>.
        </p>
        <p class="mt-2 text-[13px] text-wa-secondary">
          As conversas continuam. As mensagens ligadas a esses arquivos passam a mostrar
          “arquivo removido para liberar espaço”. Se o WhatsApp ainda tiver o arquivo, você
          pode tentar baixar de novo; arquivos com mais de algumas semanas costumam sumir de lá.
        </p>
        <div class="mt-5 flex justify-end gap-2">
          <button type="button" ref=${ref} onClick=${onCancel} disabled=${busy}
            class="px-4 py-2 rounded-lg text-[14px] text-wa-text border border-wa-border hover:bg-wa-hover disabled:opacity-60">Cancelar</button>
          <button type="button" onClick=${onConfirm} disabled=${busy}
            class="px-4 py-2 rounded-lg text-[14px] font-medium bg-red-600 text-white hover:bg-red-700 disabled:opacity-60">
            ${busy ? 'Apagando...' : 'Apagar'}
          </button>
        </div>
      </div>
    </div>
  `;
}

export function FileManager() {
  const [usage, setUsage] = useState(null);
  const [root, setRoot] = useState('received');
  const [path, setPath] = useState('');
  const [sort, setSort] = useState('date:desc');
  const [kind, setKind] = useState('');
  const [flat, setFlat] = useState(false);
  const [orphans, setOrphans] = useState(false);
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [nextOffset, setNextOffset] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState(() => new Map());
  const [preview, setPreview] = useState(null);
  const [confirm, setConfirm] = useState(null);
  const [deleting, setDeleting] = useState(false);
  const [notice, setNotice] = useState('');
  const reqRef = useRef(0);

  const [sortKey, sortOrder] = sort.split(':');
  // Folders make no sense once the list is filtered/flat: show files directly.
  const effectiveFlat = flat || orphans || !!kind;

  const loadUsage = useCallback(async (refresh = false) => {
    try {
      const res = await getFilesUsage(refresh);
      if (res && res.ok) setUsage(res.data);
    } catch { /* keeps the previous numbers */ }
  }, []);

  const load = useCallback(async () => {
    const req = ++reqRef.current;
    setLoading(true);
    setError('');
    try {
      const res = await listFiles({
        root, path, flat: effectiveFlat, sort: sortKey, order: sortOrder,
        kind, orphans, offset: 0, limit: PAGE,
      });
      if (req !== reqRef.current) return;
      if (!res || !res.ok) { setError((res && res.error) || 'Não foi possível listar os arquivos.'); setItems([]); return; }
      setItems(res.data.items);
      setTotal(res.data.total);
      setNextOffset(res.data.next_offset);
    } catch {
      if (req === reqRef.current) setError('Não foi possível listar os arquivos.');
    } finally {
      if (req === reqRef.current) setLoading(false);
    }
  }, [root, path, sortKey, sortOrder, kind, orphans, effectiveFlat]);

  async function loadMore() {
    if (nextOffset == null || loadingMore) return;
    const req = reqRef.current;
    setLoadingMore(true);
    try {
      const res = await listFiles({
        root, path, flat: effectiveFlat, sort: sortKey, order: sortOrder,
        kind, orphans, offset: nextOffset, limit: PAGE,
      });
      if (req !== reqRef.current) return;
      if (res && res.ok) {
        setItems((prev) => [...prev, ...res.data.items]);
        setNextOffset(res.data.next_offset);
      }
    } finally {
      setLoadingMore(false);
    }
  }

  useEffect(() => { loadUsage(true); }, []);
  useEffect(() => { setSelected(new Map()); load(); }, [load]);

  // Another tab / the policy changed the files: refresh both views.
  useEffect(() => {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const token = localStorage.getItem('whatsbot_token') || '';
    const qs = token ? `?token=${encodeURIComponent(token)}` : '';
    let ws;
    let timer;
    try {
      ws = new WebSocket(`${protocol}//${location.host}/ws${qs}`);
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.event === 'storage_changed') {
            clearTimeout(timer);
            timer = setTimeout(() => { loadUsage(true); load(); }, 400);
          }
        } catch { /* ignore */ }
      };
    } catch { /* live refresh is optional */ }
    return () => { clearTimeout(timer); if (ws) ws.close(); };
  }, [load]);

  function switchRoot(next) {
    setRoot(next);
    setPath('');
    setKind('');
    setOrphans(false);
    setFlat(false);
  }

  function toggle(item) {
    setSelected((prev) => {
      const next = new Map(prev);
      if (next.has(item.id)) next.delete(item.id); else next.set(item.id, item);
      return next;
    });
  }

  const allSelected = items.length > 0 && items.every((i) => selected.has(i.id));
  function toggleAll() {
    setSelected(allSelected ? new Map() : new Map(items.map((i) => [i.id, i])));
  }

  const selectedList = useMemo(() => Array.from(selected.values()), [selected]);
  const selectedBytes = selectedList.reduce((n, i) => n + (i.size || 0), 0);

  function askDelete(list) {
    if (!list.length) return;
    const bytes = list.reduce((n, i) => n + (i.size || 0), 0);
    const fileCount = list.reduce((n, i) => n + (i.type === 'dir' ? (i.count || 0) : 1), 0);
    const dirs = list.filter((i) => i.type === 'dir').length;
    const label = list.length === 1 && list[0].type === 'file'
      ? 'este arquivo'
      : dirs && list.length === dirs
        ? `${plural(dirs, 'pasta', 'pastas')} (${plural(fileCount, 'arquivo', 'arquivos')})`
        : plural(fileCount, 'arquivo', 'arquivos');
    setConfirm({ ids: list.map((i) => i.id), bytes, label });
  }

  async function doDelete() {
    if (!confirm) return;
    setDeleting(true);
    try {
      const res = await deleteFiles(confirm.ids);
      if (res && res.ok) {
        const d = res.data;
        setNotice(`${plural(d.files, 'arquivo apagado', 'arquivos apagados')} · ${fmtBytes(d.bytes)} liberados${d.failed ? ` · ${d.failed} não puderam ser apagados` : ''}.`);
      } else {
        setNotice((res && res.error) || 'Não foi possível apagar.');
      }
    } catch {
      setNotice('Não foi possível apagar.');
    } finally {
      setDeleting(false);
      setConfirm(null);
      setSelected(new Map());
      loadUsage(true);
      load();
    }
  }

  useEffect(() => {
    if (!notice) return undefined;
    const t = setTimeout(() => setNotice(''), 8000);
    return () => clearTimeout(t);
  }, [notice]);

  const crumbs = path ? path.split('/') : [];
  const rootInfo = ROOTS.find((r) => r.id === root);

  function openItem(item) {
    if (item.type === 'dir') { setPath(item.rel); return; }
    setPreview(item);
  }

  return html`
    <div class="flex flex-col gap-4">
      <${UsageBar} usage=${usage} onLimit=${() => loadUsage(true)} />

      ${notice ? html`<div class="rounded-lg border border-wa-border bg-wa-bg px-4 py-2.5 text-[13.5px] text-wa-text" role="status">${notice}</div>` : null}

      <section class="bg-wa-bg rounded-xl border border-wa-border shadow-sm overflow-hidden" aria-label="Arquivos">
        <div class="px-4 pt-3 border-b border-wa-border">
          <div class="flex gap-1" role="tablist">
            ${ROOTS.map((r) => html`
              <button type="button" role="tab" aria-selected=${root === r.id}
                onClick=${() => switchRoot(r.id)}
                class="px-4 py-2 text-[14px] rounded-t-lg border-b-2 -mb-px ${root === r.id ? 'border-wa-teal text-wa-text font-medium' : 'border-transparent text-wa-secondary hover:text-wa-text'}">
                ${r.label}
              </button>`)}
          </div>
        </div>

        <div class="p-4 flex flex-col gap-3">
          <p class="text-[13px] text-wa-secondary">${rootInfo.hint} Só aparece aqui o que é seguro apagar: conversas, contatos e configurações nunca são mexidos.</p>

          <div class="flex flex-wrap items-end gap-2">
            <label class="flex flex-col gap-1 text-[12px] text-wa-secondary">
              Ordenar por
              <select value=${sort} onChange=${(e) => setSort(e.target.value)} class="wa-field rounded-lg px-3 py-2 text-[14px] outline-none focus:ring-2 focus:ring-wa-teal">
                ${SORTS.map((s) => html`<option value=${s.id}>${s.label}</option>`)}
              </select>
            </label>
            <label class="flex flex-col gap-1 text-[12px] text-wa-secondary">
              Tipo
              <select value=${kind} onChange=${(e) => setKind(e.target.value)} class="wa-field rounded-lg px-3 py-2 text-[14px] outline-none focus:ring-2 focus:ring-wa-teal">
                ${KINDS.map((k) => html`<option value=${k.id}>${k.label}</option>`)}
              </select>
            </label>
            <label class="flex items-center gap-2 text-[13px] text-wa-text pb-2 cursor-pointer">
              <input type="checkbox" checked=${flat} disabled=${orphans || !!kind} onChange=${(e) => { setFlat(e.target.checked); setPath(''); }} />
              Ver todos juntos, sem pastas
            </label>
            ${root === 'received' ? html`
              <label class="flex items-center gap-2 text-[13px] text-wa-text pb-2 cursor-pointer" title="Arquivos que nenhuma conversa usa mais">
                <input type="checkbox" checked=${orphans} onChange=${(e) => { setOrphans(e.target.checked); setPath(''); }} />
                Só arquivos sem conversa
              </label>` : null}
          </div>

          ${!effectiveFlat && crumbs.length ? html`
            <nav class="flex flex-wrap items-center gap-1 text-[13px]" aria-label="Pasta atual">
              <button type="button" onClick=${() => setPath('')} class="text-wa-teal underline">Início</button>
              ${crumbs.map((c, i) => html`
                <span class="text-wa-secondary">/</span>
                <button type="button" onClick=${() => setPath(crumbs.slice(0, i + 1).join('/'))} class="text-wa-teal underline">${c}</button>`)}
            </nav>` : null}

          ${selectedList.length ? html`
            <div class="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-wa-hover border border-wa-border px-3 py-2">
              <span class="text-[13.5px] text-wa-text">${plural(selectedList.length, 'selecionado', 'selecionados')} · ${fmtBytes(selectedBytes)}</span>
              <div class="flex gap-2">
                <button type="button" onClick=${() => setSelected(new Map())} class="px-3 py-1.5 rounded-lg text-[13px] text-wa-secondary hover:bg-wa-panel">Limpar seleção</button>
                <button type="button" onClick=${() => askDelete(selectedList)} class="px-3 py-1.5 rounded-lg text-[13px] font-medium bg-red-600 text-white hover:bg-red-700">Apagar selecionados</button>
              </div>
            </div>` : null}

          ${loading ? html`<div class="py-10 text-center text-[14px] text-wa-secondary animate-pulse-slow">Carregando...</div>`
            : error ? html`<div class="py-8 text-center text-[14px] text-red-600" role="alert">${error}</div>`
            : items.length === 0 ? html`
              <div class="py-10 text-center text-[14px] text-wa-secondary">
                ${kind || orphans ? 'Nenhum arquivo encontrado com esse filtro.' : 'Nenhum arquivo aqui. O espaço está livre.'}
              </div>`
            : html`
              <div class="hidden md:block overflow-x-auto">
                <table class="w-full text-[13.5px]">
                  <thead>
                    <tr class="text-left text-[12px] uppercase tracking-wider text-wa-secondary border-b border-wa-border">
                      <th class="py-2 pr-2 w-8"><input type="checkbox" checked=${allSelected} onChange=${toggleAll} aria-label="Selecionar todos desta lista" /></th>
                      <th class="py-2 pr-3">Nome</th>
                      <th class="py-2 pr-3">Conversa</th>
                      <th class="py-2 pr-3">Tipo</th>
                      <th class="py-2 pr-3 text-right">Tamanho</th>
                      <th class="py-2 pr-3">Data</th>
                      <th class="py-2 w-20"></th>
                    </tr>
                  </thead>
                  <tbody>
                    ${items.map((it) => html`
                      <tr key=${it.id} class="border-b border-wa-border last:border-b-0 hover:bg-wa-hover ${selected.has(it.id) ? 'bg-wa-hover' : ''}">
                        <td class="py-2 pr-2"><input type="checkbox" checked=${selected.has(it.id)} onChange=${() => toggle(it)} aria-label=${`Selecionar ${it.name}`} /></td>
                        <td class="py-2 pr-3 max-w-[280px]">
                          <button type="button" onClick=${() => openItem(it)} class="flex items-center gap-2 text-left text-wa-text hover:text-wa-teal max-w-full">
                            <span aria-hidden="true">${KIND_ICON[it.kind] || '📄'}</span>
                            <span class="truncate">${it.is_group ? '👥 ' : ''}${it.label || it.name}</span>
                          </button>
                        </td>
                        <td class="py-2 pr-3 max-w-[220px] truncate"><${Origin} item=${it} /></td>
                        <td class="py-2 pr-3 text-wa-secondary">${KIND_LABEL[it.kind] || 'Arquivo'}</td>
                        <td class="py-2 pr-3 text-right text-wa-text whitespace-nowrap">${fmtBytes(it.size)}</td>
                        <td class="py-2 pr-3 text-wa-secondary whitespace-nowrap">${fmtDate(it.mtime)}</td>
                        <td class="py-2 text-right">
                          <button type="button" onClick=${() => askDelete([it])} class="px-2 py-1 rounded text-[12.5px] text-red-600 hover:bg-wa-panel">Apagar</button>
                        </td>
                      </tr>`)}
                  </tbody>
                </table>
              </div>

              <ul class="md:hidden flex flex-col gap-2">
                ${items.map((it) => html`
                  <li key=${it.id} class="rounded-lg border border-wa-border bg-wa-panel p-3 ${selected.has(it.id) ? 'ring-2 ring-wa-teal' : ''}">
                    <div class="flex items-start gap-3">
                      <input type="checkbox" class="mt-1" checked=${selected.has(it.id)} onChange=${() => toggle(it)} aria-label=${`Selecionar ${it.name}`} />
                      <div class="min-w-0 flex-1">
                        <button type="button" onClick=${() => openItem(it)} class="flex items-center gap-2 text-left text-[14px] text-wa-text max-w-full">
                          <span aria-hidden="true">${KIND_ICON[it.kind] || '📄'}</span>
                          <span class="truncate">${it.is_group ? '👥 ' : ''}${it.label || it.name}</span>
                        </button>
                        <div class="mt-1 text-[12.5px] text-wa-secondary"><${Origin} item=${it} /></div>
                        <div class="mt-1 text-[12.5px] text-wa-secondary">${KIND_LABEL[it.kind] || 'Arquivo'} · ${fmtBytes(it.size)} · ${fmtDate(it.mtime)}</div>
                      </div>
                      <button type="button" onClick=${() => askDelete([it])} class="shrink-0 px-2 py-1 rounded text-[12.5px] text-red-600 hover:bg-wa-hover">Apagar</button>
                    </div>
                  </li>`)}
              </ul>

              <div class="flex flex-wrap items-center justify-between gap-2 pt-1 text-[12.5px] text-wa-secondary">
                <span>Mostrando ${items.length.toLocaleString('pt-BR')} de ${total.toLocaleString('pt-BR')}</span>
                ${nextOffset != null ? html`
                  <button type="button" onClick=${loadMore} disabled=${loadingMore}
                    class="px-3 py-1.5 rounded-lg border border-wa-border text-wa-text hover:bg-wa-hover disabled:opacity-60">
                    ${loadingMore ? 'Carregando...' : 'Mostrar mais'}
                  </button>` : null}
              </div>`}
        </div>
      </section>

      ${preview ? html`<${Preview} item=${preview} onClose=${() => setPreview(null)} />` : null}
      ${confirm ? html`<${ConfirmDelete} summary=${confirm} busy=${deleting} onCancel=${() => setConfirm(null)} onConfirm=${doDelete} />` : null}
    </div>
  `;
}
