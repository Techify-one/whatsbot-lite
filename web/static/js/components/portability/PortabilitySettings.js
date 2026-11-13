// Card "Importar/Exportar" da aba Sistema (Painel → Sistema → `#import-export`).
//
// Exige a senha do painel: o pacote leva as conversas e a chave do LLM desta instalação, e sem senha o
// `/api/*` fica aberto a quem alcançar a porta. Sem ela o servidor responde 403 `password_required` em
// toda rota; aqui a tela nem oferece os assistentes e leva ao card de senha.

import { h } from 'preact';
import { useCallback, useEffect, useState } from 'preact/hooks';
import htm from 'htm';
import { listJobs } from '../../services/portabilityApi.js';
import {
  entitiesWithErrors, exportSummary, formatBytes, jobDuration, stateLabel,
} from '../../utils/portabilityLogic.js';
import { ErrorDetailsModal } from './ErrorDetailsModal.js';
import { ExportWizard } from './ExportWizard.js';
import { ResultTable, Warnings } from './ImportReport.js';
import { ImportWizard } from './ImportWizard.js';

const html = htm.bind(h);

const KIND_LABEL = { export: 'Exportação', import_dryrun: 'Verificação de pacote', import: 'Importação' };

function when(ts) {
  if (!ts) return '';
  try { return new Date(ts * 1000).toLocaleString('pt-BR'); } catch (_) { return ''; }
}

/** O que aparece ao expandir uma operação: resumo do export, ou tabela + avisos + erros. */
function JobDetail({ job, onShowErrors }) {
  const report = job.report || {};
  if (job.kind === 'export') {
    if (job.state !== 'succeeded') return html`<p class="text-[12px] text-wa-secondary">Sem detalhes para esta exportação.</p>`;
    const s = exportSummary(report);
    return html`
      <p class="text-[13px] text-wa-text">
        ${s.total.toLocaleString('pt-BR')} registros, ${formatBytes(s.bytes)}.
        ${s.secrets ? 'Com segredos cifrados.' : 'Sem segredos.'}
      </p>`;
  }
  if (!report.entities || !Object.keys(report.entities).length) {
    return html`<p class="text-[12px] text-wa-secondary">Sem relatório para esta operação.</p>`;
  }
  return html`
    <div class="flex flex-col gap-2">
      <${ResultTable} report=${report}
        onShowErrors=${entitiesWithErrors(report).length ? (entity) => onShowErrors(job.id, entity) : null} />
      <${Warnings} report=${report} defaultOpen=${true} />
    </div>`;
}

function RecentJobs({ refreshKey, openId, setOpenId }) {
  const [rows, setRows] = useState([]);
  const [errorsFor, setErrorsFor] = useState(null);
  useEffect(() => {
    let alive = true;
    listJobs(15).then((r) => { if (alive && r && r.ok) setRows(r.data || []); }).catch(() => {});
    return () => { alive = false; };
  }, [refreshKey]);
  if (!rows.length) return null;
  return html`
    <section class="flex flex-col gap-2">
      <h4 class="text-sm font-semibold text-wa-text">Operações recentes</h4>
      <div class="overflow-x-auto rounded-lg border border-wa-border">
        <table class="w-full text-[13px]">
          <thead class="bg-wa-panel text-wa-secondary">
            <tr>
              <th class="text-left font-medium px-2 py-1">Tipo</th>
              <th class="text-left font-medium px-2 py-1">Estado</th>
              <th class="text-left font-medium px-2 py-1">Data</th>
              <th class="text-left font-medium px-2 py-1">Duração</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map((j) => {
              const open = openId === j.id;
              return html`
                <tr key=${j.id} class="border-t border-wa-border text-wa-text cursor-pointer hover:bg-wa-hover"
                  onClick=${() => setOpenId(open ? '' : j.id)} aria-expanded=${open}>
                  <td class="px-2 py-2"><span aria-hidden="true" class="text-wa-secondary mr-1">${open ? '▾' : '▸'}</span>${KIND_LABEL[j.kind] || j.kind}</td>
                  <td class="px-2 py-2">${stateLabel(j.state, j)}</td>
                  <td class="px-2 py-2 whitespace-nowrap">${when(j.created_at)}</td>
                  <td class="px-2 py-2 whitespace-nowrap">${jobDuration(j) || '—'}</td>
                </tr>
                ${j.state === 'failed' && j.error ? html`
                  <tr key=${j.id + '-err'}><td colspan="4" class="px-2 pb-2 text-[12px] text-red-600">${j.error}</td></tr>` : null}
                ${open ? html`
                  <tr key=${j.id + '-detail'} class="bg-wa-panel">
                    <td colspan="4" class="px-3 py-3"><${JobDetail} job=${j} onShowErrors=${(id, entity) => setErrorsFor({ jobId: id, entity })} /></td>
                  </tr>` : null}`;
            })}
          </tbody>
        </table>
      </div>
      ${errorsFor ? html`<${ErrorDetailsModal} jobId=${errorsFor.jobId} entity=${errorsFor.entity}
        onClose=${() => setErrorsFor(null)} />` : null}
    </section>`;
}

/** Aviso "defina uma senha" com o atalho para o card de senha da mesma aba. */
function PasswordGate() {
  const goToPassword = (e) => {
    e.preventDefault();
    window.location.hash = 'password';
    const el = document.getElementById('password');
    if (el && el.scrollIntoView) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  return html`
    <div class="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-3 flex flex-col gap-2" role="note">
      <p class="text-[13px] font-semibold text-amber-800">Defina uma senha para o painel primeiro</p>
      <p class="text-[13px] text-amber-800">
        O pacote leva as conversas e a chave de API desta instalação, e importar grava nela. Sem senha, qualquer
        pessoa que alcance este endereço poderia exportar tudo ou importar dados. Defina a senha em
        <a href="#password" onClick=${goToPassword} class="underline font-medium">Senha do Painel</a>,
        clique em “Salvar Configurações” e volte aqui.
      </p>
    </div>`;
}

export function PortabilitySettings({ id, hasPassword }) {
  const [tick, setTick] = useState(0);
  const [openId, setOpenId] = useState('');
  // O servidor é a barreira real; se ele recusar por falta de senha (a config da tela pode estar velha),
  // a tela cai no mesmo aviso.
  const [gated, setGated] = useState(false);
  const bump = useCallback(() => setTick((n) => n + 1), []);
  const showHistory = useCallback((jobId) => { setOpenId(jobId); setTick((n) => n + 1); }, []);
  const blocked = !hasPassword || gated;

  return html`
    <div id=${id || undefined} class="bg-wa-bg rounded-xl p-5 border border-wa-border shadow-sm scroll-mt-4">
      <h3 class="text-xs font-semibold text-wa-secondary uppercase tracking-wider mb-4">Importar / Exportar</h3>
      <p class="text-[13px] text-wa-secondary mb-4">
        Leve as configurações, as tags, os contatos e as conversas desta instalação para outra, ou traga de uma
        instalação que você já tinha. O pacote só é lido por esta mesma tela.
      </p>
      <div class="flex flex-col gap-6">
        ${blocked ? html`<${PasswordGate} />` : html`
          <${ExportWizard} onChanged=${bump} onPasswordRequired=${() => setGated(true)} />
          <hr class="border-wa-border" />
          <${ImportWizard} onChanged=${bump} onShowHistory=${showHistory} onPasswordRequired=${() => setGated(true)} />
          <${RecentJobs} refreshKey=${tick} openId=${openId} setOpenId=${setOpenId} />`}
      </div>
    </div>`;
}
