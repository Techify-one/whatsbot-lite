// Lógica PURA do Importar/Exportar (sem DOM, sem rede): testável com
// `node --test tests/portability-logic.test.cjs`. Os componentes de components/portability/ só desenham
// o que sai daqui.
//
// O servidor manda CÓDIGO de aviso (portability/warnings.py), nunca texto com dado: a frase vive só
// aqui. tests/test_portability_warnings.py confere que as duas listas andam juntas.

export const MIN_PASSWORD_LEN = 12;

/** Espelho de portability/registry.py::SCOPES (rótulos pt-BR para a tela). */
export const SCOPES = [
  { id: 'settings', label: 'Configurações do sistema',
    hint: 'Preferências gerais, modelos, avisos e limites. Não leva senha do painel, identidade da instalação nem estado do WhatsApp.' },
  { id: 'catalog', label: 'Tags',
    hint: 'As tags (nome e cor) para organizar os contatos.' },
  { id: 'conversations', label: 'Contatos e conversas',
    hint: 'Contatos, observações, tags de cada contato e todo o histórico de mensagens (inclusive notas privadas). É o que os clientes escreveram: guarde o pacote com cuidado.' },
  { id: 'plugins', label: 'Plugins',
    hint: 'O código dos plugins que você escolher, a configuração e os dados deles. Ao importar, o plugin chega desligado e pede a sua confirmação: ele é código que passa a rodar neste servidor.' },
  { id: 'agents', label: 'Agentes de IA',
    hint: 'Agentes, prompts, variáveis, ajustes de ferramentas e as ferramentas com código. O código das ferramentas é Python: ao importar, elas chegam desligadas e pedem a sua confirmação.' },
];

export const ENTITY_LABELS = {
  config: 'Configurações do sistema',
  tags: 'Tags',
  contacts: 'Contatos',
  messages: 'Mensagens',
  media: 'Arquivos de mídia',
  avatars: 'Fotos de perfil',
  ai_variables: 'Variáveis da IA',
  ai_prompts: 'Prompts da IA',
  ai_agents: 'Agentes de IA',
  ai_tools: 'Ferramentas com código',
  tool_overrides: 'Ajustes de ferramentas',
  plugins: 'Plugins (código)',
  plugin_config: 'Configuração dos plugins',
  plugin_data: 'Dados dos plugins',
};

/** Os valores internos (`skip`/`merge`/`overwrite`) não mudam — só o texto. "Manter o que já existe" era lido
 *  como "não importar nada", mas ele só deixa intacto o que já existe aqui: o que falta é criado. */
export const STRATEGIES = ['skip', 'merge', 'overwrite'];

export const STRATEGY_LABELS = {
  skip: 'Só adicionar o que falta',
  merge: 'Completar o que existe',
  overwrite: 'Substituir pelo do pacote',
};

export const STRATEGY_HINTS = {
  skip: 'Cria os itens novos e não mexe nos que já existem aqui.',
  merge: 'Mantém o que já está preenchido aqui e só preenche o que estiver vazio ou faltando.',
  overwrite: 'Troca o item daqui pelo que veio no pacote.',
};

/** Linhas que só aceitam "só adicionar" (o servidor nem lê o modo): mensagens. */
export const FIXED_HINT = 'Mensagens que já existem aqui nunca são alteradas nem duplicadas.';

export const STRATEGY_COLUMN_LABEL = 'Se já existir neste servidor';
export const SKIP_COLUMN_LABEL = 'Sem alteração';
export const SKIP_COLUMN_HINT = 'Idênticos, ou já existentes aqui e mantidos pela opção escolhida.';

