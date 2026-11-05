import { h } from 'preact';
import { useState } from 'preact/hooks';
import htm from 'htm';
import { login } from '../services/api.js';

const html = htm.bind(h);

export function LoginScreen({ onLogin }) {
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    if (!password.trim()) return;

    setLoading(true);
    setError('');

    try {
      const res = await login(password);
      if (res.ok) {
        localStorage.setItem('whatsbot_token', res.data.token);
        onLogin();
      } else {
        setError(res.error || 'Senha incorreta.');
      }
    } catch {
      setError('Erro de conexão.');
    }

    setLoading(false);
  }

  return html`
    <div class="h-dvh bg-wa-panel flex items-center justify-center">
      <div class="bg-wa-bg rounded-xl shadow-lg border border-wa-border p-8 w-full max-w-sm">
        <div class="text-center mb-6">
          <div class="w-16 h-16 mx-auto mb-3 bg-wa-teal rounded-full flex items-center justify-center">
            <svg viewBox="0 0 24 24" width="32" height="32" fill="white">
              <path d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1 1.71 0 3.1 1.39 3.1 3.1v2z"/>
            </svg>
          </div>
          <h1 class="text-xl font-semibold text-wa-text">Whatsbot-lite</h1>
          <p class="text-sm text-wa-secondary mt-1">Digite a senha para acessar o painel</p>
        </div>

        <form onSubmit=${handleSubmit}>
          <input
            type="password"
            value=${password}
            onInput=${(e) => setPassword(e.target.value)}
            placeholder="Senha"
            autofocus
            class="w-full bg-wa-panel text-wa-text px-4 py-3 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none mb-3"
          />

          ${error ? html`
            <p class="text-red-500 text-xs mb-3">${error}</p>
          ` : null}

          <button
            type="submit"
            disabled=${loading || !password.trim()}
            class="w-full py-3 bg-wa-teal hover:bg-wa-tealDark disabled:opacity-50 text-white font-medium rounded-lg transition-colors"
          >
            ${loading ? 'Entrando...' : 'Entrar'}
          </button>
        </form>
      </div>
    </div>
  `;
}
