// Importar: enviar o pacote → (senha, se tiver segredos) → conferir o relatório da verificação →
// escolher o que fazer com o que já existe → confirmar → resultado.
//
// A verificação roda o MESMO código do import sem gravar nada, então a tabela que o operador vê é
// exatamente o que vai acontecer. Nada é gravado antes de "Iniciar importação".

import { h } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';
import htm from 'htm';
import {
  confirmImport, listJobs, recalculateImport, resumeImport, uploadImport,
} from '../../services/portabilityApi.js';
import {
  MAX_IMPORT_BYTES, buildConfirmRequest, canContinue, codePluginRows, codeToolRows,
  confirmNeedsPassword, findResumable, formatBytes, initialStrategy, interruptedText, isInterrupted,
  isPasswordRequired, isPreviewStale, isWaitingPlugins, overwriteRows, secretsState, totalChanges,
  waitingPluginsText,
} from '../../utils/portabilityLogic.js';
import { ConfirmImportModal } from './ConfirmImportModal.js';
import { ErrorDetailsModal } from './ErrorDetailsModal.js';
import { ResultTable, Warnings } from './ImportReport.js';
import { JobProgress } from './JobProgress.js';

const html = htm.bind(h);

// Verificação que o operador descartou: não volta depois de um F5. Só conveniência do navegador — sem
// storage a tela funciona igual (a verificação reaparece).
const DISMISSED_KEY = 'wb_portability_dismissed_dry';
const readDismissed = () => { try { return localStorage.getItem(DISMISSED_KEY) || ''; } catch (_) { return ''; } };
const writeDismissed = (id) => { try { if (id) localStorage.setItem(DISMISSED_KEY, id); } catch (_) { /* ignore */ } };

