import { h } from 'preact';
import { useState, useEffect, useRef } from 'preact/hooks';
import htm from 'htm';
import { ConnectionStatus, QRCodeModal } from './QRCode.js';
import { ConfigPanel } from './ConfigPanel.js';
import { getGowaVersion } from '../services/api.js';

const html = htm.bind(h);

export function Dashboard({ status, qrAvailable, qrVersion, config, saving, onSave, onNotify, onReopenSetup }) {
  const connected = status?.connected || false;
  const [showQR, setShowQR] = useState(!connected);
  const [gowaVersion, setGowaVersion] = useState('');
  const [gowaVersionKnown, setGowaVersionKnown] = useState(true);
  const userDismissedQR = useRef(false);
  const prevConnected = useRef(connected);

  // Auto-open QR modal when disconnected (unless user dismissed it)
  useEffect(() => {
    // Reset dismiss flag when transitioning from connected to disconnected
    if (prevConnected.current && !connected) {
      userDismissedQR.current = false;
      setShowQR(true);
    }
    // Auto-close when connected
    if (connected) {
      setShowQR(false);
    }
    prevConnected.current = connected;
  }, [connected]);

  // Auto-open on first load if not connected
  useEffect(() => {
    if (!connected && !userDismissedQR.current) {
      setShowQR(true);
    }
  }, []);

  // Version chip next to the connection status, so the GOWA version is visible
  // at the top of /painel without scrolling down to the GOWA card.
  useEffect(() => {
    let alive = true;
    getGowaVersion()
      .then(res => {
        if (!alive || !res || !res.ok) return;
        setGowaVersion(res.data.installed_version || '');
        setGowaVersionKnown(res.data.installed_version_known !== false);
      })
      .catch(() => { /* ignore */ });
    return () => { alive = false; };
  }, []);

  function handleCloseQR() {
    userDismissedQR.current = true;
    setShowQR(false);
  }

  return html`
    <div class="flex flex-col gap-4">
      <${ConnectionStatus}
        connected=${connected}
        botPhone=${status?.bot_phone || ''}
        botName=${status?.bot_name || ''}
        gowaVersion=${gowaVersion}
        gowaVersionKnown=${gowaVersionKnown}
        onOpenQR=${() => setShowQR(true)}
      />

      ${showQR ? html`
        <${QRCodeModal}
          connected=${connected}
          qrAvailable=${qrAvailable}
          qrVersion=${qrVersion}
          botPhone=${status?.bot_phone || ''}
          botName=${status?.bot_name || ''}
          onClose=${handleCloseQR}
        />
      ` : null}

      <${ConfigPanel}
        config=${config}
        saving=${saving}
        onSave=${onSave}
        onNotify=${onNotify}
      />

      ${onReopenSetup ? html`
        <div class="flex justify-end">
          <button
            onClick=${onReopenSetup}
            class="px-4 py-2.5 text-[14px] rounded-lg border border-wa-border hover:bg-wa-hover transition-colors flex items-center gap-2 text-wa-text bg-wa-bg"
          >
            <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M19 3h-4.18C14.4 1.84 13.3 1 12 1c-1.3 0-2.4.84-2.82 2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-7 0c.55 0 1 .45 1 1s-.45 1-1 1-1-.45-1-1 .45-1 1-1zm-2 14l-4-4 1.41-1.41L10 14.17l6.59-6.59L18 9l-8 8z"/></svg>
            Refazer configuração
          </button>
        </div>
      ` : null}
    </div>
  `;
}
