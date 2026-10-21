import { h } from 'preact';
import { useState, useEffect, useCallback } from 'preact/hooks';
import htm from 'htm';
import {
  checkGowaUpdate,
  installGowaUpdate,
  rollbackGowa,
} from '../services/api.js';
import { createWebSocket } from '../services/websocket.js';
import { GowaProxySettings } from './GowaProxySettings.js';

const html = htm.bind(h);

const PHASE_LABELS = {
  downloading: 'Baixando...',
  verifying: 'Verificando integridade...',
  installing: 'Instalando...',
  restarting: 'Reiniciando o WhatsApp...',
  health_check: 'Conferindo se subiu...',
  rollback: 'Revertendo...',
};

const SOURCE_LABELS = {
  bundled: 'do instalador',
  managed: 'atualizada pelo painel',
  env: 'caminho customizado',
};

/**
 * GOWA (WhatsApp engine) version + update card.
 *
 * The auto-check toggle is owned by ConfigPanel so it rides along with the
 * single "Salvar Configurações" button; everything else is local state.
 */
export function GowaSettings({ id, autoCheck, onAutoCheckChange, onNotify }) {
  const [info, setInfo] = useState(null);
  const [checking, setChecking] = useState(false);
  const [working, setWorking] = useState(false);
  const [progress, setProgress] = useState(null);
  const [confirmUnsupported, setConfirmUnsupported] = useState(false);
  const [confirmRollback, setConfirmRollback] = useState(false);

  const load = useCallback(async (force = false) => {
    setChecking(true);
    try {
      const res = await checkGowaUpdate(force);
      if (res && res.ok) setInfo(res.data);
      else if (onNotify) onNotify((res && res.error) || 'Falha ao consultar o GOWA.');
    } catch (e) {
      if (onNotify) onNotify('Falha ao consultar o GOWA.');
    } finally {
      setChecking(false);
    }
  }, [onNotify]);

  useEffect(() => { load(false); }, []);

  // Own WS connection for progress, like DatabaseSettings does for migrations.
  // The install can also be triggered from the main-screen modal, so this card
  // has to follow along even when it did not start the update itself.
  useEffect(() => {
    const ws = createWebSocket({
      gowa_update_progress: (d) => { setWorking(true); setProgress(d); },
      gowa_update_done: (d) => {
        setWorking(false);
        setProgress(null);
        setConfirmUnsupported(false);
        setConfirmRollback(false);
        if (onNotify && d && d.message) onNotify(d.message);
        load(false);
      },
    });
    return () => ws.close();
  }, []);

  async function handleUpdate() {
    if (!info) return;
    if (!info.latest_supported && !confirmUnsupported) {
      setConfirmUnsupported(true);
      return;
    }
    setWorking(true);
    setProgress({ phase: 'downloading', progress: 0 });
    try {
      const res = await installGowaUpdate(info.latest_version, !info.latest_supported);
      if (!res || !res.ok) {
        if (onNotify) onNotify((res && res.error) || 'Falha ao atualizar o GOWA.');
        setWorking(false);
        setProgress(null);
      }
    } catch (e) {
      if (onNotify) onNotify('Falha ao atualizar o GOWA.');
      setWorking(false);
      setProgress(null);
    }
  }

  // Installs the version this WhatsBot-Lite release recommends (the validated
  // one), not GitHub's latest. Same endpoint, same lock, same rollback.
  async function handleInstallRecommended() {
    if (!info || !info.recommended_version) return;
    setWorking(true);
    setProgress({ phase: 'downloading', progress: 0 });
    try {
      const res = await installGowaUpdate(info.recommended_version, false);
      if (!res || !res.ok) {
        if (onNotify) onNotify((res && res.error) || 'Falha ao atualizar o GOWA.');
        setWorking(false);
        setProgress(null);
      }
    } catch (e) {
      if (onNotify) onNotify('Falha ao atualizar o GOWA.');
      setWorking(false);
      setProgress(null);
    }
  }

  async function handleRollback() {
    if (!confirmRollback) {
      setConfirmRollback(true);
      return;
    }
    setWorking(true);
    setProgress({ phase: 'rollback', progress: 100 });
    try {
      const res = await rollbackGowa();
      if (!res || !res.ok) {
        if (onNotify) onNotify((res && res.error) || 'Falha ao reverter o GOWA.');
        setWorking(false);
        setProgress(null);
      }
    } catch (e) {
      if (onNotify) onNotify('Falha ao reverter o GOWA.');
      setWorking(false);
      setProgress(null);
    }
  }

  const installedKnown = info?.installed_version_known !== false;
  const installed = info ? (info.installed_version || '') : '...';
  const recommendedVersion = info?.recommended_version || '';
  // Only worth showing when it differs from "Última" (otherwise it is noise).
  const showRecommended = !!(
    info?.recommended_update_available && info?.recommended_supported
    && recommendedVersion && recommendedVersion !== (info?.latest_version || '')
  );
  const latest = info?.latest_version || '';
  const updateAvailable = !!info?.update_available;
  const whatsappRecommended = info?.whatsapp_update_recommended === true;
  const latestSupported = info?.latest_supported !== false;
  const platformOk = info?.platform_supported !== false;
  const phase = progress?.phase || '';
  const pct = phase === 'downloading' ? Number(progress?.progress || 0) : 100;
  const indeterminate = working && phase !== 'downloading';

  return html`
    <div id=${id || undefined} class="bg-wa-bg rounded-xl p-5 border border-wa-border shadow-sm scroll-mt-4">
      <h3 class="text-xs font-semibold text-wa-secondary uppercase tracking-wider mb-4">
        GOWA (motor do WhatsApp)
      </h3>

      <div class="flex flex-col gap-4">
        <div id="gowa-version" class="p-3 bg-wa-panel rounded-lg border border-wa-border scroll-mt-4">
          <div class="flex items-start justify-between gap-4 flex-wrap">
            <div>
              <label class="text-sm font-semibold text-wa-text">Versão do GOWA</label>
              <div class="flex items-center gap-3 mt-1.5 flex-wrap">
                <span class="text-xs text-wa-secondary">
                  Instalada:
                  ${info && !installedKnown ? html`
                    <span class="font-semibold text-amber-700" title="Sem como saber qual versão do GOWA está em uso">não identificada</span>
                  ` : html`
                    <span class="font-mono font-semibold text-wa-text">${installed}</span>
                  `}
                </span>
                ${info?.source ? html`
                  <span class="text-[11px] px-2 py-0.5 rounded-full bg-wa-secondary/20 text-wa-secondary">
                    ${SOURCE_LABELS[info.source] || info.source}
                  </span>
                ` : null}
                ${showRecommended ? html`
                  <span class="text-xs text-wa-secondary">
                    Recomendada nesta versão: <span class="font-mono font-semibold text-blue-600">${recommendedVersion}</span>
                  </span>
                ` : null}
                ${latest ? html`
                  <span class="text-xs text-wa-secondary">
                    Última: <span class="font-mono font-semibold ${updateAvailable ? 'text-blue-600' : 'text-green-600'}">${latest}</span>
                  </span>
                ` : null}
                ${!checking && latest && !updateAvailable && installedKnown && !info?.recommended_update_available ? html`
                  <span class="text-xs text-green-600 font-medium">Atualizado</span>
                ` : null}
                ${updateAvailable && latestSupported ? html`
                  <span class="text-xs text-blue-600 font-medium">Nova versão disponível</span>
                ` : null}
                ${updateAvailable && latestSupported && whatsappRecommended ? html`
                  <span class="text-[11px] px-2 py-0.5 rounded-full bg-amber-100 text-amber-800 font-medium">
                    Recomendada por compatibilidade
                  </span>
                ` : null}
                ${updateAvailable && !latestSupported ? html`
                  <span class="text-xs text-amber-700 font-medium">Não homologada</span>
                ` : null}
              </div>
            </div>

            <div class="flex items-center gap-2">
              <button
                onClick=${() => load(true)}
                disabled=${checking || working}
                class="px-3 py-2 bg-wa-panel hover:bg-wa-hover disabled:opacity-50 text-wa-text text-sm rounded-lg border border-wa-border transition-colors"
                title="Verificar atualizações do GOWA"
              >
                ${checking ? '...' : 'Verificar'}
              </button>
              ${showRecommended && platformOk && !(updateAvailable && recommendedVersion === latest) ? html`
                <button
                  onClick=${handleInstallRecommended}
                  disabled=${working}
                  class="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-medium rounded-lg transition-colors whitespace-nowrap"
                  title="Versão validada para este WhatsBot-Lite"
                >
                  ${working ? 'Atualizando...' : `Instalar a recomendada (${recommendedVersion})`}
                </button>
              ` : null}
              ${updateAvailable && platformOk ? html`
                <button
                  onClick=${handleUpdate}
                  disabled=${working}
                  class="px-4 py-2 ${confirmUnsupported ? 'bg-amber-600 hover:bg-amber-700' : 'bg-blue-600 hover:bg-blue-700'} disabled:opacity-50 text-white text-sm font-medium rounded-lg transition-colors whitespace-nowrap"
                >
                  ${working ? 'Atualizando...' : confirmUnsupported ? 'Instalar mesmo assim' : `Atualizar para ${latest}`}
                </button>
              ` : null}
            </div>
          </div>

          ${info?.error ? html`
            <p class="text-xs text-amber-700 mt-3">${info.error}</p>
          ` : null}

          ${info?.rate_limited ? html`
            <p class="text-xs text-wa-secondary mt-3">
              Limite de consultas do GitHub atingido; mostrando o último resultado conhecido.
            </p>
          ` : null}

          ${!platformOk ? html`
            <p class="text-xs text-amber-700 mt-3">
              Atualização automática indisponível em ${info?.platform || 'nesta plataforma'}:
              o GOWA não publica binário para esta combinação de sistema e arquitetura.
            </p>
          ` : null}

          ${updateAvailable && !latestSupported ? html`
            <div class="mt-3 p-3 rounded-lg bg-amber-50 border border-amber-300 text-amber-800 text-xs leading-relaxed">
              <strong>Versão ${latest} fora da faixa homologada</strong> (${info?.supported_range}).
              O WhatsBot-Lite não foi testado com ela e a atualização pode quebrar o envio
              e o recebimento de mensagens. Se algo der errado, use Reverter abaixo.
            </div>
          ` : null}

          ${updateAvailable && latestSupported && info?.update_reason ? html`
            <div class="mt-3 p-3 rounded-lg ${whatsappRecommended ? 'bg-blue-50 border-blue-200 text-blue-900' : 'bg-wa-panel border-wa-border text-wa-secondary'} border text-xs leading-relaxed">
              <strong>${whatsappRecommended ? 'Análise de compatibilidade:' : 'Análise automática:'}</strong>
              <span class="block mt-1">${info.update_reason}</span>
              ${info?.assessment_error ? html`
                <span class="block mt-1 text-amber-700">
                  A LLM não pôde concluir a análise; foram aplicadas somente as regras locais.
                </span>
              ` : null}
            </div>
          ` : null}

          ${working ? html`
            <div class="mt-3">
              <div class="flex items-center justify-between text-xs text-wa-secondary mb-1.5">
                <span>${PHASE_LABELS[phase] || 'Atualizando...'}</span>
                ${!indeterminate ? html`<span class="font-mono">${pct}%</span>` : null}
              </div>
              <div class="w-full h-2 rounded-full bg-wa-bg overflow-hidden">
                <div
                  class="h-full bg-wa-teal transition-all duration-300 ${indeterminate ? 'animate-pulse' : ''}"
                  style=${`width: ${pct}%`}
                ></div>
              </div>
            </div>
          ` : null}

          ${info?.can_rollback && !working ? html`
            <div class="mt-3 pt-3 border-t border-wa-border flex items-center justify-between gap-3 flex-wrap">
              <span class="text-xs text-wa-secondary">
                Versão anterior disponível: <span class="font-mono text-wa-text">${info.backup_version}</span>
              </span>
              <button
                onClick=${handleRollback}
                class="px-3 py-1.5 text-xs rounded-lg border transition-colors ${confirmRollback ? 'bg-red-600 border-red-600 text-white hover:bg-red-700' : 'bg-wa-panel border-wa-border text-red-600 hover:bg-wa-hover'}"
              >
                ${confirmRollback ? 'Confirmar reversão' : `Reverter para ${info.backup_version}`}
              </button>
            </div>
          ` : null}
        </div>

        <${GowaProxySettings} id="gowa-proxy" onNotify=${onNotify} />

        <label id="gowa-auto-update" class="flex items-start gap-2 cursor-pointer scroll-mt-4">
          <input
            type="checkbox"
            checked=${!!autoCheck}
            onChange=${(e) => onAutoCheckChange(e.target.checked)}
            class="w-4 h-4 mt-0.5 rounded border-wa-border accent-wa-teal"
          />
          <span>
            <span class="text-sm text-wa-text">Verificar atualizações do GOWA diariamente</span>
            <span class="block text-xs text-wa-secondary">
              O alerta automático só aparece quando as notas indicam mudanças relevantes
              para compatibilidade com o WhatsApp. Outras versões continuam disponíveis aqui.
            </span>
          </span>
        </label>
      </div>
    </div>
  `;
}
