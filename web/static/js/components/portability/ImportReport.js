// Relatório de uma verificação/importação: tabela por recurso (com seleção em lote e o menu
// "Se já existir neste servidor") e a lista de avisos recolhível. Usado pelo assistente de importar
// e pelo histórico ("Operações recentes"), por isso não conhece nenhum estado do assistente.
//
// Os valores internos das estratégias (skip/merge/overwrite) não mudam: só o texto, que vem de
// STRATEGY_LABELS / STRATEGY_HINTS (mapas fechados em portabilityLogic.js).

import { h } from 'preact';
import { useState } from 'preact/hooks';
import htm from 'htm';
import {
  FIXED_HINT, SKIP_COLUMN_HINT, SKIP_COLUMN_LABEL, STRATEGIES, STRATEGY_COLUMN_LABEL, STRATEGY_HINTS,
  STRATEGY_LABELS, applyBulkStrategy, bulkResultText, entityRows, isFixedStrategy, noteTexts,
  strategyChoices, warningRows, warningsTitle,
} from '../../utils/portabilityLogic.js';

const html = htm.bind(h);

/** Avisos amarelos. Recolhido por padrão na verificação (`defaultOpen` = false); aberto no histórico. */
export function Warnings({ report, defaultOpen = false }) {
  const rows = warningRows(report);
  const notes = noteTexts(report);
  const [open, setOpen] = useState(defaultOpen);
  if (!rows.length && !notes.length) return null;
  return html`
    <div class="rounded-lg border border-amber-500/30 bg-amber-500/10">
      <button type="button" onClick=${() => setOpen(!open)} aria-expanded=${open}
        class="w-full flex items-center justify-between gap-2 px-3 py-2 text-left cursor-pointer">
        <span class="text-[13px] font-medium text-amber-800">${warningsTitle(report)}</span>
        <span class="text-[12px] text-amber-800">${open ? 'Ocultar' : 'Mostrar'}</span>
      </button>
      ${open ? html`
        <div class="px-3 pb-3 flex flex-col gap-1">
          ${notes.map((n) => html`<p key=${n} class="text-[13px] text-amber-800">${n}</p>`)}
          ${rows.map((w) => html`
            <p key=${w.entity + w.code} class="text-[13px] text-amber-800">
              <strong>${w.entityLabel}</strong> (${w.count}): ${w.text}
              ${w.keys.length ? html` <span class="text-[12px] opacity-80">— ex.: ${w.keys.join(', ')}</span>` : null}
            </p>`)}
        </div>` : null}
    </div>`;
}

/** Ícone "?" do cabeçalho: abre as três explicações em uma frase cada. */
function StrategyHelp() {
  const [open, setOpen] = useState(false);
  return html`
    <span class="relative inline-block align-middle">
      <button type="button" onClick=${() => setOpen(!open)} aria-label="O que cada opção faz" aria-expanded=${open}
        class="ml-1 w-4 h-4 rounded-full border border-wa-border text-[10px] leading-none text-wa-secondary hover:text-wa-text cursor-pointer">?</button>
      ${open ? html`
        <div role="note" class="absolute z-10 left-0 top-6 w-72 rounded-lg border border-wa-border bg-wa-bg p-3 shadow-lg text-left font-normal">
          ${STRATEGIES.map((k) => html`
            <p key=${k} class="text-[12px] text-wa-text mb-1 last:mb-0">
              <strong>${STRATEGY_LABELS[k]}:</strong> ${STRATEGY_HINTS[k]}
            </p>`)}
        </div>` : null}
    </span>`;
}

/**
 * Tabela do relatório.
 * - `strategy`/`onStrategy`: quando vêm, mostra a coluna de escolha e a seleção em lote.
 * - `onShowErrors(entity)`: quando vem, linhas com erro ganham o botão "Ver erros".
 */
