import { h } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';
import htm from 'htm';
import {
  getInstallState, subscribeInstall, promptInstall,
  installPromptDue, markInstallPromptShown, dismissInstallPrompt,
} from '../pwa.js';

const html = htm.bind(h);
const APP_NAME = 'Whatsbot-lite';

/**
 * Pop-up shown when the app is opened in a phone browser (never inside the installed
 * app, never on a computer). Once per session; "Agora não" snoozes it for 7 days.
 * Browsers cannot launch an installed PWA from script, so when it is already installed
 * we only say so and point to the home-screen icon.
 */
export function InstallPrompt() {
  const [state, setState] = useState(getInstallState);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const dialog = useRef(null);
  const decided = useRef(false);
  useEffect(() => subscribeInstall(setState), []);

  // Wait briefly: beforeinstallprompt / getInstalledRelatedApps resolve after first paint.
  useEffect(() => {
    if (decided.current || !installPromptDue(state)) return undefined;
    const timer = setTimeout(() => {
      const latest = getInstallState();
      if (decided.current || !installPromptDue(latest)) return;
      decided.current = true;
      markInstallPromptShown();
      setOpen(true);
    }, 2500);
    return () => clearTimeout(timer);
  }, [state.mobile, state.standalone, state.secure]);

  useEffect(() => {
    const el = dialog.current;
    if (!el) return;
    if (open && !el.open) el.showModal();
    if (!open && el.open) el.close();
  }, [open]);

  if (state.standalone || !state.mobile) return null;

  const already = state.installed || state.installedElsewhere;
  const later = () => { dismissInstallPrompt(); setOpen(false); };
  async function install() {
    if (busy) return;
    setBusy(true);
    const result = await promptInstall();
    setBusy(false);
    if (result !== 'unavailable') setOpen(false);
  }

  return html`
    <dialog ref=${dialog} class="pwa-dialog" aria-labelledby="pwa-prompt-title" onCancel=${later} onClose=${() => setOpen(false)}>
      <h2 id="pwa-prompt-title">${already ? `${APP_NAME} já está instalado` : `Instalar o ${APP_NAME}`}</h2>
      ${already
        ? html`<p>Abra o app pelo ícone <strong>${APP_NAME}</strong> na tela inicial do celular: ele abre em tela cheia, sem a barra do navegador.</p>`
        : state.ios
          ? html`<p>Toque em Compartilhar no Safari e depois em <strong>Adicionar à Tela de Início</strong> para usar como app.</p>`
          : state.canPrompt
            ? html`<p>Use o ${APP_NAME} como um app, em tela cheia e com ícone na tela inicial.</p>`
            : html`<p>No menu do navegador, toque em <strong>Instalar app</strong> ou <strong>Adicionar à tela inicial</strong>.</p>`}
      <div class="pwa-actions">
        ${!already && state.canPrompt
          ? html`<button type="button" class="pwa-primary" disabled=${busy} onClick=${install}>${busy ? 'Aguarde…' : 'Instalar agora'}</button>`
          : null}
        <button type="button" class="pwa-close" onClick=${later}>${already ? 'Entendi' : 'Agora não'}</button>
      </div>
    </dialog>
  `;
}
