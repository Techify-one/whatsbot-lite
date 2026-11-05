import { h } from 'preact';
import { useState, useEffect, useCallback } from 'preact/hooks';
import htm from 'htm';
import { getUsageSummary, getUsageByContact, getConfig } from '../services/api.js';

const html = htm.bind(h);

const PERIODS = [
  { key: '24h', label: '24h' },
  { key: '3d', label: '3 dias' },
  { key: '7d', label: '7 dias' },
  { key: '30d', label: '30 dias' },
  { key: 'all', label: 'Tudo' },
  { key: 'custom', label: 'Personalizado' },
];

function formatUsd(value) {
  return `$${(value || 0).toFixed(4)}`;
}

function formatBrl(usd, rate) {
  return `R$ ${((usd || 0) * (rate || 5.5)).toFixed(2).replace('.', ',')}`;
}

function formatTokens(n) {
  if (!n) return '0';
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

// Share of the input that came from the prompt cache, only over calls where the
// cache was measured (older history has no data). null = nothing to show.
function cachePct(stats) {
  const base = stats?.cache_measured_prompt_tokens || 0;
  if (!base) return null;
  return ((stats.cached_tokens || 0) / base) * 100;
}

function formatPct(pct) {
  return pct === null || pct === undefined ? '—' : `${pct.toFixed(0)}%`;
}

function toTimestamp(dateStr, timeStr) {
  if (!dateStr) return null;
  return new Date(`${dateStr}T${timeStr || '00:00'}`).getTime() / 1000;
}

export function CostsDashboard() {
  const [period, setPeriod] = useState('all');
  const [customStartDate, setCustomStartDate] = useState('');
  const [customStartTime, setCustomStartTime] = useState('00:00');
  const [customEndDate, setCustomEndDate] = useState('');
  const [customEndTime, setCustomEndTime] = useState('23:59');
  const [summary, setSummary] = useState(null);
  const [contacts, setContacts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [usdBrlRate, setUsdBrlRate] = useState(5.5);
  const [sortField, setSortField] = useState('cost_usd');
  const [sortAsc, setSortAsc] = useState(false);
  const [search, setSearch] = useState('');

  useEffect(() => {
    getConfig().then(res => {
      if (res.ok && res.data?.usd_brl_rate) {
        setUsdBrlRate(res.data.usd_brl_rate);
      }
    });
  }, []);

  const fetchData = useCallback(async () => {
    setLoading(true);
    const params = {};
    if (period === 'custom') {
      if (customStartDate) params.start = toTimestamp(customStartDate, customStartTime);
      if (customEndDate) params.end = toTimestamp(customEndDate, customEndTime);
    } else if (period !== 'all') {
      params.period = period;
    }

    const [sumRes, conRes] = await Promise.all([
      getUsageSummary(params),
      getUsageByContact(params),
    ]);

    if (sumRes.ok) setSummary(sumRes.data);
    if (conRes.ok) setContacts(conRes.data || []);
    setLoading(false);
  }, [period, customStartDate, customStartTime, customEndDate, customEndTime]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  function handleSort(field) {
    if (sortField === field) {
      setSortAsc(!sortAsc);
    } else {
      setSortField(field);
      setSortAsc(false);
    }
  }

  const filtered = contacts.filter(c => {
    if (!search) return true;
    const q = search.toLowerCase();
    return (c.name || '').toLowerCase().includes(q) || (c.phone || '').includes(q);
  });

  const textCachePct = c => cachePct((c.by_type || {}).text);

  const sorted = [...filtered].sort((a, b) => {
    const va = sortField === 'cache_pct' ? (textCachePct(a) ?? -1) : (a[sortField] || 0);
    const vb = sortField === 'cache_pct' ? (textCachePct(b) ?? -1) : (b[sortField] || 0);
    if (sortField === 'name') {
      return sortAsc
        ? String(va).localeCompare(String(vb))
        : String(vb).localeCompare(String(va));
    }
    return sortAsc ? va - vb : vb - va;
  });

  const typeLabel = { text: 'Texto', audio: 'Audio', image: 'Imagem' };

  return html`
    <div class="space-y-4">
      <!-- Period selector -->
      <div class="bg-wa-bg rounded-xl border border-wa-border p-4">
        <div class="flex flex-wrap items-center gap-2">
          ${PERIODS.map(p => html`
            <button
              key=${p.key}
              onClick=${() => setPeriod(p.key)}
              class="px-3 py-1.5 rounded-lg text-[13px] font-medium transition-colors ${
                period === p.key
                  ? 'bg-wa-teal text-white'
                  : 'bg-wa-panel text-wa-secondary hover:bg-wa-hover'
              }"
            >${p.label}</button>
          `)}
        </div>
        ${period === 'custom' ? html`
          <div class="flex flex-wrap gap-3 mt-3 items-center">
            <label class="text-[13px] text-wa-secondary">De:</label>
            <input
              type="date"
              value=${customStartDate}
              onInput=${e => setCustomStartDate(e.target.value)}
              class="border border-wa-border rounded-lg px-3 py-1.5 text-[13px]"
            />
            <input
              type="time"
              value=${customStartTime}
              onInput=${e => setCustomStartTime(e.target.value)}
              class="border border-wa-border rounded-lg px-3 py-1.5 text-[13px]"
            />
            <label class="text-[13px] text-wa-secondary">Ate:</label>
            <input
              type="date"
              value=${customEndDate}
              onInput=${e => setCustomEndDate(e.target.value)}
              class="border border-wa-border rounded-lg px-3 py-1.5 text-[13px]"
            />
            <input
              type="time"
              value=${customEndTime}
              onInput=${e => setCustomEndTime(e.target.value)}
              class="border border-wa-border rounded-lg px-3 py-1.5 text-[13px]"
            />
          </div>
        ` : null}
      </div>

      ${loading ? html`
        <div class="text-center text-wa-secondary animate-pulse-slow py-8">Carregando dados...</div>
      ` : html`
        <!-- Summary cards -->
        ${summary ? html`
          <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            <!-- Total cost -->
            <div class="bg-wa-bg rounded-xl border border-wa-border p-4">
              <div class="text-[12px] text-wa-secondary uppercase tracking-wide mb-1">Custo estimado</div>
              <div class="text-[22px] font-semibold text-wa-text">${formatUsd(summary.cost_usd)}</div>
              <div class="text-[14px] text-wa-secondary">${formatBrl(summary.cost_usd, usdBrlRate)}</div>
              <div class="text-[11px] text-wa-secondary mt-1">Calculado pelo preço listado de cada modelo; o valor cobrado pelo provedor pode variar.</div>
            </div>
            <!-- Prompt cache -->
            <div class="bg-wa-bg rounded-xl border border-wa-border p-4">
              <div class="text-[12px] text-wa-secondary uppercase tracking-wide mb-1">Cache da entrada</div>
              <div class="text-[22px] font-semibold text-wa-text">${formatPct(cachePct((summary.by_type || {}).text))}</div>
              ${(summary.saved_usd || 0) > 0 ? html`
                <div class="text-[14px] text-wa-secondary">Economia estimada: ${formatUsd(summary.saved_usd)} (${formatBrl(summary.saved_usd, usdBrlRate)})</div>
              ` : html`
                <div class="text-[13px] text-wa-secondary">${cachePct((summary.by_type || {}).text) === null ? 'Sem dados de cache neste período.' : 'Sem economia no período.'}</div>
              `}
              <div class="text-[11px] text-wa-secondary mt-1">Parte do texto enviado à IA que foi reaproveitada (mais barata). Só chamadas de texto.</div>
            </div>
            <!-- Total tokens -->
            <div class="bg-wa-bg rounded-xl border border-wa-border p-4">
              <div class="text-[12px] text-wa-secondary uppercase tracking-wide mb-1">Tokens</div>
              <div class="text-[22px] font-semibold text-wa-text">${formatTokens(summary.total_tokens)}</div>
              <div class="text-[13px] text-wa-secondary flex gap-3">
                <span class="text-blue-600">${formatTokens(summary.prompt_tokens)} entrada</span>
                <span class="text-orange-600">${formatTokens(summary.completion_tokens)} saida</span>
              </div>
            </div>
            <!-- Call count -->
            <div class="bg-wa-bg rounded-xl border border-wa-border p-4">
              <div class="text-[12px] text-wa-secondary uppercase tracking-wide mb-1">Chamadas IA</div>
              <div class="text-[22px] font-semibold text-wa-text">${summary.call_count || 0}</div>
              <div class="text-[13px] text-wa-secondary space-x-2">
                ${Object.entries(summary.by_type || {}).map(([type, data]) => html`
                  <span key=${type}>${typeLabel[type] || type}: ${data.call_count}</span>
                `)}
              </div>
            </div>
          </div>

          <!-- Cost by type -->
          <div class="bg-wa-bg rounded-xl border border-wa-border p-4">
            <div class="text-[13px] font-medium text-wa-text mb-3">Custo por tipo de chamada</div>
            <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
              ${['text', 'audio', 'image'].map(type => {
                const data = (summary.by_type || {})[type] || { cost_usd: 0, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, call_count: 0 };
                return html`
                  <div key=${type} class="bg-wa-panel rounded-lg p-3">
                    <div class="text-[12px] text-wa-secondary mb-1">${typeLabel[type]}</div>
                    <div class="text-[16px] font-semibold text-wa-text">${formatUsd(data.cost_usd)}</div>
                    <div class="text-[12px] text-wa-secondary">${formatBrl(data.cost_usd, usdBrlRate)}</div>
                    <div class="text-[12px] mt-1 flex gap-2">
                      <span class="text-blue-600">${formatTokens(data.prompt_tokens)} entrada</span>
                      <span class="text-orange-600">${formatTokens(data.completion_tokens)} saida</span>
                    </div>
                    <div class="text-[12px] text-wa-secondary">${data.call_count} chamadas</div>
                  </div>
                `;
              })}
            </div>
          </div>
        ` : null}

        <!-- Search + Contact table -->
        <div class="bg-wa-bg rounded-xl border border-wa-border overflow-hidden">
          <div class="px-4 py-3 border-b border-wa-border">
            <input
              type="text"
              placeholder="Buscar por nome ou telefone..."
              value=${search}
              onInput=${e => setSearch(e.target.value)}
              class="w-full wa-field border border-wa-border rounded-lg px-3 py-2 text-[13px] outline-none focus:border-wa-teal transition-colors"
            />
          </div>
          <div class="overflow-x-auto wa-scrollbar">
          <table class="costs-table w-full text-[13px]">
            <thead>
              <tr class="bg-wa-panel border-b border-wa-border">
                <th
                  class="text-left px-4 py-2.5 font-medium text-wa-secondary cursor-pointer hover:text-wa-text"
                  onClick=${() => handleSort('name')}
                >
                  Contato ${sortField === 'name' ? (sortAsc ? ' ↑' : ' ↓') : ''}
                </th>
                <th
                  class="text-right px-4 py-2.5 font-medium text-wa-secondary cursor-pointer hover:text-wa-text"
                  onClick=${() => handleSort('cost_usd')}
                >
                  Custo estimado ${sortField === 'cost_usd' ? (sortAsc ? ' ↑' : ' ↓') : ''}
                </th>
                <th
                  class="text-right px-4 py-2.5 font-medium text-wa-secondary cursor-pointer hover:text-wa-text"
                  onClick=${() => handleSort('prompt_tokens')}
                >
                  <span class="text-blue-600">Entrada</span> ${sortField === 'prompt_tokens' ? (sortAsc ? ' ↑' : ' ↓') : ''}
                </th>
                <th
                  class="text-right px-4 py-2.5 font-medium text-wa-secondary cursor-pointer hover:text-wa-text"
                  onClick=${() => handleSort('completion_tokens')}
                >
                  <span class="text-orange-600">Saida</span> ${sortField === 'completion_tokens' ? (sortAsc ? ' ↑' : ' ↓') : ''}
                </th>
                <th
                  class="text-right px-4 py-2.5 font-medium text-wa-secondary cursor-pointer hover:text-wa-text"
                  onClick=${() => handleSort('call_count')}
                >
                  Chamadas ${sortField === 'call_count' ? (sortAsc ? ' ↑' : ' ↓') : ''}
                </th>
                <th
                  class="text-right px-4 py-2.5 font-medium text-wa-secondary cursor-pointer hover:text-wa-text"
                  onClick=${() => handleSort('cache_pct')}
                >
                  Cache ${sortField === 'cache_pct' ? (sortAsc ? ' ↑' : ' ↓') : ''}
                </th>
                <th class="text-right px-4 py-2.5 font-medium text-wa-secondary">Detalhes</th>
              </tr>
            </thead>
            <tbody>
              ${sorted.length === 0 ? html`
                <tr>
                  <td colspan="7" class="text-center py-8 text-wa-secondary">
                    ${search ? 'Nenhum contato encontrado.' : 'Nenhum dado de uso encontrado para este periodo.'}
                  </td>
                </tr>
              ` : sorted.map(c => html`
                <${ContactRow}
                  key=${c.phone}
                  contact=${c}
                  usdBrlRate=${usdBrlRate}
                  typeLabel=${typeLabel}
                />
              `)}
            </tbody>
          </table>
          </div>
        </div>
      `}
    </div>
  `;
}

function ContactRow({ contact: c, usdBrlRate, typeLabel }) {
  const [expanded, setExpanded] = useState(false);

  return html`
    <tr class="border-b border-wa-border/50 hover:bg-wa-hover transition-colors">
      <td class="px-4 py-2.5">
        ${c.name ? html`
          <div class="font-medium text-wa-text">${c.name}</div>
          <div class="text-[11px] text-wa-secondary">${c.phone}</div>
        ` : html`
          <div class="font-medium text-wa-text">${c.phone}</div>
        `}
      </td>
      <td class="text-right px-4 py-2.5">
        <div class="font-mono font-medium text-wa-text">${formatUsd(c.cost_usd)}</div>
        <div class="font-mono text-[11px] text-wa-secondary">${formatBrl(c.cost_usd, usdBrlRate)}</div>
      </td>
      <td class="text-right px-4 py-2.5 font-mono text-blue-600">${formatTokens(c.prompt_tokens)}</td>
      <td class="text-right px-4 py-2.5 font-mono text-orange-600">${formatTokens(c.completion_tokens)}</td>
      <td class="text-right px-4 py-2.5 text-wa-secondary">${c.call_count}</td>
      <td class="text-right px-4 py-2.5 font-mono text-wa-secondary">${formatPct(cachePct((c.by_type || {}).text))}</td>
      <td class="text-right px-4 py-2.5">
        <button
          onClick=${() => setExpanded(!expanded)}
          class="text-wa-teal hover:underline text-[12px]"
        >${expanded ? 'Fechar' : 'Ver'}</button>
      </td>
    </tr>
    ${expanded ? html`
      <tr class="bg-wa-panel">
        <td colspan="7" class="px-4 py-3">
          <div class="grid grid-cols-1 sm:grid-cols-3 gap-2 text-[12px]">
            ${['text', 'audio', 'image'].map(type => {
              const data = (c.by_type || {})[type] || { cost_usd: 0, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, call_count: 0 };
              return html`
                <div key=${type} class="bg-wa-bg rounded-lg border border-wa-border/50 p-2">
                  <div class="font-medium text-wa-secondary">${typeLabel[type]}</div>
                  <div class="text-wa-text">${formatUsd(data.cost_usd)} (${formatBrl(data.cost_usd, usdBrlRate)})</div>
                  <div class="mt-0.5 flex gap-2">
                    <span class="text-blue-600">${formatTokens(data.prompt_tokens)} entrada</span>
                    <span class="text-orange-600">${formatTokens(data.completion_tokens)} saida</span>
                  </div>
                  <div class="text-wa-secondary">${data.call_count} chamadas</div>
                </div>
              `;
            })}
          </div>
        </td>
      </tr>
    ` : null}
  `;
}
