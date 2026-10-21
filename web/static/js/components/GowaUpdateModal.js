import { h } from 'preact';
import { useState } from 'preact/hooks';
import htm from 'htm';

const html = htm.bind(h);

// Human labels for the phases emitted by gowa/updater.py.
const PHASE_LABELS = {
  downloading: 'Baixando...',
  verifying: 'Verificando integridade...',
  installing: 'Instalando...',
  restarting: 'Reiniciando o WhatsApp...',
  health_check: 'Conferindo se subiu...',
  rollback: 'Revertendo...',
};

export function GowaUpdateModal({
  // 'recommended': the WhatsBot-Lite release recommends this GOWA (decided
  // locally, never obligatory). 'compat': the release analyzer found a
  // WhatsApp compatibility reason in a newer upstream GOWA.
  mode = 'compat',
  latestVersion,
  installedVersion,
  installedKnown = true,
  supported = true,
  releaseUrl,
  riskLevel = 'medium',
  reason = '',
  evidence = [],
  installing = false,
  progress = null,
  onUpdateNow,
  onLater,
  onSkip,
}) {
  const [confirmUnsupported, setConfirmUnsupported] = useState(false);
  const recommended = mode === 'recommended';
  // An empty/unknown installed version is shown as such, never as "?" or a guess.
  const versionUnknown = !installedKnown || !installedVersion;

  const phase = progress?.phase || '';
  const phaseLabel = PHASE_LABELS[phase] || 'Atualizando...';
  const pct = phase === 'downloading' ? Number(progress?.progress || 0) : 100;
  // Only the download reports real percentages; the later phases are quick and
  // indeterminate, so they show a full bar with the phase name instead.
  const indeterminate = installing && phase !== 'downloading';

  function handleClose() {
    if (installing) return;
    onLater();
  }

  function handleUpdate() {
    if (!supported && !confirmUnsupported) {
      setConfirmUnsupported(true);
      return;
    }
    onUpdateNow(!supported);
  }

  return html`
    <div
      class="fixed inset-0 bg-black/50 z-[60] flex items-center justify-center p-4"
      onClick=${(e) => { if (e.target === e.currentTarget) handleClose(); }}
    >
      <div class="bg-wa-bg rounded-2xl shadow-2xl max-w-sm w-full p-6 relative">
        ${!installing ? html`
          <button
            onClick=${handleClose}
            class="absolute top-3 right-3 text-wa-secondary hover:text-wa-text transition-colors p-1 rounded"
            title="Fechar"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        ` : null}

        <div class="flex items-center gap-3 mb-3">
          <div class="w-10 h-10 rounded-full bg-wa-teal/10 flex items-center justify-center shrink-0">
            <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" class="text-wa-teal" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 0 1-9 9 9 9 0 0 1-6.36-2.64L3 21"/><path d="M3 12a9 9 0 0 1 9-9 9 9 0 0 1 6.36 2.64L21 3"/><polyline points="21 3 21 9 15 9"/><polyline points="3 21 3 15 9 15"/></svg>
          </div>
          <h2 class="text-base font-semibold text-wa-text">
            ${recommended ? 'Motor do WhatsApp recomendado' : 'Atualização recomendada do GOWA'}
          </h2>
        </div>

        ${recommended ? html`
          <p class="text-sm text-wa-text mb-3 leading-relaxed">
            O WhatsBot-Lite foi atualizado e recomenda a versão
            <span class="font-mono font-semibold text-wa-teal">${latestVersion}</span>
            do motor do WhatsApp (GOWA).
          </p>
        ` : null}

        <p class="text-sm text-wa-secondary mb-4">
          Instalada:
          ${versionUnknown ? html`
            <span class="font-semibold text-wa-text">não identificada</span>
          ` : html`
            <span class="font-mono font-semibold text-wa-text">${installedVersion}</span>
          `}
          <span class="mx-1.5">→</span>
          ${recommended ? 'Recomendada' : 'Disponível'}: <span class="font-mono font-semibold text-wa-teal">${latestVersion || '?'}</span>
        </p>

        ${recommended && versionUnknown ? html`
          <div class="mb-4 p-3 rounded-lg bg-amber-50 border border-amber-200 text-amber-800 text-xs leading-relaxed">
            Não consegui identificar a versão instalada. Se você colocou um
            arquivo mais novo à mão, instalar a recomendada pode trocá-lo por uma
            versão mais antiga. Se algo der errado, use o botão Reverter em
            Painel → Sistema → GOWA.
          </div>
        ` : null}

        ${recommended ? html`
          <p class="text-xs text-wa-secondary mb-4 leading-relaxed">
            É uma recomendação, não uma obrigação: você pode continuar com a versão
            atual. Trocar de versão pode mudar o comportamento do WhatsApp, e dá para
            voltar à anterior pelo botão Reverter.
          </p>
        ` : null}

        ${reason ? html`
          <div class="mb-4 p-3 rounded-lg bg-blue-50 border border-blue-200 text-blue-900 text-xs leading-relaxed">
            <strong>
              ${riskLevel === 'high' ? 'Compatibilidade importante:' : 'Motivo da recomendação:'}
            </strong>
            <span class="block mt-1">${reason}</span>
            ${Array.isArray(evidence) && evidence.length ? html`
              <ul class="mt-2 pl-4 list-disc text-blue-800">
                ${evidence.slice(0, 3).map(item => html`<li>${item}</li>`)}
              </ul>
            ` : null}
            <span class="block mt-2 text-blue-700">
              Isso pode reduzir riscos de incompatibilidade, mas não garante que o número não será bloqueado.
            </span>
          </div>
        ` : null}

        ${!supported ? html`
          <div class="mb-4 p-3 rounded-lg bg-amber-50 border border-amber-300 text-amber-800 text-xs leading-relaxed">
            <strong>Versão não homologada.</strong> O WhatsBot-Lite não foi testado com
            ela e a atualização pode quebrar o envio e o recebimento de mensagens.
            Se algo der errado, use o botão Reverter em Configurações.
          </div>
        ` : null}

        ${installing ? html`
          <div class="mb-4">
            <div class="flex items-center justify-between text-xs text-wa-secondary mb-1.5">
              <span>${phaseLabel}</span>
              ${!indeterminate ? html`<span class="font-mono">${pct}%</span>` : null}
            </div>
            <div class="w-full h-2 rounded-full bg-wa-panel overflow-hidden">
              <div
                class="h-full bg-wa-teal transition-all duration-300 ${indeterminate ? 'animate-pulse' : ''}"
                style=${`width: ${pct}%`}
              ></div>
            </div>
            <p class="text-xs text-wa-secondary mt-2">
              Não feche o painel. O WhatsApp reconecta sozinho ao final.
            </p>
          </div>
        ` : html`
          <div class="flex flex-col gap-2">
            <button
              onClick=${handleUpdate}
              class="w-full text-center py-2.5 px-4 ${confirmUnsupported ? 'bg-amber-600 hover:bg-amber-700' : 'bg-wa-teal hover:bg-wa-tealDark'} text-white font-medium rounded-lg transition-colors"
            >
              ${confirmUnsupported ? 'Sim, instalar mesmo assim' : 'Atualizar agora'}
            </button>
            <button
              onClick=${handleClose}
              class="w-full text-center py-2.5 px-4 bg-wa-panel hover:bg-wa-hover text-wa-text rounded-lg transition-colors"
            >
              Agora não
            </button>
          </div>

          <div class="flex items-center justify-between mt-4 text-xs">
            ${recommended ? html`<span></span>` : html`
              <button
                onClick=${() => onSkip(latestVersion)}
                class="text-wa-secondary hover:text-wa-text transition-colors underline"
              >
                Pular esta versão
              </button>
            `}
            ${releaseUrl ? html`
              <a
                href=${releaseUrl}
                target="_blank"
                rel="noopener noreferrer"
                class="text-wa-secondary hover:text-wa-text transition-colors"
              >
                Ver novidades
              </a>
            ` : null}
          </div>
        `}
      </div>
    </div>
  `;
}
