import { h } from 'preact';
import { useState, useEffect } from 'preact/hooks';
import htm from 'htm';
import { testApiKey, checkForUpdates, performUpdate, markAllUnread, markAllRead } from '../services/api.js';
import { ModelSelect } from './ModelSelect.js';
import { DatabaseSettings } from './DatabaseSettings.js';
import { GowaSettings } from './GowaSettings.js';

const html = htm.bind(h);

const PANEL_TABS = [
  { id: 'agent', slug: 'agente', label: 'Agente' },
  { id: 'models', slug: 'modelos-midia', label: 'Modelos e mídia' },
  { id: 'system', slug: 'sistema', label: 'Sistema' },
];

const PANEL_TARGET_TABS = {
  agente: 'agent',
  automation: 'agent',
  'auto-reply': 'agent',
  'default-ai': 'agent',
  groups: 'agent',
  'ai-reactivation': 'agent',
  'ai-behavior': 'agent',
  prompt: 'agent',
  behavior: 'agent',
  context: 'agent',
  batch: 'agent',
  'split-messages': 'agent',
  'transfer-alert': 'agent',
  'low-balance': 'agent',
  'mark-conversations': 'agent',
  'modelos-midia': 'models',
  'api-models': 'models',
  'api-key': 'models',
  'improvement-model': 'models',
  model: 'models',
  'image-transcription': 'models',
  'document-transcription': 'models',
  'audio-transcription': 'models',
  'download-midia': 'models',
  'media-autodownload': 'models',
  sistema: 'system',
  advanced: 'system',
  'max-executions': 'system',
  password: 'system',
  update: 'system',
  'update-notifications': 'system',
  database: 'system',
  'database-migration': 'system',
  gowa: 'system',
  'gowa-version': 'system',
  'gowa-proxy': 'system',
  'gowa-auto-update': 'system',
};

function panelTabFromLocation() {
  let target = '';
  try {
    target = decodeURIComponent(window.location.hash.slice(1));
  } catch {
    target = window.location.hash.slice(1);
  }
  if (PANEL_TARGET_TABS[target]) return PANEL_TARGET_TABS[target];

  const slug = new URLSearchParams(window.location.search).get('aba') || '';
  return PANEL_TABS.find((tab) => tab.slug === slug)?.id || 'agent';
}

function panelTabHref(tabId) {
  const tab = PANEL_TABS.find((item) => item.id === tabId) || PANEL_TABS[0];
  return `/painel?aba=${tab.slug}`;
}

function Section({ id, title, active = true, children }) {
  return html`
    <div id=${id || undefined} class="${active ? '' : 'hidden'} bg-wa-bg rounded-xl p-5 border border-wa-border shadow-sm scroll-mt-4">
      ${title ? html`
        <h3 class="text-xs font-semibold text-wa-secondary uppercase tracking-wider mb-4">${title}</h3>
      ` : null}
      <div class="flex flex-col gap-4">
        ${children}
      </div>
    </div>
  `;
}

// Rows of the auto-download matrix. Stickers share the "Fotos" row.
const MEDIA_ROWS = [
  { id: 'audio', label: 'Áudios', icon: '🎧' },
  { id: 'image', label: 'Fotos e figurinhas', icon: '🖼️' },
  { id: 'video', label: 'Vídeos', icon: '🎬' },
  { id: 'document', label: 'Documentos', icon: '📄' },
];

// Religar a IA automaticamente (server/ai_reactivation.py). Dias da semana: 0 = segunda.
const REACTIVATION_UNITS = [
  { id: 'minutes', label: 'Minutos sem mensagens' },
  { id: 'hours', label: 'Horas sem mensagens' },
  { id: 'days', label: 'Dias sem mensagens' },
  { id: 'schedule', label: 'Dias da semana e horário' },
];

const REACTIVATION_OWNERS = [
  { id: 'both', label: 'Cliente ou atendente (qualquer um)' },
  { id: 'customer', label: 'Cliente' },
  { id: 'attendant', label: 'Usuário/atendente' },
];

const WEEKDAYS = [
  { id: 0, label: 'Seg', full: 'segunda-feira' },
  { id: 1, label: 'Ter', full: 'terça-feira' },
  { id: 2, label: 'Qua', full: 'quarta-feira' },
  { id: 3, label: 'Qui', full: 'quinta-feira' },
  { id: 4, label: 'Sex', full: 'sexta-feira' },
  { id: 5, label: 'Sáb', full: 'sábado' },
  { id: 6, label: 'Dom', full: 'domingo' },
];

const REACTIVATION_VALUE_HINT = {
  minutes: 'minutos',
  hours: 'horas',
  days: 'dias',
};

const DEFAULT_AUTODOWNLOAD = {
  audio: { individual: true, group: true },
  image: { individual: false, group: false },
  video: { individual: false, group: false },
  document: { individual: false, group: false },
};

