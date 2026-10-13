import { h } from 'preact';
import { useState } from 'preact/hooks';
import htm from 'htm';
import { downloadMessageMedia } from '../../services/api.js';

const html = htm.bind(h);

const NOUN = {
  image: 'Foto',
  sticker: 'Figurinha',
  video: 'Vídeo',
  audio: 'Áudio',
  document: 'Documento',
};

// "removido" must agree with the noun ("foto removida", "vídeo removido").
const REMOVED = {
  image: 'Foto removida',
  sticker: 'Figurinha removida',
  video: 'Vídeo removido',
  audio: 'Áudio removido',
  document: 'Documento removido',
};

const ICON = {
  image: '🖼️',
  sticker: '🖼️',
  video: '🎬',
  audio: '🎧',
  document: '📄',
};

// Shown instead of a file the app did not keep (download policy) or that was
// deleted later from "Arquivos". One click is one attempt: WhatsApp only keeps
// a file for a while, so an old one may no longer be there.
export function MediaPlaceholder({ phone, message, readOnly = false }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const kind = message.media_type;
  const noun = NOUN[kind] || 'Arquivo';
  const removed = message.media_state === 'removed';
  const canTry = !readOnly && !!message.msg_id;

  async function download(e) {
    e.stopPropagation();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      const res = await downloadMessageMedia(phone, message.msg_id);
      if (!res || !res.ok) setError((res && res.error) || 'Não foi possível baixar agora.');
    } catch {
      setError('Não foi possível baixar agora.');
    } finally {
      setBusy(false);
    }
  }

  return html`
    <div class="flex flex-col gap-1 mb-1 min-w-[190px] max-w-[280px] rounded-[6px] border border-wa-border bg-wa-hover px-3 py-2">
      <div class="flex items-center gap-2 text-[13px] text-wa-text">
        <span aria-hidden="true" class="text-[18px] leading-none">${ICON[kind] || '📎'}</span>
        <span class="font-medium">
          ${removed ? `${REMOVED[kind] || 'Arquivo removido'} para liberar espaço` : `${noun} não baixado`}
        </span>
      </div>
      ${canTry ? html`
        <button
          type="button"
          onClick=${download}
          disabled=${busy}
          class="self-start text-[12.5px] font-medium text-wa-teal underline disabled:opacity-60 disabled:no-underline"
          title="Se o WhatsApp ainda tiver o arquivo, ele será baixado."
        >${busy ? 'Baixando...' : 'Baixar'}</button>
        <span class="text-[11px] leading-[14px] text-wa-secondary">
          Se o WhatsApp ainda tiver o arquivo, ele será baixado.
        </span>
      ` : null}
      ${error ? html`<span class="text-[12px] leading-[15px] text-red-600" role="alert">${error}</span>` : null}
    </div>
  `;
}
