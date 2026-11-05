import { h } from 'preact';
import { useState, useEffect, useRef } from 'preact/hooks';
import htm from 'htm';
import { fetchQrBlob, refreshQr, setupRequestKey, setupKeyStatus } from '../services/api.js';
import { formatPhone } from './QRCode.js';

const html = htm.bind(h);

const STEPS = ['Conectar', 'Chave de API', 'Agente de IA', 'Testar'];
const MAX_POLL_ATTEMPTS = 75; // ~2.5 min at 2s interval — beyond the Techify TTL
// Manual fallback: the user has to open WhatsApp and send by hand, so give them
// a much longer window before timing out (~10 min at 2s interval).
const MAX_MANUAL_POLL_ATTEMPTS = 300;

// Example agent prompt shown on step 3 — a simple snack bar, 4 instruction
// blocks, kept short so the user can read it at a glance.
const EXAMPLE_PROMPT = `Você é o atendente virtual da Lanchonete DigiBurger. Atenda de forma simpática, rápida e com linguagem informal.

# Cardápio e preços
X-Burguer R$ 18
X-Salada R$ 20
X-Tudo R$ 26
Batata frita (porção) R$ 15
Refrigerante lata R$ 6
Suco natural R$ 9

Quando perguntarem o cardápio, liste os itens com os preços.

# Como anotar um pedido
Pergunte o que a pessoa quer e a quantidade, confirme o pedido e pergunte se é entrega ou retirada. Para entrega, peça o endereço completo e informe a taxa de R$ 5.

# Horário de funcionamento
Funcionamos de todos os dias, das 18h às 23h. Fora desse horário, avise com educação e diga que retorna assim que a lanchonete abrir.

# Regras
Nunca invente itens ou preços fora do cardápio. Se não souber responder, diga que vai chamar um atendente humano.`;

function StepDots({ step }) {
  return html`
    <div class="flex items-center justify-center gap-2 mb-6">
      ${STEPS.map((label, i) => {
        const n = i + 1;
        const done = n < step;
        const active = n === step;
        return html`
          <div key=${n} class="flex items-center gap-2">
            <div class="flex items-center gap-1.5">
              <div class="w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold transition-colors ${
                done || active
                  ? 'bg-wa-teal text-white shadow-sm shadow-wa-teal/40'
                  : 'bg-wa-panel text-wa-secondary border-2 border-wa-border'}">
                ${done ? '✓' : n}
              </div>
              <span class="text-xs ${active ? 'text-wa-teal font-semibold' : 'text-wa-secondary'}">${label}</span>
            </div>
            ${n < STEPS.length ? html`<div class="w-5 h-0.5 rounded-full ${n < step ? 'bg-wa-teal' : 'bg-wa-border'}"></div>` : null}
          </div>
        `;
      })}
    </div>
  `;
}

