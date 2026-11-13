// Exportar: escolher o que levar → (opcional) senha para os segredos → gerar → baixar.
//
// Segredos (a chave do LLM) só viajam CIFRADOS com a senha que o operador digita aqui. Sem a senha o
// pacote sai sem eles; no destino basta informar de novo. A senha não é guardada em lugar nenhum além do
// estado do formulário, e é apagada assim que o job é criado.

import { h } from 'preact';
import { useState } from 'preact/hooks';
import htm from 'htm';
import { createExport, downloadExport } from '../../services/portabilityApi.js';
import {
  MIN_PASSWORD_LEN, SCOPES, buildExportRequest, exportSummary, formatBytes, isPasswordRequired,
} from '../../utils/portabilityLogic.js';
import { JobProgress } from './JobProgress.js';

const html = htm.bind(h);

export function ExportWizard({ onChanged, onPasswordRequired }) {
  const [scopes, setScopes] = useState(['settings', 'catalog']);
  const [withSecrets, setWithSecrets] = useState(false);
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [withMedia, setWithMedia] = useState(false);
  const [withAvatars, setWithAvatars] = useState(false);
  const [mediaFrom, setMediaFrom] = useState('');
  const [mediaTo, setMediaTo] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [jobId, setJobId] = useState('');
  const [result, setResult] = useState(null);
  const [downloading, setDownloading] = useState(false);

  const toggle = (id) => setScopes((cur) => (cur.includes(id) ? cur.filter((s) => s !== id) : [...cur, id]));

  const start = async (e) => {
    e.preventDefault();
    setError('');
    const req = buildExportRequest({
      scopes, includeSecrets: withSecrets, password, confirm,
      includeMedia: withMedia && scopes.includes('conversations'), mediaFrom, mediaTo,
      includeAvatars: withAvatars,
    });
    if (req.error) { setError(req.error); return; }
    setBusy(true);
    try {
      const res = await createExport(req.body);
      if (isPasswordRequired(res)) { if (onPasswordRequired) onPasswordRequired(); return; }
      if (!res || !res.ok) { setError((res && res.error) || 'Não foi possível iniciar a exportação.'); return; }
      setPassword(''); setConfirm('');           // a senha não fica na tela
      setResult(null);
      setJobId(res.data.id);
    } finally { setBusy(false); }
  };

  const finished = (job) => {
    if (job.state === 'succeeded') setResult(job);
    if (onChanged) onChanged();
  };

  const download = async () => {
    setDownloading(true);
    setError('');
    try {
      const res = await downloadExport(jobId);
      if (!res.ok) setError(res.error || 'Falha ao baixar o pacote.');
    } finally { setDownloading(false); }
  };

  const reset = () => { setJobId(''); setResult(null); setError(''); };
  const summary = result ? exportSummary(result.report) : null;

  return html`
    <section class="flex flex-col gap-4">
      <div>
        <h4 class="text-sm font-semibold text-wa-text">Exportar</h4>
        <p class="text-[13px] text-wa-secondary mt-1">
          Gera um pacote (.zip) para levar a outra instalação do WhatsBot-Lite. O pacote fica disponível por 24 horas.
        </p>
      </div>

      ${jobId ? html`
        <${JobProgress} jobId=${jobId} title="Gerando o pacote" onDone=${finished} />
        ${result ? html`
          <div class="rounded-lg border border-wa-border bg-wa-panel p-4 flex flex-col gap-3">
            <p class="text-sm text-wa-text">
              Pacote pronto: <strong>${summary.total.toLocaleString('pt-BR')} registros</strong>,
              ${formatBytes(summary.bytes)}.
              ${summary.secrets ? 'Os segredos estão cifrados com a senha que você definiu.' : 'Sem segredos.'}
            </p>
            ${summary.secrets ? html`
              <p class="text-[12px] text-amber-700 bg-amber-500/10 border border-amber-500/30 rounded-md py-2 px-3">
                Guarde a senha: sem ela os segredos do pacote não podem ser recuperados.
              </p>` : null}
            ${error ? html`<p class="text-[13px] text-red-600">${error}</p>` : null}
            <div class="flex flex-wrap gap-2">
              <button type="button" onClick=${download} disabled=${downloading}
                class="bg-wa-teal text-white text-sm font-medium rounded-lg py-2 px-4 hover:opacity-90 disabled:opacity-50 cursor-pointer">
                ${downloading ? 'Baixando…' : 'Baixar pacote'}
              </button>
              <button type="button" onClick=${reset}
                class="px-4 py-2 rounded-lg text-sm text-wa-text hover:bg-wa-hover cursor-pointer">Nova exportação</button>
            </div>
          </div>` : null}
        ${!result ? html`
          <div><button type="button" onClick=${reset}
            class="text-[12px] text-wa-secondary hover:text-wa-text underline cursor-pointer">Voltar</button></div>` : null}
      ` : html`
        <form onSubmit=${start} class="flex flex-col gap-4">
          <fieldset class="flex flex-col gap-2">
            <legend class="text-sm font-medium text-wa-text mb-1">O que exportar</legend>
            ${SCOPES.map((s) => html`
              <label key=${s.id} class="flex items-start gap-3 cursor-pointer">
                <input type="checkbox" class="mt-1" checked=${scopes.includes(s.id)} onChange=${() => toggle(s.id)} />
                <span class="min-w-0">
                  <span class="block text-sm text-wa-text">${s.label}</span>
                  <span class="block text-[12px] text-wa-secondary">${s.hint}</span>
                </span>
              </label>`)}
          </fieldset>

          ${scopes.includes('conversations') ? html`
            <div class="rounded-lg border border-wa-border bg-wa-panel p-3 flex flex-col gap-3">
              <label class="flex items-start gap-3 cursor-pointer">
                <input type="checkbox" class="mt-1" checked=${withMedia}
                  onChange=${(e) => setWithMedia(e.target.checked)} />
                <span class="min-w-0">
                  <span class="block text-sm text-wa-text">Incluir arquivos de mídia</span>
                  <span class="block text-[12px] text-wa-secondary">
                    Fotos, áudios, vídeos e documentos das mensagens. Deixa o pacote bem maior; sem isto as mensagens
                    levam só o texto e as mídias aparecem como “Arquivo removido” no destino.
                  </span>
                </span>
              </label>
              ${withMedia ? html`
                <div class="grid gap-2 sm:grid-cols-2">
                  <label class="text-[12px] text-wa-secondary flex flex-col gap-1">De (opcional)
                    <input type="date" value=${mediaFrom} onInput=${(e) => setMediaFrom(e.target.value)}
                      class="wa-field w-full px-3 py-2 rounded-md text-[14px]" />
                  </label>
                  <label class="text-[12px] text-wa-secondary flex flex-col gap-1">Até (opcional)
                    <input type="date" value=${mediaTo} onInput=${(e) => setMediaTo(e.target.value)}
                      class="wa-field w-full px-3 py-2 rounded-md text-[14px]" />
                  </label>
                </div>
                <p class="text-[12px] text-wa-secondary">
                  O período vale só para a mídia: as mensagens saem todas. Vazio = todas as mídias.
                </p>` : null}
              <label class="flex items-start gap-3 cursor-pointer">
                <input type="checkbox" class="mt-1" checked=${withAvatars}
                  onChange=${(e) => setWithAvatars(e.target.checked)} />
                <span class="min-w-0">
                  <span class="block text-sm text-wa-text">Incluir fotos de perfil dos contatos</span>
                  <span class="block text-[12px] text-wa-secondary">
                    Normalmente desnecessário: o WhatsBot-Lite baixa as fotos de novo sozinho ao abrir a conversa.
                    Marque só se quiser a tela idêntica logo no primeiro acesso.
                  </span>
                </span>
              </label>
            </div>` : null}

          <div class="rounded-lg border border-wa-border bg-wa-panel p-3 flex flex-col gap-3">
            <label class="flex items-start gap-3 cursor-pointer">
              <input type="checkbox" class="mt-1" checked=${withSecrets}
                onChange=${(e) => setWithSecrets(e.target.checked)} />
              <span class="min-w-0">
                <span class="block text-sm text-wa-text">Incluir segredos (cifrados com senha)</span>
                <span class="block text-[12px] text-wa-secondary">
                  A chave de API do modelo de IA. Sem isto, será preciso informá-la de novo no destino.
                </span>
              </span>
            </label>
            ${withSecrets ? html`
              <div class="grid gap-2 sm:grid-cols-2">
                <input type="password" autocomplete="new-password" placeholder=${`Senha do pacote (mín. ${MIN_PASSWORD_LEN})`}
                  value=${password} onInput=${(e) => setPassword(e.target.value)}
                  class="wa-field w-full px-3 py-2 rounded-md text-[14px]" />
                <input type="password" autocomplete="new-password" placeholder="Repita a senha"
                  value=${confirm} onInput=${(e) => setConfirm(e.target.value)}
                  class="wa-field w-full px-3 py-2 rounded-md text-[14px]" />
              </div>
              <p class="text-[12px] text-wa-secondary">
                Esta senha é só do pacote (não é a senha do painel). Não há como recuperá-la: quem tiver o pacote e a
                senha lê os segredos.
              </p>` : null}
          </div>

          ${error ? html`<p class="text-[13px] text-red-600">${error}</p>` : null}
          <div>
            <button type="submit" disabled=${busy || scopes.length === 0}
              class="bg-wa-teal text-white text-sm font-medium rounded-lg py-2 px-4 hover:opacity-90 disabled:opacity-50 cursor-pointer">
              ${busy ? 'Iniciando…' : 'Gerar pacote'}
            </button>
          </div>
        </form>
      `}
    </section>
  `;
}
