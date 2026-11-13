// Acompanha UM job por polling (nunca pelo /ws: o broadcast é global e o progresso de um
// pacote não pode vazar para os outros usuários logados).
//
// - Consulta a cada 2 s (backoff de até 10 s se a rede falhar) e PARA quando o job
//   termina ou fica esperando uma ação humana (`shouldStopPolling`).
// - `onDone(job)` dispara UMA vez, na primeira vez em que o job para.
// - Cancelar é pedido ao servidor; o job só encerra quando o handler confere entre lotes.

import { h } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';
import htm from 'htm';
import { cancelJob, getJob } from '../../services/portabilityApi.js';
import {
  nextPollDelay, percentOf, progressText, shouldStopPolling, stateLabel,
} from '../../utils/portabilityLogic.js';

const html = htm.bind(h);

export function JobProgress({ jobId, title, onDone, onJob }) {
  const [job, setJob] = useState(null);
  const [error, setError] = useState('');
  const [cancelling, setCancelling] = useState(false);
  const done = useRef(false);
  const cbs = useRef({ onDone, onJob });
  cbs.current = { onDone, onJob };

  useEffect(() => {
    if (!jobId) return undefined;
    done.current = false;
    let alive = true;
    let timer = null;
    let failures = 0;

    const tick = async () => {
      let res = null;
      try { res = await getJob(jobId); } catch (_) { res = null; }
      if (!alive) return;
      if (res && res.ok && res.data) {
        failures = 0;
        setError('');
        setJob(res.data);
        if (cbs.current.onJob) cbs.current.onJob(res.data);
        if (shouldStopPolling(res.data)) {
          if (!done.current) { done.current = true; if (cbs.current.onDone) cbs.current.onDone(res.data); }
          return;
        }
      } else {
        failures += 1;
        // 404 = o job sumiu (expirou/foi apagado): para de insistir.
        if (res && res.status === 404) { setError('Este job não existe mais.'); return; }
        setError('Sem resposta do servidor — tentando de novo…');
      }
      timer = setTimeout(tick, nextPollDelay(failures));
    };
    tick();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [jobId]);

  if (!jobId) return null;
  const pct = percentOf(job);
  const running = job && (job.state === 'running' || job.state === 'queued');

  const onCancel = async () => {
    setCancelling(true);
    try { await cancelJob(jobId); } finally { setCancelling(false); }
  };

  return html`
    <div class="rounded-lg border border-wa-border bg-wa-panel p-4 flex flex-col gap-2" role="status" aria-live="polite">
      <div class="flex items-center justify-between gap-3">
        <span class="text-sm font-medium text-wa-text">${title || 'Operação'}</span>
        <span class="text-[12px] text-wa-secondary shrink-0">${job ? stateLabel(job.state, job) : 'Consultando…'}</span>
      </div>
      <div class="h-2 rounded-full bg-wa-hover overflow-hidden" aria-hidden="true">
        <div
          class=${`h-full bg-wa-teal transition-all ${running && pct == null ? 'animate-pulse w-full opacity-60' : ''}`}
          style=${pct == null ? (running ? '' : 'width:100%') : `width:${pct}%`}
        ></div>
      </div>
      <div class="flex items-center justify-between gap-3 min-w-0">
        <span class="text-[12px] text-wa-secondary truncate min-w-0">${job ? progressText(job) : ''}${pct != null ? ` · ${pct}%` : ''}</span>
        ${running ? html`
          <button
            type="button" onClick=${onCancel} disabled=${cancelling || (job && job.cancel_requested)}
            class="text-[12px] px-3 py-1 rounded-md border border-wa-border text-wa-text hover:bg-wa-hover disabled:opacity-50 cursor-pointer shrink-0"
          >${job && job.cancel_requested ? 'Cancelando…' : 'Cancelar'}</button>
        ` : null}
      </div>
      ${error ? html`<p class="text-[12px] text-amber-700">${error}</p>` : null}
      ${job && job.state === 'failed' && job.error ? html`
        <p class="text-[13px] text-red-600 bg-red-500/10 border border-red-500/30 rounded-md py-2 px-3">${job.error}</p>
      ` : null}
    </div>
  `;
}