export function ImportWizard({ onChanged, onShowHistory, onPasswordRequired }) {
  const fileInput = useRef(null);
  const abort = useRef(null);
  const [file, setFile] = useState(null);
  const [uploadPw, setUploadPw] = useState('');
  // Senha do pacote digitada UMA vez (no envio). Fica só na memória do navegador — nunca em storage nem na
  // URL — e é reenviada no corpo das chamadas que o servidor exige (recalcular, confirmar, retomar), porque
  // o servidor a consome e não a guarda. Some ao concluir, descartar ou recarregar a página.
  const [savedPw, setSavedPw] = useState('');
  const [error, setError] = useState('');
  const [sent, setSent] = useState(null);          // {loaded, total} durante o envio
  const [uploading, setUploading] = useState(false);
  const [dryId, setDryId] = useState('');
  const [dry, setDry] = useState(null);            // job da verificação concluída
  const [strategy, setStrategy] = useState({});
  const [confirmPw, setConfirmPw] = useState('');
  const [overwriteSecrets, setOverwriteSecrets] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [askConfirm, setAskConfirm] = useState(false);
  const [recalculating, setRecalculating] = useState(false);
  const [recalcError, setRecalcError] = useState('');
  const [errorsFor, setErrorsFor] = useState(null);       // {jobId, entity}
  const [importId, setImportId] = useState('');
  const [final, setFinal] = useState(null);
  const [attempt, setAttempt] = useState(0);        // remonta o JobProgress a cada retomada
  const [resumePw, setResumePw] = useState('');
  const [resuming, setResuming] = useState(false);

  // F5 no meio do fluxo: o estado vive no servidor (jobs), então a tela se reconstrói a partir dele.
  useEffect(() => {
    let alive = true;
    listJobs(30).then((res) => {
      if (!alive || !res || !res.ok) return;
      const found = findResumable(res.data || [], { dismissed: readDismissed() });
      if (!found) return;
      const { job } = found;
      if (found.type === 'import') {
        setDryId(job.source_job || job.id);
        setDry({ state: 'succeeded', report: job.report || {} });
        setImportId(job.id);
        if (job.state === 'awaiting_restart') setFinal(job);          // interrompido OU esperando plugins
      } else if (found.type === 'dryrun') {
        setDryId(job.id);
      } else {
        setDryId(job.id);
        setDry(job);
        setStrategy(initialStrategy(job.report));
      }
    }).catch(() => {});
    return () => { alive = false; };
  }, []);

  const pick = (e) => {
    const f = e.target.files && e.target.files[0];
    setError('');
    if (f && f.size > MAX_IMPORT_BYTES) {
      setFile(null);
      setError(`O arquivo tem ${formatBytes(f.size)} e o limite é ${formatBytes(MAX_IMPORT_BYTES)}.`);
      return;
    }
    setFile(f || null);
  };

  const send = async () => {
    if (!file) return;
    setError(''); setUploading(true); setSent({ loaded: 0, total: file.size });
    const ctl = new AbortController(); abort.current = ctl;
    try {
      const res = await uploadImport(file, {
        password: uploadPw, signal: ctl.signal,
        onProgress: (loaded, total) => setSent({ loaded, total }),
      });
      if (isPasswordRequired(res)) { if (onPasswordRequired) onPasswordRequired(); return; }
      if (!res || !res.ok) { if (!(res && res.aborted)) setError((res && res.error) || 'Falha no envio.'); return; }
      setSavedPw(uploadPw);                       // lembrada para recalcular/confirmar/retomar sem pedir de novo
      setUploadPw('');
      setDryId(res.data.id);
    } finally { setUploading(false); abort.current = null; }
  };

  const dryDone = (job) => {
    if (job.state === 'succeeded') { setDry(job); setStrategy(initialStrategy(job.report)); }
    if (onChanged) onChanged();
  };

  const setOne = (entity, value) => setStrategy((cur) => ({ ...cur, [entity]: value }));

  // "Recalcular prévia": refaz a verificação (sem gravar nada) com os modos escolhidos. O servidor volta o
  // MESMO job para a fila; remontamos o JobProgress para acompanhá-lo.
  const recalc = async () => {
    setError(''); setRecalcError('');
    const pw = savedPw || confirmPw;
    if (confirmNeedsPassword(dry.report) && !pw) {
      setRecalcError('Este pacote tem segredos: digite a senha do pacote no campo logo abaixo e clique de novo.');
      return;
    }
    setRecalculating(true);
    try {
      const res = await recalculateImport(dryId, { strategy, ...(pw ? { password: pw } : {}) });
      if (!res || !res.ok) { setRecalcError((res && res.error) || 'Não foi possível recalcular.'); return; }
      setDry(null);                                      // volta ao "Verificando…"
      setAttempt((n) => n + 1);
    } finally { setRecalculating(false); }
  };

  const confirmArgs = (acceptCode) => ({
    report: dry.report, strategy, password: savedPw || confirmPw, overwriteSecrets, acceptCode });

  // 1º passo do "Confirmar importação": valida e só então abre o popup.
  const askToConfirm = () => {
    setError('');
    // O "entendo que traz código" é pedido DENTRO do popup, então aqui só valida o resto.
    const req = buildConfirmRequest(confirmArgs(true));
    if (req.error) { setError(req.error); return; }
    setAskConfirm(true);
  };

  const confirm = async (acceptCode) => {
    setError('');
    const req = buildConfirmRequest(confirmArgs(!!acceptCode));
    if (req.error) { setAskConfirm(false); setError(req.error); return; }
    setConfirming(true);
    try {
      const res = await confirmImport(dryId, req.body);
      if (isPasswordRequired(res)) { setAskConfirm(false); if (onPasswordRequired) onPasswordRequired(); return; }
      if (!res || !res.ok) { setAskConfirm(false); setError((res && res.error) || 'Não foi possível confirmar.'); return; }
      setAskConfirm(false);
      setConfirmPw('');
      setImportId(res.data.id);
    } finally { setConfirming(false); }
  };

  const importDone = (job) => {
    setFinal(job);
    // Interrompido ainda vai precisar da senha para continuar; terminou de vez, esquece.
    if (['succeeded', 'failed', 'cancelled'].includes(job.state)) setSavedPw('');
    if (onChanged) onChanged();
  };

  // Import interrompido (o servidor reiniciou no meio): a senha só vivia na memória do processo que
  // morreu, então é pedida de novo. O que já entrou não duplica.
  const resume = async () => {
    setError('');
    const needsPw = !!final && (final.needs_password || (final.report && final.report.secrets === 'ok'));
    const pw = savedPw || resumePw;
    if (needsPw && !pw) { setError('Este pacote tem segredos: informe a senha para continuar.'); return; }
    setResuming(true);
    try {
      const res = await resumeImport(importId, pw ? { password: pw } : {});
      if (!res || !res.ok) { setError((res && res.error) || 'Não foi possível continuar.'); return; }
      setResumePw('');
      setFinal(null);
      setAttempt((n) => n + 1);
    } finally { setResuming(false); }
  };

  const reset = () => {
    writeDismissed(dryId);
    setFile(null); setUploadPw(''); setSavedPw(''); setError(''); setSent(null); setDryId(''); setDry(null);
    setStrategy({}); setConfirmPw(''); setOverwriteSecrets(false); setImportId(''); setFinal(null);
    setAttempt(0); setResumePw(''); setAskConfirm(false); setErrorsFor(null); setRecalcError('');
    if (fileInput.current) fileInput.current.value = '';
  };

  const report = dry && dry.report;
  const state = report ? secretsState(report) : 'absent';
  const changes = report ? totalChanges(report) : 0;
  const pct = sent && sent.total ? Math.round((100 * sent.loaded) / sent.total) : 0;
  const stale = !!report && isPreviewStale(report, strategy);
  const optionLabels = [overwriteSecrets && 'Sobrescrever segredos que já existem'].filter(Boolean);

  return html`
    <section class="flex flex-col gap-4">
      <div>
        <h4 class="text-sm font-semibold text-wa-text">Importar</h4>
        <p class="text-[13px] text-wa-secondary mt-1">
          Lê um pacote gerado por esta tela. Primeiro mostramos o que mudaria — nada é gravado até você confirmar.
        </p>
      </div>

      ${!dryId ? html`
        <div class="flex flex-col gap-3">
          <input ref=${fileInput} type="file" accept=".zip,application/zip" onChange=${pick}
            class="text-[13px] text-wa-text" disabled=${uploading} />
          <input type="password" autocomplete="new-password" value=${uploadPw}
            onInput=${(e) => setUploadPw(e.target.value)} disabled=${uploading}
            placeholder="Senha do pacote (só se ele tiver segredos)"
            class="wa-field w-full sm:max-w-sm px-3 py-2 rounded-md text-[14px]" />
          ${uploading ? html`
            <div class="flex flex-col gap-1">
              <div class="h-2 rounded-full bg-wa-hover overflow-hidden">
                <div class="h-full bg-wa-teal transition-all" style=${`width:${pct}%`}></div>
              </div>
              <span class="text-[12px] text-wa-secondary">Enviando… ${pct}% (${formatBytes(sent.loaded)} de ${formatBytes(sent.total)})</span>
            </div>` : null}
          ${error ? html`<p class="text-[13px] text-red-600">${error}</p>` : null}
          <div class="flex flex-wrap gap-2">
            <button type="button" onClick=${send} disabled=${!file || uploading}
              class="bg-wa-teal text-white text-sm font-medium rounded-lg py-2 px-4 hover:opacity-90 disabled:opacity-50 cursor-pointer">
              ${uploading ? 'Enviando…' : 'Enviar e verificar'}
            </button>
            ${uploading ? html`<button type="button" onClick=${() => abort.current && abort.current.abort()}
              class="px-4 py-2 rounded-lg text-sm text-wa-text hover:bg-wa-hover cursor-pointer">Cancelar envio</button>` : null}
          </div>
        </div>
      ` : null}

      ${dryId && !dry ? html`<${JobProgress} key=${`dry-${attempt}`} jobId=${dryId} title="Verificando o pacote" onDone=${dryDone} />
        <div><button type="button" onClick=${reset} class="text-[12px] text-wa-secondary hover:text-wa-text underline cursor-pointer">Voltar</button></div>` : null}

      ${dry && !importId ? html`
        <div class="flex flex-col gap-3">
          <div class="rounded-lg border border-blue-500/40 bg-blue-500/10 px-3 py-2" role="note">
            <p class="text-[13px] font-semibold text-blue-800">Prévia — nada foi importado ainda</p>
            <p class="text-[12px] text-blue-800 mt-0.5">
              A tabela abaixo mostra o que ACONTECERIA com cada recurso, de acordo com a opção escolhida em “Se já existir
              neste servidor”. Ajuste as opções, use “Recalcular prévia” para ver os números novos e só então clique em
              “Confirmar importação”.
            </p>
          </div>
          <p class="text-sm text-wa-text">
            ${changes === 0
              ? 'Prévia: nada seria alterado, tudo do pacote já está nesta instalação.'
              : html`<strong>${changes.toLocaleString('pt-BR')}</strong> registro(s) seriam criados ou alterados (prévia).`}
          </p>
          ${report.already_imported ? html`
            <p class="text-[13px] text-amber-800 bg-amber-500/10 border border-amber-500/30 rounded-md py-2 px-3" role="status">
              Este mesmo pacote já foi importado antes. Importar de novo não duplica nada, mas também não deve mudar muito.
            </p>` : null}
          ${stale ? html`
            <div class="flex flex-col gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2" role="status">
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-[13px] text-amber-800">Os números não refletem suas últimas trocas.</span>
                <button type="button" onClick=${recalc} disabled=${recalculating}
                  class="text-[13px] px-3 py-1 rounded-md border border-amber-600/50 text-amber-800 hover:bg-amber-500/20 disabled:opacity-50 cursor-pointer">
                  ${recalculating ? 'Recalculando…' : 'Recalcular prévia'}</button>
              </div>
              ${confirmNeedsPassword(report) && !savedPw ? html`
                <input type="password" autocomplete="new-password" value=${confirmPw}
                  onInput=${(e) => { setConfirmPw(e.target.value); setRecalcError(''); }} placeholder="Senha do pacote (necessária para recalcular)"
                  class="wa-field w-full sm:max-w-sm px-3 py-2 rounded-md text-[14px]" />` : null}
              ${recalcError ? html`<p class="text-[13px] text-red-600" role="alert">${recalcError}</p>` : null}
            </div>` : null}
          <${ResultTable} report=${report} strategy=${strategy} onStrategy=${setOne}
            onStrategyMany=${setStrategy} onShowErrors=${(entity) => setErrorsFor({ jobId: dryId, entity })} />
          <${Warnings} report=${report} />

          ${confirmNeedsPassword(report) ? html`
            <div class="flex flex-col gap-2">
              ${savedPw ? html`
                <p class="text-[13px] text-wa-secondary">A senha do pacote que você informou no envio será usada para importar os segredos.</p>` : html`
                <p class="text-[13px] text-wa-text">
                  ${state === 'locked'
                    ? 'O pacote tem segredos cifrados. Informe a senha para importá-los.'
                    : 'Informe a senha de novo para importar os segredos.'}
                </p>
                <input type="password" autocomplete="new-password" value=${confirmPw}
                  onInput=${(e) => setConfirmPw(e.target.value)} placeholder="Senha do pacote"
                  class="wa-field w-full sm:max-w-sm px-3 py-2 rounded-md text-[14px]" />`}
              <label class="flex items-start gap-2 text-[13px] text-wa-text cursor-pointer">
                <input type="checkbox" class="mt-1" checked=${overwriteSecrets}
                  onChange=${(e) => setOverwriteSecrets(e.target.checked)} />
                <span>Sobrescrever segredos que já existem aqui (por padrão eles são mantidos)</span>
              </label>
            </div>` : null}

          ${error ? html`<p class="text-[13px] text-red-600">${error}</p>` : null}
          <div class="flex flex-wrap gap-2">
            <button type="button" onClick=${askToConfirm} disabled=${confirming || recalculating}
              class="bg-wa-teal text-white text-sm font-medium rounded-lg py-2 px-4 hover:opacity-90 disabled:opacity-50 cursor-pointer">
              ${confirming ? 'Confirmando…' : 'Confirmar importação'}
            </button>
            <button type="button" onClick=${reset}
              class="px-4 py-2 rounded-lg text-sm text-wa-text hover:bg-wa-hover cursor-pointer">Descartar</button>
          </div>
        </div>
      ` : null}

      ${importId ? html`
        <${JobProgress} key=${attempt} jobId=${importId} title="Importando" onDone=${importDone} />
        ${final && canContinue(final) ? html`
          <div class="flex flex-col gap-3">
            <p class="text-sm text-wa-text">${isWaitingPlugins(final) ? waitingPluginsText(final) : interruptedText(final)}</p>
            ${(final.needs_password || (final.report && final.report.secrets === 'ok')) && !savedPw ? html`
              <input type="password" autocomplete="new-password" value=${resumePw}
                onInput=${(e) => setResumePw(e.target.value)} placeholder="Senha do pacote"
                class="wa-field w-full sm:max-w-sm px-3 py-2 rounded-md text-[14px]" />` : null}
            ${error ? html`<p class="text-[13px] text-red-600">${error}</p>` : null}
            <div class="flex flex-wrap gap-2">
              <button type="button" onClick=${resume} disabled=${resuming}
                class="bg-wa-teal text-white text-sm font-medium rounded-lg py-2 px-4 hover:opacity-90 disabled:opacity-50 cursor-pointer">
                ${resuming ? 'Continuando…' : 'Continuar importação'}
              </button>
              <button type="button" onClick=${reset}
                class="px-4 py-2 rounded-lg text-sm text-wa-text hover:bg-wa-hover cursor-pointer">Deixar para depois</button>
            </div>
          </div>` : null}
        ${final && final.state === 'succeeded' ? html`
          <div class="flex flex-col gap-3">
            <p class="text-sm text-wa-text">
              Importação concluída: <strong>${totalChanges(final.report).toLocaleString('pt-BR')}</strong> registro(s) criados ou alterados.
              O detalhe (tabela, avisos e erros) está em “Operações recentes”.
            </p>
            <div class="flex flex-wrap gap-2">
              <button type="button" onClick=${() => { window.location.reload(); }}
                class="border border-wa-border text-wa-text text-sm rounded-lg py-2 px-4 hover:bg-wa-hover cursor-pointer">Recarregar a página</button>
              <button type="button" onClick=${() => { if (onShowHistory) onShowHistory(final.id); }}
                class="bg-wa-teal text-white text-sm font-medium rounded-lg py-2 px-4 hover:opacity-90 cursor-pointer">Ver no histórico</button>
              <button type="button" onClick=${reset}
                class="px-4 py-2 rounded-lg text-sm text-wa-text hover:bg-wa-hover cursor-pointer">Importar outro pacote</button>
            </div>
          </div>` : null}
        ${final && final.state !== 'succeeded' && !canContinue(final) ? html`
          <div><button type="button" onClick=${reset}
            class="px-4 py-2 rounded-lg text-sm text-wa-text hover:bg-wa-hover cursor-pointer">Recomeçar</button></div>` : null}
      ` : null}
      ${askConfirm && report ? html`
        <${ConfirmImportModal} overwrites=${overwriteRows(report, strategy)} options=${optionLabels}
          codeTools=${codeToolRows(report)} codePlugins=${codePluginRows(report)}
          stale=${stale} busy=${confirming} onConfirm=${confirm} onCancel=${() => setAskConfirm(false)} />` : null}
      ${errorsFor ? html`<${ErrorDetailsModal} jobId=${errorsFor.jobId} entity=${errorsFor.entity}
        onClose=${() => setErrorsFor(null)} />` : null}
    </section>
  `;
}