/** Código de aviso → frase pt-BR. Espelho de portability/warnings.py::WARNING_CODES. */
export const WARNING_LABELS = {
  key_not_importable: 'Chave que não pode ser importada (estado ou segurança) — ignorada.',
  secret_missing: 'O segredo não veio no pacote — será preciso informar de novo.',
  secret_kept: 'Já existe um segredo aqui; ele foi mantido.',
  invalid_value: 'Valor inválido — não foi gravado.',
  env_override_active: 'Uma variável de ambiente deste servidor vai se sobrepor ao valor importado — ajuste a variável para valer.',
  invalid_row: 'Registro inválido — ignorado.',
  row_failed: 'Falhou ao processar o registro.',
  duplicate_in_package: 'O pacote tinha o mesmo número duas vezes (com e sem o 9) — foram unidos em um contato.',
  tag_unknown: 'Tag que não existe aqui nem veio no pacote — não aplicada ao contato.',
  contact_unavailable: 'O contato da mensagem não foi importado — mensagem ignorada.',
  agent_prompt_missing: 'O agente aponta para um prompt que não existe aqui nem veio no pacote — ele usa o prompt padrão.',
  agent_tool_missing: 'O agente lista uma ferramenta que não existe aqui nem veio no pacote — ela não ficará disponível para ele.',
  tool_imported_disabled: 'Ferramenta com código chegou DESLIGADA. Leia o código e ligue-a manualmente se confiar nela.',
  tool_replaced_disabled: 'Ferramenta com código foi SUBSTITUÍDA pela do pacote e ficou DESLIGADA até você ligá-la de novo.',
  tool_code_not_accepted: 'O pacote traz código de ferramenta e a confirmação não foi dada — nada foi gravado.',
  tool_has_dependencies: 'A ferramenta pede pacotes pip: eles serão instalados quando você a ligar e reiniciar.',
  override_tool_unknown: 'Ajuste de uma ferramenta que não está registrada aqui (plugin ausente?) — pulado.',
  plugin_code_unavailable: 'O pacote não traz o código deste plugin — ele não foi instalado.',
  plugin_package_invalid: 'O .zip do plugin é inseguro, grande demais ou o manifest não confere — o plugin não foi instalado.',
  plugin_api_incompatible: 'O plugin pede uma versão da API que esta instalação não tem — não foi instalado.',
  plugin_downgrade_skipped: 'O pacote traz uma versão MENOR do plugin do que a instalada aqui — a versão daqui foi mantida.',
  plugin_orphan_dir: 'Já existe uma pasta com o nome deste plugin, sem registro — por segurança nada foi trocado.',
  plugin_has_dependencies: 'O plugin pede pacotes pip: eles serão instalados quando você ativá-lo.',
  plugin_installed_disabled: 'Plugin instalado DESLIGADO. Leia o que ele faz e ative-o manualmente se confiar nele.',
  plugin_replaced_disabled: 'Plugin ATUALIZADO pelo do pacote e DESLIGADO — ative-o de novo quando quiser usá-lo.',
  plugin_code_not_accepted: 'O pacote traz código de plugin e a confirmação não foi dada — nada foi gravado.',
  plugin_unknown: 'Plugin que não está instalado aqui nem veio no pacote — pulado.',
  plugin_data_pending: 'Os dados do plugin esperam: ative o plugin, reinicie o servidor e clique em Continuar.',
  plugin_table_unknown: 'O plugin não tem esta tabela (outra versão, ou nome forjado) — pulada.',
  plugin_table_not_empty: 'A tabela do plugin aqui já tem dados — foi pulada inteira para não misturar.',
  plugin_row_id_conflict: 'Linha de plugin com número já ocupado aqui — ficou a do destino.',
  plugin_table_failed: 'A tabela do plugin falhou e nada dela foi gravado.',
  plugin_table_truncated: 'Tabela de plugin grande demais: só as primeiras linhas foram para o pacote.',
  plugin_blob_dropped: 'Coluna binária de plugin não viaja — ficou vazia.',
  environment_value_dropped: 'Configuração de plugin que aponta para a própria máquina (localhost) — não foi levada.',
  secret_dropped: 'Coluna secreta de uma linha de plugin sem chave primária — não pôde ser cifrada e foi omitida.',
  reference_cleared: 'A linha de plugin apontava para um contato que não existe aqui — ficou sem vínculo.',
  reference_missing: 'A linha de plugin apontava para um contato que não existe aqui — foi ignorada.',
  media_not_included: 'O pacote não traz o arquivo de algumas mensagens — entram só com o texto.',
  media_missing: 'O arquivo da mídia não existe mais no disco — a mensagem vai só com o texto.',
  media_out_of_period: 'Mídia fora do período escolhido — a mensagem vai só com o texto.',
  media_too_large: 'Arquivo grande demais para o pacote — a mensagem vai só com o texto.',
  media_limit_reached: 'O limite de tamanho do pacote foi atingido — o restante das mídias ficou de fora.',
  media_failed: 'Não foi possível gravar o arquivo de mídia — a mensagem entrou só com o texto.',
  media_disk_full: 'Sem espaço em disco: as mídias restantes não foram gravadas (as mensagens entraram).',
  avatar_failed: 'A foto de perfil do contato não pôde ser importada (arquivo inválido ou sem espaço) — o contato entrou sem ela.',
  avatar_too_large: 'A foto de perfil do contato passa do limite de tamanho — ficou de fora do pacote.',
};

