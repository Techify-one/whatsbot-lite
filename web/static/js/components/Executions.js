import { h } from 'preact';
import { useState, useEffect, useCallback, useRef } from 'preact/hooks';
import htm from 'htm';
import { getExecutions, getExecution } from '../services/api.js';

import nav from '../utils/navigation.js';

const html = htm.bind(h);

const STEP_COLORS = {
  webhook_received: { bg: 'bg-wa-panel', text: 'text-wa-text', border: 'border-wa-border' },
  batch_accumulated: { bg: 'bg-blue-50', text: 'text-blue-700', border: 'border-blue-300' },
  media_processed: { bg: 'bg-purple-50', text: 'text-purple-700', border: 'border-purple-300' },
  llm_request: { bg: 'bg-amber-50', text: 'text-amber-700', border: 'border-amber-300' },
  llm_response: { bg: 'bg-green-50', text: 'text-green-700', border: 'border-green-300' },
  tool_executed: { bg: 'bg-orange-50', text: 'text-orange-700', border: 'border-orange-300' },
  gowa_send: { bg: 'bg-blue-50', text: 'text-blue-700', border: 'border-blue-300' },
  response_sent: { bg: 'bg-green-50', text: 'text-green-700', border: 'border-green-300' },
  error: { bg: 'bg-red-50', text: 'text-red-700', border: 'border-red-300' },
};

const STATUS_BADGES = {
  running: { bg: 'bg-yellow-100', text: 'text-yellow-800', label: 'Em execução' },
  completed: { bg: 'bg-green-100', text: 'text-green-800', label: 'Concluída' },
  failed: { bg: 'bg-red-100', text: 'text-red-800', label: 'Falhou' },
};

