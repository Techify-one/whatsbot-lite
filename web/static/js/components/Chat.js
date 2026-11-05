import { h } from 'preact';
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import htm from 'htm';
import { authHeaders } from '../services/api.js';
import { MicIcon, StopIcon } from './contacts/icons.js';
import { ModelPicker } from './ModelSelect.js';

const html = htm.bind(h);

async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!data.ok) throw new Error(data.error || 'Falha na solicitação');
  return data.data;
}

function escapeHtml(value) {
  return String(value || '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function inlineMarkdown(value) {
  let out = escapeHtml(value);
  out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return out;
}

function renderMarkdown(value) {
  const parts = String(value || '').split(/```/);
  return parts.map((part, index) => {
    if (index % 2) {
      const newline = part.indexOf('\n');
      const language = newline >= 0 ? part.slice(0, newline).trim() : '';
      const code = newline >= 0 ? part.slice(newline + 1) : part;
      return html`<div class="chat-code-wrap"><div class="chat-code-head">${language || 'código'}</div><pre><code>${code}</code></pre></div>`;
    }
    const lines = part.split('\n');
    return html`<div>${lines.map((line, lineIndex) => {
      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      const bullet = line.match(/^\s*[-*]\s+(.+)$/);
      if (heading) return html`<div class="font-semibold mt-3 mb-1 ${heading[1].length === 1 ? 'text-lg' : 'text-base'}" dangerouslySetInnerHTML=${{ __html: inlineMarkdown(heading[2]) }}></div>`;
      if (bullet) return html`<div class="pl-4 relative before:content-['•'] before:absolute before:left-1" dangerouslySetInnerHTML=${{ __html: inlineMarkdown(bullet[1]) }}></div>`;
      if (!line.trim()) return html`<div class="h-2"></div>`;
      return html`<div key=${lineIndex} dangerouslySetInnerHTML=${{ __html: inlineMarkdown(line) }}></div>`;
    })}</div>`;
  });
}

function formatEstimatedUsd(value) {
  const amount = Number(value || 0);
  const digits = amount >= 0.01 ? 4 : amount >= 0.0001 ? 6 : 8;
  return `US$ ${amount.toLocaleString('pt-BR', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

function formatCompactNumber(value) {
  const amount = Number(value || 0);
  if (Math.abs(amount) < 1000) return amount.toLocaleString('pt-BR');
  return amount.toLocaleString('pt-BR', { notation: 'compact', maximumFractionDigits: 1 });
}

function formatRecordTime(seconds) {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, '0')}`;
}

function pendingInstallOffer(messages) {
  const offer = [...(messages || [])].reverse().find(message => (
    message.kind === 'install_offer' && (message.metadata || {}).status === 'pending'
  ));
  return offer ? { ...(offer.metadata || {}), offer_message_id: offer.id } : null;
}

function chatRouteFromPath() {
  const match = window.location.pathname.match(
    /^\/chat\/projects\/([^/]+)(?:\/conversations\/([^/]+)(?:\/messages\/(\d+))?)?$/,
  );
  return match ? {
    projectId: decodeURIComponent(match[1]),
    conversationId: match[2] ? decodeURIComponent(match[2]) : '',
    messageId: match[3] ? Number(match[3]) : null,
  } : { projectId: '', conversationId: '', messageId: null };
}

function chatPath(projectId = '', conversationId = '', messageId = null) {
  if (!projectId) return '/chat';
  let path = `/chat/projects/${encodeURIComponent(projectId)}`;
  if (conversationId) path += `/conversations/${encodeURIComponent(conversationId)}`;
  if (conversationId && messageId != null) path += `/messages/${messageId}`;
  return path;
}

function navigateChat(projectId, conversationId = '', messageId = null, { replace = false } = {}) {
  const target = chatPath(projectId, conversationId, messageId);
  if (window.location.pathname === target) return;
  history[replace ? 'replaceState' : 'pushState'](null, '', target);
}

function statusLabel(name) {
  const labels = {
    read_file: 'Lendo arquivo', list_files: 'Listando arquivos', search_content: 'Pesquisando no projeto',
    write_file: 'Criando arquivo', edit_file: 'Editando arquivo', move_file: 'Movendo arquivo',
    delete_file: 'Excluindo arquivo', run_command: 'Executando comando',
    read_whatsbot_file: 'Consultando o Whatsbot-lite', list_whatsbot_files: 'Listando referências',
    search_whatsbot: 'Pesquisando no Whatsbot-lite',
  };
  return labels[name] || name || 'Executando ação';
}

function Actions({ items, running }) {
  const detailsRef = useRef(null);
  useEffect(() => {
    if (detailsRef.current) detailsRef.current.open = !!running;
  }, [running]);
  if (!items.length) return null;
  return html`
    <details ref=${detailsRef} class="chat-actions my-2 rounded-lg border border-wa-border bg-wa-panel/60">
      <summary class="cursor-pointer px-3 py-2 text-sm text-wa-secondary select-none">
        ${running ? 'Trabalhando…' : `${items.length} ${items.length === 1 ? 'ação' : 'ações'} concluídas`}
      </summary>
      <div class="border-t border-wa-border divide-y divide-wa-border">
        ${items.map((item, index) => {
          const meta = item.metadata || {};
          return html`<div key=${item.id || index} class="px-3 py-2 text-xs">
            <div class="flex items-center gap-2">
              <span class=${meta.status === 'failed' ? 'text-red-500' : meta.status === 'running' ? 'text-amber-500' : 'text-wa-teal'}>${meta.status === 'running' ? '●' : meta.status === 'failed' ? '×' : '✓'}</span>
              <span class="font-medium text-wa-text">${statusLabel(meta.name)}</span>
            </div>
            ${(meta.args && Object.keys(meta.args).length) ? html`<pre class="mt-1 ml-5 overflow-auto text-wa-secondary whitespace-pre-wrap">${JSON.stringify(meta.args, null, 2)}</pre>` : null}
            ${item.content && item.content !== meta.name ? html`<pre class="mt-1 ml-5 max-h-40 overflow-auto text-wa-secondary whitespace-pre-wrap">${item.content}</pre>` : null}
          </div>`;
        })}
      </div>
    </details>
  `;
}

function MessageList({ messages, streaming, running, projectId, conversationId, linkedMessageId, onMessageLink }) {
  const nodes = [];
  let actions = [];
  function flush() {
    if (actions.length) {
      nodes.push(html`<${Actions} key=${`a-${nodes.length}`} items=${actions} running=${running && actions.some(a => (a.metadata || {}).status === 'running')} />`);
      actions = [];
    }
  }
  for (const message of messages) {
    if (message.kind === 'action') { actions.push(message); continue; }
    if (message.kind === 'install_offer') continue;
    flush();
    if (message.kind === 'system') {
      nodes.push(html`<div id=${`chat-message-${message.id}`} class="mx-auto max-w-3xl my-2 text-center text-sm text-red-500">${message.content}</div>`);
      continue;
    }
    if (message.kind === 'metrics') {
      const metric = message.metadata || {};
      nodes.push(html`<div id=${`chat-message-${message.id}`} class="my-2 overflow-x-auto whitespace-nowrap text-center text-[11px] text-wa-secondary wa-scrollbar">
        ${formatCompactNumber(metric.total_tokens)} tokens · ${formatCompactNumber(metric.cache_read_tokens)} cache${metric.cache_write_tokens ? ` · ${formatCompactNumber(metric.cache_write_tokens)} gravados` : ''}${metric.estimated_cost_usd !== undefined ? html`<span class="text-wa-text/75"> · Resp. <strong>${formatEstimatedUsd(metric.estimated_cost_usd)}</strong> · Total <strong>${formatEstimatedUsd(metric.conversation_estimated_cost_usd)}</strong></span>` : null}
      </div>`);
      continue;
    }
    const user = message.role === 'user';
    const permalink = chatPath(projectId, conversationId, message.id);
    nodes.push(html`
      <div id=${`chat-message-${message.id}`} key=${message.id} class="group flex ${user ? 'justify-end' : 'justify-start'} my-3 scroll-mt-6 ${Number(linkedMessageId) === Number(message.id) ? 'chat-message-linked' : ''}">
        <div class="chat-message-bubble relative max-w-[88%] md:max-w-[78%] rounded-xl px-4 py-3 shadow-sm ${user ? 'bg-wa-outgoing' : 'bg-wa-bg border border-wa-border'}">
          <div class="chat-markdown text-[14px] leading-6">${renderMarkdown(message.content)}</div>
          ${Number.isInteger(Number(message.id)) ? html`<a href=${permalink} onClick=${event => onMessageLink(event, message.id)} class="chat-message-link" title="Link desta mensagem" aria-label="Link desta mensagem">↗</a>` : null}
        </div>
      </div>
    `);
  }
  flush();
  if (streaming) {
    nodes.push(html`<div class="flex justify-start my-3"><div class="chat-message-bubble max-w-[88%] rounded-xl px-4 py-3 bg-wa-bg border border-wa-border shadow-sm"><div class="chat-markdown text-[14px] leading-6">${renderMarkdown(streaming)}<span class="inline-block w-1.5 h-4 ml-1 bg-wa-teal animate-pulse"></span></div></div></div>`);
  }
  return html`${nodes}`;
}

export function Chat() {
  const [data, setData] = useState({ projects: [], installed_plugins: [], default_model: '' });
  const [models, setModels] = useState([]);
  const [projectId, setProjectId] = useState('');
  const [conversationId, setConversationId] = useState('');
  const [conversation, setConversation] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [running, setRunning] = useState(false);
  const [streaming, setStreaming] = useState('');
  const [status, setStatus] = useState('');
  const [runId, setRunId] = useState('');
  const [installReady, setInstallReady] = useState(null);
  const [installing, setInstalling] = useState(false);
  const [files, setFiles] = useState([]);
  const [selectedFile, setSelectedFile] = useState(null);
  const [showFiles, setShowFiles] = useState(false);
  const [notice, setNotice] = useState('');
  const [newName, setNewName] = useState('');
  const [showNew, setShowNew] = useState(false);
  const [commandIndex, setCommandIndex] = useState(0);
  const [expandedProjectIds, setExpandedProjectIds] = useState([]);
  const [draggingProjectId, setDraggingProjectId] = useState('');
  const [linkedMessageId, setLinkedMessageId] = useState(null);
  const [recording, setRecording] = useState(false);
  const [recordDuration, setRecordDuration] = useState(0);
  const [transcribing, setTranscribing] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const endRef = useRef(null);
  const inputRef = useRef(null);
  const modelRef = useRef(null);
  const audioRecorderRef = useRef(null);
  const recordTimerRef = useRef(null);
  const cancelRecordingRef = useRef(false);
  const liveStreamRunRef = useRef('');
  const streamControllerRef = useRef(null);
  const streamConversationRef = useRef('');

  const project = data.projects.find(p => p.id === projectId) || null;
  const selectedModel = models.find(m => m.id === (conversation && conversation.model));
  const supportsReasoning = !!(selectedModel && (selectedModel.supported_parameters || []).some(p => p === 'reasoning' || p === 'reasoning_effort'));
  const installedProjectPlugin = project && project.plugin_id
    ? data.installed_plugins.find(p => p.id === project.plugin_id)
    : null;
  const commands = [
    { value: '/compact', title: 'Compactar contexto', description: 'Resume o histórico preservando decisões e próximos passos' },
    { value: '/model', title: 'Escolher modelo', description: 'Abre o seletor de modelo desta conversa' },
    { value: '/cache', title: 'Testar cache', description: 'Executa duas chamadas e confere tokens reutilizados' },
    { value: '/files', title: 'Ver arquivos', description: 'Abre os arquivos do projeto atual', pluginOnly: true },
    { value: '/new', title: 'Nova conversa', description: 'Cria outra conversa dentro deste projeto' },
  ];
  const commandQuery = input.startsWith('/') && !input.includes(' ') ? input.toLowerCase() : '';
  const commandOptions = commandQuery
    ? commands.filter(command => (!command.pluginOnly || (project && project.kind === 'plugin')) && command.value.startsWith(commandQuery))
    : [];

  function resizeComposer(target) {
    if (!target) return;
    target.style.height = 'auto';
    target.style.height = `${Math.min(target.scrollHeight, 240)}px`;
    target.style.overflowY = target.scrollHeight > 240 ? 'auto' : 'hidden';
  }

  function detachLiveStream() {
    if (streamControllerRef.current) streamControllerRef.current.abort();
    streamControllerRef.current = null;
    streamConversationRef.current = '';
    liveStreamRunRef.current = '';
  }

  function updateInput(value, target) {
    setInput(value);
    setCommandIndex(0);
    resizeComposer(target || inputRef.current);
  }

  function selectCommand(command) {
    if (!command) return;
    if (command.value === '/model') {
      setInput('');
      requestAnimationFrame(() => modelRef.current && modelRef.current.focus());
      return;
    }
    if (command.value === '/cache') {
      setInput('');
      testCache();
      return;
    }
    if (command.value === '/files') {
      setInput('');
      setShowFiles(true);
      loadFiles();
      return;
    }
    if (command.value === '/new') {
      setInput('');
      newConversation();
      return;
    }
    if (command.value === '/compact') {
      setInput('');
      send(null, '/compact');
    }
  }

  async function reload(preferredProject) {
    try {
      const fresh = await api('GET', '/api/chat');
      setData(fresh);
      const next = preferredProject === null
        ? ((fresh.projects[0] && fresh.projects[0].id) || '')
        : (preferredProject || projectId || (fresh.projects[0] && fresh.projects[0].id) || '');
      setProjectId(next);
      setExpandedProjectIds(previous => previous.length ? previous.filter(id => fresh.projects.some(project => project.id === id)) : (next ? [next] : []));
      return fresh;
    } catch (error) { setNotice(error.message); return null; }
  }

  async function openRouteFromLocation({ replaceInvalid = true } = {}) {
    const route = chatRouteFromPath();
    const fresh = await reload(route.projectId || undefined);
    if (!fresh) return;
    const routedProject = fresh.projects.find(item => item.id === route.projectId);
    if (route.conversationId) {
      const opened = await openConversation(route.conversationId, {
        skipNavigation: true, messageId: route.messageId,
      });
      if (opened && (opened.project.id !== route.projectId || (route.messageId && !(opened.messages || []).some(message => Number(message.id) === route.messageId)))) {
        navigateChat(opened.project.id, opened.conversation.id, null, { replace: true });
        setLinkedMessageId(null);
      }
      return;
    }
    if (routedProject) {
      setProjectId(routedProject.id); setConversationId(''); setConversation(null); setMessages([]);
      setLinkedMessageId(null); setInstallReady(null);
      setExpandedProjectIds(previous => [...new Set([...previous, routedProject.id])]);
      if (routedProject.kind === 'plugin') loadFiles(routedProject.id);
      return;
    }
    const first = fresh.projects[0];
    const firstConversation = first && first.conversations && first.conversations[0];
    if (firstConversation) {
      await openConversation(firstConversation.id, { skipNavigation: true });
      navigateChat(first.id, firstConversation.id, null, { replace: true });
    } else if (first) {
      setProjectId(first.id);
      navigateChat(first.id, '', null, { replace: true });
    } else if (replaceInvalid) {
      navigateChat('', '', null, { replace: true });
    }
  }

  useEffect(() => {
    openRouteFromLocation();
    fetch('/api/models', { headers: authHeaders() }).then(r => r.json()).then(r => r.ok && setModels(r.data || [])).catch(() => {});
    function onPopState() { openRouteFromLocation({ replaceInvalid: false }); }
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, []);

  useEffect(() => () => {
    detachLiveStream();
    cancelRecordingRef.current = true;
    clearInterval(recordTimerRef.current);
    if (audioRecorderRef.current) audioRecorderRef.current.stop();
  }, []);

  useEffect(() => {
    if (!mobileSidebarOpen) return;
    function closeOnEscape(event) {
      if (event.key === 'Escape') setMobileSidebarOpen(false);
    }
    document.addEventListener('keydown', closeOnEscape);
    return () => document.removeEventListener('keydown', closeOnEscape);
  }, [mobileSidebarOpen]);

  useEffect(() => {
    if (linkedMessageId != null) {
      requestAnimationFrame(() => {
        const target = document.getElementById(`chat-message-${linkedMessageId}`);
        if (target) target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      });
    } else if (endRef.current) {
      endRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages, streaming, status, linkedMessageId]);

  useEffect(() => {
    if (!conversationId || !running || liveStreamRunRef.current === runId) return;
    let stopped = false;
    let refreshing = false;
    async function refreshBackgroundRun() {
      if (stopped || refreshing) return;
      refreshing = true;
      try {
        const result = await api('GET', `/api/chat/conversations/${conversationId}/activity`);
        if (stopped) return;
        const conversationMessages = result.messages || [];
        setMessages(conversationMessages);
        setInstallReady(pendingInstallOffer(conversationMessages));
        if (result.active_run) {
          setRunId(result.active_run.run_id);
          setStatus(result.active_run.status || 'Trabalhando em segundo plano');
        } else {
          setRunning(false); setRunId(''); setStatus('');
        }
      } catch (_) {
        // Leaving the page or a short restart does not cancel the server task.
        // The next poll recovers the messages already persisted by the run.
      } finally {
        refreshing = false;
      }
    }
    refreshBackgroundRun();
    const timer = setInterval(refreshBackgroundRun, 2000);
    return () => { stopped = true; clearInterval(timer); };
  }, [conversationId, running, runId]);

  async function openConversation(id, options = {}) {
    try {
      if (streamConversationRef.current && streamConversationRef.current !== id) detachLiveStream();
      const result = await api('GET', `/api/chat/conversations/${id}`);
      const conversationMessages = result.messages || [];
      const activeRun = result.active_run || null;
      setConversationId(id); setConversation(result.conversation); setMessages(conversationMessages);
      setProjectId(result.project.id); setLinkedMessageId(options.messageId || null);
      setInstallReady(pendingInstallOffer(conversationMessages));
      setRunning(!!activeRun); setRunId(activeRun ? activeRun.run_id : '');
      setStatus(activeRun ? (activeRun.status || 'Trabalhando em segundo plano') : '');
      setMobileSidebarOpen(false); setShowFiles(false);
      setExpandedProjectIds(previous => [...new Set([...previous, result.project.id])]);
      if (result.project.kind === 'plugin') loadFiles(result.project.id);
      if (!options.skipNavigation) navigateChat(result.project.id, id, options.messageId || null);
      return result;
    } catch (error) { setNotice(error.message); return null; }
  }

  function openMessageLink(event, messageId) {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigateChat(projectId, conversationId, messageId);
    setLinkedMessageId(Number(messageId));
  }

  async function newConversation(pid = projectId) {
    if (!pid) return;
    try {
      const p = data.projects.find(x => x.id === pid);
      const inherited = p && p.conversations && p.conversations[0];
      const result = await api('POST', `/api/chat/projects/${pid}/conversations`, {
        model: inherited ? inherited.model : data.default_model,
        reasoning: inherited ? inherited.reasoning : '',
      });
      await reload(pid); await openConversation(result.id);
    } catch (error) { setNotice(error.message); }
  }

  async function createProject(event) {
    event.preventDefault();
    if (!newName.trim()) return;
    try {
      const created = await api('POST', '/api/chat/projects', { name: newName.trim() });
      setNewName(''); setShowNew(false); setExpandedProjectIds(previous => [...new Set([...previous, created.id])]); await reload(created.id); await newConversation(created.id);
    } catch (error) { setNotice(error.message); }
  }

  async function openInstalled(pluginId) {
    if (!pluginId) return;
    try {
      const created = await api('POST', '/api/chat/projects', { plugin_id: pluginId });
      setExpandedProjectIds(previous => [...new Set([...previous, created.id])]);
      await reload(created.id);
      const conversations = created.conversations || [];
      if (conversations.length) await openConversation(conversations[0].id); else await newConversation(created.id);
    } catch (error) { setNotice(error.message); }
  }

  async function updateConversation(values) {
    if (!conversationId) return;
    try {
      const updated = await api('PUT', `/api/chat/conversations/${conversationId}`, values);
      setConversation(updated); await reload(projectId);
    } catch (error) { setNotice(error.message); }
  }

  function selectProject(project) {
    detachLiveStream();
    setProjectId(project.id);
    setConversationId(''); setConversation(null); setMessages([]); setLinkedMessageId(null); setInstallReady(null);
    setShowFiles(false);
    navigateChat(project.id);
    setExpandedProjectIds(previous => previous.includes(project.id)
      ? previous.filter(id => id !== project.id)
      : [...previous, project.id]);
    if (project.kind === 'plugin') loadFiles(project.id);
  }

  async function renameProject(project, event) {
    event && event.stopPropagation();
    const name = window.prompt('Novo nome do projeto:', project.name);
    if (!name || name.trim() === project.name) return;
    try {
      await api('PUT', `/api/chat/projects/${project.id}`, { name: name.trim() });
      await reload(project.id);
    } catch (error) { setNotice(error.message); }
  }

  async function deleteProject(project, event) {
    event && event.stopPropagation();
    if (!window.confirm(`Ocultar o projeto “${project.name}”?\n\nO plugin instalado e os arquivos do projeto serão preservados.`)) return;
    try {
      await api('DELETE', `/api/chat/projects/${project.id}`);
      setExpandedProjectIds(previous => previous.filter(id => id !== project.id));
      setConversationId(''); setConversation(null); setMessages([]);
      const fresh = await reload(null);
      const first = fresh && fresh.projects[0];
      const firstConversation = first && first.conversations && first.conversations[0];
      if (firstConversation) await openConversation(firstConversation.id);
      else navigateChat(first ? first.id : '');
      setNotice('Projeto removido da lista. Plugin e arquivos preservados.');
    } catch (error) { setNotice(error.message); }
  }

  async function renameConversationItem(item, event) {
    event && event.stopPropagation();
    const title = window.prompt('Novo nome da conversa:', item.title);
    if (!title || title.trim() === item.title) return;
    try {
      await api('PUT', `/api/chat/conversations/${item.id}`, { title: title.trim() });
      if (conversationId === item.id) setConversation(previous => ({ ...previous, title: title.trim() }));
      await reload(projectId);
    } catch (error) { setNotice(error.message); }
  }

  async function deleteConversationItem(item, event) {
    event && event.stopPropagation();
    if (!window.confirm(`Apagar a conversa “${item.title}” e todo o histórico dela?`)) return;
    try {
      await api('DELETE', `/api/chat/conversations/${item.id}`);
      const fresh = await reload(projectId);
      const currentProject = fresh && fresh.projects.find(candidate => candidate.id === projectId);
      const remaining = (currentProject && currentProject.conversations) || [];
      if (conversationId === item.id) {
        if (remaining.length) await openConversation(remaining[0].id);
        else {
          setConversationId(''); setConversation(null); setMessages([]); setLinkedMessageId(null);
          navigateChat(projectId);
        }
      }
    } catch (error) { setNotice(error.message); }
  }

  async function dropProject(targetProjectId) {
    const sourceProjectId = draggingProjectId;
    setDraggingProjectId('');
    if (!sourceProjectId || sourceProjectId === targetProjectId) return;
    const projects = [...data.projects];
    const sourceIndex = projects.findIndex(project => project.id === sourceProjectId);
    const targetIndex = projects.findIndex(project => project.id === targetProjectId);
    if (sourceIndex < 0 || targetIndex < 0) return;
    const [moved] = projects.splice(sourceIndex, 1);
    projects.splice(targetIndex, 0, moved);
    setData(previous => ({ ...previous, projects }));
    try {
      await api('POST', '/api/chat/projects/reorder', { project_ids: projects.map(project => project.id) });
    } catch (error) {
      setNotice(error.message);
      await reload(projectId);
    }
  }

  async function moveProject(projectIdToMove, offset, event) {
    event && event.stopPropagation();
    const projects = [...data.projects];
    const sourceIndex = projects.findIndex(project => project.id === projectIdToMove);
    const targetIndex = sourceIndex + offset;
    if (sourceIndex < 0 || targetIndex < 0 || targetIndex >= projects.length) return;
    const [moved] = projects.splice(sourceIndex, 1);
    projects.splice(targetIndex, 0, moved);
    setData(previous => ({ ...previous, projects }));
    try {
      await api('POST', '/api/chat/projects/reorder', { project_ids: projects.map(project => project.id) });
    } catch (error) {
      setNotice(error.message);
      await reload(projectId);
    }
  }

  async function loadFiles(pid = projectId) {
    if (!pid) return;
    try { const result = await api('GET', `/api/chat/projects/${pid}/files`); setFiles(result.files || []); }
    catch { setFiles([]); }
  }

  async function openFile(path) {
    try { setSelectedFile(await api('GET', `/api/chat/projects/${projectId}/file?path=${encodeURIComponent(path)}`)); }
    catch (error) { setNotice(error.message); }
  }

  async function transcribeAndSendAudio(blob, targetConversationId) {
    setTranscribing(true);
    setNotice('Transcrevendo áudio…');
    try {
      const form = new FormData();
      form.append('audio', blob, 'voice.ogg');
      const response = await fetch(`/api/chat/conversations/${targetConversationId}/transcribe-audio`, {
        method: 'POST', headers: authHeaders(), body: form,
      });
      const result = await response.json();
      if (!result.ok) throw new Error(result.error || 'Falha ao transcrever o áudio');
      const transcription = String((result.data || {}).transcription || '').trim();
      if (!transcription) throw new Error('O áudio não gerou uma transcrição.');
      if (targetConversationId !== chatRouteFromPath().conversationId) {
        throw new Error('A conversa mudou durante a transcrição. Grave novamente na conversa desejada.');
      }
      setNotice('');
      setInput(transcription);
      setTranscribing(false);
      await send(null, transcription, { fromAudio: true });
    } catch (error) {
      setNotice(error.message);
      setTranscribing(false);
    }
  }

  async function startAudioRecording() {
    if (running || transcribing || recording) return;
    if (typeof window.Recorder !== 'function') {
      setNotice('Gravador de áudio indisponível. Recarregue a página e tente novamente.');
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setNotice('O navegador só permite o microfone em HTTPS ou em http://localhost.');
      return;
    }
    cancelRecordingRef.current = false;
    const targetConversationId = conversationId;
    try {
      const recorder = new window.Recorder({
        encoderPath: '/static/vendor/opus-recorder/encoderWorker.min.js',
        encoderApplication: 2048,
        encoderSampleRate: 48000,
        numberOfChannels: 1,
      });
      audioRecorderRef.current = recorder;
      recorder.onstart = () => {
        setNotice(''); setRecording(true); setRecordDuration(0);
        recordTimerRef.current = setInterval(() => setRecordDuration(value => value + 1), 1000);
      };
      recorder.ondataavailable = (data) => {
        if (cancelRecordingRef.current || !data || data.size === 0) return;
        const blob = new Blob([data], { type: 'audio/ogg' });
        transcribeAndSendAudio(blob, targetConversationId);
      };
      recorder.onstop = () => {
        setRecording(false); setRecordDuration(0);
        clearInterval(recordTimerRef.current);
        audioRecorderRef.current = null;
      };
      await recorder.start();
    } catch (error) {
      audioRecorderRef.current = null;
      setRecording(false); setRecordDuration(0);
      clearInterval(recordTimerRef.current);
      setNotice(error && error.name === 'NotAllowedError'
        ? 'Permissão para o microfone negada. Habilite o acesso nas configurações do navegador.'
        : `Não foi possível iniciar a gravação: ${error && error.message ? error.message : error}`);
    }
  }

  function stopAudioRecording() {
    cancelRecordingRef.current = false;
    if (audioRecorderRef.current) audioRecorderRef.current.stop();
  }

  function cancelAudioRecording() {
    cancelRecordingRef.current = true;
    setRecording(false); setRecordDuration(0);
    clearInterval(recordTimerRef.current);
    if (audioRecorderRef.current) audioRecorderRef.current.stop();
    setNotice('Gravação cancelada.');
  }

  async function send(event, overrideContent = '', options = {}) {
    event && event.preventDefault();
    if (running || (transcribing && !options.fromAudio) || !(overrideContent || input).trim()) return;
    if (!conversationId) { setNotice('Crie uma conversa primeiro.'); return; }
    const content = (overrideContent || input).trim();
    const optimistic = { id: `local-${Date.now()}`, role: 'user', kind: 'message', content };
    if (linkedMessageId != null) {
      setLinkedMessageId(null);
      navigateChat(projectId, conversationId);
    }
    setInput('');
    if (inputRef.current) {
      inputRef.current.style.height = 'auto';
      inputRef.current.style.overflowY = 'hidden';
    }
    setMessages(prev => [...prev, optimistic]); setStreaming(''); setRunning(true); setStatus('Iniciando'); setInstallReady(null);
    const streamController = new AbortController();
    streamControllerRef.current = streamController;
    streamConversationRef.current = conversationId;
    try {
      const response = await fetch(`/api/chat/conversations/${conversationId}/messages`, {
        method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify({ content }),
        signal: streamController.signal,
      });
      if (!response.ok) { const failure = await response.json(); throw new Error(failure.error || 'Falha ao iniciar'); }
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        const lines = buffer.split('\n'); buffer = lines.pop();
        for (const line of lines) {
          if (!line.trim()) continue;
          const evt = JSON.parse(line);
          const payload = evt.data || {};
          if (evt.event === 'run_started') {
            liveStreamRunRef.current = payload.run_id;
            setRunId(payload.run_id);
          }
          else if (evt.event === 'content') setStreaming(prev => prev + (payload.delta || ''));
          else if (evt.event === 'status') setStatus(payload.label || 'Trabalhando');
          else if (evt.event === 'action_started') setMessages(prev => [...prev, payload]);
          else if (evt.event === 'action_completed') setMessages(prev => {
            const existing = prev.findIndex(message => message.id === payload.id);
            if (existing < 0) return [...prev, payload];
            const next = [...prev]; next[existing] = payload; return next;
          });
          else if (evt.event === 'metrics') setMessages(prev => [...prev, payload]);
          else if (evt.event === 'message' || evt.event === 'message_saved') { setStreaming(''); setMessages(prev => [...prev, payload]); }
          else if (evt.event === 'install_ready') setInstallReady(payload);
          else if (evt.event === 'install_completed') setNotice(`${payload.message} O Whatsbot-lite será reiniciado.`);
          else if (evt.event === 'validation_failed') setNotice(`O plugin ainda não passou na validação: ${payload.error}`);
          else if (evt.event === 'compacted') setNotice('O contexto foi compactado automaticamente.');
          else if (evt.event === 'conversation_updated') setConversation(prev => ({ ...prev, title: payload.title }));
          else if (evt.event === 'run_failed') throw new Error(payload.error || 'Execução falhou');
        }
      }
      await openConversation(conversationId); await reload(projectId); if (project && project.kind === 'plugin') await loadFiles();
    } catch (error) {
      if (!error || error.name !== 'AbortError') setNotice(error.message);
    }
    finally {
      if (streamControllerRef.current === streamController) {
        streamControllerRef.current = null;
        streamConversationRef.current = '';
        liveStreamRunRef.current = '';
        setRunning(false); setStreaming(''); setStatus(''); setRunId('');
      }
    }
  }

  async function cancel() {
    if (!runId) return;
    try { await api('POST', `/api/chat/runs/${runId}/cancel`, {}); setStatus('Cancelando…'); }
    catch (error) { setNotice(error.message); }
  }

  async function install() {
    if (installing) return;
    setInstalling(true);
    try {
      const result = await api('POST', `/api/chat/projects/${projectId}/install`, { conversation_id: conversationId });
      setNotice(result.message + (result.restarting ? ' O Whatsbot-lite será reiniciado.' : ''));
      setInstallReady(null);
      setMessages(previous => [
        ...previous.map(message => message.kind === 'install_offer'
          ? { ...message, metadata: { ...(message.metadata || {}), status: result.updated ? 'updated' : 'installed' } }
          : message),
        {
          id: `install-${Date.now()}`,
          role: 'assistant',
          kind: 'message',
          content: `${result.message} O Whatsbot-lite será reiniciado para carregar o plugin.`,
        },
      ]);
    } catch (error) { setNotice(error.message); }
    finally { setInstalling(false); }
  }

  async function rollback() {
    try {
      const result = await api('POST', `/api/chat/projects/${projectId}/rollback`, {});
      setNotice(`Backup restaurado (v${result.version}). O Whatsbot-lite será reiniciado.`);
    } catch (error) { setNotice(error.message); }
  }

  async function testCache() {
    if (!conversation) return;
    setNotice('Testando cache com duas chamadas repetidas…');
    try {
      const result = await api('POST', '/api/chat/cache-test', { model: conversation.model, reasoning: conversation.reasoning });
      setNotice(result.critical ? `Pendência crítica: ${result.status}` : result.status);
    } catch (error) { setNotice(error.message); }
  }

  const existingPluginIds = new Set(data.projects.filter(p => p.plugin_id).map(p => p.plugin_id));
  const unopened = data.installed_plugins.filter(p => !existingPluginIds.has(p.id));

  return html`
    <div class="chat-shell h-full min-h-0 flex text-wa-text overflow-hidden">
      <button
        type="button"
        class="chat-sidebar-backdrop ${mobileSidebarOpen ? 'is-open' : ''}"
        onClick=${() => setMobileSidebarOpen(false)}
        aria-label="Fechar lista de projetos"
        tabindex=${mobileSidebarOpen ? '0' : '-1'}
      ></button>
      <aside id="chat-project-sidebar" class="chat-sidebar w-72 shrink-0 flex flex-col ${mobileSidebarOpen ? 'is-mobile-open' : ''}">
        <div class="chat-sidebar-head p-4">
          <div class="flex items-center gap-3">
            <a href="/" class="chat-icon-button" title="Voltar ao Whatsbot-lite" aria-label="Voltar ao Whatsbot-lite">←</a>
            <div class="min-w-0 flex-1"><div class="font-semibold text-[15px]">Chat</div><div class="text-[11px] opacity-70">Whatsbot-lite + Criador de Plugins</div></div>
            <button onClick=${() => setShowNew(!showNew)} class="chat-icon-button" title="Novo plugin" aria-label="Novo plugin">＋</button>
            <button type="button" onClick=${() => setMobileSidebarOpen(false)} class="chat-icon-button chat-mobile-close" title="Fechar projetos" aria-label="Fechar projetos">×</button>
          </div>
          ${showNew ? html`<form onSubmit=${createProject} class="chat-new-project mt-4"><label class="block text-[11px] font-semibold mb-1.5">NOVO PROJETO</label><input autoFocus value=${newName} onInput=${e => setNewName(e.target.value)} placeholder="Nome do plugin" class="w-full px-3 py-2.5 rounded-lg text-sm text-wa-text"/><button class="mt-2 w-full py-2 rounded-lg bg-wa-teal text-white font-medium text-sm cursor-pointer">Criar projeto</button></form>` : null}
          ${unopened.length ? html`<select class="chat-open-plugin mt-3 w-full px-3 py-2.5 rounded-lg text-xs text-wa-text cursor-pointer" value="" onChange=${e => openInstalled(e.target.value)}><option value="">Abrir plugin instalado…</option>${unopened.map(p => html`<option value=${p.id}>${p.id}</option>`)}</select>` : null}
        </div>
        <div class="px-3 pt-4 pb-2 text-[10px] tracking-[0.14em] font-bold opacity-55">PROJETOS</div>
        <div class="flex-1 overflow-auto wa-scrollbar px-2 pb-3">
          ${data.projects.map((p, projectIndex) => html`
            <div key=${p.id} class="mb-1" draggable="true" onDragStart=${event => { setDraggingProjectId(p.id); event.dataTransfer.effectAllowed = 'move'; }} onDragOver=${event => { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; }} onDrop=${event => { event.preventDefault(); dropProject(p.id); }} onDragEnd=${() => setDraggingProjectId('')}>
              <div class="chat-project ${projectId === p.id ? 'is-active' : ''} ${expandedProjectIds.includes(p.id) ? 'is-expanded' : ''} ${draggingProjectId === p.id ? 'is-dragging' : ''}">
                <a href=${chatPath(p.id)} class="chat-project-main no-underline" onClick=${event => {
                  if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
                  event.preventDefault(); selectProject(p);
                }} title="Abrir projeto">
                  <span class="chat-drag-handle" title="Arraste para mudar a posição">⋮⋮</span>
                  <span class="chat-project-mark">${p.kind === 'system' ? '●' : '◆'}</span>
                  <span class="min-w-0 flex-1"><span class="block font-semibold truncate">${p.name}</span><span class="block text-[11px] truncate opacity-70">${p.plugin_id || 'Ajuda do sistema'}</span></span>
                  <span class="chat-project-chevron">›</span>
                </a>
                <button disabled=${projectIndex === 0} class="chat-row-action chat-mobile-reorder" onClick=${event => moveProject(p.id, -1, event)} title="Mover projeto para cima" aria-label="Mover projeto para cima">↑</button>
                <button disabled=${projectIndex === data.projects.length - 1} class="chat-row-action chat-mobile-reorder" onClick=${event => moveProject(p.id, 1, event)} title="Mover projeto para baixo" aria-label="Mover projeto para baixo">↓</button>
                <button class="chat-row-action" onClick=${event => renameProject(p, event)} title="Renomear projeto" aria-label="Renomear projeto">✎</button>
                ${p.kind !== 'system' ? html`<button class="chat-row-action is-danger" onClick=${event => deleteProject(p, event)} title="Remover projeto" aria-label="Remover projeto">×</button>` : null}
              </div>
              ${expandedProjectIds.includes(p.id) ? html`<div class="chat-conversations py-1.5 pl-4">
                ${(p.conversations || []).map(c => html`<div class="chat-conversation ${conversationId === c.id ? 'is-active' : ''}" title=${c.title}>
                  <a href=${chatPath(p.id, c.id)} class="chat-conversation-main no-underline" onClick=${event => {
                    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
                    event.preventDefault(); openConversation(c.id);
                  }}><span class="chat-conversation-dot"></span><span class="truncate">${c.title}</span></a>
                  <button class="chat-row-action" onClick=${event => renameConversationItem(c, event)} title="Renomear conversa" aria-label="Renomear conversa">✎</button>
                  <button class="chat-row-action is-danger" onClick=${event => deleteConversationItem(c, event)} title="Apagar conversa" aria-label="Apagar conversa">×</button>
                </div>`)}
                <button onClick=${() => newConversation(p.id)} class="chat-new-conversation">＋ Nova conversa</button>
              </div>` : null}
            </div>
          `)}
        </div>
      </aside>

      <section class="chat-main flex-1 min-w-0 flex flex-col">
        <header class="chat-topbar min-h-[64px] px-5 py-2 flex items-center gap-3">
          <button
            type="button"
            class="chat-mobile-menu"
            onClick=${() => setMobileSidebarOpen(true)}
            aria-expanded=${mobileSidebarOpen}
            aria-controls="chat-project-sidebar"
            title="Abrir projetos"
            aria-label="Abrir projetos"
          >
            <svg viewBox="0 0 24 24" width="21" height="21" fill="currentColor" aria-hidden="true"><path d="M4 6h16v2H4V6zm0 5h16v2H4v-2zm0 5h16v2H4v-2z"/></svg>
          </button>
          <div class="chat-topbar-title min-w-0 mr-auto"><div class="font-semibold truncate">${conversation ? conversation.title : project ? project.name : 'Chat'}</div><div class="text-[11px] text-wa-secondary truncate">${project && project.kind === 'plugin' ? `Projeto ${project.plugin_id}` : 'Ajuda e configuração do Whatsbot-lite'}</div></div>
          ${project && project.kind === 'plugin' ? html`<button onClick=${() => { setShowFiles(!showFiles); loadFiles(); }} class="chat-secondary-button">▱ Arquivos</button>` : null}
          ${installedProjectPlugin && installedProjectPlugin.load_error ? html`<button onClick=${rollback} class="chat-danger-button" title=${installedProjectPlugin.load_error}>Restaurar</button>` : null}
        </header>

        <div class="flex-1 min-h-0 flex">
          <div class="chat-transcript flex-1 min-w-0 overflow-auto wa-scrollbar p-4 md:p-6">
            <div class="max-w-3xl mx-auto">
              ${!conversation ? html`<div class="h-full min-h-64 flex flex-col items-center justify-center text-center text-wa-secondary"><div class="text-4xl mb-3">✦</div><div class="text-lg text-wa-text">${project ? 'Crie ou abra uma conversa' : 'Escolha um projeto'}</div><div class="text-sm mt-2 max-w-md">Pergunte como configurar o Whatsbot-lite ou descreva o plugin que deseja criar.</div></div>` : html`<${MessageList} messages=${messages} streaming=${streaming} running=${running} projectId=${projectId} conversationId=${conversationId} linkedMessageId=${linkedMessageId} onMessageLink=${openMessageLink} />`}
              ${running && status ? html`<div class="text-xs text-wa-secondary animate-pulse my-2">${status}</div>` : null}
              ${installReady ? html`<div class="my-4 p-4 rounded-xl border border-wa-teal/40 bg-wa-bg shadow-sm">
                <div class="font-medium">${installReady.installed ? 'Plugin validado. Quer atualizar o plugin instalado?' : 'Plugin pronto. Quer instalar?'}</div>
                <div class="text-xs text-wa-secondary mt-1">${installReady.plugin_id} v${installReady.version} · ${installReady.tests} teste(s)</div>
                <div class="flex flex-wrap gap-2 mt-3"><button onClick=${install} disabled=${installing} class="px-4 py-2 rounded bg-wa-teal text-white text-sm disabled:opacity-60">${installing ? 'Instalando…' : 'Sim, instalar'}</button><button onClick=${() => setInstallReady(null)} disabled=${installing} class="px-4 py-2 rounded border border-wa-border text-sm">Agora não</button><a href=${`/api/chat/projects/${projectId}/export`} class="px-4 py-2 rounded border border-wa-border text-sm no-underline text-wa-text">Baixar ZIP</a></div>
              </div>` : null}
              <div ref=${endRef}></div>
            </div>
          </div>

          ${showFiles && project && project.kind === 'plugin' ? html`<aside class="chat-files w-[40%] min-w-72 max-w-xl flex flex-col">
            <div class="px-3 py-2 border-b border-wa-border flex items-center justify-between"><span class="font-medium text-sm">Arquivos do projeto</span><button class="chat-icon-button" onClick=${() => setShowFiles(false)}>×</button></div>
            <div class="chat-files-body flex min-h-0 flex-1">
              <div class="chat-file-list w-44 overflow-auto border-r border-wa-border wa-scrollbar">${files.map(file => html`<button title=${file} onClick=${() => openFile(file)} class="chat-file-row ${selectedFile && selectedFile.path === file ? 'is-active' : ''}">${file}</button>`)}</div>
              <pre class="chat-file-content flex-1 overflow-auto p-3 text-xs whitespace-pre-wrap wa-scrollbar">${selectedFile ? selectedFile.content : 'Selecione um arquivo.'}</pre>
            </div>
          </aside>` : null}
        </div>

        ${conversation ? html`<div class="chat-composer-zone px-3 pb-4 pt-2">
          ${notice ? html`<div class="chat-notice max-w-3xl mx-auto mb-2 px-3 py-2.5 rounded-lg text-xs flex gap-2"><span class="flex-1">${notice}</span><button class="cursor-pointer font-bold" onClick=${() => setNotice('')}>×</button></div>` : null}
          <form onSubmit=${send} class="chat-composer max-w-3xl mx-auto relative">
            ${recording ? html`<div class="chat-audio-recording flex min-h-[76px] items-center gap-3 px-4">
              <button type="button" onClick=${cancelAudioRecording} class="chat-audio-cancel" title="Cancelar gravação" aria-label="Cancelar gravação">×</button>
              <span class="h-2.5 w-2.5 shrink-0 animate-pulse rounded-full bg-red-500"></span>
              <span class="font-medium text-red-500">${formatRecordTime(recordDuration)}</span>
              <span class="chat-recording-label min-w-0 flex-1 text-sm text-wa-secondary">Gravando áudio…</span>
              <button type="button" onClick=${stopAudioRecording} class="chat-audio-stop" title="Parar e enviar" aria-label="Parar e enviar"><${StopIcon} /></button>
            </div>` : html`
            ${commandOptions.length ? html`<div class="chat-command-menu absolute left-0 right-0 bottom-[calc(100%+8px)] rounded-xl overflow-hidden shadow-xl">
              <div class="px-3 py-2 text-[10px] tracking-[0.12em] font-bold text-wa-secondary">COMANDOS</div>
              ${commandOptions.map((command, index) => html`<button type="button" onMouseDown=${e => { e.preventDefault(); selectCommand(command); }} class="chat-command-row ${index === commandIndex ? 'is-active' : ''}"><code>${command.value}</code><span><strong>${command.title}</strong><small>${command.description}</small></span></button>`)}
            </div>` : null}
            <textarea ref=${inputRef} rows="1" value=${input} disabled=${running || transcribing} onInput=${e => updateInput(e.target.value, e.target)} onKeyDown=${e => {
              if (commandOptions.length && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
                e.preventDefault();
                setCommandIndex(index => (index + (e.key === 'ArrowDown' ? 1 : -1) + commandOptions.length) % commandOptions.length);
              } else if (commandOptions.length && e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault(); selectCommand(commandOptions[commandIndex] || commandOptions[0]);
              } else if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault(); send();
              }
            }} placeholder=${project && project.kind === 'system' ? 'Pergunte sobre o Whatsbot-lite…' : 'Descreva o plugin ou a alteração…'} class="chat-composer-input wa-scrollbar"></textarea>
            <div class="chat-composer-bar">
              <div class="chat-composer-options flex min-w-0 items-center gap-1.5">
                <${ModelPicker} controlRef=${modelRef} models=${models} value=${conversation.model} disabled=${running || transcribing} onChange=${id => updateConversation({ model: id, reasoning: '' })} />
                ${supportsReasoning ? html`<select class="chat-effort-select" value=${conversation.reasoning || ''} disabled=${running || transcribing} title="Nível de raciocínio" onChange=${e => updateConversation({ reasoning: e.target.value })}><option value="">Padrão</option><option value="low">Baixo</option><option value="medium">Médio</option><option value="high">Alto</option></select>` : null}
                <button type="button" onClick=${testCache} disabled=${running || transcribing} class="chat-composer-tool" title="Testar cache">Cache</button>
              </div>
              <div class="chat-composer-actions flex items-center gap-2 shrink-0">
                <span class="hidden sm:inline text-[10px] text-wa-secondary">${transcribing ? 'Transcrevendo áudio…' : 'Enter envia · / comandos'}</span>
                ${!running ? html`<button type="button" onClick=${startAudioRecording} disabled=${transcribing} class="chat-mic-button" title="Gravar áudio" aria-label="Gravar áudio"><${MicIcon} /></button>` : null}
                ${running ? html`<button type="button" onClick=${cancel} class="chat-stop-button" title="Interromper">■</button>` : html`<button class="chat-send-button" title="Enviar" aria-label="Enviar" disabled=${transcribing || !input.trim()}>↑</button>`}
              </div>
            </div>
            `}
          </form>
        </div>` : null}
      </section>
    </div>
  `;
}

export default Chat;