export function ResultTable({ report, strategy = {}, onStrategy, onStrategyMany, onShowErrors }) {
  const rows = entityRows(report);
  const editable = !!onStrategy;
  const [picked, setPicked] = useState([]);                 // entidades marcadas para o lote
  const [note, setNote] = useState('');
  const allPicked = rows.length > 0 && picked.length === rows.length;

  const toggle = (entity) => setPicked((cur) => (
    cur.includes(entity) ? cur.filter((e) => e !== entity) : [...cur, entity]));
  const toggleAll = () => setPicked(allPicked ? [] : rows.map((r) => r.entity));

  const bulk = (mode) => {
    const res = applyBulkStrategy(report, strategy, picked, mode);
    if (onStrategyMany) onStrategyMany(res.strategy);
    setNote(bulkResultText(res));
  };

  return html`
    <div class="flex flex-col gap-2">
      ${editable ? html`
        <div class="flex flex-wrap items-center gap-2 rounded-lg border border-wa-border bg-wa-panel px-3 py-2">
          <span class="text-[12px] text-wa-secondary">
            ${picked.length ? `${picked.length} marcada(s):` : 'Marque linhas para trocar várias de uma vez'}
          </span>
          ${STRATEGIES.map((k) => html`
            <button key=${k} type="button" disabled=${!picked.length} title=${STRATEGY_HINTS[k]}
              onClick=${() => bulk(k)}
              class="text-[12px] px-2 py-1 rounded-md border border-wa-border text-wa-text hover:bg-wa-hover disabled:opacity-40 cursor-pointer">
              ${STRATEGY_LABELS[k]}</button>`)}
          <button type="button" disabled=${!picked.length} onClick=${() => bulk('default')}
            class="text-[12px] px-2 py-1 rounded-md border border-wa-border text-wa-text hover:bg-wa-hover disabled:opacity-40 cursor-pointer">
            Padrão</button>
          ${note ? html`<span class="text-[12px] text-wa-secondary" role="status">${note}</span>` : null}
        </div>` : null}
      <div class="overflow-x-auto rounded-lg border border-wa-border">
        <table class="w-full text-[13px]">
          <thead class="bg-wa-panel text-wa-secondary">
            <tr>
              ${editable ? html`
                <th class="px-2 py-2 w-8">
                  <input type="checkbox" checked=${allPicked} onChange=${toggleAll} aria-label="Marcar todas as linhas" />
                </th>` : null}
              <th class="text-left px-3 py-2 font-medium">O quê</th>
              <th class="px-2 py-2 font-medium">Novos</th>
              <th class="px-2 py-2 font-medium">Alterados</th>
              <th class="px-2 py-2 font-medium" title=${SKIP_COLUMN_HINT}>${SKIP_COLUMN_LABEL}</th>
              <th class="px-2 py-2 font-medium">Com erro</th>
              ${editable ? html`<th class="text-left px-3 py-2 font-medium">${STRATEGY_COLUMN_LABEL}<${StrategyHelp} /></th>` : null}
            </tr>
          </thead>
          <tbody>
            ${rows.map((r) => {
              const fixed = isFixedStrategy(report, r.entity);
              const current = strategy[r.entity] || 'skip';
              return html`
              <tr key=${r.entity} class="border-t border-wa-border text-wa-text align-top">
                ${editable ? html`
                  <td class="px-2 py-2 text-center">
                    <input type="checkbox" checked=${picked.includes(r.entity)} onChange=${() => toggle(r.entity)}
                      aria-label=${`Marcar ${r.label}`} />
                  </td>` : null}
                <td class="px-3 py-2">${r.label}</td>
                <td class="px-2 py-2 text-center">${r.create}</td>
                <td class="px-2 py-2 text-center">${r.update}</td>
                <td class="px-2 py-2 text-center">${r.skip}</td>
                <td class=${`px-2 py-2 text-center ${r.error ? 'text-red-600 font-medium' : ''}`}>
                  ${r.error}
                  ${r.error && onShowErrors ? html`
                    <button type="button" onClick=${() => onShowErrors(r.entity)}
                      class="ml-1 text-[12px] underline text-wa-teal hover:opacity-80 cursor-pointer">Ver erros</button>` : null}
                </td>
                ${editable ? html`
                  <td class="px-3 py-2 min-w-[210px]">
                    ${fixed ? html`
                      <span class="block text-wa-text">${STRATEGY_LABELS.skip} (fixo)</span>
                      <span class="block text-[12px] text-wa-secondary">${FIXED_HINT}</span>` : html`
                      <select class="wa-field px-2 py-1 rounded-md text-[13px] w-full" value=${current}
                        title=${STRATEGY_HINTS[current] || ''}
                        onChange=${(e) => onStrategy(r.entity, e.target.value)}>
                        ${strategyChoices(report, r.entity).map((s) => html`<option key=${s} value=${s}>${STRATEGY_LABELS[s] || s}</option>`)}
                      </select>
                      <span class="block text-[12px] text-wa-secondary mt-1">${STRATEGY_HINTS[current] || ''}</span>`}
                  </td>` : null}
              </tr>`;
            })}
          </tbody>
        </table>
      </div>
    </div>`;
}
