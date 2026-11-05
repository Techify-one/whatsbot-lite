import { h } from 'preact';
import { useEffect, useState } from 'preact/hooks';
import htm from 'htm';

import { createWebSocket } from '../services/websocket.js';

const html = htm.bind(h);

const STAGE_LABEL = {
  validating: 'Validando destino',
  wiping: 'Apagando schema existente',
  schema: 'Aplicando schema (Alembic)',
  copying: 'Copiando dados',
  done: 'Migração concluída',
  failed: 'Migração falhou',
};

export function DatabaseSettings({ id, onNotify }) {
  const [info, setInfo] = useState(null);
  const [target, setTarget] = useState('');
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState(null);
  const [confirming, setConfirming] = useState(false);
  const [confirmingWipe, setConfirmingWipe] = useState(false);
  const [wipeAck, setWipeAck] = useState('');

  // Live progress updates from the server.
  useEffect(() => {
    const ws = createWebSocket({
      db_migration_progress: (data) => {
        setProgress(data);
        if (data.stage === 'done' || data.stage === 'failed') {
          setRunning(false);
        }
      },
    });
    return () => ws.close();
  }, []);

  useEffect(() => {
    refresh();
  }, []);

  async function refresh() {
    try {
      const res = await fetch('/api/admin/database', {
        headers: authHeader(),
      });
      const json = await res.json();
      if (json.ok) setInfo(json.data);
    } catch (e) {
      // Surface error softly — the rest of the panel still renders.
      console.warn('Falha ao consultar /api/admin/database', e);
    }
  }

  async function start({ forceDrop = false } = {}) {
    if (!target.trim()) {
      onNotify?.('Informe a URL Postgres', 'error');
      return;
    }
    setRunning(true);
    setConfirmingWipe(false);
    setWipeAck('');
    setProgress({
      stage: forceDrop ? 'wiping' : 'validating',
      message: forceDrop ? 'Apagando destino e reiniciando migração' : 'Iniciando migração',
    });
    try {
      const res = await fetch('/api/admin/migrate-to-postgres', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeader() },
        body: JSON.stringify({ postgres_url: target.trim(), force_drop: forceDrop }),
      });
      const json = await res.json();
      if (!json.ok) {
        setRunning(false);
        setProgress({ stage: 'failed', error: json.error });
        onNotify?.(json.error || 'Falha ao iniciar', 'error');
      }
    } catch (e) {
      setRunning(false);
      setProgress({ stage: 'failed', error: String(e) });
    }
  }

  function authHeader() {
    const token = localStorage.getItem('whatsbot_token');
    return token ? { Authorization: `Bearer ${token}` } : {};
  }

  const rowsPct = progress && progress.rows_total
    ? Math.min(100, Math.round((progress.rows_done / progress.rows_total) * 100))
    : 0;
  const tablesPct = progress && progress.tables_total
    ? Math.round((progress.tables_done / progress.tables_total) * 100)
    : 0;

  return html`
    <div id=${id || undefined} class="bg-wa-bg rounded-xl p-5 border border-wa-border shadow-sm scroll-mt-4">
      <h3 class="text-xs font-semibold text-wa-secondary uppercase tracking-wider mb-4">
        Banco de dados
      </h3>
      <div class="flex flex-col gap-4 text-sm">
        ${info ? html`
          <div class="rounded-lg bg-wa-panel px-3 py-2">
            <div><span class="font-medium">Backend atual:</span> ${info.dialect}</div>
            <div class="text-xs text-wa-secondary break-all">${info.url_redacted}</div>
            ${info.sqlite_path ? html`
              <div class="text-xs text-wa-secondary">Arquivo SQLite: ${info.sqlite_path}</div>
            ` : null}
            <div class="text-xs text-wa-secondary mt-1">
              Config persistente: <code>${info.config_file}</code>
            </div>
          </div>
        ` : html`
          <div class="text-wa-secondary">Carregando informações do banco…</div>
        `}

        ${info && info.dialect === 'sqlite' ? html`
          <div id="database-migration" class="flex flex-col gap-2 scroll-mt-4">
            <label class="text-sm font-medium">URL Postgres</label>
            <input
              type="text"
              value=${target}
              onInput=${(e) => setTarget(e.target.value)}
              placeholder="postgresql+psycopg://user:senha@host:5432/whatsbot"
              class="rounded-lg border border-wa-border px-3 py-2 text-sm font-mono"
            />
            <p class="text-xs text-wa-secondary">
              O banco destino precisa estar vazio. Após a migração o Whatsbot-lite reinicia
              automaticamente apontando pro Postgres; o arquivo SQLite atual é preservado
              para rollback manual.
            </p>
            ${!confirming ? html`
              <button
                onClick=${() => setConfirming(true)}
                disabled=${running || !target.trim()}
                class="self-start px-4 py-2 bg-wa-teal hover:bg-wa-tealDark disabled:opacity-50 text-white text-sm font-medium rounded-lg"
              >
                Migrar agora
              </button>
            ` : html`
              <div class="rounded-lg border border-yellow-300 bg-yellow-50 p-3 text-sm">
                <p class="font-medium text-yellow-900">
                  Confirme: copiar todos os dados deste SQLite para o Postgres acima?
                </p>
                <p class="text-xs text-yellow-800 mt-1">
                  O servidor reinicia ao final. Pode levar alguns minutos dependendo do volume.
                </p>
                <div class="flex gap-2 mt-2">
                  <button
                    onClick=${() => { setConfirming(false); start({ forceDrop: false }); }}
                    class="px-3 py-1.5 bg-red-600 hover:bg-red-700 text-white text-xs font-medium rounded-lg"
                  >Sim, migrar</button>
                  <button
                    onClick=${() => setConfirming(false)}
                    class="px-3 py-1.5 bg-wa-border hover:bg-wa-hover text-xs rounded-lg"
                  >Cancelar</button>
                </div>
              </div>
            `}
          </div>
        ` : info ? html`
          <div class="rounded-lg bg-emerald-50 border border-emerald-200 p-3 text-sm text-emerald-900">
            Já está rodando em Postgres. Para voltar a SQLite, edite manualmente: 
            <code> storages/database.json</code> e reinicie.
          </div>
        ` : null}

        ${progress ? html`
          <div class="rounded-lg border border-wa-border p-3 flex flex-col gap-2">
            <div class="flex items-center justify-between">
              <span class="font-medium">${STAGE_LABEL[progress.stage] || progress.stage}</span>
              ${progress.table ? html`<span class="text-xs text-wa-secondary">${progress.table}</span>` : null}
            </div>
            ${progress.message ? html`<div class="text-xs text-wa-secondary">${progress.message}</div>` : null}
            ${progress.tables_total ? html`
              <div>
                <div class="text-xs">Tabelas: ${progress.tables_done}/${progress.tables_total}</div>
                <div class="w-full bg-wa-border rounded h-1.5 mt-1 overflow-hidden">
                  <div class="bg-wa-teal h-full" style=${{ width: tablesPct + '%' }}></div>
                </div>
              </div>
            ` : null}
            ${progress.rows_total ? html`
              <div>
                <div class="text-xs">
                  Linhas (${progress.table || 'tabela'}): ${progress.rows_done}/${progress.rows_total}
                </div>
                <div class="w-full bg-wa-border rounded h-1.5 mt-1 overflow-hidden">
                  <div class="bg-emerald-500 h-full" style=${{ width: rowsPct + '%' }}></div>
                </div>
              </div>
            ` : null}
            ${progress.error ? html`
              <div class="text-xs text-red-700 bg-red-50 rounded p-2">${progress.error}</div>
            ` : null}
            ${progress.stage === 'failed' && (progress.conflicts || []).length > 0 ? html`
              <${WipeAndRetryPanel}
                conflicts=${progress.conflicts}
                target=${target}
                running=${running}
                confirming=${confirmingWipe}
                ack=${wipeAck}
                onAckChange=${setWipeAck}
                onAskConfirm=${() => { setConfirmingWipe(true); setWipeAck(''); }}
                onCancel=${() => { setConfirmingWipe(false); setWipeAck(''); }}
                onConfirm=${() => start({ forceDrop: true })}
              />
            ` : null}
            ${progress.stage === 'done' ? html`
              <div class="text-xs text-emerald-700">O servidor está reiniciando para usar o novo banco…</div>
            ` : null}
          </div>
        ` : null}
      </div>
    </div>
  `;
}


