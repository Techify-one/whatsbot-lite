import { h } from 'preact';
import { useState, useEffect, useCallback } from 'preact/hooks';
import htm from 'htm';
import { getGowaProxy, saveGowaProxy, testGowaProxy } from '../services/api.js';

const html = htm.bind(h);

const SCHEME_LABELS = {
  socks5: 'SOCKS5',
  http: 'HTTP',
  https: 'HTTPS',
};

const EMPTY_FORM = {
  enabled: false,
  mode: 'fields',
  scheme: 'socks5',
  host: '',
  port: '',
  username: '',
  password: '',
  url: '',
};

/**
 * Outbound proxy for the WhatsApp connection (GOWA >= 8.11.0).
 *
 * Two ways to fill it in, because proxy vendors hand out one or the other:
 * separate fields (IP + porta, optional usuário/senha) or a single URL.
 * Saving restarts GOWA — the WhatsApp socket only picks the proxy up on start.
 */
export function GowaProxySettings({ id, onNotify }) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [meta, setMeta] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);

  const applyPayload = useCallback((data) => {
    setMeta(data);
    setForm({
      enabled: !!data.enabled,
      mode: data.mode || 'fields',
      scheme: data.scheme || 'socks5',
      host: data.host || '',
      port: data.port === 0 ? '' : (data.port ?? ''),
      username: data.username || '',
      password: data.password || '',
      url: data.url || '',
    });
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getGowaProxy();
      if (res && res.ok) applyPayload(res.data);
      else if (onNotify) onNotify((res && res.error) || 'Falha ao ler a configuração de proxy.');
    } catch (e) {
      if (onNotify) onNotify('Falha ao ler a configuração de proxy.');
    } finally {
      setLoading(false);
    }
  }, [applyPayload, onNotify]);

  useEffect(() => { load(); }, []);

  function update(patch) {
    setTestResult(null);
    setForm((prev) => ({ ...prev, ...patch }));
  }

  function payload() {
    return {
      enabled: form.enabled,
      mode: form.mode,
      scheme: form.scheme,
      host: form.host,
      port: form.port === '' ? 0 : Number(form.port),
      username: form.username,
      password: form.password,
      url: form.url,
    };
  }

  async function handleTest() {
    setTesting(true);
    setTestResult(null);
    try {
      const res = await testGowaProxy(payload());
      if (res && res.ok) setTestResult(res.data);
      else setTestResult({ ok: false, message: (res && res.error) || 'Falha ao testar o proxy.' });
    } catch (e) {
      setTestResult({ ok: false, message: 'Falha ao testar o proxy.' });
    } finally {
      setTesting(false);
    }
  }

  async function handleSave() {
    setSaving(true);
    try {
      const res = await saveGowaProxy({ ...payload(), restart: true });
      if (res && res.ok) {
        applyPayload(res.data);
        setTestResult(null);
        if (onNotify) {
          if (res.data.error) onNotify(res.data.error);
          else if (res.data.restarted) onNotify('Proxy salvo. Reconectando o WhatsApp pelo proxy...');
          else onNotify('Proxy salvo.');
        }
      } else if (onNotify) {
        onNotify((res && res.error) || 'Falha ao salvar o proxy.');
      }
    } catch (e) {
      if (onNotify) onNotify('Falha ao salvar o proxy.');
    } finally {
      setSaving(false);
    }
  }

  const busy = saving || testing || loading;
  const unsupported = meta && meta.supported === false;
  const envLocked = !!(meta && meta.env_locked);

  return html`
    <div id=${id || undefined} class="p-3 bg-wa-panel rounded-lg border border-wa-border scroll-mt-4">
      <div class="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <label class="text-sm font-semibold text-wa-text">Proxy da conexão do WhatsApp</label>
          <p class="text-xs text-wa-secondary mt-1 max-w-xl">
            Faz o GOWA falar com o WhatsApp através de um proxy (SOCKS5, HTTP ou HTTPS).
            Útil para fixar o IP de saída ou operar atrás de uma rede restrita.
          </p>
        </div>
        <label class="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked=${!!form.enabled}
            disabled=${loading || envLocked}
            onChange=${(e) => update({ enabled: e.target.checked })}
            class="w-4 h-4 rounded border-wa-border accent-wa-teal"
          />
          <span class="text-sm text-wa-text">${form.enabled ? 'Ativado' : 'Desativado'}</span>
        </label>
      </div>

      ${envLocked ? html`
        <p class="text-xs text-amber-700 mt-3">
          O proxy está fixado pela variável de ambiente <span class="font-mono">WHATSBOT_GOWA_PROXY</span>
          e ela tem prioridade sobre o que for salvo aqui.
        </p>
      ` : null}

      ${unsupported ? html`
        <div class="mt-3 p-3 rounded-lg bg-amber-50 border border-amber-300 text-amber-800 text-xs leading-relaxed">
          <strong>Requer o GOWA ${meta.min_version} ou mais novo.</strong>
          ${meta.installed_version_known === false
            ? 'Não consegui identificar a versão instalada, então não dá para garantir que ela respeita o proxy.'
            : `A versão instalada é a ${meta.installed_version} e ela ignora a configuração de proxy.`}
          Atualize o GOWA no bloco acima e depois ative o proxy.
        </div>
      ` : null}

      ${form.enabled ? html`
        <div class="mt-4 flex flex-col gap-3">
          <div class="flex items-center gap-2 flex-wrap">
            <span class="text-xs text-wa-secondary">Como informar:</span>
            ${['fields', 'url'].map((mode) => html`
              <button
                key=${mode}
                type="button"
                onClick=${() => update({ mode })}
                class="px-3 py-1.5 text-xs rounded-lg border transition-colors ${form.mode === mode
                  ? 'bg-wa-teal border-wa-teal text-white'
                  : 'bg-wa-bg border-wa-border text-wa-text hover:bg-wa-hover'}"
              >
                ${mode === 'fields' ? 'IP e porta' : 'URL única'}
              </button>
            `)}
          </div>

          ${form.mode === 'fields' ? html`
            <div class="grid grid-cols-1 sm:grid-cols-4 gap-3">
              <div>
                <label class="block text-xs text-wa-secondary mb-1">Tipo</label>
                <select
                  class="wa-field w-full px-3 py-2 rounded-lg border border-wa-border text-sm"
                  value=${form.scheme}
                  onChange=${(e) => update({ scheme: e.target.value })}
                >
                  ${(meta?.schemes || ['socks5', 'http', 'https']).map((s) => html`
                    <option key=${s} value=${s}>${SCHEME_LABELS[s] || s}</option>
                  `)}
                </select>
              </div>
              <div class="sm:col-span-2">
                <label class="block text-xs text-wa-secondary mb-1">IP ou host</label>
                <input
                  type="text"
                  class="wa-field w-full px-3 py-2 rounded-lg border border-wa-border text-sm"
                  placeholder="123.45.67.89"
                  value=${form.host}
                  onInput=${(e) => update({ host: e.target.value })}
                />
              </div>
              <div>
                <label class="block text-xs text-wa-secondary mb-1">Porta</label>
                <input
                  type="number"
                  min="1"
                  max="65535"
                  class="wa-field w-full px-3 py-2 rounded-lg border border-wa-border text-sm"
                  placeholder="1080"
                  value=${form.port}
                  onInput=${(e) => update({ port: e.target.value })}
                />
              </div>
              <div class="sm:col-span-2">
                <label class="block text-xs text-wa-secondary mb-1">Usuário (opcional)</label>
                <input
                  type="text"
                  autocomplete="off"
                  class="wa-field w-full px-3 py-2 rounded-lg border border-wa-border text-sm"
                  placeholder="deixe vazio se o proxy não pedir"
                  value=${form.username}
                  onInput=${(e) => update({ username: e.target.value })}
                />
              </div>
              <div class="sm:col-span-2">
                <label class="block text-xs text-wa-secondary mb-1">Senha (opcional)</label>
                <input
                  type="password"
                  autocomplete="new-password"
                  class="wa-field w-full px-3 py-2 rounded-lg border border-wa-border text-sm"
                  placeholder="deixe vazio se o proxy não pedir"
                  value=${form.password}
                  onInput=${(e) => update({ password: e.target.value })}
                />
              </div>
            </div>
          ` : html`
            <div>
              <label class="block text-xs text-wa-secondary mb-1">URL do proxy</label>
              <input
                type="text"
                autocomplete="off"
                spellcheck="false"
                class="wa-field w-full px-3 py-2 rounded-lg border border-wa-border text-sm font-mono"
                placeholder="socks5://usuario:senha@123.45.67.89:1080"
                value=${form.url}
                onInput=${(e) => update({ url: e.target.value })}
              />
              <p class="text-xs text-wa-secondary mt-1">
                Aceita <span class="font-mono">socks5://</span>, <span class="font-mono">http://</span>
                e <span class="font-mono">https://</span>. Usuário e senha são opcionais.
              </p>
            </div>
          `}

          ${testResult ? html`
            <div class="p-3 rounded-lg text-xs leading-relaxed border ${testResult.ok
              ? 'bg-green-50 border-green-300 text-green-800'
              : 'bg-red-50 border-red-300 text-red-800'}">
              ${testResult.ok ? 'Proxy OK. ' : 'Proxy com problema. '}${testResult.message}
            </div>
          ` : null}
        </div>
      ` : null}

      <div class="mt-4 flex items-center gap-2 flex-wrap">
        ${form.enabled ? html`
          <button
            type="button"
            onClick=${handleTest}
            disabled=${busy}
            class="px-3 py-2 bg-wa-bg hover:bg-wa-hover disabled:opacity-50 text-wa-text text-sm rounded-lg border border-wa-border transition-colors"
          >
            ${testing ? 'Testando...' : 'Testar conexão'}
          </button>
        ` : null}
        <button
          type="button"
          onClick=${handleSave}
          disabled=${busy}
          class="px-4 py-2 bg-wa-teal hover:opacity-90 disabled:opacity-50 text-white text-sm font-medium rounded-lg transition-colors"
        >
          ${saving ? 'Salvando...' : 'Salvar proxy e reconectar'}
        </button>
        ${meta?.effective_url && form.enabled ? html`
          <span class="text-xs text-wa-secondary font-mono">em uso: ${meta.effective_url}</span>
        ` : null}
      </div>

      <p class="text-xs text-wa-secondary mt-2">
        Salvar reinicia o motor do WhatsApp: a conexão cai por alguns segundos e volta pelo proxy.
      </p>
    </div>
  `;
}
