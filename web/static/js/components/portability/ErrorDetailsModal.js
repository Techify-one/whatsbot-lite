// Erros por linha de um job (verificação ou importação). Só o que o servidor já mascarou:
// entidade, posição, código, motivo e identificador mascarado — nunca conteúdo de mensagem.
// Paginado ("Carregar mais"); "Copiar" e "Baixar JSON" levam exatamente o que está na tela.

import { h } from 'preact';
import { useEffect, useState } from 'preact/hooks';
import htm from 'htm';
import { getJobErrors } from '../../services/portabilityApi.js';
import { ENTITY_LABELS, errorCodeLabel, errorsAsJson, truncationText } from '../../utils/portabilityLogic.js';

const html = htm.bind(h);
const PAGE = 100;

export function ErrorDetailsModal({ jobId, entity, onClose }) {
  const [items, setItems] = useState([]);
  const [totals, setTotals] = useState({});
  const [after, setAfter] = useState(null);
  const [more, setMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [open, setOpen] = useState(null);               // id do erro com o JSON aberto

  const load = async (cursor) => {
    setLoading(true); setError('');
    try {
      const res = await getJobErrors(jobId, { entity, limit: PAGE, after: cursor });
      if (!res || !res.ok) { setError((res && res.error) || 'Não foi possível carregar os erros.'); return; }
      const d = res.data || {};
      setItems((cur) => (cursor == null ? d.items : [...cur, ...d.items]));
      setTotals(d.totals || {});
      setAfter(d.next_after);
      setMore((d.items || []).length === PAGE);
    } finally { setLoading(false); }
  };

  useEffect(() => { load(null); }, [jobId, entity]);
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const [copied, setCopied] = useState('');
  const copy = async () => {
    try { await navigator.clipboard.writeText(errorsAsJson(items)); setCopied('Erros copiados.'); }
    catch (_) { setCopied('Não foi possível copiar.'); }
    setTimeout(() => setCopied(''), 3000);
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([errorsAsJson(items)], { type: 'application/json' }));
    const a = document.createElement('a');
    a.href = url; a.download = `erros-importacao-${String(jobId).slice(0, 8)}.json`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  const cut = truncationText(totals, entity || null);
  const title = entity ? `Erros — ${ENTITY_LABELS[entity] || entity}` : 'Erros da operação';

  return html`
    <div class="fixed inset-0 bg-black/50 z-[60] flex items-center justify-center p-4"
      onClick=${(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div class="bg-wa-bg rounded-2xl shadow-2xl max-w-2xl w-full max-h-[85vh] flex flex-col p-6 relative"
        role="dialog" aria-modal="true" aria-label=${title}>
        <button type="button" onClick=${onClose} aria-label="Fechar"
          class="absolute top-3 right-3 text-wa-secondary hover:text-wa-text p-1 rounded cursor-pointer">✕</button>
        <h3 class="text-base font-semibold text-wa-text pr-6">${title}</h3>
        <p class="text-[12px] text-wa-secondary mt-1">
          Identificadores aparecem mascarados. Nunca mostramos conteúdo de mensagem, telefone completo nem segredo.
        </p>
        ${cut ? html`<p class="text-[12px] text-amber-700 mt-2" role="status">${cut}</p>` : null}
        ${copied ? html`<p class="text-[12px] text-wa-secondary mt-2" role="status">${copied}</p>` : null}
        <div class="flex gap-2 mt-3">
          <button type="button" onClick=${copy} disabled=${!items.length}
            class="text-[12px] px-3 py-1 rounded-md border border-wa-border text-wa-text hover:bg-wa-hover disabled:opacity-50 cursor-pointer">Copiar</button>
          <button type="button" onClick=${download} disabled=${!items.length}
            class="text-[12px] px-3 py-1 rounded-md border border-wa-border text-wa-text hover:bg-wa-hover disabled:opacity-50 cursor-pointer">Baixar JSON</button>
        </div>
        <ul class="mt-3 overflow-y-auto min-h-0 flex flex-col divide-y divide-wa-border border border-wa-border rounded-lg">
          ${items.map((e) => html`
            <li key=${e.id} class="px-3 py-2 text-[13px] text-wa-text min-w-0">
              <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <strong>${ENTITY_LABELS[e.entity] || e.entity}</strong>
                <span class="text-[12px] text-wa-secondary">${e.row_index != null ? `linha ${e.row_index}` : 'sem linha (unidade)'}</span>
                <span class="text-[12px] text-wa-secondary font-mono">${e.code}</span>
              </div>
              <p class="text-[12px] text-wa-secondary mt-1 break-words">
                ${errorCodeLabel(e.code)}${e.detail && e.detail.key ? html` — <span class="font-mono">${e.detail.key}</span>` : null}
              </p>
              <button type="button" onClick=${() => setOpen(open === e.id ? null : e.id)}
                class="text-[12px] underline text-wa-teal mt-1 cursor-pointer">${open === e.id ? 'Ocultar JSON' : 'Ver JSON'}</button>
              ${open === e.id ? html`
                <pre class="mt-1 text-[11px] bg-wa-panel rounded-md p-2 overflow-x-auto">${JSON.stringify(e.detail || {}, null, 2)}</pre>` : null}
            </li>`)}
          ${!items.length && !loading ? html`<li class="px-3 py-3 text-[13px] text-wa-secondary">Nenhum erro registrado.</li>` : null}
        </ul>
        ${error ? html`<p class="text-[13px] text-red-600 mt-2">${error}</p>` : null}
        ${more ? html`
          <button type="button" onClick=${() => load(after)} disabled=${loading}
            class="mt-3 self-start text-[13px] px-3 py-1.5 rounded-md border border-wa-border text-wa-text hover:bg-wa-hover disabled:opacity-50 cursor-pointer">
            ${loading ? 'Carregando…' : 'Carregar mais'}</button>` : null}
      </div>
    </div>`;
}