function formatTime(ts) {
  if (!ts) return '-';
  const d = new Date(ts * 1000);
  return d.toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function formatDuration(ms) {
  if (ms == null) return '-';
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60000).toFixed(1)}min`;
}

function formatRelativeTs(ts, baseTsMs) {
  const diffMs = Math.round((ts * 1000) - baseTsMs);
  if (diffMs <= 0) return '+0ms';
  if (diffMs < 1000) return `+${diffMs}ms`;
  return `+${(diffMs / 1000).toFixed(2)}s`;
}

function StepBadge({ type }) {
  const colors = STEP_COLORS[type] || STEP_COLORS.error;
  return html`<span class="inline-block px-2 py-0.5 text-xs font-medium rounded ${colors.bg} ${colors.text}">${type}</span>`;
}

function StatusBadge({ status }) {
  const badge = STATUS_BADGES[status] || STATUS_BADGES.failed;
  return html`<span class="inline-block px-2 py-0.5 text-xs font-medium rounded ${badge.bg} ${badge.text}">${badge.label}</span>`;
}

function JsonBlock({ data, expandSignal }) {
  const [expanded, setExpanded] = useState(expandSignal ? expandSignal.value : true);
  useEffect(() => {
    if (expandSignal) setExpanded(expandSignal.value);
  }, [expandSignal]);
  if (!data || (typeof data === 'object' && Object.keys(data).length === 0)) return null;
  if (!expanded) return null;

  const json = typeof data === 'string' ? data : JSON.stringify(data, null, 2);

  return html`
    <div class="mt-1">
      <pre class="text-xs bg-wa-panel border border-wa-border rounded p-2 overflow-x-auto">${json}</pre>
    </div>
  `;
}

// ── Detail View ──────────────────────────────────────────────────

function ExecutionDetail({ execution, onBack }) {
  const baseTsMs = execution.started_at * 1000;
  const steps = execution.steps || [];
  const [expandSignal, setExpandSignal] = useState({ value: true, ver: 0 });
  const toggleAll = () => setExpandSignal(s => ({ value: !s.value, ver: s.ver + 1 }));

  return html`
    <div class="flex flex-col h-full">
      <!-- Header -->
      <div class="flex items-center gap-3 px-4 py-3 border-b border-wa-border bg-wa-bg">
        <button
          onClick=${onBack}
          class="p-1.5 rounded hover:bg-wa-hover transition-colors"
          title="Voltar"
        >
          <svg viewBox="0 0 24 24" width="20" height="20" fill="#54656f"><path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>
        </button>
        <div class="flex-1">
          <div class="flex items-center gap-2">
            <span class="text-sm font-bold text-wa-text">#${execution.id}</span>
            <${StatusBadge} status=${execution.status} />
            <span class="text-xs text-wa-secondary">${execution.trigger_type}</span>
          </div>
          <div class="text-xs text-wa-secondary">
            ${execution.phone} · ${formatTime(execution.started_at)}
            ${execution.duration_ms != null ? ` · ${formatDuration(execution.duration_ms)}` : ''}
          </div>
        </div>
        ${steps.length > 0 ? html`
          <button
            onClick=${toggleAll}
            class="px-2 py-1 text-xs text-wa-secondary hover:text-wa-teal hover:bg-wa-hover rounded transition-colors"
            title=${expandSignal.value ? 'Recolher todos os eventos' : 'Expandir todos os eventos'}
          >${expandSignal.value ? 'Recolher tudo' : 'Expandir tudo'}</button>
        ` : null}
      </div>

      <!-- Timeline -->
      <div class="flex-1 overflow-y-auto p-4">
        ${execution.error ? html`
          <div class="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg">
            <span class="text-sm font-medium text-red-700">Erro: </span>
            <span class="text-sm text-red-600">${execution.error}</span>
          </div>
        ` : null}

        <div class="relative pl-6">
          <!-- Vertical line -->
          <div class="absolute left-[9px] top-2 bottom-2 w-px bg-wa-border"></div>

          ${steps.map((step, i) => {
            const colors = STEP_COLORS[step.step_type] || STEP_COLORS.error;
            const isError = step.status === 'error';
            return html`
              <div key=${step.id} class="relative mb-4 last:mb-0">
                <!-- Dot -->
                <div class="absolute -left-6 top-1 w-[18px] h-[18px] rounded-full border-2 ${isError ? 'bg-red-100 border-red-400' : `bg-wa-bg ${colors.border}`}"></div>
                <!-- Content -->
                <div class="ml-2">
                  <div class="flex items-center gap-2 flex-wrap">
                    <span class="text-xs font-mono text-wa-secondary">${formatRelativeTs(step.ts, baseTsMs)}</span>
                    <${StepBadge} type=${step.step_type} />
                    ${isError ? html`<span class="text-xs text-red-600 font-medium">ERRO</span>` : null}
                  </div>
                  <${JsonBlock} data=${step.data} expandSignal=${expandSignal} />
                </div>
              </div>
            `;
          })}

          ${steps.length === 0 ? html`
            <div class="text-sm text-wa-secondary italic">Nenhum passo registrado.</div>
          ` : null}
        </div>
      </div>
    </div>
  `;
}

// ── List View ────────────────────────────────────────────────────

function executionIdFromUrl() {
  const m = window.location.pathname.match(/^\/executions\/(\d+)$/);
  return m ? parseInt(m[1], 10) : null;
}

export function Executions() {
  const [executions, setExecutions] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(0);
  const [filterPhone, setFilterPhone] = useState('');
  const [filterStatus, setFilterStatus] = useState('');
  const [selected, setSelected] = useState(null);
  const [selectedData, setSelectedData] = useState(null);
  const PAGE_SIZE = 30;

  const fetchList = useCallback(async () => {
    setLoading(true);
    const params = { limit: PAGE_SIZE, offset: page * PAGE_SIZE };
    if (filterPhone) params.phone = filterPhone;
    if (filterStatus) params.status = filterStatus;
    const res = await getExecutions(params);
    if (res.ok) {
      setExecutions(res.data.items || []);
      setTotal(res.data.total || 0);
    }
    setLoading(false);
  }, [page, filterPhone, filterStatus]);

  useEffect(() => { fetchList(); }, [fetchList]);

  // Auto-refresh every 5s
  useEffect(() => {
    const id = setInterval(fetchList, 5000);
    return () => clearInterval(id);
  }, [fetchList]);

  const handleSelect = useCallback(async (id, opts = {}) => {
    const res = await getExecution(id);
    if (res.ok) {
      setSelectedData(res.data);
      setSelected(id);
      if (!opts.skipPush) {
        const target = `/executions/${id}`;
        if (window.location.pathname !== target) {
          nav.swap(target);
        }
      }
    }
  }, []);

  const handleBack = useCallback(() => {
    setSelected(null);
    setSelectedData(null);
    if (window.location.pathname !== '/executions') {
      nav.swap('/executions');
    }
    fetchList();
  }, [fetchList]);

  // Open from URL on mount, and sync with browser back/forward via popstate.
  const selectedRef = useRef(selected);
  useEffect(() => { selectedRef.current = selected; }, [selected]);

  useEffect(() => {
    const initial = executionIdFromUrl();
    if (initial != null) handleSelect(initial, { skipPush: true });

    function onPop() {
      const urlId = executionIdFromUrl();
      if (urlId == null) {
        setSelected(null);
        setSelectedData(null);
      } else if (urlId !== selectedRef.current) {
        handleSelect(urlId, { skipPush: true });
      }
    }
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, [handleSelect]);

  // Detail view
  if (selected && selectedData) {
    return html`
      <div class="h-full bg-wa-bg rounded-xl border border-wa-border shadow-sm overflow-hidden">
        <${ExecutionDetail} execution=${selectedData} onBack=${handleBack} />
      </div>
    `;
  }

  const totalPages = Math.ceil(total / PAGE_SIZE);

  // List view
  return html`
    <div class="bg-wa-bg rounded-xl border border-wa-border shadow-sm flex flex-col h-full">
      <!-- Header -->
      <div class="px-4 py-3 border-b border-wa-border">
        <h2 class="text-base font-bold text-wa-text mb-2">Execuções</h2>
        <div class="flex gap-2 flex-wrap">
          <input
            type="text"
            placeholder="Filtrar por telefone..."
            value=${filterPhone}
            onInput=${(e) => { setFilterPhone(e.target.value); setPage(0); }}
            class="bg-wa-panel text-wa-text px-3 py-1.5 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none w-48"
          />
          <select
            value=${filterStatus}
            onChange=${(e) => { setFilterStatus(e.target.value); setPage(0); }}
            class="bg-wa-panel text-wa-text px-3 py-1.5 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
          >
            <option value="">Todos os status</option>
            <option value="completed">Concluída</option>
            <option value="failed">Falhou</option>
            <option value="running">Em execução</option>
          </select>
          <span class="text-xs text-wa-secondary self-center ml-auto">${total} execução(ões)</span>
        </div>
      </div>

      <!-- Table -->
      <div class="flex-1 overflow-y-auto">
        ${loading && executions.length === 0 ? html`
          <div class="p-8 text-center text-wa-secondary text-sm">Carregando...</div>
        ` : executions.length === 0 ? html`
          <div class="p-8 text-center text-wa-secondary text-sm">Nenhuma execução encontrada.</div>
        ` : html`
          <table class="w-full text-sm">
            <thead class="bg-wa-panel sticky top-0">
              <tr>
                <th class="text-left px-4 py-2 font-medium text-wa-secondary text-xs">#</th>
                <th class="text-left px-4 py-2 font-medium text-wa-secondary text-xs">Telefone</th>
                <th class="text-left px-4 py-2 font-medium text-wa-secondary text-xs">Tipo</th>
                <th class="text-left px-4 py-2 font-medium text-wa-secondary text-xs">Status</th>
                <th class="text-left px-4 py-2 font-medium text-wa-secondary text-xs">Início</th>
                <th class="text-left px-4 py-2 font-medium text-wa-secondary text-xs">Duração</th>
                <th class="text-left px-4 py-2 font-medium text-wa-secondary text-xs">Steps</th>
              </tr>
            </thead>
            <tbody>
              ${executions.map(ex => html`
                <tr
                  key=${ex.id}
                  onClick=${() => handleSelect(ex.id)}
                  class="border-t border-wa-border hover:bg-wa-hover cursor-pointer transition-colors"
                >
                  <td class="px-4 py-2.5 font-mono font-bold text-wa-text">${ex.id}</td>
                  <td class="px-4 py-2.5 text-wa-text">${ex.phone}</td>
                  <td class="px-4 py-2.5">
                    <span class="text-xs px-1.5 py-0.5 rounded bg-wa-panel text-wa-secondary">${ex.trigger_type}</span>
                  </td>
                  <td class="px-4 py-2.5"><${StatusBadge} status=${ex.status} /></td>
                  <td class="px-4 py-2.5 text-wa-secondary text-xs">${formatTime(ex.started_at)}</td>
                  <td class="px-4 py-2.5 text-wa-secondary text-xs">${formatDuration(ex.duration_ms)}</td>
                  <td class="px-4 py-2.5 text-wa-secondary text-xs">${ex.step_count || 0}</td>
                </tr>
              `)}
            </tbody>
          </table>
        `}
      </div>

      <!-- Pagination -->
      ${totalPages > 1 ? html`
        <div class="flex items-center justify-between px-4 py-2 border-t border-wa-border text-xs text-wa-secondary">
          <button
            onClick=${() => setPage(Math.max(0, page - 1))}
            disabled=${page === 0}
            class="px-3 py-1 rounded border border-wa-border hover:bg-wa-hover disabled:opacity-30 transition-colors"
          >Anterior</button>
          <span>Página ${page + 1} de ${totalPages}</span>
          <button
            onClick=${() => setPage(Math.min(totalPages - 1, page + 1))}
            disabled=${page >= totalPages - 1}
            class="px-3 py-1 rounded border border-wa-border hover:bg-wa-hover disabled:opacity-30 transition-colors"
          >Próxima</button>
        </div>
      ` : null}
    </div>
  `;
}
