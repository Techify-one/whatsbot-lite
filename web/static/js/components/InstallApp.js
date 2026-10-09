import { h } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';
import htm from 'htm';
import { getInstallState, subscribeInstall, promptInstall } from '../pwa.js';

const html = htm.bind(h);

export function InstallApp() {
  const [state, setState] = useState(getInstallState);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const dialog = useRef(null);
  useEffect(() => subscribeInstall(setState), []);

  async function install() {
    if (busy) return;
    setBusy(true);
    const result = await promptInstall();
    setBusy(false);
    setMessage(result === 'accepted'
      ? 'Pedido aceito. Aguarde a confirmação do navegador.'
      : result === 'dismissed'
        ? 'Instalação cancelada. Você pode continuar usando pelo navegador.'
        : 'Use o menu do navegador para instalar, se essa opção estiver disponível.');
  }

  return html`
    <button type="button" class="pwa-menu-button" onClick=${() => { setMessage(''); dialog.current.showModal(); }}>
      <span aria-hidden="true">↓</span>
      ${state.installed ? 'App instalado' : 'Instalar no celular'}
    </button>
    <dialog ref=${dialog} class="pwa-dialog" aria-labelledby="pwa-install-title">
      <h2 id="pwa-install-title">${state.installed ? 'WhatsBot-Lite instalado' : 'Instalar WhatsBot-Lite'}</h2>
      ${state.installed
        ? html`<p>${state.standalone ? 'Você já está usando o app instalado.' : 'Instalação concluída. Abra o WhatsBot-Lite pelo ícone na tela inicial.'}</p>`
        : !state.secure
          ? html`<p>Abra o endereço HTTPS do seu WhatsBot-Lite para instalar no celular. Um endereço HTTP da rede local não permite a instalação completa.</p>`
          : html`
              ${state.ios
                ? html`<p>No iPhone ou iPad, abra este endereço no Safari, toque em Compartilhar e em <strong>Adicionar à Tela de Início</strong>. Se aparecer, mantenha <strong>Abrir como App da Web</strong> ativado e toque em Adicionar.</p>`
                : html`<p>No Android, abra este endereço no Chrome. Toque em <strong>Instalar agora</strong> quando disponível, ou use o menu do navegador → <strong>Instalar app</strong> / <strong>Adicionar à tela inicial</strong>.</p>`}
              ${state.canPrompt ? html`<button type="button" class="pwa-primary" disabled=${busy} onClick=${install}>${busy ? 'Aguarde…' : 'Instalar agora'}</button>` : null}
              ${state.registrationFailed ? html`<p>Não foi possível preparar a instalação. Verifique a conexão e recarregue a página.</p>` : null}
            `}
      <p class="pwa-note">É preciso ter internet e manter o servidor WhatsBot-Lite ligado. A instalação não transfere o bot para o celular nem habilita notificações com o app fechado. O login pode ser solicitado novamente.</p>
      <p role="status" aria-live="polite">${message}</p>
      <form method="dialog"><button class="pwa-close">Fechar</button></form>
    </dialog>
  `;
}
