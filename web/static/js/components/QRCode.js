import { h } from 'preact';
import { useState, useEffect, useRef } from 'preact/hooks';
import htm from 'htm';
import { fetchQrBlob, reconnect, logout, refreshQr } from '../services/api.js';

const html = htm.bind(h);

export function formatPhone(phone) {
  if (!phone) return '';
  if (phone.length === 13 && phone.startsWith('55')) {
    return `+${phone.slice(0, 2)} (${phone.slice(2, 4)}) ${phone.slice(4, 9)}-${phone.slice(9)}`;
  }
  if (phone.length === 12 && phone.startsWith('55')) {
    return `+${phone.slice(0, 2)} (${phone.slice(2, 4)}) ${phone.slice(4, 8)}-${phone.slice(8)}`;
  }
  return `+${phone}`;
}

export function ConnectionStatus({ connected, botPhone, botName, gowaVersion, gowaVersionKnown = true, onOpenQR }) {
  const [copied, setCopied] = useState(false);

  function handleCopyLink() {
    if (!botPhone) return;
    const link = `https://wa.me/${botPhone}?text=Oi`;
    navigator.clipboard.writeText(link).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  return html`
    <div class="rounded-xl px-4 py-3 flex items-center justify-between border shadow-sm ${connected ? 'bg-green-50 border-green-200' : 'bg-red-50 border-red-200'}">
      <div class="flex items-center gap-3">
        <span class="w-2.5 h-2.5 rounded-full ${connected ? 'bg-green-500' : 'bg-red-500 animate-pulse'}"></span>
        <div class="flex items-center gap-2 text-sm">
          <span class="font-semibold ${connected ? 'text-green-700' : 'text-red-700'}">
            ${connected ? 'Conectado' : 'Desconectado'}
          </span>
          ${connected && botName ? html`
            <span class="text-wa-secondary">·</span>
            <span class="text-wa-text font-medium">${botName}</span>
          ` : null}
          ${gowaVersion ? html`
            <span class="text-wa-secondary">·</span>
            <span class="text-wa-secondary text-xs font-mono" title="Versão do GOWA, o motor do WhatsApp">
              GOWA v${gowaVersion}
            </span>
          ` : !gowaVersionKnown ? html`
            <span class="text-wa-secondary">·</span>
            <span class="text-amber-700 text-xs" title="Não foi possível identificar a versão do GOWA, o motor do WhatsApp">
              GOWA: versão não identificada
            </span>
          ` : null}
          ${connected && botPhone ? html`
            <span class="text-wa-secondary text-xs">${formatPhone(botPhone)}</span>
            <button
              onClick=${handleCopyLink}
              class="px-2 py-0.5 text-xs rounded border transition-colors ${copied ? 'bg-green-100 border-green-300 text-green-700' : 'bg-wa-bg border-wa-border text-wa-secondary hover:text-wa-text hover:bg-wa-panel'}"
              title="Copiar link wa.me"
            >
              ${copied ? 'Copiado!' : 'Copiar Link'}
            </button>
          ` : null}
        </div>
      </div>
      <div class="flex items-center gap-2">
        <button
          onClick=${() => reconnect()}
          class="px-3 py-1.5 bg-wa-teal hover:bg-wa-tealDark text-white text-xs rounded-lg transition-colors"
        >
          Reconectar
        </button>
        <button
          onClick=${() => logout()}
          class="px-3 py-1.5 bg-red-500 hover:bg-red-600 text-white text-xs rounded-lg transition-colors"
        >
          Desconectar
        </button>
        <button
          onClick=${onOpenQR}
          class="px-3 py-1.5 border border-wa-border bg-wa-bg hover:bg-wa-panel text-wa-text text-xs rounded-lg transition-colors flex items-center gap-1.5"
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>
          QR Code
        </button>
      </div>
    </div>
  `;
}

export function QRCodeModal({ connected, qrAvailable, qrVersion, botPhone, botName, onClose }) {
  const [imgSrc, setImgSrc] = useState(null);
  const [imgError, setImgError] = useState(false);

  // Update QR image only when version changes (backend cached, ~20s per QR)
  useEffect(() => {
    if (!connected && qrAvailable && qrVersion) {
      let cancelled = false;
      fetchQrBlob().then(url => {
        if (!cancelled && url) { setImgSrc(url); setImgError(false); }
      });
      return () => { cancelled = true; };
    } else if (connected) {
      setImgSrc(prev => { if (prev) URL.revokeObjectURL(prev); return null; });
    }
  }, [connected, qrAvailable, qrVersion]);

  return html`
    <div
      class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4"
      onClick=${(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div class="bg-wa-bg rounded-2xl shadow-2xl max-w-sm w-full p-6 flex flex-col items-center relative">
        <!-- Close button -->
        <button
          onClick=${onClose}
          class="absolute top-3 right-3 text-wa-secondary hover:text-wa-text transition-colors p-1 rounded"
          title="Fechar"
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>

        <h2 class="text-sm font-semibold text-wa-text mb-3">WhatsApp QR Code</h2>

        <div class="w-[220px] h-[220px] flex items-center justify-center bg-wa-panel rounded-lg mb-3 overflow-hidden">
          ${connected ? html`
            <div class="text-center">
              <div class="text-4xl mb-2">\u2713</div>
              <span class="text-green-600 font-medium">Conectado!</span>
              ${botPhone ? html`
                <div class="mt-2 text-sm text-wa-secondary">
                  ${botName ? html`<div class="font-medium text-wa-text">${botName}</div>` : ''}
                  <div class="text-xs text-wa-secondary">${formatPhone(botPhone)}</div>
                </div>
              ` : ''}
            </div>
          ` : qrAvailable && imgSrc && !imgError ? html`
            <img
              src=${imgSrc}
              alt="QR Code"
              class="qr-image w-full h-full object-contain"
              style="background:#ffffff"
              onError=${() => setImgError(true)}
            />
          ` : html`
            <div class="text-center text-wa-secondary">
              <div class="animate-pulse-slow text-lg mb-1">...</div>
              <span class="text-sm">Aguardando QR Code...</span>
            </div>
          `}
        </div>

        ${connected ? html`
          <span class="text-green-600 text-sm font-medium">
            Conectado ao WhatsApp${botPhone ? html` · <span class="text-wa-secondary font-normal">${formatPhone(botPhone)}</span>` : ''}
          </span>
        ` : qrAvailable ? html`
          <span class="text-yellow-600 text-sm mb-1">Escaneie o QR Code com seu celular</span>
          <button
            onClick=${() => refreshQr()}
            class="text-wa-teal hover:text-wa-tealDark text-xs underline mb-2 transition-colors"
          >
            Atualizar QR Code
          </button>
        ` : html`
          <span class="text-red-500 text-sm">Desconectado</span>
        `}
      </div>
    </div>
  `;
}