function WipeAndRetryPanel({ conflicts, target, running, confirming, ack, onAckChange, onAskConfirm, onCancel, onConfirm }) {
  const expectedAck = 'APAGAR TUDO';
  const canConfirm = ack.trim() === expectedAck && !running;

  if (!confirming) {
    return html`
      <div class="rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs text-amber-900 flex flex-col gap-2">
        <div class="font-medium">
          O banco destino não está vazio. Tabelas encontradas:
        </div>
        <ul class="font-mono list-disc pl-5 max-h-32 overflow-auto">
          ${conflicts.map((t) => html`<li key=${t}>${t}</li>`)}
        </ul>
        <p>
          Você pode apagar essas tabelas (e qualquer outra no schema <code>public</code>)
          e tentar a migração de novo. Operação destrutiva — leia o aviso antes de confirmar.
        </p>
        <button
          onClick=${onAskConfirm}
          class="self-start px-3 py-1.5 bg-red-600 hover:bg-red-700 text-white text-xs font-medium rounded-lg"
        >Apagar destino e retentar</button>
      </div>
    `;
  }

  return html`
    <div class="rounded-lg border border-red-400 bg-red-50 p-3 text-xs text-red-900 flex flex-col gap-2">
      <div class="font-bold text-sm">⚠️ Atenção: ação destrutiva irreversível</div>
      <p>
        O Whatsbot-lite vai executar <code>DROP SCHEMA public CASCADE</code> em:
      </p>
      <div class="font-mono break-all bg-wa-bg/60 rounded px-2 py-1">${target}</div>
      <p>
        Isso apaga <strong>TODAS</strong> as tabelas, índices e sequências do schema <code>public</code>
        deste banco — incluindo dados que não pertencem ao Whatsbot-lite, se houver.
        <strong>Não há rollback.</strong>
      </p>
      <p>
        Confirme que esse banco é exclusivo do Whatsbot-lite e que você tem backup.
        Digite <code class="font-bold">${expectedAck}</code> abaixo para liberar o botão:
      </p>
      <input
        type="text"
        value=${ack}
        onInput=${(e) => onAckChange(e.target.value)}
        placeholder=${expectedAck}
        class="rounded border border-red-300 px-2 py-1 text-sm font-mono"
        autofocus
      />
      <div class="flex gap-2">
        <button
          onClick=${onConfirm}
          disabled=${!canConfirm}
          class="px-3 py-1.5 bg-red-600 hover:bg-red-700 disabled:opacity-40 disabled:cursor-not-allowed text-white text-xs font-medium rounded-lg"
        >Apagar tudo e migrar</button>
        <button
          onClick=${onCancel}
          class="px-3 py-1.5 bg-wa-border hover:bg-wa-hover text-xs rounded-lg"
        >Cancelar</button>
      </div>
    </div>
  `;
}