export const NOTE_LABELS = {
  alembic_head_differs: 'O pacote veio de uma versão diferente do banco. Confira o resultado com atenção.',
  same_instance: 'Este pacote foi exportado DESTA mesma instalação.',
};

export function warningLabel(code) {
  const base = String(code).split(':')[0];
  return WARNING_LABELS[base] || `Aviso: ${base}`;
}

// ── estado do job ───────────────────────────────────────────────────────────

export const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);
const STATE_LABELS = {
  queued: 'Na fila',
  running: 'Em andamento',
  succeeded: 'Concluído',
  failed: 'Falhou',
  cancelled: 'Cancelado',
  awaiting_confirmation: 'Aguardando confirmação',
  awaiting_restart: 'Aguardando',
};
const INTERRUPTED_LABEL = 'Interrompido — pode continuar';
const WAITING_PLUGINS_LABEL = 'Esperando o plugin ser ativado';

export function stateLabel(state, job) {
  if (job && job.interrupted && state === 'awaiting_restart') return INTERRUPTED_LABEL;
  if (job && job.resume_stage === 'plugins' && state === 'awaiting_restart') return WAITING_PLUGINS_LABEL;
  return STATE_LABELS[state] || String(state || '');
}

/** Para de consultar quando o job terminou OU está esperando uma ação humana. */
export function shouldStopPolling(job) {
  if (!job) return false;
  return TERMINAL.has(job.state) || job.state === 'awaiting_confirmation'
    || job.state === 'awaiting_restart';
}

/** Percentual 0..100 inteiro; null quando não dá para saber (barra indeterminada). */
export function percentOf(job) {
  // Concluído com sucesso = 100%, qualquer que seja o último checkpoint gravado.
  if (job && job.state === 'succeeded') return 100;
  const p = job && job.progress && job.progress.percent;
  if (typeof p !== 'number' || !Number.isFinite(p)) return null;
  return Math.max(0, Math.min(100, Math.round(p)));
}

/** Frase de progresso: "Mensagens — 1.200 registros". Sem total, só a contagem. */
export function progressText(job) {
  const pr = (job && job.progress) || {};
  if (job && job.state === 'queued') return 'Aguardando na fila…';
  if (job && job.state === 'succeeded') return 'Concluído';
  if (pr.entity === 'finalize') return 'Finalizando o pacote…';
  if (!pr.entity) return job && job.state === 'running' ? 'Iniciando…' : '';
  const name = ENTITY_LABELS[pr.entity] || pr.entity;
  const done = Number.isFinite(pr.done) ? pr.done : 0;
  return `${name} — ${done.toLocaleString('pt-BR')} ${done === 1 ? 'registro' : 'registros'}`;
}