export function ConfigPanel({ config, saving, onSave, onNotify }) {
  const [activeTab, setActiveTab] = useState(panelTabFromLocation);
  const [apiKey, setApiKey] = useState('');
  const [model, setModel] = useState('');
  const [improvementModel, setImprovementModel] = useState('');
  const [systemPrompt, setSystemPrompt] = useState('');
  const [autoReply, setAutoReply] = useState(true);
  const [maxContext, setMaxContext] = useState(10);
  const [batchDelay, setBatchDelay] = useState(3);
  const [splitMessages, setSplitMessages] = useState(true);
  const [splitDelay, setSplitDelay] = useState(2);
  const [audioTranscriptionMode, setAudioTranscriptionMode] = useState('received');
  const [audioTranscriptionTarget, setAudioTranscriptionTarget] = useState('private');
  const [audioTranscriptionChatPrefix, setAudioTranscriptionChatPrefix] = useState('');
  const [imageTranscriptionEnabled, setImageTranscriptionEnabled] = useState(true);
  const [documentTranscriptionEnabled, setDocumentTranscriptionEnabled] = useState(true);
  const [transferAlertEnabled, setTransferAlertEnabled] = useState(true);
  const [transferAlertDuration, setTransferAlertDuration] = useState(5);
  const [lowBalanceEnabled, setLowBalanceEnabled] = useState(true);
  const [lowBalanceThreshold, setLowBalanceThreshold] = useState(0.5);
  const [gowaAutoCheck, setGowaAutoCheck] = useState(true);
  const [updateNotificationsEnabled, setUpdateNotificationsEnabled] = useState(true);
  const [maxExecutions, setMaxExecutions] = useState(200);
  const [confirmUnreadAll, setConfirmUnreadAll] = useState(false);
  const [markingAllUnread, setMarkingAllUnread] = useState(false);
  const [confirmReadAll, setConfirmReadAll] = useState(false);
  const [markingAllRead, setMarkingAllRead] = useState(false);
  const [defaultAiEnabled, setDefaultAiEnabled] = useState(true);
  const [groupReplyMode, setGroupReplyMode] = useState('mention_only');
  const [reactEnabled, setReactEnabled] = useState(false);
  const [reactUnit, setReactUnit] = useState('hours');
  const [reactValue, setReactValue] = useState(24);
  const [reactWeekdays, setReactWeekdays] = useState([0, 1, 2, 3, 4]);
  const [reactTime, setReactTime] = useState('08:00');
  const [reactLastSender, setReactLastSender] = useState('both');
  const [reactIncludeGroupsArchived, setReactIncludeGroupsArchived] = useState(false);
  const [mediaAutodownload, setMediaAutodownload] = useState(DEFAULT_AUTODOWNLOAD);
  const [testing, setTesting] = useState(false);
  const [webPassword, setWebPassword] = useState('');
  const [webPasswordConfirm, setWebPasswordConfirm] = useState('');
  const [removePassword, setRemovePassword] = useState(false);

  const [saveSuccess, setSaveSuccess] = useState(false);
  const [promptFullscreen, setPromptFullscreen] = useState(false);
  const [updating, setUpdating] = useState(false);
  const [currentVersion, setCurrentVersion] = useState('');
  const [latestVersion, setLatestVersion] = useState('');
  const [updateAvailable, setUpdateAvailable] = useState(false);
  const [checkingUpdate, setCheckingUpdate] = useState(false);

  // Check for updates on mount
  useEffect(() => {
    fetchVersionInfo();
  }, []);

  async function fetchVersionInfo(force = false) {
    setCheckingUpdate(true);
    try {
      const res = await checkForUpdates(force);
      if (res.ok) {
        setCurrentVersion(res.data.current_version || '');
        setLatestVersion(res.data.latest_version || '');
        setUpdateAvailable(res.data.update_available || false);
      }
    } catch (e) {}
    setCheckingUpdate(false);
  }

  // Populate form when config loads
  useEffect(() => {
    if (config) {
      setApiKey(''); // Don't show masked key in input
      setModel(config.model || '');
      setImprovementModel(config.improvement_model || '');
      setSystemPrompt(config.system_prompt || '');
      setAutoReply(config.auto_reply ?? true);
      setMaxContext(config.max_context_messages ?? 10);
      setBatchDelay(config.message_batch_delay ?? 3);
      setSplitMessages(config.split_messages ?? true);
      setSplitDelay(config.split_message_delay ?? 2);
      setAudioTranscriptionMode(config.audio_transcription_mode ?? 'received');
      setAudioTranscriptionTarget(config.audio_transcription_target ?? 'private');
      setAudioTranscriptionChatPrefix(config.audio_transcription_chat_prefix ?? '');
      setImageTranscriptionEnabled(config.image_transcription_enabled ?? true);
      setDocumentTranscriptionEnabled(config.document_transcription_enabled ?? true);
      setTransferAlertEnabled(config.transfer_alert_enabled ?? true);
      setTransferAlertDuration(config.transfer_alert_duration ?? 5);
      setLowBalanceEnabled(config.low_balance_enabled ?? true);
      setLowBalanceThreshold(config.low_balance_threshold ?? 0.5);
      setGowaAutoCheck(config.gowa_auto_check_enabled ?? true);
      setUpdateNotificationsEnabled(config.whatsbot_update_notifications_enabled ?? true);
      setMaxExecutions(config.max_executions ?? 200);
      setDefaultAiEnabled(config.default_ai_enabled ?? true);
      setGroupReplyMode(config.group_reply_mode ?? 'mention_only');
      setReactEnabled(config.ai_reactivation_enabled ?? false);
      setReactUnit(config.ai_reactivation_unit ?? 'hours');
      setReactValue(config.ai_reactivation_value ?? 24);
      setReactWeekdays(config.ai_reactivation_weekdays ?? [0, 1, 2, 3, 4]);
      setReactTime(config.ai_reactivation_time ?? '08:00');
      setReactLastSender(config.ai_reactivation_last_sender ?? 'both');
      setReactIncludeGroupsArchived(config.ai_reactivation_include_groups_archived ?? false);
      setMediaAutodownload({ ...DEFAULT_AUTODOWNLOAD, ...(config.media_autodownload || {}) });
    }
  }, [config]);

  // Help links may identify a tab, a setting, or both. Old links such as
  // /painel#prompt remain valid because the target itself selects the tab.
  useEffect(() => {
    function syncPanelLocation() {
      setActiveTab(panelTabFromLocation());
      window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
        let hash = '';
        try {
          hash = decodeURIComponent(window.location.hash.slice(1));
        } catch {
          hash = window.location.hash.slice(1);
        }
        const target = hash ? document.getElementById(hash) : document.getElementById('panel-tabs');
        if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }));
    }
    syncPanelLocation();
    window.addEventListener('hashchange', syncPanelLocation);
    window.addEventListener('popstate', syncPanelLocation);
    return () => {
      window.removeEventListener('hashchange', syncPanelLocation);
      window.removeEventListener('popstate', syncPanelLocation);
    };
  }, [!!config]);

  function selectPanelTab(event, tabId) {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    setActiveTab(tabId);
    history.pushState(null, '', panelTabHref(tabId));
    window.requestAnimationFrame(() => {
      document.getElementById('panel-tabs')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }

  const [testResult, setTestResult] = useState(null); // {ok, message}

  async function handleTestKey() {
    const key = apiKey.trim();
    if (!key) {
      onNotify('Insira uma API key primeiro.');
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      const res = await testApiKey(key);
      if (res.ok) {
        setTestResult({ ok: res.data.valid, message: res.data.message });
        onNotify(res.data.message);
        // Auto-save when key is valid
        if (res.data.valid) {
          await onSave({ openrouter_api_key: key });
        }
      } else {
        setTestResult({ ok: false, message: res.error || 'Erro ao testar.' });
        onNotify(res.error || 'Erro ao testar.');
      }
    } catch {
      setTestResult({ ok: false, message: 'Erro de conexão.' });
      onNotify('Erro de conexão.');
    }
    setTesting(false);
  }

  const handleUpdate = async () => {
    if (!confirm('Deseja atualizar o WhatsBot-Lite para a versão mais recente?\nO sistema reiniciará automaticamente após a atualização.')) return;
    setUpdating(true);
    try {
      const res = await performUpdate();
      if (res.ok) {
        onNotify(res.data?.message || 'Atualização concluída. O WhatsBot-Lite será reiniciado automaticamente.');
      } else {
        onNotify(res.error || 'Erro ao atualizar.');
      }
    } catch (e) {
      onNotify('Erro de conexão ao atualizar.');
    } finally {
      setUpdating(false);
    }
  };

  async function handleMarkAllUnread() {
    setMarkingAllUnread(true);
    try {
      const res = await markAllUnread();
      if (res.ok) {
        onNotify(`${res.data?.count ?? 0} conversa(s) marcada(s) como não lida(s).`);
      } else {
        onNotify(res.error || 'Erro ao marcar conversas.');
      }
    } catch (e) {
      onNotify('Erro de conexão ao marcar conversas.');
    } finally {
      setMarkingAllUnread(false);
      setConfirmUnreadAll(false);
    }
  }

  async function handleMarkAllRead() {
    setMarkingAllRead(true);
    try {
      const res = await markAllRead();
      if (res.ok) {
        onNotify(`${res.data?.count ?? 0} conversa(s) marcada(s) como lida(s).`);
      } else {
        onNotify(res.error || 'Erro ao marcar conversas.');
      }
    } catch (e) {
      onNotify('Erro de conexão ao marcar conversas.');
    } finally {
      setMarkingAllRead(false);
      setConfirmReadAll(false);
    }
  }

  function toggleWeekday(id) {
    setReactWeekdays((current) => (
      current.includes(id) ? current.filter((d) => d !== id) : [...current, id].sort()
    ));
  }

  async function handleSave() {
    if (reactEnabled && reactUnit === 'schedule' && reactWeekdays.length === 0) {
      onNotify('Escolha ao menos um dia da semana para religar a IA.');
      return;
    }
    const data = {
      model: model.trim() || 'deepseek/deepseek-v4.1-flash',
      improvement_model: improvementModel.trim(),
      system_prompt: systemPrompt,
      auto_reply: autoReply,
      max_context_messages: parseInt(maxContext, 10) || 10,
      message_batch_delay: isNaN(parseFloat(batchDelay)) ? 0 : parseFloat(batchDelay),
      split_messages: splitMessages,
      split_message_delay: isNaN(parseFloat(splitDelay)) ? 0 : parseFloat(splitDelay),
      audio_transcription_mode: audioTranscriptionMode,
      audio_transcription_target: audioTranscriptionTarget,
      audio_transcription_chat_prefix: audioTranscriptionChatPrefix,
      image_transcription_enabled: imageTranscriptionEnabled,
      document_transcription_enabled: documentTranscriptionEnabled,
      transfer_alert_enabled: transferAlertEnabled,
      transfer_alert_duration: parseInt(transferAlertDuration, 10) || 5,
      low_balance_enabled: lowBalanceEnabled,
      low_balance_threshold: isNaN(parseFloat(lowBalanceThreshold)) ? 0.5 : parseFloat(lowBalanceThreshold),
      gowa_auto_check_enabled: gowaAutoCheck,
      whatsbot_update_notifications_enabled: updateNotificationsEnabled,
      max_executions: parseInt(maxExecutions, 10) || 200,
      default_ai_enabled: defaultAiEnabled,
      group_reply_mode: groupReplyMode,
      ai_reactivation_enabled: reactEnabled,
      ai_reactivation_unit: reactUnit,
      ai_reactivation_value: parseInt(reactValue, 10) || 1,
      ai_reactivation_weekdays: reactWeekdays,
      ai_reactivation_time: reactTime || '08:00',
      ai_reactivation_last_sender: reactLastSender,
      ai_reactivation_include_groups_archived: reactIncludeGroupsArchived,
      // The schedule is read in the zone of whoever saved it, not the server's.
      ai_reactivation_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
      media_autodownload: mediaAutodownload,
    };
    // Only include api_key if user typed a new one
    if (apiKey.trim()) {
      data.openrouter_api_key = apiKey.trim();
    }
    // Handle password change/removal
    if (removePassword) {
      data.web_password = '';
    } else if (webPassword.trim()) {
      if (webPassword !== webPasswordConfirm) {
        onNotify('As senhas não coincidem.');
        return;
      }
      data.web_password = webPassword;
    }
    setSaveSuccess(false);
    const result = await onSave(data);
    if (result !== false) {
      setSaveSuccess(true);
      setWebPassword('');
      setWebPasswordConfirm('');
      setRemovePassword(false);
      setTimeout(() => setSaveSuccess(false), 3000);
    }
  }

  if (!config) {
    return html`<div class="bg-wa-bg rounded-xl p-5 animate-pulse-slow text-wa-secondary border border-wa-border">Carregando...</div>`;
  }

  return html`
    <div class="flex flex-col gap-4 flex-1">

      <nav
        id="panel-tabs"
        aria-label="Seções do painel"
        class="bg-wa-bg border border-wa-border rounded-xl p-1.5 shadow-sm scroll-mt-4 overflow-x-auto"
      >
        <div class="flex min-w-max sm:min-w-0 gap-1">
          ${PANEL_TABS.map((tab) => html`
            <a
              key=${tab.id}
              id=${tab.slug}
              href=${panelTabHref(tab.id)}
              aria-current=${activeTab === tab.id ? 'page' : undefined}
              onClick=${(event) => selectPanelTab(event, tab.id)}
              class="flex-1 min-w-[130px] px-4 py-2.5 rounded-lg text-sm font-semibold text-center no-underline transition-colors ${activeTab === tab.id
                ? 'bg-wa-teal text-white shadow-sm'
                : 'text-wa-secondary hover:bg-wa-hover hover:text-wa-text'}"
            >${tab.label}</a>
          `)}
        </div>
      </nav>

      <!-- Section: Automacao -->
      <${Section} id="automation" title="Automação" active=${activeTab === 'agent'}>
        <label id="auto-reply" class="flex items-center gap-3 text-sm font-semibold text-wa-text cursor-pointer p-3 rounded-lg border scroll-mt-4 ${autoReply ? 'bg-green-50 border-green-200' : 'bg-red-50 border-red-200'}">
          <input
            type="checkbox"
            checked=${autoReply}
            onChange=${(e) => setAutoReply(e.target.checked)}
            class="w-4 h-4 rounded border-wa-border accent-wa-teal"
          />
          Ativar agente de IA para responder mensagens
        </label>

        <label id="default-ai" class="flex items-center gap-3 text-sm font-semibold text-wa-text cursor-pointer p-3 rounded-lg border scroll-mt-4 ${defaultAiEnabled ? 'bg-green-50 border-green-200' : 'bg-red-50 border-red-200'}">
          <input
            type="checkbox"
            checked=${defaultAiEnabled}
            onChange=${(e) => setDefaultAiEnabled(e.target.checked)}
            class="w-4 h-4 rounded border-wa-border accent-wa-teal"
          />
          IA ativada por padrão para novos contatos
        </label>

        <div id="groups" class="scroll-mt-4">
          <label class="block text-sm font-semibold text-wa-text mb-1">Resposta da IA em grupos</label>
          <select
            value=${groupReplyMode}
            onChange=${(e) => setGroupReplyMode(e.target.value)}
            class="w-full bg-wa-panel text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
          >
            <option value="mention_only">Somente quando o bot for mencionado</option>
            <option value="always">Sempre (responder a todas as mensagens do grupo)</option>
            <option value="never">Nunca (não responder em grupos)</option>
          </select>
          <span class="text-xs text-wa-secondary">Vale apenas para grupos com a IA ativada. "Somente quando mencionado" exige um @menção ao bot; "Sempre" responde a qualquer mensagem do grupo.</span>
        </div>

        <!-- Religar a IA automaticamente -->
        <label id="ai-reactivation" class="flex items-center gap-3 text-sm font-semibold text-wa-text cursor-pointer p-3 rounded-lg border scroll-mt-4 ${reactEnabled ? 'bg-green-50 border-green-200' : 'bg-red-50 border-red-200'}">
          <input
            type="checkbox"
            checked=${reactEnabled}
            onChange=${(e) => setReactEnabled(e.target.checked)}
            class="w-4 h-4 rounded border-wa-border accent-wa-teal"
          />
          Religar a IA automaticamente na conversa
        </label>
        <span class="text-xs text-wa-secondary -mt-2">Liga de novo a IA nas conversas em que ela foi desligada, seja por transferência para um atendente, desligada à mão ou desligada por padrão para novos contatos.</span>

        ${reactEnabled ? html`
          <div class="flex flex-col gap-4 p-3 bg-wa-panel rounded-lg border border-wa-border">
            <div>
              <label for="ai-reactivation-unit" class="block text-sm font-semibold text-wa-text mb-1">Quando religar</label>
              <select
                id="ai-reactivation-unit"
                value=${reactUnit}
                onChange=${(e) => setReactUnit(e.target.value)}
                class="w-full bg-wa-bg text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
              >
                ${REACTIVATION_UNITS.map((u) => html`<option value=${u.id}>${u.label}</option>`)}
              </select>
            </div>

            ${reactUnit === 'schedule' ? html`
              <div class="flex flex-col gap-3">
                <div>
                  <span class="block text-xs font-medium text-wa-text mb-1">Dias da semana</span>
                  <div class="flex flex-wrap gap-2">
                    ${WEEKDAYS.map((d) => html`
                      <button
                        type="button"
                        aria-pressed=${reactWeekdays.includes(d.id)}
                        title=${d.full}
                        onClick=${() => toggleWeekday(d.id)}
                        class="px-3 py-1.5 rounded-lg text-xs font-semibold border transition-colors ${reactWeekdays.includes(d.id)
                          ? 'bg-wa-teal text-white border-wa-teal'
                          : 'bg-wa-bg text-wa-secondary border-wa-border hover:bg-wa-hover'}"
                      >${d.label}</button>
                    `)}
                  </div>
                </div>
                <div>
                  <label for="ai-reactivation-time" class="block text-xs font-medium text-wa-text mb-1">Horário em que a IA volta a ser ligada</label>
                  <input
                    id="ai-reactivation-time"
                    type="time"
                    value=${reactTime}
                    onInput=${(e) => setReactTime(e.target.value)}
                    class="w-32 bg-wa-bg text-wa-text px-3 py-1.5 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
                  />
                </div>
                <span class="text-xs text-wa-secondary">Nos dias marcados, neste horário (no fuso do seu navegador), a IA é religada nas conversas desligadas.</span>
              </div>
            ` : html`
              <div>
                <label for="ai-reactivation-value" class="block text-xs font-medium text-wa-text mb-1">Tempo sem mensagens (${REACTIVATION_VALUE_HINT[reactUnit]})</label>
                <input
                  id="ai-reactivation-value"
                  type="number"
                  min="1"
                  step="1"
                  value=${reactValue}
                  onInput=${(e) => setReactValue(e.target.value)}
                  class="w-32 bg-wa-bg text-wa-text px-3 py-1.5 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
                />
                <span class="block text-xs text-wa-secondary mt-1">Conta a partir da última mensagem do cliente ou do atendente. As respostas da própria IA não contam.</span>
              </div>
            `}

            <div>
              <label for="ai-reactivation-sender" class="block text-xs font-medium text-wa-text mb-1">Dono da última mensagem</label>
              <select
                id="ai-reactivation-sender"
                value=${reactLastSender}
                onChange=${(e) => setReactLastSender(e.target.value)}
                class="w-full bg-wa-bg text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
              >
                ${REACTIVATION_OWNERS.map((o) => html`<option value=${o.id}>${o.label}</option>`)}
              </select>
              <span class="block text-xs text-wa-secondary mt-1">Só religa as conversas cuja última mensagem foi enviada por quem você escolher.</span>
            </div>

            <label id="ai-reactivation-groups" class="flex items-start gap-2 text-sm font-semibold text-wa-text cursor-pointer scroll-mt-4">
              <input
                type="checkbox"
                checked=${reactIncludeGroupsArchived}
                onChange=${(e) => setReactIncludeGroupsArchived(e.target.checked)}
                class="w-4 h-4 mt-0.5 rounded border-wa-border accent-wa-teal"
              />
              <span class="flex flex-col">
                Religar IA em grupos e mensagens arquivadas
                <span class="text-xs font-normal text-wa-secondary">Desmarcado, grupos e conversas arquivadas ficam como estão. Em grupos, a resposta continua seguindo a opção "Resposta da IA em grupos".</span>
              </span>
            </label>
          </div>
        ` : null}
      <//>

      <!-- Section: API e Modelos -->
      <${Section} id="api-models" title="API e Modelos" active=${activeTab === 'models'}>
        <!-- API Key -->
        <div id="api-key" class="scroll-mt-4">
          <label class="block text-sm font-semibold text-wa-text mb-1">Chave de API Techify</label>
          <div class="flex gap-2">
            <input
              type="password"
              value=${apiKey}
              onInput=${(e) => setApiKey(e.target.value)}
              placeholder=${config.openrouter_api_key || 'sk-or-...'}
              class="flex-1 bg-wa-panel text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
            />
            <button
              onClick=${handleTestKey}
              disabled=${testing}
              class="px-4 py-2 bg-wa-panel hover:bg-wa-hover disabled:opacity-50 text-wa-text text-sm rounded-lg transition-colors whitespace-nowrap border border-wa-border"
            >
              ${testing ? '...' : 'Testar'}
            </button>
          </div>
          ${testResult ? html`
            <p class="text-xs mt-1 ${testResult.ok ? 'text-green-600' : 'text-red-500'}">
              ${testResult.ok ? '\u2713' : '\u2717'} ${testResult.message}
            </p>
          ` : config.openrouter_api_key ? html`
            <p class="text-xs mt-1 text-wa-secondary">Chave salva: ${config.openrouter_api_key}</p>
          ` : null}
        </div>

        <!-- Improvement model -->
        <div id="improvement-model" class="scroll-mt-4">
          <label class="block text-sm font-semibold text-wa-text mb-1">Modelo de IA (sugestão de melhoria)</label>
          <${ModelSelect}
            value=${improvementModel}
            onChange=${setImprovementModel}
            placeholder="Usar o mesmo do chat"
          />
          <p class="text-xs mt-1 text-wa-secondary">
            Modelo usado ao gerar análises de melhoria das respostas marcadas como incorretas. Deixe em branco para usar o modelo do chat.
          </p>
        </div>

        <!-- Model -->
        <div id="model" class="scroll-mt-4">
          <label class="block text-sm font-semibold text-wa-text mb-1">Modelo de IA (chat)</label>
          <${ModelSelect}
            value=${model}
            onChange=${setModel}
            placeholder="deepseek/deepseek-v4.1-flash"
          />
        </div>

        <!-- Image description toggle -->
        <label id="image-transcription" class="flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-colors scroll-mt-4 ${imageTranscriptionEnabled ? 'bg-green-50 border-green-200 hover:bg-green-100' : 'bg-wa-panel border-wa-border hover:bg-wa-hover'}">
          <input
            type="checkbox"
            checked=${imageTranscriptionEnabled}
            onChange=${(e) => setImageTranscriptionEnabled(e.target.checked)}
            class="w-4 h-4 rounded border-wa-border accent-wa-teal mt-0.5"
          />
          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2">
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class=${imageTranscriptionEnabled ? 'text-green-600' : 'text-wa-secondary'}>
                <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
                <circle cx="8.5" cy="8.5" r="1.5"/>
                <polyline points="21 15 16 10 5 21"/>
              </svg>
              <span class="text-sm font-semibold text-wa-text">Descrever imagem</span>
              <span class="text-xs px-2 py-0.5 rounded-full ${imageTranscriptionEnabled ? 'bg-green-600 text-white' : 'bg-wa-secondary/20 text-wa-secondary'}">
                ${imageTranscriptionEnabled ? 'Ativado' : 'Desativado'}
              </span>
            </div>
            <span class="block text-xs text-wa-secondary mt-1">
              Usa IA para descrever automaticamente o conteúdo de imagens recebidas pelo contato
            </span>
          </div>
        </label>

        <!-- Document transcription toggle -->
        <label id="document-transcription" class="flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-colors scroll-mt-4 ${documentTranscriptionEnabled ? 'bg-green-50 border-green-200 hover:bg-green-100' : 'bg-wa-panel border-wa-border hover:bg-wa-hover'}">
          <input
            type="checkbox"
            checked=${documentTranscriptionEnabled}
            onChange=${(e) => setDocumentTranscriptionEnabled(e.target.checked)}
            class="w-4 h-4 rounded border-wa-border accent-wa-teal mt-0.5"
          />
          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2">
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class=${documentTranscriptionEnabled ? 'text-green-600' : 'text-wa-secondary'}>
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
                <line x1="16" y1="13" x2="8" y2="13"/>
                <line x1="16" y1="17" x2="8" y2="17"/>
                <polyline points="10 9 9 9 8 9"/>
              </svg>
              <span class="text-sm font-semibold text-wa-text">Ler documento</span>
              <span class="text-xs px-2 py-0.5 rounded-full ${documentTranscriptionEnabled ? 'bg-green-600 text-white' : 'bg-wa-secondary/20 text-wa-secondary'}">
                ${documentTranscriptionEnabled ? 'Ativado' : 'Desativado'}
              </span>
            </div>
            <span class="block text-xs text-wa-secondary mt-1">
              Usa IA para extrair o conteúdo de documentos recebidos (PDF, DOCX e arquivos de texto)
            </span>
          </div>
        </label>

        <!-- Audio transcription mode & target -->
        <div id="audio-transcription" class="flex flex-col gap-3 p-3 bg-wa-panel rounded-lg border border-wa-border scroll-mt-4">
          <div class="text-sm font-semibold text-wa-text">Transcrição de áudio</div>
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label class="block text-xs font-medium text-wa-text mb-1">Transcrever mensagens</label>
              <select
                value=${audioTranscriptionMode}
                onChange=${(e) => setAudioTranscriptionMode(e.target.value)}
                class="w-full bg-wa-bg text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
              >
                <option value="received">Somente recebidas</option>
                <option value="sent">Somente enviadas</option>
                <option value="both">Nos dois sentidos</option>
                <option value="off">Não transcrever</option>
              </select>
            </div>
            <div>
              <label class="block text-xs font-medium text-wa-text mb-1">Onde aparece a transcrição</label>
              <select
                value=${audioTranscriptionTarget}
                onChange=${(e) => setAudioTranscriptionTarget(e.target.value)}
                disabled=${audioTranscriptionMode === 'off'}
                class="w-full bg-wa-bg text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none disabled:opacity-50"
              >
                <option value="private">Mensagem privada (só no painel)</option>
                <option value="chat">Direto no chat (envia ao contato)</option>
              </select>
            </div>
          </div>
          ${audioTranscriptionMode !== 'off' && audioTranscriptionTarget === 'chat' ? html`
            <div>
              <label class="block text-xs font-medium text-wa-text mb-1">Prefixo (opcional)</label>
              <textarea
                value=${audioTranscriptionChatPrefix}
                onInput=${(e) => setAudioTranscriptionChatPrefix(e.target.value)}
                rows="2"
                placeholder="Ex: 🎙 Transcrição: "
                class="w-full bg-wa-bg text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none resize-none"
              ></textarea>
              <span class="text-xs text-wa-secondary">Texto colado antes da transcrição enviada ao chat. Deixe em branco para enviar só o texto.</span>
            </div>
          ` : null}
        </div>
      <//>

      <!-- Section: Download automático de mídia -->
      <${Section} id="download-midia" title="Download automático de mídia" active=${activeTab === 'models'}>
        <p class="text-xs text-wa-secondary -mt-1">
          Escolha quais arquivos recebidos o WhatsBot guarda no seu espaço. O que ficar desligado
          aparece na conversa como “não baixado”, com um botão para tentar baixar depois, e a IA
          ainda descreve ou transcreve o conteúdo antes de o arquivo ser descartado.
          Desligar economiza espaço, principalmente em grupos movimentados.
        </p>
        <div class="overflow-x-auto rounded-lg border border-wa-border">
          <table class="w-full text-sm">
            <thead>
              <tr class="bg-wa-panel text-left text-xs uppercase tracking-wider text-wa-secondary">
                <th class="px-3 py-2 font-semibold">Tipo de arquivo</th>
                <th class="px-3 py-2 font-semibold text-center">Conversas individuais</th>
                <th class="px-3 py-2 font-semibold text-center">Grupos</th>
              </tr>
            </thead>
            <tbody>
              ${MEDIA_ROWS.map((row) => html`
                <tr key=${row.id} class="border-t border-wa-border">
                  <td class="px-3 py-2.5 text-wa-text"><span aria-hidden="true">${row.icon}</span> ${row.label}</td>
                  ${['individual', 'group'].map((scope) => html`
                    <td class="px-3 py-2.5 text-center">
                      <label class="inline-flex items-center justify-center cursor-pointer p-1">
                        <input
                          type="checkbox"
                          checked=${!!(mediaAutodownload[row.id] && mediaAutodownload[row.id][scope])}
                          onChange=${(e) => {
                            const checked = e.target.checked;
                            setMediaAutodownload((prev) => ({ ...prev, [row.id]: { ...prev[row.id], [scope]: checked } }));
                          }}
                          class="w-4 h-4 rounded border-wa-border accent-wa-teal"
                          aria-label=${`Baixar ${row.label.toLowerCase()} ${scope === 'group' ? 'de grupos' : 'de conversas individuais'} automaticamente`}
                        />
                      </label>
                    </td>`)}
                </tr>`)}
            </tbody>
          </table>
        </div>
        <p class="text-xs text-wa-secondary">
          Para ver e apagar o que já está guardado, abra${' '}
          <a href="/arquivos" class="text-wa-teal underline">Arquivos</a>.
        </p>
      <//>

      <!-- Section: Comportamento da IA -->
      <${Section} id="ai-behavior" title="Comportamento da IA" active=${activeTab === 'agent'}>
        <div id="prompt" class="flex-1 flex flex-col scroll-mt-4">
          <div class="flex items-center justify-between mb-1">
            <label class="block text-sm font-semibold text-wa-text">Instruções</label>
            <button
              type="button"
              onClick=${() => setPromptFullscreen(true)}
              class="text-wa-secondary hover:text-wa-teal transition-colors p-1 rounded"
              title="Abrir editor em tela cheia"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>
            </button>
          </div>
          <textarea
            value=${systemPrompt}
            onInput=${(e) => setSystemPrompt(e.target.value)}
            rows="4"
            class="w-full flex-1 bg-wa-panel text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none resize-none"
          ></textarea>
        </div>
      <//>

      <!-- Fullscreen Prompt Editor -->
      ${promptFullscreen ? html`
        <div class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4" onClick=${(e) => { if (e.target === e.currentTarget) setPromptFullscreen(false); }}>
          <div class="bg-wa-bg w-full h-full rounded-xl flex flex-col shadow-2xl overflow-hidden">
            <div class="flex items-center justify-between px-5 py-3 border-b border-wa-border">
              <h2 class="text-sm font-semibold text-wa-text">Comportamento da IA</h2>
              <button
                type="button"
                onClick=${() => setPromptFullscreen(false)}
                class="text-wa-secondary hover:text-wa-text transition-colors p-1 rounded"
                title="Fechar"
              >
                <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
              </button>
            </div>
            <textarea
              value=${systemPrompt}
              onInput=${(e) => setSystemPrompt(e.target.value)}
              class="flex-1 w-full bg-wa-bg text-wa-text px-5 py-4 text-sm leading-relaxed focus:outline-none resize-none"
              autofocus
            ></textarea>
          </div>
        </div>
      ` : null}

      <!-- Section: Comportamento -->
      <${Section} id="behavior" title="Comportamento" active=${activeTab === 'agent'}>
        <!-- Context & Batch Settings -->
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div id="context" class="scroll-mt-4">
            <label class="block text-sm font-semibold text-wa-text mb-1">Mensagens de contexto</label>
            <input
              type="number"
              min="2"
              max="100"
              value=${maxContext}
              onInput=${(e) => setMaxContext(e.target.value)}
              class="w-full bg-wa-panel text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
            />
            <span class="text-xs text-wa-secondary">Qtd de msgs enviadas ao LLM</span>
          </div>
          <div id="batch" class="scroll-mt-4">
            <label class="block text-sm font-semibold text-wa-text mb-1">Agrupar mensagens (s)</label>
            <input
              type="number"
              min="0"
              max="30"
              step="0.5"
              value=${batchDelay}
              onInput=${(e) => setBatchDelay(e.target.value)}
              class="w-full bg-wa-panel text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
            />
            <span class="text-xs text-wa-secondary">Espera antes de responder</span>
          </div>
        </div>

        <!-- Split Messages -->
        <div id="split-messages" class="flex flex-col gap-2 p-3 bg-wa-panel rounded-lg border border-wa-border scroll-mt-4">
          <label class="flex items-center gap-2 text-sm font-semibold text-wa-text cursor-pointer">
            <input
              type="checkbox"
              checked=${splitMessages}
              onChange=${(e) => setSplitMessages(e.target.checked)}
              class="w-4 h-4 rounded border-wa-border accent-wa-teal"
            />
            Mensagens picadas (dividir resposta)
          </label>
          <span class="text-xs text-wa-secondary">Divide a resposta da IA em várias mensagens curtas, como uma conversa natural</span>
          ${splitMessages ? html`
            <div class="mt-1">
              <label class="block text-xs font-medium text-wa-text mb-1">Intervalo entre mensagens (s)</label>
              <input
                type="number"
                min="0"
                max="10"
                step="0.5"
                value=${splitDelay}
                onInput=${(e) => setSplitDelay(e.target.value)}
                class="w-32 bg-wa-bg text-wa-text px-3 py-1.5 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
              />
            </div>
          ` : null}
        </div>

        <!-- Transfer Alert -->
        <div id="transfer-alert" class="flex flex-col gap-2 p-3 bg-wa-panel rounded-lg border border-wa-border scroll-mt-4">
          <label class="flex items-center gap-2 text-sm font-semibold text-wa-text cursor-pointer">
            <input
              type="checkbox"
              checked=${transferAlertEnabled}
              onChange=${(e) => setTransferAlertEnabled(e.target.checked)}
              class="w-4 h-4 rounded border-wa-border accent-wa-teal"
            />
            Alerta sonoro ao transferir para humano
          </label>
          <span class="text-xs text-wa-secondary">Emite um alerta sonoro quando a IA transfere o atendimento para um humano</span>
          ${transferAlertEnabled ? html`
            <div class="mt-1">
              <label class="block text-xs font-medium text-wa-text mb-1">Duração do alerta (segundos)</label>
              <input
                type="number"
                min="1"
                max="30"
                step="1"
                value=${transferAlertDuration}
                onInput=${(e) => setTransferAlertDuration(e.target.value)}
                class="w-32 bg-wa-bg text-wa-text px-3 py-1.5 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
              />
            </div>
          ` : null}
        </div>

        <!-- Low balance alert -->
        <div id="low-balance" class="flex flex-col gap-2 p-3 bg-wa-panel rounded-lg border border-wa-border scroll-mt-4">
          <label class="flex items-center gap-2 text-sm font-semibold text-wa-text cursor-pointer">
            <input
              type="checkbox"
              checked=${lowBalanceEnabled}
              onChange=${(e) => setLowBalanceEnabled(e.target.checked)}
              class="w-4 h-4 rounded border-wa-border accent-wa-teal"
            />
            Avisar quando o saldo estiver acabando
          </label>
          <span class="text-xs text-wa-secondary">Exibe um pop-up no painel com link de recarga quando o saldo cair abaixo do limite</span>
          ${lowBalanceEnabled ? html`
            <div class="mt-1">
              <label class="block text-xs font-medium text-wa-text mb-1">Limite (USD)</label>
              <input
                type="number"
                min="0"
                max="100"
                step="0.01"
                value=${lowBalanceThreshold}
                onInput=${(e) => setLowBalanceThreshold(e.target.value)}
                class="w-32 bg-wa-bg text-wa-text px-3 py-1.5 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
              />
              <span class="text-xs text-wa-secondary block mt-1">Padrão: 0.50 (50 centavos de dólar)</span>
            </div>
          ` : null}
        </div>

        <!-- Mark all read / unread -->
        <div id="mark-conversations" class="flex flex-col gap-2 p-3 bg-wa-panel rounded-lg border border-wa-border scroll-mt-4">
          <label class="text-sm font-semibold text-wa-text">Marcar conversas</label>
          <span class="text-xs text-wa-secondary">Reacende ou limpa o indicador verde de não lido no painel. Para uma conversa específica, use o botão direito sobre o contato na lista.</span>
          ${confirmUnreadAll ? html`
            <div class="mt-1 flex flex-col gap-2 p-3 rounded-lg bg-amber-50 border border-amber-300">
              <span class="text-sm font-medium text-amber-800">Marcar TODAS as conversas como não lidas?</span>
              <span class="text-xs text-amber-700">Reacende o indicador verde em todos os contatos do painel. Não afeta o WhatsApp do celular.</span>
              <div class="flex gap-2 mt-1">
                <button
                  type="button"
                  disabled=${markingAllUnread}
                  onClick=${handleMarkAllUnread}
                  class="px-4 py-2 rounded-lg text-sm font-medium bg-amber-600 text-white hover:opacity-90 disabled:opacity-50 transition-opacity"
                >${markingAllUnread ? 'Marcando...' : 'Confirmar'}</button>
                <button
                  type="button"
                  disabled=${markingAllUnread}
                  onClick=${() => setConfirmUnreadAll(false)}
                  class="px-4 py-2 rounded-lg text-sm font-medium bg-wa-bg text-wa-text border border-wa-border hover:bg-wa-hover disabled:opacity-50 transition-colors"
                >Cancelar</button>
              </div>
            </div>
          ` : confirmReadAll ? html`
            <div class="mt-1 flex flex-col gap-2 p-3 rounded-lg bg-amber-50 border border-amber-300">
              <span class="text-sm font-medium text-amber-800">Marcar TODAS as conversas como lidas?</span>
              <span class="text-xs text-amber-700">Remove o indicador verde de não lido de todos os contatos do painel.</span>
              <div class="flex gap-2 mt-1">
                <button
                  type="button"
                  disabled=${markingAllRead}
                  onClick=${handleMarkAllRead}
                  class="px-4 py-2 rounded-lg text-sm font-medium bg-amber-600 text-white hover:opacity-90 disabled:opacity-50 transition-opacity"
                >${markingAllRead ? 'Marcando...' : 'Confirmar'}</button>
                <button
                  type="button"
                  disabled=${markingAllRead}
                  onClick=${() => setConfirmReadAll(false)}
                  class="px-4 py-2 rounded-lg text-sm font-medium bg-wa-bg text-wa-text border border-wa-border hover:bg-wa-hover disabled:opacity-50 transition-colors"
                >Cancelar</button>
              </div>
            </div>
          ` : html`
            <div class="flex flex-wrap gap-2 mt-1">
              <button
                type="button"
                onClick=${() => { setConfirmReadAll(false); setConfirmUnreadAll(true); }}
                class="px-4 py-2 rounded-lg text-sm font-medium bg-wa-teal text-white hover:opacity-90 transition-opacity"
              >Marcar todas como não lidas</button>
              <button
                type="button"
                onClick=${() => { setConfirmUnreadAll(false); setConfirmReadAll(true); }}
                class="px-4 py-2 rounded-lg text-sm font-medium bg-wa-bg text-wa-text border border-wa-border hover:bg-wa-hover transition-colors"
              >Marcar todas como lidas</button>
            </div>
          `}
        </div>
      <//>

      <!-- Section: Avancado -->
      <${Section} id="advanced" title="Avançado" active=${activeTab === 'system'}>
        <!-- Max Executions -->
        <div id="max-executions" class="scroll-mt-4">
          <label class="block text-sm font-semibold text-wa-text mb-1">Execuções salvas</label>
          <input
            type="number"
            min="10"
            max="10000"
            step="10"
            value=${maxExecutions}
            onInput=${(e) => setMaxExecutions(e.target.value)}
            class="w-full bg-wa-panel text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
          />
          <span class="text-xs text-wa-secondary">Quantidade máxima de execuções e payloads mantidos no banco</span>
        </div>

        <!-- Panel Password -->
        <div id="password" class="flex flex-col gap-2 p-3 bg-wa-panel rounded-lg border border-wa-border scroll-mt-4">
          <div class="flex items-center justify-between">
            <label class="text-sm font-semibold text-wa-text">Senha do Painel</label>
            ${config.has_password ? html`
              <span class="text-xs bg-wa-teal text-white px-2 py-0.5 rounded-full">Ativa</span>
            ` : html`
              <span class="text-xs bg-wa-secondary/20 text-wa-secondary px-2 py-0.5 rounded-full">Desativada</span>
            `}
          </div>
          <span class="text-xs text-wa-secondary">Protege o acesso ao painel web com senha</span>
          ${!removePassword ? html`
            <input
              type="password"
              value=${webPassword}
              onInput=${(e) => setWebPassword(e.target.value)}
              placeholder=${config.has_password ? 'Nova senha (deixe vazio para manter)' : 'Definir senha'}
              class="w-full bg-wa-bg text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
            />
            ${webPassword ? html`
              <input
                type="password"
                value=${webPasswordConfirm}
                onInput=${(e) => setWebPasswordConfirm(e.target.value)}
                placeholder="Confirmar senha"
                class="w-full bg-wa-bg text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none ${webPassword && webPasswordConfirm && webPassword !== webPasswordConfirm ? 'border-red-400' : ''}"
              />
              ${webPassword && webPasswordConfirm && webPassword !== webPasswordConfirm ? html`
                <span class="text-xs text-red-500">As senhas não coincidem</span>
              ` : null}
            ` : null}
          ` : null}
          ${config.has_password ? html`
            <label class="flex items-center gap-2 text-sm text-red-600 cursor-pointer mt-1">
              <input
                type="checkbox"
                checked=${removePassword}
                onChange=${(e) => { setRemovePassword(e.target.checked); if (e.target.checked) { setWebPassword(''); setWebPasswordConfirm(''); } }}
                class="w-4 h-4 rounded border-wa-border accent-red-600"
              />
              Remover senha
            </label>
          ` : null}
        </div>

        <!-- Update -->
        <div id="update" class="p-3 bg-wa-panel rounded-lg border border-wa-border scroll-mt-4">
          <div class="flex items-start justify-between gap-4">
            <div>
              <label class="text-sm font-semibold text-wa-text">Atualizar WhatsBot-Lite</label>
              <div class="flex items-center gap-3 mt-1.5">
                <span class="text-xs text-wa-secondary">
                  Atual: <span class="font-mono font-semibold text-wa-text">${currentVersion || '...'}</span>
                </span>
                ${latestVersion ? html`
                  <span class="text-xs text-wa-secondary">
                    Última: <span class="font-mono font-semibold ${updateAvailable ? 'text-blue-600' : 'text-green-600'}">${latestVersion}</span>
                  </span>
                ` : null}
                ${!checkingUpdate && !updateAvailable && latestVersion ? html`
                  <span class="text-xs text-green-600 font-medium">Atualizado</span>
                ` : null}
                ${updateAvailable ? html`
                  <span class="text-xs text-blue-600 font-medium">Nova versão disponível</span>
                ` : null}
              </div>
              <label id="update-notifications" class="flex items-start gap-2 mt-3 text-xs text-wa-text cursor-pointer scroll-mt-4">
                <input
                  type="checkbox"
                  checked=${updateNotificationsEnabled}
                  onChange=${(e) => setUpdateNotificationsEnabled(e.target.checked)}
                  class="w-4 h-4 mt-0.5 rounded border-wa-border accent-wa-teal"
                />
                <span>
                  Avisar quando houver uma nova versão
                  <span class="block text-wa-secondary mt-0.5">Se estiver desligado, as atualizações ainda podem ser verificadas manualmente.</span>
                </span>
              </label>
            </div>
            <div class="flex items-center gap-2 ml-4">
              <button
                onClick=${() => fetchVersionInfo(true)}
                disabled=${checkingUpdate || updating}
                class="px-3 py-2 bg-wa-panel hover:bg-wa-hover disabled:opacity-50 text-wa-text text-sm rounded-lg transition-colors"
                title="Verificar atualizações"
              >
                ${checkingUpdate ? '...' : 'Verificar'}
              </button>
              ${updateAvailable ? html`
                <button
                  onClick=${handleUpdate}
                  disabled=${updating}
                  class="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-medium rounded-lg transition-colors whitespace-nowrap"
                >
                  ${updating ? 'Atualizando...' : 'Atualizar'}
                </button>
              ` : null}
            </div>
          </div>
        </div>
      <//>

      <div class=${activeTab === 'system' ? '' : 'hidden'}>
        <${DatabaseSettings} id="database" onNotify=${onNotify} />
      </div>

      <div class=${activeTab === 'system' ? '' : 'hidden'}>
        <${GowaSettings}
          id="gowa"
          autoCheck=${gowaAutoCheck}
          onAutoCheckChange=${setGowaAutoCheck}
          onNotify=${onNotify}
        />
      </div>

      <!-- Save Button (sticky) -->
      <div class="sticky bottom-0 z-10 bg-wa-panel pt-2 pb-1">
        <button
          onClick=${handleSave}
          disabled=${saving}
          class="w-full py-2.5 ${saveSuccess ? 'bg-green-600' : 'bg-wa-teal hover:bg-wa-tealDark'} disabled:opacity-50 text-white font-medium rounded-lg transition-colors shadow-sm"
        >
          ${saving ? 'Salvando...' : saveSuccess ? '\u2713 Salvo!' : 'Salvar Configurações'}
        </button>
      </div>
    </div>
  `;
}