export function SetupWizard({ status, qrAvailable, qrVersion, config, onComplete, onConfigSave, canClose, onClose }) {
  const [step, setStep] = useState(1);

  // ── Step 1: QR / connection ──────────────────────────────────────
  const [qrImgSrc, setQrImgSrc] = useState(null);
  const [qrImgError, setQrImgError] = useState(false);
  const advancedRef = useRef(false);

  useEffect(() => {
    if (step !== 1 || status.connected) return;
    if (qrAvailable && qrVersion) {
      let cancelled = false;
      fetchQrBlob().then(url => {
        if (!cancelled && url) {
          setQrImgSrc(prev => { if (prev) URL.revokeObjectURL(prev); return url; });
          setQrImgError(false);
        }
      });
      return () => { cancelled = true; };
    }
  }, [step, status.connected, qrAvailable, qrVersion]);

  useEffect(() => () => { if (qrImgSrc) URL.revokeObjectURL(qrImgSrc); }, []);

  // Auto-advance to step 2 once WhatsApp is connected.
  useEffect(() => {
    if (step === 1 && status.connected && !advancedRef.current) {
      advancedRef.current = true;
      const t = setTimeout(() => setStep(2), 1200);
      return () => clearTimeout(t);
    }
  }, [step, status.connected]);

  // ── Step 2: API key provisioning ─────────────────────────────────
  // keyState: 'idle' | 'requesting' | 'polling' | 'manual' | 'ready' | 'error'
  const [keyState, setKeyState] = useState('idle');
  const [keyError, setKeyError] = useState('');
  // Manual-fallback payload (set when WhatsApp blocks the bot's send with the
  // reach-out timelock): { provision_number, provision_message, wa_link, qr_data_uri }.
  const [manualData, setManualData] = useState(null);
  const [msgCopied, setMsgCopied] = useState(false);
  const pollAttemptsRef = useRef(0);
  const hasKey = !!(config && config.openrouter_api_key && config.openrouter_api_key.length > 0);

  async function startProvisioning() {
    setKeyError('');
    setManualData(null);
    setKeyState('requesting');
    let res;
    try { res = await setupRequestKey(); } catch (e) { res = null; }
    if (!res || !res.ok) {
      setKeyError((res && res.error) || 'Não foi possível solicitar a chave. Tente novamente.');
      setKeyState('error');
      return;
    }
    pollAttemptsRef.current = 0;
    // The bot couldn't send (WhatsApp reach-out timelock): switch to the manual
    // flow. Polling is already armed server-side, so the key still arrives on
    // its own once the user sends the message from their phone.
    if (res.data && res.data.status === 'manual') {
      setManualData(res.data);
      setKeyState('manual');
      return;
    }
    setKeyState('polling');
  }

  function copyProvisionMessage() {
    const txt = manualData && manualData.provision_message;
    if (!txt) return;
    navigator.clipboard.writeText(txt).then(() => {
      setMsgCopied(true);
      setTimeout(() => setMsgCopied(false), 2000);
    });
  }

  // Poll the backend every 2s while waiting for the key — both for the
  // automatic ('polling') and the manual ('manual') flows.
  useEffect(() => {
    if (keyState !== 'polling' && keyState !== 'manual') return;
    const manual = keyState === 'manual';
    const cap = manual ? MAX_MANUAL_POLL_ATTEMPTS : MAX_POLL_ATTEMPTS;
    let stopped = false;
    const tick = async () => {
      pollAttemptsRef.current += 1;
      let res;
      try { res = await setupKeyStatus(); } catch (e) { res = null; }
      if (stopped) return;
      const st = res && res.ok && res.data ? res.data.status : 'error';
      if (st === 'ready') {
        setKeyState('ready');
      } else if (st === 'expired' && !manual) {
        setKeyError('A solicitação expirou. Toque para tentar de novo.');
        setKeyState('error');
      } else if (st === 'error' && !manual) {
        setKeyError('Não conseguimos receber a chave. Toque para tentar de novo.');
        setKeyState('error');
      } else if (pollAttemptsRef.current >= cap) {
        // In manual mode the user may simply not have sent yet — keep the
        // instructions on screen instead of bouncing them to a hard error.
        if (manual) {
          setKeyError('Ainda não recebemos sua mensagem. Envie pelo WhatsApp e aguarde, ou pule por agora.');
        } else {
          setKeyError('A chave não chegou a tempo. Toque para tentar de novo.');
          setKeyState('error');
        }
      }
    };
    const timer = setInterval(tick, 2000);
    return () => { stopped = true; clearInterval(timer); };
  }, [keyState]);

  // Once the key is ready, turn on the AI agent automatically, show the
  // success state briefly, then advance to the agent-prompt step.
  useEffect(() => {
    if (keyState !== 'ready') return;
    if (onConfigSave) onConfigSave({ auto_reply: true });
    const t = setTimeout(() => setStep(3), 1800);
    return () => clearTimeout(t);
  }, [keyState]);

  // ── Step 3: agent prompt ─────────────────────────────────────────
  const [agentPrompt, setAgentPrompt] = useState((config && config.system_prompt) || '');
  const [showExample, setShowExample] = useState(false);

  function saveAgentPrompt() {
    const txt = agentPrompt.trim();
    if (txt && onConfigSave) onConfigSave({ system_prompt: txt });
    setStep(4);
  }

  // ── Step 4: test link ────────────────────────────────────────────
  const [copied, setCopied] = useState(false);
  const phone = status.bot_phone || '';
  const waLink = phone ? `https://wa.me/${phone}?text=Oi` : '';

  function handleCopy() {
    if (!waLink) return;
    navigator.clipboard.writeText(waLink).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  // ── Render helpers ───────────────────────────────────────────────
  const btnPrimary = 'px-5 py-2.5 rounded-lg text-sm font-semibold bg-wa-teal hover:bg-wa-tealDark text-white shadow-md shadow-wa-teal/30 transition-colors disabled:opacity-60 disabled:cursor-not-allowed';
  const btnGhost = 'px-5 py-2.5 rounded-lg text-sm font-medium border-2 border-wa-border bg-wa-bg hover:bg-wa-hover text-wa-text transition-colors';

  function renderStep1() {
    return html`
      <div class="flex flex-col items-center text-center">
        <h2 class="text-xl font-bold text-wa-text mb-1">Conecte seu WhatsApp</h2>
        <p class="text-sm text-wa-secondary mb-4">
          Escaneie o código abaixo para o Whatsbot-lite atender no seu número.
        </p>
        <div class="w-[248px] h-[248px] flex items-center justify-center bg-wa-panel border-2 border-wa-border rounded-2xl overflow-hidden mb-3">
          ${status.connected ? html`
            <div class="text-center">
              <div class="text-5xl mb-2 text-wa-teal">✓</div>
              <div class="text-wa-teal font-semibold">Conectado!</div>
              ${status.bot_name ? html`<div class="text-sm text-wa-text font-medium mt-1">${status.bot_name}</div>` : null}
              ${status.bot_phone ? html`<div class="text-xs text-wa-secondary">${formatPhone(status.bot_phone)}</div>` : null}
            </div>
          ` : qrAvailable && qrImgSrc && !qrImgError ? html`
            <img
              src=${qrImgSrc}
              alt="QR Code"
              class="qr-image w-full h-full object-contain"
              onError=${() => setQrImgError(true)}
            />
          ` : html`
            <div class="text-center text-wa-secondary">
              <div class="animate-pulse-slow text-lg mb-1">...</div>
              <span class="text-sm">Aguardando QR Code...</span>
            </div>
          `}
        </div>
        ${status.connected ? html`
          <p class="text-sm text-wa-teal font-medium">Avançando...</p>
        ` : html`
          <div class="text-xs text-wa-secondary leading-relaxed">
            No celular: <span class="text-wa-text font-medium">Configurações → Aparelhos conectados → Conectar um aparelho</span>
          </div>
          <button onClick=${() => refreshQr()} class="text-wa-teal hover:text-wa-tealDark text-xs font-medium underline mt-2 transition-colors">
            Atualizar QR Code
          </button>
        `}
      </div>
    `;
  }

  function renderStep2() {
    if (keyState === 'ready') {
      return html`
        <div class="flex flex-col items-center text-center">
          <div class="text-5xl mb-2 text-wa-teal">✓</div>
          <h2 class="text-xl font-bold text-wa-text mb-1">Chave de API criada!</h2>
          <p class="text-sm text-wa-secondary mb-2">
            Sua conta foi criada com <span class="text-wa-teal font-semibold">1 dólar de crédito grátis</span> para começar.
          </p>
          <p class="text-sm text-wa-teal font-semibold bg-wa-teal/10 px-4 py-2 rounded-lg">
            Agente de IA ativado — já vai responder suas mensagens.
          </p>
        </div>
      `;
    }
    if (keyState === 'requesting' || keyState === 'polling') {
      return html`
        <div class="flex flex-col items-center text-center py-2">
          <div class="animate-pulse-slow text-4xl mb-3">⏳</div>
          <h2 class="text-xl font-bold text-wa-text mb-1">
            ${keyState === 'requesting' ? 'Enviando solicitação...' : 'Criando sua conta...'}
          </h2>
          <p class="text-sm text-wa-secondary">
            ${keyState === 'requesting'
              ? 'Pedindo sua chave de API pelo WhatsApp.'
              : 'Gerando sua chave de API e o crédito de 1 dólar. Isso leva alguns segundos.'}
          </p>
        </div>
      `;
    }
    if (keyState === 'manual') {
      const m = manualData || {};
      return html`
        <div class="flex flex-col items-center text-center">
          <h2 class="text-xl font-bold text-wa-text mb-1">Falta só um passo</h2>
          <p class="text-sm text-wa-secondary mb-3 max-w-md leading-relaxed">
            O WhatsApp bloqueou o envio automático para um contato novo (proteção
            anti-spam). É só você mesmo enviar a mensagem abaixo pelo seu WhatsApp —
            assim que enviar, sua chave de API é criada e configurada sozinha.
          </p>

          <div class="w-full max-w-md grid sm:grid-cols-2 gap-4 items-start text-left">
            <div class="flex flex-col gap-3">
              <div class="rounded-xl border-2 border-wa-border bg-wa-panel p-3">
                <div class="text-xs text-wa-secondary mb-0.5">Enviar para</div>
                <div class="text-sm font-semibold text-wa-text">
                  ${m.provision_number ? formatPhone(m.provision_number) : '...'}
                </div>
              </div>
              <div class="rounded-xl border-2 border-wa-border bg-wa-panel p-3">
                <div class="flex items-center justify-between mb-1">
                  <div class="text-xs text-wa-secondary">Mensagem (exatamente assim)</div>
                  <button
                    type="button"
                    onClick=${copyProvisionMessage}
                    class="text-xs font-semibold ${msgCopied ? 'text-green-600' : 'text-wa-teal hover:text-wa-tealDark'} transition-colors"
                  >
                    ${msgCopied ? '✓ Copiada' : 'Copiar'}
                  </button>
                </div>
                <div class="text-sm text-wa-text bg-wa-bg rounded-lg px-3 py-2 border border-wa-border">
                  ${m.provision_message || ''}
                </div>
              </div>
              ${m.wa_link ? html`
                <a
                  href=${m.wa_link}
                  target="_blank"
                  rel="noopener noreferrer"
                  class="w-full text-center py-3 rounded-xl text-sm font-bold text-white bg-wa-teal hover:bg-wa-tealDark shadow-md shadow-wa-teal/30 transition-colors"
                >
                  Abrir no WhatsApp e enviar
                </a>
              ` : null}
            </div>

            <div class="flex flex-col items-center gap-2">
              ${m.qr_data_uri ? html`
                <div class="bg-white p-2 rounded-xl border-2 border-wa-border">
                  <img src=${m.qr_data_uri} alt="QR Code para enviar a mensagem" class="w-40 h-40 object-contain" />
                </div>
                <p class="text-xs text-wa-secondary leading-snug text-center">
                  Ou aponte a câmera do celular para este código — ele abre o
                  WhatsApp com a mensagem pronta.
                </p>
              ` : null}
            </div>
          </div>

          <div class="mt-4 flex items-center gap-2 text-sm text-wa-teal font-medium">
            <span class="animate-pulse-slow">⏳</span>
            <span>Aguardando sua mensagem e a chave de API chegar...</span>
          </div>
          ${keyError ? html`
            <div class="mt-2 text-xs text-wa-secondary max-w-md">${keyError}</div>
          ` : null}
        </div>
      `;
    }
    // 'idle' or 'error'
    return html`
      <div class="flex flex-col items-center text-center">
        <h2 class="text-xl font-bold text-wa-text mb-1">Criar conta e Ganhar Chave de API</h2>
        <p class="text-sm text-wa-teal font-semibold bg-wa-teal/10 px-3 py-1 rounded-full mb-3">+ 1 dólar de crédito grátis</p>
        ${phone ? html`
          <p class="text-xs text-wa-secondary mb-3">
            Vinculada ao seu WhatsApp:
            <span class="text-wa-text font-semibold">${formatPhone(phone)}</span>
          </p>
        ` : null}
        <p class="text-sm text-wa-secondary mb-4 leading-relaxed max-w-md">
          Ao tocar no botão, seu WhatsApp envia uma mensagem para o nosso agente de IA de controle,
          que cria sua conta, configura a chave de API e libera 1 dólar de crédito automaticamente.
          Você não precisa fazer mais nada.
        </p>
        ${keyState === 'error' && keyError ? html`
          <div class="w-full max-w-md mb-3 px-3 py-2 rounded-lg bg-red-50 border-2 border-red-200 text-red-600 text-sm font-medium">
            ${keyError}
          </div>
        ` : null}
        ${hasKey && keyState === 'idle' ? html`
          <div class="w-full max-w-md mb-3 px-3 py-2 rounded-lg bg-green-50 border-2 border-green-200 text-green-700 text-sm font-medium">
            Você já tem uma chave de API configurada.
          </div>
        ` : null}
      </div>
    `;
  }

  function renderStep3() {
    return html`
      <div class="flex flex-col text-left">
        <div class="text-center mb-3">
          <h2 class="text-xl font-bold text-wa-text mb-1">Descreva seu agente de IA</h2>
          <p class="text-sm text-wa-secondary">
            Escreva quem é o seu atendente, o que você oferece e como ele deve responder.
          </p>
        </div>
        <div class="flex items-center justify-between mb-2">
          <span class="text-sm font-semibold text-wa-text">Instruções do agente</span>
          <button
            type="button"
            onClick=${() => setShowExample(v => !v)}
            class="inline-flex items-center gap-1 px-3 py-1 rounded-full text-xs font-semibold bg-wa-teal/10 text-wa-teal hover:bg-wa-teal/20 transition-colors"
          >
            ${showExample ? '✕ Ocultar exemplo' : '💡 Ver exemplo (lanchonete)'}
          </button>
        </div>
        ${showExample ? html`
          <div class="mb-3 rounded-xl border-2 border-wa-teal/30 bg-wa-teal/5 p-3">
            <pre class="m-0 text-xs leading-relaxed text-wa-text whitespace-pre-wrap font-mono max-h-52 overflow-auto">${EXAMPLE_PROMPT}</pre>
            <button
              type="button"
              onClick=${() => { setAgentPrompt(EXAMPLE_PROMPT); setShowExample(false); }}
              class="mt-3 px-3 py-1.5 rounded-lg text-xs font-semibold border-2 border-wa-teal/50 bg-wa-bg text-wa-teal hover:bg-wa-teal/10 transition-colors"
            >
              Usar este exemplo
            </button>
          </div>
        ` : null}
        <textarea
          value=${agentPrompt}
          onInput=${e => setAgentPrompt(e.currentTarget.value)}
          placeholder="Ex: Você é o atendente da Pizzaria do Bairro. Seja simpático e objetivo. Liste os sabores e preços quando perguntarem o cardápio..."
          class="w-full h-[44dvh] min-h-[200px] resize-none rounded-xl border-2 border-wa-border bg-wa-panel p-4 text-sm leading-relaxed text-wa-text placeholder:text-gray-400 shadow-inner focus:outline-none focus:bg-white focus:border-wa-teal focus:ring-4 focus:ring-wa-teal/20 transition-colors"
        ></textarea>
        <p class="text-xs text-wa-secondary mt-2">
          Dica: divida em blocos com títulos (ex: <span class="font-mono text-wa-text"># Cardápio</span>) — fica mais fácil de o agente seguir.
        </p>
      </div>
    `;
  }

  function renderStep4() {
    return html`
      <div class="flex flex-col items-center text-center">
        <div class="text-5xl mb-2">🎉</div>
        <h2 class="text-xl font-bold text-wa-text mb-1">Tudo pronto!</h2>
        <p class="text-sm text-wa-secondary mb-1">
          Copie o link abaixo e abra no seu WhatsApp para me mandar um “oi”.
        </p>
        <p class="text-xs text-wa-secondary mb-4">
          Número conectado:
          <span class="text-wa-text font-semibold">${phone ? formatPhone(phone) : 'carregando...'}</span>
        </p>
        <button
          onClick=${handleCopy}
          disabled=${!waLink}
          class="w-full max-w-lg py-4 rounded-xl text-base font-bold text-white shadow-lg transition-colors disabled:opacity-60 disabled:cursor-not-allowed ${
            copied ? 'bg-green-600 hover:bg-green-600' : 'bg-wa-teal hover:bg-wa-tealDark shadow-wa-teal/40'}"
        >
          ${copied ? '✓ Link copiado!' : 'Copiar link de contato'}
        </button>
        ${waLink ? html`
          <a href=${waLink} target="_blank" rel="noopener noreferrer" class="text-wa-teal hover:text-wa-tealDark text-xs font-medium underline mt-3">
            ou abrir no WhatsApp agora
          </a>
        ` : null}
      </div>
    `;
  }

  function renderFooter() {
    if (step === 1) {
      return html`
        <div class="flex flex-col items-center gap-2">
          <div class="text-xs text-wa-secondary">A próxima etapa abre sozinha após conectar.</div>
          <button onClick=${onComplete} class="text-wa-secondary hover:text-wa-text text-xs font-medium underline transition-colors">
            Pular configuração e ir para o chat
          </button>
        </div>
      `;
    }
    if (step === 2) {
      if (keyState === 'idle') {
        return html`
          <div class="flex items-center gap-2">
            ${hasKey ? html`
              <button onClick=${() => setStep(3)} class=${btnGhost}>Pular</button>
              <button onClick=${startProvisioning} class=${btnPrimary}>Gerar nova chave</button>
            ` : html`
              <button onClick=${startProvisioning} class=${btnPrimary}>Criar minha conta e receber a chave</button>
            `}
          </div>
        `;
      }
      if (keyState === 'error') {
        return html`
          <div class="flex items-center gap-2">
            <button onClick=${() => setStep(3)} class=${btnGhost}>Pular por agora</button>
            <button onClick=${startProvisioning} class=${btnPrimary}>Tentar novamente</button>
          </div>
        `;
      }
      if (keyState === 'manual') {
        return html`
          <div class="flex items-center gap-2">
            <button onClick=${() => setStep(3)} class=${btnGhost}>Pular por agora</button>
            <button onClick=${startProvisioning} class=${btnGhost}>Tentar pelo bot de novo</button>
          </div>
        `;
      }
      // requesting / polling / ready — no action
      return html`<div class="text-xs text-wa-secondary">Aguarde um instante...</div>`;
    }
    if (step === 3) {
      return html`
        <div class="flex items-center gap-2">
          <button onClick=${() => setStep(4)} class=${btnGhost}>Pular</button>
          <button onClick=${saveAgentPrompt} class=${btnPrimary}>Salvar e continuar</button>
        </div>
      `;
    }
    // step 4
    return html`<button onClick=${onComplete} class=${btnPrimary}>Concluir</button>`;
  }

  return html`
    <div class="h-dvh w-full overflow-y-auto bg-gradient-to-br from-wa-teal/15 via-wa-panel to-wa-tealDark/15">
      <div class="min-h-full flex items-center justify-center p-4">
      <div class="bg-wa-bg rounded-2xl shadow-2xl ring-1 ring-black/5 max-w-4xl w-full relative overflow-hidden my-auto">
        <div class="h-1.5 w-full bg-gradient-to-r from-wa-teal to-wa-tealDark"></div>
        <div class="p-6 sm:p-8">
          ${canClose ? html`
            <button
              onClick=${onClose}
              class="absolute top-4 right-4 text-wa-secondary hover:text-wa-text hover:bg-wa-hover transition-colors p-1.5 rounded-lg"
              title="Fechar"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          ` : null}

          <div class="text-center mb-1">
            <h1 class="text-2xl font-bold text-wa-teal">Bem-vindo ao Whatsbot-lite</h1>
            <p class="text-sm text-wa-secondary">Vamos configurar em 4 passos rápidos</p>
          </div>
          <div class="mt-5">
            <${StepDots} step=${step} />
          </div>

          <div class="min-h-[300px] flex flex-col justify-center">
            ${step === 1 ? renderStep1()
              : step === 2 ? renderStep2()
              : step === 3 ? renderStep3()
              : renderStep4()}
          </div>

          <div class="mt-6 pt-5 border-t-2 border-wa-border flex items-center justify-center">
            ${renderFooter()}
          </div>
        </div>
      </div>
      </div>
    </div>
  `;
}
