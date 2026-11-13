// Overlay BLOQUEANTE do reinício automático. O fluxo roda sem recarregar a página (a senha do pacote só
// existe na memória do navegador): pedir o reinício → servidor volta (TROCA do boot_id) → retomar → resultado.
//
// Não há prazo duro de falha: passado RESTART_SLOW_AFTER_SEC a mensagem muda ("demorando mais que o normal
// — continuamos tentando") e aparece "Recarregar a página", mas a espera segue.

import { h } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';
import htm from 'htm';
import { fetchBootId, getJob, restartServer, resumeImport } from '../../services/portabilityApi.js';
import {
  RESTART_SLOW_AFTER_SEC, RESTART_STEPS, elapsedText, serverBack, shouldStopPolling,
} from '../../utils/portabilityLogic.js';

const html = htm.bind(h);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * @param {{jobId: string, bootId: string, password: string,
 *          onFinished: (job:any)=>void, onClose: ()=>void}} props
 * `onFinished(job)` recebe o job quando a etapa 2 termina (qualquer desfecho).
 */
export function RestartOverlay({ jobId, bootId, password, onFinished, onClose, attempt = 0 }) {
  const [step, setStep] = useState('saving');
  const [elapsed, setElapsed] = useState(0);
  const [failure, setFailure] = useState('');
  const [result, setResult] = useState(null);
  const [round, setRound] = useState(0);              // "Tentar de novo" incrementa
  const t0 = useRef(Date.now());

  useEffect(() => {
    t0.current = Date.now();
    setElapsed(0);
    const t = setInterval(() => setElapsed((Date.now() - t0.current) / 1000), 1000);
    return () => clearInterval(t);
  }, [round, attempt]);

  useEffect(() => {
    let alive = true;
    const run = async () => {
      setFailure(''); setResult(null);
      // 1) o estado já está no servidor (job em awaiting_restart): só pedimos o reinício.
      setStep('saving');
      const asked = await restartServer();
      if (!alive) return;
      if (!asked || !asked.ok) { setFailure((asked && asked.error) || 'Não foi possível reiniciar o servidor.'); setStep('done'); return; }
      // 2) o servidor cai em ~1,5 s e volta com OUTRO boot_id.
      setStep('restarting');
      for (;;) {
        await sleep(2000);
        if (!alive) return;
        const now = await fetchBootId();
        if (serverBack(bootId, now)) break;
      }
      setStep('back');
      await sleep(800);
      if (!alive) return;
      // 3) retoma com a senha que só existe aqui. O servidor pode ainda estar terminando o boot (plugin
      //    carregando): 409 é tentado de novo por um tempo. O `error` já diz QUAL plugin falta.
      setStep('resuming');
      let res = null;
      for (let i = 0; i < 20; i += 1) {
        res = await resumeImport(jobId, password ? { password } : {});
        if (!alive) return;
        if (res && res.ok) break;
        if (!(res && res.status === 409)) break;
        setFailure(res.error || '');
        await sleep(3000);
        if (!alive) return;
      }
      if (!res || !res.ok) {
        setFailure((res && res.error) || 'Não foi possível continuar a importação.');
        setStep('done');
        return;
      }
      setFailure('');
      // 4) acompanha a etapa 2 até parar.
      for (;;) {
        await sleep(2000);
        if (!alive) return;
        const j = await getJob(jobId);
        if (j && j.ok && j.data && shouldStopPolling(j.data)) { setResult(j.data); setStep('done'); onFinished(j.data); return; }
      }
    };
    run();
    return () => { alive = false; };
  }, [round]);

  const slow = elapsed >= RESTART_SLOW_AFTER_SEC && step !== 'done';
  const idx = RESTART_STEPS.findIndex((s) => s.id === step);
  const ok = result && result.state === 'succeeded';
  const paused = result && result.state === 'awaiting_restart';

  return html`
    <div class="fixed inset-0 bg-black/60 z-[70] flex items-center justify-center p-4" role="alertdialog" aria-modal="true"
      aria-label="Reiniciando o servidor">
      <div class="bg-wa-bg rounded-2xl shadow-2xl max-w-md w-full p-6 flex flex-col gap-4">
        <h3 class="text-base font-semibold text-wa-text">Reinício automático</h3>
        <ol class="flex flex-col gap-2">
          ${RESTART_STEPS.map((s, i) => {
            const state = i < idx || (step === 'done' && i <= idx) ? 'done' : i === idx ? 'now' : 'todo';
            const failed = s.id === 'done' && step === 'done' && (failure || (result && !ok));
            return html`
            <li key=${s.id} class=${`flex items-center gap-2 text-[13px] ${state === 'todo' ? 'text-wa-secondary' : 'text-wa-text'}`}>
              <span aria-hidden="true" class="w-4 text-center">${failed ? '✗' : state === 'done' ? '✓' : state === 'now' ? '…' : '·'}</span>
              <span>${s.id === 'done' && step === 'done' ? (ok ? 'Concluído' : paused ? 'Pausado de novo' : 'Não concluído') : s.label}</span>
              ${s.id === 'restarting' && step === 'restarting' ? html`<span class="text-[12px] text-wa-secondary">(${elapsedText(elapsed)})</span>` : null}
            </li>`;
          })}
        </ol>
        ${step === 'restarting' ? html`
          <p class="text-[12px] text-wa-secondary">Não há previsão de duração: depende do servidor e dos plugins carregarem.</p>` : null}
        ${slow ? html`
          <p class="text-[13px] text-amber-800 bg-amber-500/10 border border-amber-500/30 rounded-md py-2 px-3" role="status">
            Está demorando mais que o normal — continuamos tentando.
            <button type="button" onClick=${() => window.location.reload()}
              class="ml-1 underline cursor-pointer">Recarregar a página</button>
          </p>` : null}
        ${failure ? html`<p class="text-[13px] ${step === 'done' ? 'text-red-600' : 'text-amber-800'}" role="alert">${failure}</p>` : null}
        ${result && result.state === 'failed' ? html`<p class="text-[13px] text-red-600" role="alert">${result.error || 'A importação falhou.'}</p>` : null}
        ${paused ? html`<p class="text-[13px] text-amber-800">Ainda faltam itens: confira se os plugins estão ativos e tente de novo.</p>` : null}
        ${step === 'done' ? html`
          <div class="flex flex-wrap gap-2 justify-end">
            ${ok ? html`
              <button type="button" onClick=${() => window.location.reload()}
                class="border border-wa-border text-wa-text text-sm rounded-lg py-2 px-4 hover:bg-wa-hover cursor-pointer">Recarregar a página</button>` : html`
              <button type="button" onClick=${() => setRound((n) => n + 1)}
                class="border border-wa-border text-wa-text text-sm rounded-lg py-2 px-4 hover:bg-wa-hover cursor-pointer">Tentar de novo</button>`}
            <button type="button" onClick=${onClose}
              class="bg-wa-teal text-white text-sm font-medium rounded-lg py-2 px-4 hover:opacity-90 cursor-pointer">${ok ? 'Ver resultado' : 'Fechar'}</button>
          </div>` : null}
      </div>
    </div>`;
}