/** Intervalo do próximo poll: 2 s; dobra a cada falha seguida até 10 s. */
export function nextPollDelay(failures) {
  const n = Math.max(0, failures | 0);
  return Math.min(10_000, 2_000 * 2 ** n);
}

// ── tamanho ─────────────────────────────────────────────────────────────────

/** Espelho de MAX_IMPORT_BYTES em server/routes/portability.py. */
export const MAX_IMPORT_BYTES = 5 * 1024 * 1024 * 1024;

export function formatBytes(n) {
  const v = Number(n) || 0;
  if (v < 1024) return `${v} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let x = v / 1024;
  let i = 0;
  while (x >= 1024 && i < units.length - 1) { x /= 1024; i += 1; }
  return `${x.toLocaleString('pt-BR', { maximumFractionDigits: x >= 100 ? 0 : 1 })} ${units[i]}`;
}

// ── senha ───────────────────────────────────────────────────────────────────

/** Erro de pt-BR para o campo de senha do EXPORT, ou null se está ok. */
export function passwordError(password, confirm) {
  if (typeof password !== 'string' || password.length < MIN_PASSWORD_LEN) {
    return `A senha precisa ter pelo menos ${MIN_PASSWORD_LEN} caracteres.`;
  }
  if (password !== confirm) return 'As senhas não conferem.';
  return null;
}

// ── export ──────────────────────────────────────────────────────────────────

/** `AAAA-MM-DD` (campo de data) → epoch do início do dia no fuso do operador; null se vazio/inválido. */
export function dayStart(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ''));
  if (!m) return null;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 0, 0, 0, 0);
  return Number.isNaN(d.getTime()) || d.getMonth() !== Number(m[2]) - 1 ? null : d.getTime() / 1000;
}

/** Fim do dia (23:59:59.999). */
export function dayEnd(value) {
  const start = dayStart(value);
  return start === null ? null : start + 86399.999;
}

/** Corpo de POST /exports, ou `{error}`. Nunca monta `password` sem segredos. */
export function buildExportRequest({
  scopes, includeSecrets, password, confirm, includeMedia = false, mediaFrom = '', mediaTo = '',
  includeAvatars = false, pluginIds = [],
}) {
  if (!Array.isArray(scopes) || scopes.length === 0) {
    return { error: 'Escolha ao menos uma coisa para exportar.' };
  }
  const body = { scopes, include_secrets: !!includeSecrets };
  if (scopes.includes('plugins')) {
    const ids = Array.isArray(pluginIds) ? pluginIds.filter((i) => typeof i === 'string' && i) : [];
    if (!ids.length) return { error: 'Marque ao menos um plugin para exportar.' };
    body.plugin_ids = ids;
  }
  // Fotos: DESLIGADAS por padrão no Lite (o cache se refaz sozinho), então só o "ligar" vai no corpo.
  if (scopes.includes('conversations') && includeAvatars) body.include_avatars = true;
  if (includeMedia) {
    if (!scopes.includes('conversations')) {
      return { error: 'A mídia pertence às conversas: marque “Contatos e conversas”.' };
    }
    const lo = dayStart(mediaFrom);
    const hi = dayEnd(mediaTo);
    if ((mediaFrom && lo === null) || (mediaTo && hi === null)) return { error: 'Data do período inválida.' };
    if (lo !== null && hi !== null && lo > hi) return { error: 'O período de mídia está invertido.' };
    body.include_media = true;
    if (lo !== null) body.media_from = lo;
    if (hi !== null) body.media_to = hi;
  }
  if (includeSecrets) {
    const err = passwordError(password, confirm);
    if (err) return { error: err };
    body.password = password;
  }
  return { body };
}

/** Resumo de uma linha do que um job de export produziu. */
export function exportSummary(report) {
  const counts = (report && report.counts) || {};
  const total = Object.values(counts).reduce((s, n) => s + (n | 0), 0);
  return {
    total,
    bytes: (report && report.bytes) | 0,
    secrets: !!(report && report.secrets_included),
  };
}

// ── import interrompido / retomada depois de um F5 ──────────────────────────

/** O servidor perdeu este import no meio (reinício)? O que já entrou fica; "Continuar" refaz o resto. */
export function isInterrupted(job) {
  return !!job && job.state === 'awaiting_restart' && job.interrupted === true;
}

/** Há o botão "Continuar" (interrompido pelo reinício OU esperando a 2ª etapa dos plugins)? */
export function canContinue(job) {
  return isInterrupted(job) || isWaitingPlugins(job);
}

/** O import espera a 2ª etapa dos plugins (ativar o plugin, reiniciar e continuar)? */
export function isWaitingPlugins(job) {
  return !!job && job.state === 'awaiting_restart' && job.resume_stage === 'plugins' && !job.interrupted;
}

/** Plugins cujas tabelas ainda esperam: `['demo', ...]` (sem repetir), vindos do relatório do job. */
export function pendingPlugins(job) {
  const list = (job && job.report && job.report.pending_plugin_data) || [];
  return [...new Set((Array.isArray(list) ? list : []).map((t) => String(t).split('/')[0]))].sort();
}

/** Plugins que o import ligou (opt-in "ativar"): só faltam o reinício e o "Continuar". */
export function activatedPlugins(job) {
  const list = (job && job.report && job.report.plugins_activated) || [];
  return (Array.isArray(list) ? list : []).map(String);
}

/** Texto da espera. Não é falha: o que já entrou fica, e os dados esperam o plugin existir. */
export function waitingPluginsText(job) {
  const names = pendingPlugins(job);
  const on = activatedPlugins(job);
  if (on.length) {
    return `A primeira etapa terminou e os plugins (${on.join(', ')}) foram ativados, mas só passam a valer depois `
      + 'de o servidor reiniciar. O que já entrou fica e nada será duplicado.';
  }
  const who = names.length ? ` (${names.join(', ')})` : '';
  return 'A primeira etapa terminou, mas os dados de plugin ainda não podem entrar: a tabela só existe depois de '
    + `o plugin ser ativado e o servidor reiniciar${who}. Ative o plugin na aba Plugins, aguarde o servidor `
    + 'reiniciar e volte aqui para continuar. O que já entrou fica e nada será duplicado.';
}

/** Texto do import interrompido. Não é falha: nada do que entrou se perde nem duplica. */
export function interruptedText(job) {
  const pw = job && job.needs_password
    ? ' Informe a senha do pacote de novo (ela só existia na memória do servidor que reiniciou).' : '';
  return 'A importação foi interrompida porque o servidor reiniciou. O que já foi importado continua aqui '
    + `e nada será duplicado ao continuar.${pw}`;
}

/**
 * Depois de um F5: o que a tela deve retomar, olhando só os jobs do servidor (do mais novo para o mais
 * antigo). `dismissed` = id da verificação que o operador descartou.
 *  - import em andamento ou interrompido → `{type:'import', job}`;
 *  - senão, a verificação MAIS NOVA que ainda não virou import → `dryrun` (rodando) ou `dry` (pronta);
 *  - senão `null`.
 */
export function findResumable(jobs, { dismissed = '', now = Date.now() / 1000 } = {}) {
  const rows = Array.isArray(jobs) ? jobs : [];
  const imports = rows.filter((j) => j && j.kind === 'import');
  const active = imports.find((j) => ['queued', 'running', 'awaiting_restart'].includes(j.state));
  if (active) return { type: 'import', job: active };
  // Import que falhou/foi cancelado NÃO consome a verificação: o pacote continua lá e reimportar é idempotente.
  const used = new Set(imports.filter((j) => j.state !== 'failed' && j.state !== 'cancelled')
    .map((j) => j.source_job).filter(Boolean));
  const dry = rows.find((j) => j && j.kind === 'import_dryrun');
  if (!dry || used.has(dry.id) || dry.id === dismissed) return null;
  if (dry.state === 'queued' || dry.state === 'running') return { type: 'dryrun', job: dry };
  if (dry.state === 'succeeded' && dry.has_package && (!dry.expires_at || dry.expires_at > now)) {
    return { type: 'dry', job: dry };
  }
  return null;
}

// ── relatório da verificação / importação ───────────────────────────────────

const OUTCOME_ORDER = ['create', 'update', 'skip', 'error'];

/** Linhas da tabela: `[{entity, label, create, update, skip, error, total, changes}]`. */
export function entityRows(report) {
  const ents = (report && report.entities) || {};
  return Object.keys(ents).map((entity) => {
    const t = ents[entity] || {};
    const row = { entity, label: ENTITY_LABELS[entity] || entity };
    let total = 0;
    for (const k of OUTCOME_ORDER) {
      row[k] = t[k] | 0;
      total += row[k];
    }
    row.total = total;
    row.changes = row.create + row.update;
    return row;
  });
}

/** Total de registros que o import ALTERARIA (criar + atualizar). */
export function totalChanges(report) {
  return entityRows(report).reduce((s, r) => s + r.changes, 0);
}

/** `[{entity, entityLabel, code, text, count, keys}]` — avisos achatados e legíveis. */
export function warningRows(report) {
  const out = [];
  const w = (report && report.warnings) || {};
  for (const entity of Object.keys(w)) {
    for (const code of Object.keys(w[entity] || {})) {
      const item = w[entity][code] || {};
      out.push({
        entity, entityLabel: ENTITY_LABELS[entity] || entity, code,
        text: warningLabel(code), count: item.count | 0,
        keys: Array.isArray(item.keys) ? item.keys : [],
      });
    }
  }
  return out;
}

export function noteTexts(report) {
  const notes = (report && report.notes) || [];
  return notes.map((n) => NOTE_LABELS[n] || n);
}

/** Estratégias válidas por entidade, vindas do servidor (fallback: só "adicionar"). */
export function strategyChoices(report, entity) {
  const list = report && report.supported_strategies && report.supported_strategies[entity];
  return Array.isArray(list) && list.length ? list : ['skip'];
}

/** Estratégia inicial = a que a prévia USOU (`report.strategy`); relatório antigo cai no padrão. */
export function initialStrategy(report) {
  return { ...((report && (report.strategy || report.default_strategy)) || {}) };
}

/** Segredos do pacote: 'absent' | 'locked' | 'ok' (o que a verificação conseguiu fazer). */
export function secretsState(report) {
  return (report && report.manifest && report.manifest.secrets) || 'absent';
}

/** O confirm EXIGE senha quando o pacote tem segredos (travados ou lidos). */
export function confirmNeedsPassword(report) {
  const s = secretsState(report);
  return s === 'locked' || s === 'ok';
}

/** Quantas ferramentas COM CÓDIGO o pacote traz (a verificação conta mesmo as que a estratégia pularia). */
export function codeToolsTotal(report) {
  return (report && report.code_tools_total) | 0;
}

/** Quantos plugins COM CÓDIGO o pacote traz. */
export function codePluginsTotal(report) {
  return (report && report.code_plugins_total) | 0;
}

/** O pacote traz código de qualquer tipo (ferramenta de IA ou plugin)? É o que exige a confirmação. */
export function codeTotal(report) {
  return codeToolsTotal(report) + codePluginsTotal(report);
}

/** Lista de plugins para o popup: `[{id, text}]`. */
export function codePluginRows(report) {
  const list = (report && Array.isArray(report.code_plugins)) ? report.code_plugins : [];
  const TEXT = {
    new: 'novo (chega desligado)', newer: 'versão maior que a instalada', same: 'mesma versão da instalada',
    older: 'versão menor que a instalada (não rebaixa)', orphan: 'pasta sem registro (não é trocado)',
  };
  return list.map((p) => ({
    id: String(p.id),
    text: `${TEXT[p.state] || 'novo (chega desligado)'}${p.version ? ` · v${p.version}` : ''}`
      + `${p.dependencies ? ' · pede pacotes pip' : ''}`,
  }));
}

/** Lista para o popup: `[{name, text}]`. */
export function codeToolRows(report) {
  const list = (report && Array.isArray(report.code_tools)) ? report.code_tools : [];
  const TEXT = { new: 'nova', same: 'igual à que já existe', differs: 'diferente da que já existe' };
  return list.map((t) => ({
    name: String(t.name), text: `${TEXT[t.state] || 'nova'}${t.dependencies ? ' · pede pacotes pip' : ''}`,
  }));
}

/** Corpo de POST /imports/{id}/confirm. `acknowledged` é o "Revisei" do popup: o servidor também o exige. */
export function buildConfirmRequest({ report, strategy, password, overwriteSecrets, acceptCode = false,
  activatePlugins = false }) {
  if (confirmNeedsPassword(report) && !password) {
    return { error: 'Este pacote tem segredos: informe a senha para importá-los.' };
  }
  const body = { strategy: { ...strategy }, acknowledged: true };
  if (password) body.password = password;
  if (overwriteSecrets) body.overwrite_secrets = true;
  // Pacote com código de ferramenta: a confirmação é separada e o servidor também a exige.
  if (codeTotal(report) > 0) {
    if (!acceptCode) return { error: 'Este pacote traz código (ferramentas de IA ou plugins): marque que você revisou a lista.' };
    body.accept_code = true;
    // Ligar os plugins do pacote é opt-in e só existe junto da confirmação de código (o servidor também exige).
    if (activatePlugins && codePluginRows(report).length > 0) body.activate_plugins = true;
  }
  return { body };
}

// ── estratégia: fixa, em lote, prévia desatualizada ─────────────────────────

/** A linha só aceita "só adicionar" (um único modo oferecido pelo servidor)? */
export function isFixedStrategy(report, entity) {
  const list = strategyChoices(report, entity);
  return list.length === 1 && list[0] === 'skip';
}

/**
 * Aplica `mode` às linhas marcadas. `mode` = uma estratégia ou `'default'` (volta ao padrão do servidor).
 * Linha que não suporta o modo NÃO muda e conta em `ignored`. Puro: devolve um objeto novo.
 * @returns {{strategy: Record<string,string>, applied: number, ignored: number}}
 */
export function applyBulkStrategy(report, strategy, selected, mode) {
  const next = { ...strategy };
  const defaults = (report && report.default_strategy) || {};
  let applied = 0;
  let ignored = 0;
  for (const entity of selected) {
    const supported = strategyChoices(report, entity);
    const target = mode === 'default' ? (defaults[entity] || 'skip') : mode;
    if (!supported.includes(target)) { ignored += 1; continue; }
    next[entity] = target;
    applied += 1;
  }
  return { strategy: next, applied, ignored };
}

/** Frase da barra de lote depois de aplicar ("3 linhas alteradas; 2 ignoradas…"). */
export function bulkResultText({ applied, ignored }) {
  const parts = [];
  if (applied) parts.push(`${applied} ${applied === 1 ? 'linha alterada' : 'linhas alteradas'}`);
  if (ignored) {
    parts.push(`${ignored} ${ignored === 1 ? 'ignorada' : 'ignoradas'} (não ${ignored === 1 ? 'aceita' : 'aceitam'} essa opção)`);
  }
  return parts.join('; ');
}

/** A seleção atual difere da estratégia com que a prévia foi calculada? (`report.strategy`) */
export function isPreviewStale(report, strategy) {
  const used = (report && report.strategy) || (report && report.default_strategy) || {};
  const names = Object.keys((report && report.entities) || {});
  return names.some((e) => (strategy[e] || used[e] || 'skip') !== (used[e] || 'skip'));
}

/** Linhas que serão SUBSTITUÍDAS pelo pacote (o mais destrutivo): `[{entity, label}]`. */
export function overwriteRows(report, strategy) {
  return entityRows(report)
    .filter((r) => (strategy[r.entity] || 'skip') === 'overwrite')
    .map((r) => ({ entity: r.entity, label: r.label }));
}

// ── avisos recolhíveis ──────────────────────────────────────────────────────

/** Quantos avisos (soma das ocorrências) + quantas notas. Vira o título "N avisos". */
export function warningsCount(report) {
  const fromWarnings = warningRows(report).reduce((s, w) => s + (w.count || 0), 0);
  return fromWarnings + noteTexts(report).length;
}

export function warningsTitle(report) {
  const n = warningsCount(report);
  return `${n} ${n === 1 ? 'aviso' : 'avisos'}`;
}

// ── erros por linha ─────────────────────────────────────────────────────────

/** Entidades do relatório com erro: `[{entity, label, error}]`. */
export function entitiesWithErrors(report) {
  return entityRows(report).filter((r) => r.error > 0)
    .map((r) => ({ entity: r.entity, label: r.label, error: r.error }));
}

const ERROR_CODE_LABELS = {
  unspecified: 'Motivo não identificado',
};

/** Frase de um erro: usa a mesma tabela de avisos (o código é o mesmo), com exceções próprias. */
export function errorCodeLabel(code) {
  return ERROR_CODE_LABELS[code] || warningLabel(code);
}

/** "mostrando N de M" quando o servidor cortou no teto; `null` se está tudo ali. */
export function truncationText(totals, entity) {
  const pool = entity ? { [entity]: (totals || {})[entity] } : (totals || {});
  let total = 0;
  let stored = 0;
  for (const v of Object.values(pool)) { if (v) { total += v.total | 0; stored += v.stored | 0; } }
  return total > stored
    ? `Mostrando ${stored.toLocaleString('pt-BR')} de ${total.toLocaleString('pt-BR')} erros.` : null;
}

/** Texto do JSON que o operador copia/baixa de um erro. Só o que o servidor já mascarou. */
export function errorsAsJson(items) {
  return JSON.stringify((items || []).map((e) => ({
    entidade: e.entity, linha: e.row_index, codigo: e.code, detalhe: e.detail,
  })), null, 2);
}

/** "1 min 05 s" / "42 s". */
export function elapsedText(sec) {
  const s = Math.max(0, Math.floor(sec || 0));
  if (s < 60) return `${s} s`;
  return `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, '0')} s`;
}

/** Duração de um job ("2 min 05 s"), ou '' se não terminou. */
export function jobDuration(job) {
  if (!job || !job.started_at || !job.finished_at) return '';
  return elapsedText(job.finished_at - job.started_at);
}

// ── gate de senha do painel ─────────────────────────────────────────────────

/** A resposta do servidor é "defina uma senha antes" (403 `password_required`)? */
export function isPasswordRequired(res) {
  return !!res && res.ok === false && res.error_code === 'password_required';
}

// ── reinício automático (plugins ativados pelo import) ──────────────────────

/** Passos visíveis do overlay, na ordem. */
export const RESTART_STEPS = [
  { id: 'saving', label: 'Salvando o estado da importação' },
  { id: 'restarting', label: 'Reiniciando o servidor' },
  { id: 'back', label: 'Servidor voltou' },
  { id: 'resuming', label: 'Retomando a importação' },
  { id: 'done', label: 'Resultado' },
];

/** Depois deste tempo a mensagem muda para "demorando mais que o normal" (sem prazo duro de falha). */
export const RESTART_SLOW_AFTER_SEC = 180;

/** O servidor voltou? Só pela TROCA do `boot_id` (a heurística "caiu e voltou" engana com --reload). */
export function serverBack(before, current) {
  return !!before && !!current && before !== current;
}
