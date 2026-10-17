// Arquivos — see what takes space and delete received/sent media safely.
// The server only ever receives opaque ids and only lists media folders, so
// nothing here can reach the database, the WhatsApp session or the code.

import { h } from 'preact';
import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks';
import htm from 'htm';
import { getFilesUsage, listFiles, deleteFiles, authHeaders } from '../services/api.js';
import { ConversationPicker } from './ConversationPicker.js';

const html = htm.bind(h);

const PAGE = 100;
// "Select everything this filter finds" walks the pages at the server's maximum
// page size; deleting sends ids in batches of the server's per-call maximum.
const SELECT_PAGE = 500;
const DELETE_BATCH = 1000;

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

const CHAT_TYPES = [
  { id: '', label: 'Conversas e grupos' },
  { id: 'individual', label: 'Só conversas individuais' },
  { id: 'group', label: 'Só grupos' },
];

const PERIODS = [
  { id: 'today', label: 'Hoje', days: 1 },
  { id: '7d', label: 'Últimos 7 dias', days: 7 },
  { id: '30d', label: 'Últimos 30 dias', days: 30 },
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

function fmtDateTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const date = d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', year: 'numeric' });
  const time = d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
  return `${date} ${time}`;
}

// <input type="date"> speaks "YYYY-MM-DD" in the user's own calendar.
function isoDate(d) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// Midnight (local time) of a picked day, as epoch seconds. ``addDays`` = 1 turns
// the last picked day into the exclusive end of the range, so that whole day counts.
function dayStart(iso, addDays = 0) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || '');
  if (!m) return '';
  return new Date(+m[1], +m[2] - 1, +m[3] + addDays).getTime() / 1000;
}

