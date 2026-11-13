// Popup de confirmação ANTES de iniciar a importação. Mostra o que será substituído (a opção mais
// destrutiva), as opções ligadas e, se a prévia estiver desatualizada, um aviso extra. O botão final só
// habilita com "Revisei e quero iniciar" — e o servidor também exige esse "revisei" (`acknowledged`).

import { h } from 'preact';
import { useEffect, useState } from 'preact/hooks';
import htm from 'htm';
import { STRATEGY_LABELS } from '../../utils/portabilityLogic.js';

const html = htm.bind(h);

export function ConfirmImportModal({
  overwrites = [], options = [], stale = false, busy = false, onConfirm, onCancel,
}) {
  const [ack, setAck] = useState(false);
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onCancel(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  return html`
    <div class="fixed inset-0 bg-black/50 z-[60] flex items-center justify-center p-4"
      onClick=${(e) => { if (e.target === e.currentTarget) onCancel(); }}>
      <div class="bg-wa-bg rounded-2xl shadow-2xl max-w-lg w-full max-h-[90vh] overflow-y-auto p-6 flex flex-col gap-3"
        role="alertdialog" aria-modal="true" aria-label="Confirmar importação">
        <h3 class="text-base font-semibold text-wa-text">Confira antes de importar</h3>
        <p class="text-[13px] text-wa-text">
          Revise tudo abaixo antes de iniciar. A importação grava no banco e não tem “desfazer”.
        </p>

        ${stale ? html`
          <p class="text-[13px] text-amber-800 bg-amber-500/10 border border-amber-500/30 rounded-md py-2 px-3" role="alert">
            Os números da tabela <strong>não refletem suas últimas trocas</strong>. Use “Recalcular prévia” para
            conferir, ou continue sabendo que os totais podem mudar.
          </p>` : null}

        ${overwrites.length ? html`
          <div class="rounded-md border border-red-500/40 bg-red-500/10 py-2 px-3">
            <p class="text-[13px] font-medium text-red-700">Serão substituídos pelo do pacote (${STRATEGY_LABELS.overwrite}):</p>
            <ul class="list-disc pl-5 mt-1">
              ${overwrites.map((o) => html`<li key=${o.entity} class="text-[13px] text-red-700">${o.label}</li>`)}
            </ul>
          </div>` : html`
          <p class="text-[13px] text-wa-secondary">Nada será substituído: só o que falta é criado ou completado.</p>`}

        ${options.length ? html`
          <div class="rounded-md border border-wa-border py-2 px-3">
            <p class="text-[13px] font-medium text-wa-text">Opções ligadas:</p>
            <ul class="list-disc pl-5 mt-1">
              ${options.map((o) => html`<li key=${o} class="text-[13px] text-wa-text">${o}</li>`)}
            </ul>
          </div>` : null}

        <p class="text-[13px] text-wa-text bg-wa-panel border border-wa-border rounded-md py-2 px-3">
          As conversas importadas entram <strong>sem notificação</strong>: não aparecem como não lidas, a IA não
          responde a elas e nenhum plugin é avisado.
        </p>

        <label class="flex items-start gap-2 text-[13px] text-wa-text cursor-pointer">
          <input type="checkbox" class="mt-1" checked=${ack} onChange=${(e) => setAck(e.target.checked)} />
          <span>Revisei e quero iniciar</span>
        </label>
        <div class="flex gap-2 justify-end">
          <button type="button" onClick=${onCancel}
            class="px-4 py-2 rounded-lg text-sm text-wa-text hover:bg-wa-hover cursor-pointer">Voltar</button>
          <button type="button" onClick=${onConfirm} disabled=${!ack || busy}
            class="bg-wa-teal text-white text-sm font-medium rounded-lg py-2 px-4 hover:opacity-90 disabled:opacity-50 cursor-pointer">
            ${busy ? 'Iniciando…' : 'Iniciar importação'}</button>
        </div>
      </div>
    </div>`;
}
