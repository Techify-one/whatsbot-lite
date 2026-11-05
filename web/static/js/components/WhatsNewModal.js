import { h } from 'preact';
import htm from 'htm';

const html = htm.bind(h);

function releaseItems(description) {
  return String(description || '')
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(line => line && !/^#+\s*(o que mudou|novidades)?\s*$/i.test(line))
    .map(line => line.replace(/^[-*•]\s*/, ''))
    .filter(Boolean);
}

export function WhatsNewModal({
  currentVersion,
  version,
  description,
  updating = false,
  error = '',
  onUpdate,
  onLater,
  onNever,
}) {
  const items = releaseItems(description);
  return html`
    <div
      class="fixed inset-0 bg-black/60 z-[60] flex items-center justify-center p-4"
      onClick=${(e) => { if (!updating && e.target === e.currentTarget) onLater(); }}
    >
      <div class="bg-wa-bg border border-wa-border rounded-2xl shadow-2xl max-w-lg w-full p-6 max-h-[85vh] flex flex-col">
        <div class="flex items-center gap-3 mb-4">
          <div class="w-11 h-11 rounded-full bg-blue-600/10 flex items-center justify-center shrink-0">
            <svg xmlns="http://www.w3.org/2000/svg" width="23" height="23" viewBox="0 0 24 24" fill="none" stroke="currentColor" class="text-blue-600" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 0 1-15.3 6.4L3 16"/><path d="M3 21v-5h5"/><path d="M3 12A9 9 0 0 1 18.3 5.6L21 8"/><path d="M21 3v5h-5"/></svg>
          </div>
          <div>
            <h2 class="text-lg font-semibold text-wa-text">Nova versão disponível</h2>
            <p class="text-sm text-wa-secondary">
              ${currentVersion ? html`v${currentVersion} → ` : ''}<span class="font-mono font-semibold text-blue-600">v${version}</span>
            </p>
          </div>
        </div>

        <div class="overflow-y-auto pr-1 flex-1">
          <p class="text-sm font-semibold text-wa-text mb-2">O que mudou:</p>
          ${items.length ? html`
            <ul class="space-y-2 text-sm text-wa-text">
              ${items.map(item => html`<li class="flex gap-2"><span class="text-blue-600 font-bold">•</span><span>${item}</span></li>`)}
            </ul>
          ` : html`<p class="text-sm text-wa-secondary">Esta versão contém melhorias e correções do Whatsbot-lite.</p>`}
        </div>

        ${error ? html`
          <div class="mt-4 p-3 rounded-lg border border-red-300 bg-red-50 text-sm text-red-700">${error}</div>
        ` : null}

        <div class="mt-5 grid grid-cols-1 sm:grid-cols-3 gap-2">
          <button
            onClick=${onNever}
            disabled=${updating}
            class="px-3 py-2.5 border border-wa-border bg-wa-panel hover:bg-wa-hover disabled:opacity-50 text-wa-secondary text-sm font-medium rounded-lg transition-colors"
          >Nunca avisar</button>
          <button
            onClick=${onLater}
            disabled=${updating}
            class="px-3 py-2.5 border border-wa-border bg-wa-panel hover:bg-wa-hover disabled:opacity-50 text-wa-text text-sm font-medium rounded-lg transition-colors"
          >Agora não</button>
          <button
            onClick=${onUpdate}
            disabled=${updating}
            class="px-3 py-2.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-60 text-white text-sm font-semibold rounded-lg transition-colors"
          >${updating ? 'Atualizando...' : 'Atualizar agora'}</button>
        </div>
        <p class="mt-3 text-[11px] leading-relaxed text-wa-secondary text-center">
          “Agora não” oculta somente a versão v${version}. Uma próxima versão será avisada novamente.
        </p>
      </div>
    </div>
  `;
}