// Waits for the typing to pause, so each keystroke does not rescan the folders.
function useDebounced(value, ms = 350) {
  const [out, setOut] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setOut(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return out;
}

function plural(n, one, many) {
  return `${n.toLocaleString('pt-BR')} ${n === 1 ? one : many}`;
}

// Link to the conversation, centered on the message that carries the file.
// Same SPA hand-off as the gear menu: push the URL and let app.js react to it.
function conversationHref(origin) {
  return `/contacts/${origin.contact_id}?msg=${origin.message_id}`;
}

function openConversation(e, origin) {
  // Modified clicks keep the browser default (new tab/window).
  if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  history.pushState(null, '', conversationHref(origin));
  window.dispatchEvent(new PopStateEvent('popstate'));
}

function Origin({ item }) {
  if (item.type === 'dir') return html`<span class="text-wa-secondary">${plural(item.count || 0, 'arquivo', 'arquivos')}</span>`;
  const o = item.origin;
  if (o && o.contact_id != null && o.message_id != null) {
    return html`
      <a href=${conversationHref(o)} onClick=${(e) => openConversation(e, o)}
         class="text-wa-teal hover:underline"
         title="Abrir a conversa na mensagem deste arquivo">${o.is_group ? '👥 ' : ''}${o.name}</a>`;
  }
  if (o) {
    return html`<span class="text-wa-text">${o.is_group ? '👥 ' : ''}${o.name}</span>`;
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

// Plain text is capped so a huge log cannot freeze the tab.
const TEXT_PREVIEW_BYTES = 256 * 1024;

// The server decides what a file is (by extension, then by its first bytes) and
// says so in Content-Type, so the viewer follows that instead of guessing from
// the name. Anything it does not recognise ends up as a download card.
function previewFamily(mime) {
  if (mime.startsWith('image/')) return 'image';
  if (mime.startsWith('video/')) return 'video';
  if (mime.startsWith('audio/')) return 'audio';
  if (mime === 'application/pdf') return 'pdf';
  if (mime.startsWith('text/')) return 'text';
  return 'other';
}

// UTF-8 first; files saved by older Windows programs are usually Latin-1.
// ``stream`` keeps a multibyte character cut by the size cap from looking invalid.
async function readText(blob) {
  const buf = await blob.slice(0, TEXT_PREVIEW_BYTES).arrayBuffer();
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(buf, { stream: true });
  } catch {
    return new TextDecoder('windows-1252').decode(buf);
  }
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
  const [mime, setMime] = useState('');
  const [text, setText] = useState(null);
  const [clipped, setClipped] = useState(false);
  const [failed, setFailed] = useState(false);
  // The browser got the file but cannot decode it (HEIC outside Safari, AMR, ...).
  const [undecodable, setUndecodable] = useState(false);
  useEffect(() => {
    let revoked = false;
    let url = '';
    fetch(src, { headers: authHeaders() })
      .then((r) => (r.ok ? r.blob() : Promise.reject(new Error('bad'))))
      .then(async (b) => {
        const type = (b.type || '').split(';')[0].trim().toLowerCase();
        const body = previewFamily(type) === 'text' ? await readText(b) : null;
        if (revoked) return;
        url = URL.createObjectURL(b);
        setMime(type);
        setClipped(body !== null && b.size > TEXT_PREVIEW_BYTES);
        setText(body);
        setBlobUrl(url);
      })
      .catch(() => { if (!revoked) setFailed(true); });
    return () => { revoked = true; if (url) URL.revokeObjectURL(url); };
  }, [item.id]);

  const family = previewFamily(mime);
  const wide = family === 'pdf' || family === 'text';
  const cantShow = undecodable || (blobUrl && family === 'other');
  const onBroken = () => setUndecodable(true);

  const body = failed ? html`<p class="text-[13px] text-wa-secondary">Não foi possível abrir este arquivo.</p>`
    : !blobUrl ? html`<p class="text-[13px] text-wa-secondary animate-pulse-slow">Carregando...</p>`
    : cantShow ? html`
        <div class="flex flex-col items-center gap-3 py-6 text-center">
          <span class="text-[40px]" aria-hidden="true">${KIND_ICON[item.kind] || '📄'}</span>
          <p class="text-[14px] text-wa-text max-w-sm">
            ${undecodable
              ? 'O seu navegador não consegue mostrar este arquivo aqui. Baixe para abrir no seu computador.'
              : 'Este tipo de arquivo não tem visualização. Baixe para abrir no seu computador.'}
          </p>
          <a href=${blobUrl} download=${item.name} class="px-4 py-2 rounded-lg bg-wa-teal text-white text-[14px] font-medium">Baixar ${item.name}</a>
        </div>`
    : family === 'image' ? html`<img src=${blobUrl} alt=${item.name} onError=${onBroken} class="max-w-full max-h-[70vh] rounded" />`
    : family === 'video' ? html`<video src=${blobUrl} controls onError=${onBroken} class="max-w-full max-h-[70vh] rounded"></video>`
    : family === 'audio' ? html`<audio src=${blobUrl} controls onError=${onBroken} class="w-full"></audio>`
    : family === 'pdf' ? html`<iframe src=${blobUrl} title=${item.name} class="w-full h-[72vh] rounded bg-white"></iframe>`
    : html`
        <div class="w-full self-stretch flex flex-col gap-2 min-h-0">
          <pre tabindex="0" class="flex-1 min-h-0 overflow-auto rounded border border-wa-border bg-wa-bg p-3 text-[12.5px] leading-relaxed text-wa-text font-mono whitespace-pre-wrap break-words max-h-[70vh]">${text}</pre>
          ${clipped ? html`<p class="text-[12px] text-wa-secondary">Mostrando só o começo do arquivo. Baixe para ver tudo.</p>` : null}
        </div>`;

  return html`
    <div class="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4"
         role="dialog" aria-modal="true" aria-label=${item.name}
         onClick=${(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div class="bg-wa-bg rounded-xl ${wide ? 'max-w-5xl' : 'max-w-3xl'} w-full max-h-[90vh] flex flex-col overflow-hidden border border-wa-border">
        <div class="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 px-4 py-3 border-b border-wa-border">
          <div class="min-w-0 flex-1 basis-[12rem]">
            <div class="text-[14px] font-medium text-wa-text truncate" title=${item.name}>${item.name}</div>
            <div class="text-[12px] text-wa-secondary">${fmtBytes(item.size)} · ${fmtDateTime(item.mtime)}</div>
          </div>
          <div class="flex shrink-0 items-center gap-1 ml-auto">
            ${blobUrl && !cantShow && family === 'pdf' ? html`
              <a href=${blobUrl} target="_blank" rel="noopener" class="px-3 py-1.5 rounded-lg text-[13px] text-wa-teal hover:bg-wa-hover">Abrir em nova aba</a>` : null}
            ${blobUrl && !cantShow ? html`
              <a href=${blobUrl} download=${item.name} class="px-3 py-1.5 rounded-lg text-[13px] text-wa-teal hover:bg-wa-hover">Baixar</a>` : null}
            <button type="button" onClick=${onClose} class="px-3 py-1.5 rounded-lg text-[13px] text-wa-secondary hover:bg-wa-hover">Fechar</button>
          </div>
        </div>
        <div class="flex-1 min-h-0 overflow-auto p-4 flex items-center justify-center bg-wa-panel">${body}</div>
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
  const [orphans, setOrphans] = useState(false);
  const [search, setSearch] = useState('');
  const [conversation, setConversation] = useState('');
  // The conversation picked from the suggestions (exact id); typed text alone stays a plain search.
  const [pickedConv, setPickedConv] = useState(null);
  const [chatType, setChatType] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [totalBytes, setTotalBytes] = useState(0);
  const [nextOffset, setNextOffset] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState(() => new Map());
  const [preview, setPreview] = useState(null);
  const [confirm, setConfirm] = useState(null);
  const [deleting, setDeleting] = useState(false);
  const [selectingAll, setSelectingAll] = useState(false);
  const [notice, setNotice] = useState('');
  const reqRef = useRef(0);

  const [sortKey, sortOrder] = sort.split(':');
  const q = useDebounced(search.trim());
  const conv = useDebounced(conversation.trim());
  const convId = pickedConv ? pickedConv.id : 0;
  const hasFilters = !!(kind || orphans || q || conv || convId || chatType || dateFrom || dateTo);
  // Folders make no sense once the list is filtered: show files directly.
  const flat = hasFilters;
  // What goes to the server; the same object serves the first page and "Mostrar mais".
  const query = {
    root, path, flat, sort: sortKey, order: sortOrder, kind, orphans,
    q, conversation: convId ? '' : conv, contact_id: convId || undefined, chat_type: chatType,
    date_from: dayStart(dateFrom), date_to: dayStart(dateTo, 1),
  };

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
      const res = await listFiles({ ...query, offset: 0, limit: PAGE });
      if (req !== reqRef.current) return;
      if (!res || !res.ok) { setError((res && res.error) || 'Não foi possível listar os arquivos.'); setItems([]); return; }
      setItems(res.data.items);
      setTotal(res.data.total);
      setTotalBytes(res.data.total_bytes || 0);
      setNextOffset(res.data.next_offset);
    } catch {
      if (req === reqRef.current) setError('Não foi possível listar os arquivos.');
    } finally {
      if (req === reqRef.current) setLoading(false);
    }
  }, [root, path, sortKey, sortOrder, kind, orphans, flat, q, conv, convId, chatType, dateFrom, dateTo]);

  async function loadMore() {
    if (nextOffset == null || loadingMore) return;
    const req = reqRef.current;
    setLoadingMore(true);
    try {
      const res = await listFiles({ ...query, offset: nextOffset, limit: PAGE });
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
    setOrphans(false);
  }

  function clearFilters() {
    setSearch('');
    setConversation('');
    setPickedConv(null);
    setChatType('');
    setDateFrom('');
    setDateTo('');
    setKind('');
    setOrphans(false);
    setPath('');
  }

  function pickPeriod(days) {
    const end = new Date();
    const start = new Date();
    start.setDate(end.getDate() - (days - 1));
    setDateFrom(isoDate(start));
    setDateTo(isoDate(end));
    setPath('');
  }

  // The two dates always stay in order: moving one past the other drags it along.
  function changeFrom(v) {
    setDateFrom(v);
    if (v && dateTo && v > dateTo) setDateTo(v);
    setPath('');
  }
  function changeTo(v) {
    setDateTo(v);
    if (v && dateFrom && v < dateFrom) setDateFrom(v);
    setPath('');
  }

  const activePeriod = PERIODS.find((p) => {
    const end = new Date();
    const start = new Date();
    start.setDate(end.getDate() - (p.days - 1));
    return dateFrom === isoDate(start) && dateTo === isoDate(end);
  });

  function toggle(item) {
    setSelected((prev) => {
      const next = new Map(prev);
      if (next.has(item.id)) next.delete(item.id); else next.set(item.id, item);
      return next;
    });
  }

  // With a filter on, the header checkbox means "everything the filter finds",
  // not just the pages loaded so far.
  const allSelected = items.length > 0 && items.every((i) => selected.has(i.id))
    && (!hasFilters || nextOffset == null || selected.size >= total);

  async function selectAllMatching() {
    const req = reqRef.current;
    setSelectingAll(true);
    try {
      const all = [];
      let offset = 0;
      while (offset != null) {
        const res = await listFiles({ ...query, offset, limit: SELECT_PAGE });
        // The filters changed while walking the pages: this list is stale.
        if (req !== reqRef.current) return;
        if (!res || !res.ok) { setNotice((res && res.error) || 'Não foi possível selecionar todos os arquivos.'); return; }
        all.push(...res.data.items);
        offset = res.data.next_offset;
      }
      setSelected(new Map(all.map((i) => [i.id, i])));
    } catch {
      if (req === reqRef.current) setNotice('Não foi possível selecionar todos os arquivos.');
    } finally {
      setSelectingAll(false);
    }
  }

  function toggleAll() {
    if (allSelected) { setSelected(new Map()); return; }
    if (hasFilters && nextOffset != null) { selectAllMatching(); return; }
    setSelected(new Map(items.map((i) => [i.id, i])));
  }

  const selectedList = useMemo(() => Array.from(selected.values()), [selected]);
  const selectedBytes = selectedList.reduce((n, i) => n + (i.size || 0), 0);
  // Selected files that are beyond the pages shown ("Mostrar mais" not clicked).
  const hiddenSelected = useMemo(() => {
    const shown = new Set(items.map((i) => i.id));
    return selectedList.filter((i) => !shown.has(i.id)).length;
  }, [items, selectedList]);

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
    const done = { files: 0, bytes: 0, failed: 0 };
    let error = '';
    try {
      // The server takes a limited number of ids per call, so a big selection goes in batches.
      for (let i = 0; i < confirm.ids.length; i += DELETE_BATCH) {
        const res = await deleteFiles(confirm.ids.slice(i, i + DELETE_BATCH));
        if (!res || !res.ok) { error = (res && res.error) || 'Não foi possível apagar.'; break; }
        done.files += res.data.files;
        done.bytes += res.data.bytes;
        done.failed += res.data.failed;
      }
    } catch {
      error = 'Não foi possível apagar.';
    } finally {
      const summary = `${plural(done.files, 'arquivo apagado', 'arquivos apagados')} · ${fmtBytes(done.bytes)} liberados${done.failed ? ` · ${done.failed} não puderam ser apagados` : ''}.`;
      // A failure halfway still reports what was already removed.
      setNotice(error ? (done.files ? `${error} Antes disso, ${summary}` : error) : summary);
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

          <div role="search" aria-label="Filtros de arquivos" class="flex flex-col gap-3">
            <div class="grid grid-cols-1 md:grid-cols-2 gap-2">
              <label class="flex flex-col gap-1 text-[12px] text-wa-secondary">
                Buscar por nome ou texto
                <input type="search" value=${search} onInput=${(e) => { setSearch(e.target.value); setPath(''); }}
                  placeholder="Ex.: contrato, boleto, foto da obra"
                  class="wa-field rounded-lg px-3 py-2 text-[14px] outline-none focus:ring-2 focus:ring-wa-teal" />
              </label>
              <${ConversationPicker} text=${conversation} picked=${pickedConv}
                onText=${(v) => { setConversation(v); setPath(''); }}
                onPick=${(c) => { setPickedConv(c); setConversation(''); setPath(''); }}
                onClear=${() => { setPickedConv(null); setPath(''); }} />
            </div>

            <div class="flex flex-wrap items-end gap-2">
              <label class="flex flex-col gap-1 text-[12px] text-wa-secondary">
                De
                <input type="date" value=${dateFrom} max=${dateTo || undefined} onChange=${(e) => changeFrom(e.target.value)}
                  class="wa-field rounded-lg px-3 py-2 text-[14px] outline-none focus:ring-2 focus:ring-wa-teal" />
              </label>
              <label class="flex flex-col gap-1 text-[12px] text-wa-secondary">
                Até
                <input type="date" value=${dateTo} min=${dateFrom || undefined} onChange=${(e) => changeTo(e.target.value)}
                  class="wa-field rounded-lg px-3 py-2 text-[14px] outline-none focus:ring-2 focus:ring-wa-teal" />
              </label>
              <div class="flex flex-wrap gap-1 pb-0.5" role="group" aria-label="Períodos prontos">
                ${PERIODS.map((p) => html`
                  <button type="button" onClick=${() => pickPeriod(p.days)} aria-pressed=${activePeriod?.id === p.id}
                    class="px-3 py-2 rounded-lg text-[13px] border ${activePeriod?.id === p.id ? 'border-wa-teal text-wa-teal font-medium' : 'border-wa-border text-wa-text hover:bg-wa-hover'}">
                    ${p.label}
                  </button>`)}
              </div>
            </div>

            <div class="flex flex-wrap items-end gap-2">
              <label class="flex flex-col gap-1 text-[12px] text-wa-secondary">
                Tipo de conversa
                <select value=${chatType} onChange=${(e) => { setChatType(e.target.value); setPath(''); }} class="wa-field rounded-lg px-3 py-2 text-[14px] outline-none focus:ring-2 focus:ring-wa-teal">
                  ${CHAT_TYPES.map((c) => html`<option value=${c.id}>${c.label}</option>`)}
                </select>
              </label>
              <label class="flex flex-col gap-1 text-[12px] text-wa-secondary">
                Tipo de arquivo
                <select value=${kind} onChange=${(e) => setKind(e.target.value)} class="wa-field rounded-lg px-3 py-2 text-[14px] outline-none focus:ring-2 focus:ring-wa-teal">
                  ${KINDS.map((k) => html`<option value=${k.id}>${k.label}</option>`)}
                </select>
              </label>
              <label class="flex flex-col gap-1 text-[12px] text-wa-secondary">
                Ordenar por
                <select value=${sort} onChange=${(e) => setSort(e.target.value)} class="wa-field rounded-lg px-3 py-2 text-[14px] outline-none focus:ring-2 focus:ring-wa-teal">
                  ${SORTS.map((s) => html`<option value=${s.id}>${s.label}</option>`)}
                </select>
              </label>
              ${root === 'received' ? html`
                <label class="flex items-center gap-2 text-[13px] text-wa-text pb-2 cursor-pointer" title="Arquivos que nenhuma conversa usa mais">
                  <input type="checkbox" checked=${orphans} onChange=${(e) => { setOrphans(e.target.checked); setPath(''); }} />
                  Só arquivos sem conversa
                </label>` : null}
              ${hasFilters ? html`
                <button type="button" onClick=${clearFilters} class="mb-1 px-3 py-2 rounded-lg text-[13px] text-wa-teal hover:bg-wa-hover underline">Limpar filtros</button>` : null}
            </div>
          </div>

          ${!flat && crumbs.length ? html`
            <nav class="flex flex-wrap items-center gap-1 text-[13px]" aria-label="Pasta atual">
              <button type="button" onClick=${() => setPath('')} class="text-wa-teal underline">Início</button>
              ${crumbs.map((c, i) => html`
                <span class="text-wa-secondary">/</span>
                <button type="button" onClick=${() => setPath(crumbs.slice(0, i + 1).join('/'))} class="text-wa-teal underline">${c}</button>`)}
            </nav>` : null}

          ${selectingAll ? html`<div class="rounded-lg bg-wa-hover border border-wa-border px-3 py-2 text-[13.5px] text-wa-text animate-pulse-slow" role="status">Selecionando todos os arquivos deste filtro...</div>` : null}

          ${selectedList.length ? html`
            <div class="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-wa-hover border border-wa-border px-3 py-2">
              <span class="text-[13.5px] text-wa-text">${plural(selectedList.length, 'selecionado', 'selecionados')} · ${fmtBytes(selectedBytes)}${hiddenSelected ? ` (${hiddenSelected.toLocaleString('pt-BR')} ainda fora da lista; use “Mostrar mais” para vê-los)` : ''}</span>
              <div class="flex gap-2">
                <button type="button" onClick=${() => setSelected(new Map())} class="px-3 py-1.5 rounded-lg text-[13px] text-wa-secondary hover:bg-wa-panel">Limpar seleção</button>
                <button type="button" onClick=${() => askDelete(selectedList)} class="px-3 py-1.5 rounded-lg text-[13px] font-medium bg-red-600 text-white hover:bg-red-700">Apagar selecionados</button>
              </div>
            </div>` : null}

          ${loading ? html`<div class="py-10 text-center text-[14px] text-wa-secondary animate-pulse-slow">Carregando...</div>`
            : error ? html`<div class="py-8 text-center text-[14px] text-red-600" role="alert">${error}</div>`
            : items.length === 0 ? html`
              <div class="py-10 text-center text-[14px] text-wa-secondary">
                ${hasFilters ? html`
                  <p>Nenhum arquivo encontrado com esses filtros.</p>
                  <button type="button" onClick=${clearFilters} class="mt-2 text-wa-teal underline">Limpar filtros</button>`
                  : 'Nenhum arquivo aqui. O espaço está livre.'}
              </div>`
            : html`
              <div class="hidden md:block overflow-x-auto">
                <table class="w-full text-[13.5px]">
                  <thead>
                    <tr class="text-left text-[12px] uppercase tracking-wider text-wa-secondary border-b border-wa-border">
                      <th class="py-2 pr-2 w-8"><input type="checkbox" checked=${allSelected} disabled=${selectingAll} onChange=${toggleAll} aria-label=${hasFilters ? 'Selecionar todos os arquivos deste filtro' : 'Selecionar todos desta lista'} /></th>
                      <th class="py-2 pr-3">Nome</th>
                      <th class="py-2 pr-3">Conversa</th>
                      <th class="py-2 pr-3">Tipo</th>
                      <th class="py-2 pr-3 text-right">Tamanho</th>
                      <th class="py-2 pr-3 whitespace-nowrap">Data e hora</th>
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
                          ${it.snippet ? html`<div class="mt-0.5 text-[12px] text-wa-secondary truncate" title=${it.snippet}>na mensagem: ${it.snippet}</div>` : null}
                        </td>
                        <td class="py-2 pr-3 max-w-[220px] truncate"><${Origin} item=${it} /></td>
                        <td class="py-2 pr-3 text-wa-secondary">${KIND_LABEL[it.kind] || 'Arquivo'}</td>
                        <td class="py-2 pr-3 text-right text-wa-text whitespace-nowrap">${fmtBytes(it.size)}</td>
                        <td class="py-2 pr-3 text-wa-secondary whitespace-nowrap">${fmtDateTime(it.mtime)}</td>
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
                        ${it.snippet ? html`<div class="mt-1 text-[12.5px] text-wa-secondary break-words" title=${it.snippet}>na mensagem: ${it.snippet}</div>` : null}
                        <div class="mt-1 text-[12.5px] text-wa-secondary"><${Origin} item=${it} /></div>
                        <div class="mt-1 text-[12.5px] text-wa-secondary">${KIND_LABEL[it.kind] || 'Arquivo'} · ${fmtBytes(it.size)} · ${fmtDateTime(it.mtime)}</div>
                      </div>
                      <button type="button" onClick=${() => askDelete([it])} class="shrink-0 px-2 py-1 rounded text-[12.5px] text-red-600 hover:bg-wa-hover">Apagar</button>
                    </div>
                  </li>`)}
              </ul>

              <div class="flex flex-wrap items-center justify-between gap-2 pt-1 text-[12.5px] text-wa-secondary">
                <span>Mostrando ${items.length.toLocaleString('pt-BR')} de ${total.toLocaleString('pt-BR')} · ${fmtBytes(totalBytes)}${hasFilters ? ' neste filtro' : ''}</span>
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
