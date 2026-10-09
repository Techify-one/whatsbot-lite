import { h, render } from 'preact';
import { useState, useEffect, useCallback, useRef } from 'preact/hooks';
import htm from 'htm';
import { InstallApp } from './components/InstallApp.js';
import { Dashboard } from './components/Dashboard.js';
import { Sandbox } from './components/Sandbox.js';
import { Contacts } from './components/Contacts.js';
import { CostsDashboard } from './components/CostsDashboard.js';
import { Executions } from './components/Executions.js';
import { LoginScreen } from './components/LoginScreen.js';
import { PluginsManager } from './components/PluginsManager.js';
import { PluginScreen } from './components/PluginScreen.js';
import { loadPluginExtensions } from './plugins/slots.js';
import { ToolsManager } from './components/ToolsManager.js';
import { Chat } from './components/Chat.js';
import { SetupWizard } from './components/SetupWizard.js';
import { LowBalanceModal } from './components/LowBalanceModal.js';
import { GowaUpdateModal } from './components/GowaUpdateModal.js';
import { WhatsNewModal } from './components/WhatsNewModal.js';
import { useWebSocket } from './hooks/useWebSocket.js';
import { useConfig } from './hooks/useConfig.js';
import { checkAuth, authHeaders, getUnreadCount, installGowaUpdate, skipGowaVersion, checkForUpdates, performUpdate, skipWhatsBotVersion } from './services/api.js';
import { playTransferAlert } from './utils/alertSound.js';
import { getNotifPref, playNotificationSound, showBrowserNotification } from './utils/notifications.js';

const LOW_BALANCE_SNOOZE_KEY = 'whatsbot_low_balance_snoozed_until';

function lowBalanceIsSnoozed() {
  try {
    const v = parseInt(localStorage.getItem(LOW_BALANCE_SNOOZE_KEY) || '0', 10);
    return v && Date.now() < v;
  } catch { return false; }
}

function snoozeLowBalance(ms) {
  try {
    localStorage.setItem(LOW_BALANCE_SNOOZE_KEY, String(Date.now() + ms));
  } catch {}
}

const GOWA_UPDATE_SNOOZE_KEY = 'whatsbot_gowa_update_snoozed_until';

function gowaUpdateIsSnoozed() {
  try {
    const v = parseInt(localStorage.getItem(GOWA_UPDATE_SNOOZE_KEY) || '0', 10);
    return v && Date.now() < v;
  } catch { return false; }
}

function snoozeGowaUpdate(ms) {
  try {
    localStorage.setItem(GOWA_UPDATE_SNOOZE_KEY, String(Date.now() + ms));
  } catch {}
}

const html = htm.bind(h);

// Core (built-in) routes. Plugin screens are merged in dynamically below.
const CORE_ROUTES = {
  '/': 'contacts',
  '/painel': 'dashboard',
  '/sandbox': 'sandbox',
  '/costs': 'costs',
  '/executions': 'executions',
  '/plugins': 'plugins',
  '/tools': 'tools',
  '/chat': 'chat',
};
const CORE_TAB_PATHS = {
  contacts: '/',
  dashboard: '/painel',
  sandbox: '/sandbox',
  costs: '/costs',
  executions: '/executions',
  plugins: '/plugins',
  tools: '/tools',
  chat: '/chat',
};

// Tab id used internally for plugin screens. We encode the plugin id and
// the screen path so the router can round-trip it.
function pluginTabId(screen) { return `plugin:${screen.pluginId}:${screen.path}`; }

function tabFromPath(pluginScreens) {
  const path = window.location.pathname;
  if (path.match(/^\/contacts\/\d+$/)) return 'contacts';
  if (path.match(/^\/executions\/\d+$/)) return 'executions';
  if (path.match(/^\/chat(?:\/|$)/)) return 'chat';
  const screen = (pluginScreens || []).find(s => s.path === path);
  if (screen) return pluginTabId(screen);
  return CORE_ROUTES[path] || 'contacts';
}

function pathForTab(tab, pluginScreens) {
  if (CORE_TAB_PATHS[tab]) return CORE_TAB_PATHS[tab];
  if (tab && tab.startsWith('plugin:')) {
    const screen = (pluginScreens || []).find(s => pluginTabId(s) === tab);
    if (screen) return screen.path;
  }
  return '/';
}

function contactIdFromPath() {
  const m = window.location.pathname.match(/^\/contacts\/(\d+)$/);
  return m ? parseInt(m[1], 10) : null;
}

function MenuItem({ active, href, onClick, icon, children }) {
  function handleClick(e) {
    // Let the browser handle modified clicks (Ctrl/Cmd/Shift) and middle-click
    // so users can open the menu item in a new tab/window.
    if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    onClick();
  }
  return html`
    <a
      href=${href}
      onClick=${handleClick}
      class="w-full text-left px-4 py-2.5 text-[14px] hover:bg-wa-hover transition-colors flex items-center gap-2 no-underline ${active ? 'text-wa-teal font-medium' : 'text-wa-text'}"
    >
      ${icon}
      ${children}
    </a>
  `;
}

function GearMenu({ tab, onTabChange, pluginScreens, hasPassword, onLogout, accountUrl }) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef(null);

  useEffect(() => {
    function handleClick(e) {
      if (menuRef.current && !menuRef.current.contains(e.target)) setOpen(false);
    }
    if (open) document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [open]);

  const close = () => setOpen(false);

  // Dark mode: toggles `.dark` on <html> (re-themes the whole app via CSS
  // variables) and persists the choice. The early script in index.html applies
  // it before first paint so there's no flash on reload.
  const [dark, setDark] = useState(() => document.documentElement.classList.contains('dark'));
  function toggleDark() {
    const next = !document.documentElement.classList.contains('dark');
    document.documentElement.classList.toggle('dark', next);
    try { localStorage.setItem('whatsbot_theme', next ? 'dark' : 'light'); } catch (e) {}
    setDark(next);
  }

  return html`
    <div ref=${menuRef} class="fixed top-3 right-3 z-50">
      <button
        onClick=${() => setOpen(!open)}
        class="w-[36px] h-[36px] flex items-center justify-center rounded-full bg-wa-bg shadow-md border border-wa-border hover:bg-wa-hover transition-colors"
      >
        <svg viewBox="0 0 24 24" width="20" height="20" fill="#54656f">
          <path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 00.12-.61l-1.92-3.32a.488.488 0 00-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 00-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.07.62-.07.94s.02.64.07.94l-2.03 1.58a.49.49 0 00-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/>
        </svg>
      </button>
      ${open ? html`
        <div class="absolute right-0 mt-1 bg-wa-bg rounded-lg shadow-lg border border-wa-border py-1 min-w-[180px] max-h-[80vh] overflow-y-auto">
          <${MenuItem} active=${tab === 'dashboard'} href=${CORE_TAB_PATHS.dashboard} onClick=${() => { onTabChange('dashboard'); close(); }}
            icon=${html`<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 00.12-.61l-1.92-3.32a.488.488 0 00-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 00-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.07.62-.07.94s.02.64.07.94l-2.03 1.58a.49.49 0 00-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/></svg>`}
          >Painel</${MenuItem}>
          <${MenuItem} active=${tab === 'chat'} href=${CORE_TAB_PATHS.chat} onClick=${() => { onTabChange('chat'); close(); }}
            icon=${html`<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M20 2H4a2 2 0 00-2 2v18l4-4h14a2 2 0 002-2V4a2 2 0 00-2-2zM7 9h10v2H7V9zm6 5H7v-2h6v2zm4-6H7V6h10v2z"/></svg>`}
          >Chat</${MenuItem}>
          <${MenuItem} active=${tab === 'sandbox'} href=${CORE_TAB_PATHS.sandbox} onClick=${() => { onTabChange('sandbox'); close(); }}
            icon=${html`<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M7 5h10v2h2V3c0-.55-.45-1-1-1H6c-.55 0-1 .45-1 1v4h2V5zm8.41 11.59L20 12l-4.59-4.59L14 8.83 17.17 12 14 15.17l1.41 1.42zM10 15.17L6.83 12 10 8.83 8.59 7.41 4 12l4.59 4.59L10 15.17zM17 19H7v-2H5v4c0 .55.45 1 1 1h12c.55 0 1-.45 1-1v-4h-2v2z"/></svg>`}
          >Sandbox</${MenuItem}>
          <${MenuItem} active=${tab === 'costs'} href=${CORE_TAB_PATHS.costs} onClick=${() => { onTabChange('costs'); close(); }}
            icon=${html`<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M11.8 10.9c-2.27-.59-3-1.2-3-2.15 0-1.09 1.01-1.85 2.7-1.85 1.78 0 2.44.85 2.5 2.1h2.21c-.07-1.72-1.12-3.3-3.21-3.81V3h-3v2.16c-1.94.42-3.5 1.68-3.5 3.61 0 2.31 1.91 3.46 4.7 4.13 2.5.6 3 1.48 3 2.41 0 .69-.49 1.79-2.7 1.79-2.06 0-2.87-.92-2.98-2.1h-2.2c.12 2.19 1.76 3.42 3.68 3.83V21h3v-2.15c1.95-.37 3.5-1.5 3.5-3.55 0-2.84-2.43-3.81-4.7-4.4z"/></svg>`}
          >Custos</${MenuItem}>
          <${MenuItem} active=${tab === 'executions'} href=${CORE_TAB_PATHS.executions} onClick=${() => { onTabChange('executions'); close(); }}
            icon=${html`<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M13 3c-4.97 0-9 4.03-9 9H1l3.89 3.89.07.14L9 12H6c0-3.87 3.13-7 7-7s7 3.13 7 7-3.13 7-7 7c-1.93 0-3.68-.79-4.94-2.06l-1.42 1.42C8.27 19.99 10.51 21 13 21c4.97 0 9-4.03 9-9s-4.03-9-9-9zm-1 5v5l4.28 2.54.72-1.21-3.5-2.08V8H12z"/></svg>`}
          >Execuções</${MenuItem}>

          ${(pluginScreens && pluginScreens.length > 0) ? html`
            <div class="border-t border-wa-border my-1"></div>
            <div class="px-4 py-1.5 text-[11px] uppercase tracking-wide text-wa-secondary">
              Plugins
            </div>
            ${pluginScreens.map(s => html`
              <${MenuItem}
                key=${pluginTabId(s)}
                active=${tab === pluginTabId(s)}
                href=${s.path}
                onClick=${() => { onTabChange(pluginTabId(s)); close(); }}
                icon=${html`<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M5 3h6v8H5V3zm8 0h6v6h-6V3zm0 8h6v10h-6V11zm-8 4h6v6H5v-6z"/></svg>`}
              >${s.title}</${MenuItem}>
            `)}
          ` : null}

          <div class="border-t border-wa-border my-1"></div>
          ${accountUrl ? html`
            <a
              href=${accountUrl}
              target="_blank"
              rel="noopener noreferrer"
              onClick=${close}
              class="w-full text-left px-4 py-2.5 text-[14px] hover:bg-wa-hover transition-colors flex items-center gap-2 no-underline text-wa-text"
            >
              <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M20 4H4c-1.11 0-1.99.89-1.99 2L2 18c0 1.11.89 2 2 2h16c1.11 0 2-.89 2-2V6c0-1.11-.89-2-2-2zm0 14H4v-6h16v6zm0-10H4V6h16v2z"/></svg>
              Saldo e Recargar
            </a>
          ` : null}
          <${MenuItem} active=${tab === 'tools'} href=${CORE_TAB_PATHS.tools} onClick=${() => { onTabChange('tools'); close(); }}
            icon=${html`<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M22.7 19l-9.1-9.1c.9-2.3.4-5-1.5-6.9-2-2-5-2.4-7.4-1.3L9 6 6 9 1.6 4.7C.4 7.1.9 10.1 2.9 12.1c1.9 1.9 4.6 2.4 6.9 1.5l9.1 9.1c.4.4 1 .4 1.4 0l2.3-2.3c.5-.4.5-1.1.1-1.4z"/></svg>`}
          >Gerenciar Tools</${MenuItem}>
          <${MenuItem} active=${tab === 'plugins'} href=${CORE_TAB_PATHS.plugins} onClick=${() => { onTabChange('plugins'); close(); }}
            icon=${html`<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M20.5 11H19V7c0-1.1-.9-2-2-2h-4V3.5C13 2.12 11.88 1 10.5 1S8 2.12 8 3.5V5H4c-1.1 0-1.99.9-1.99 2v3.8H3.5c1.49 0 2.7 1.21 2.7 2.7s-1.21 2.7-2.7 2.7H2V20c0 1.1.9 2 2 2h3.8v-1.5c0-1.49 1.21-2.7 2.7-2.7s2.7 1.21 2.7 2.7V22H17c1.1 0 2-.9 2-2v-4h1.5c1.38 0 2.5-1.12 2.5-2.5S21.88 11 20.5 11z"/></svg>`}
          >Gerenciar Plugins</${MenuItem}>

          <div class="border-t border-wa-border my-1"></div>
          <${InstallApp} />
          <button
            onClick=${toggleDark}
            class="w-full text-left px-4 py-2.5 text-[14px] hover:bg-wa-hover transition-colors flex items-center gap-2 text-wa-text"
          >
            ${dark
              ? html`<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M12 7c-2.76 0-5 2.24-5 5s2.24 5 5 5 5-2.24 5-5-2.24-5-5-5zm0-5l2.39 3.42C13.65 5.15 12.84 5 12 5c-.84 0-1.65.15-2.39.42L12 2zM3.34 7l4.16-.35C6.84 7.28 6.31 8 5.91 8.81L3.34 7zm0 10l2.57-1.81c.4.81.93 1.53 1.59 2.16L3.34 17zM12 22l-2.39-3.42c.74.27 1.55.42 2.39.42.84 0 1.65-.15 2.39-.42L12 22zm8.66-5l-4.16.35c.66-.63 1.19-1.35 1.59-2.16L20.66 17zm0-10l-2.57 1.81c-.4-.81-.93-1.53-1.59-2.16L20.66 7z"/></svg>`
              : html`<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M12 3a9 9 0 109 9c0-.46-.04-.92-.1-1.36a5.39 5.39 0 01-4.4 2.26 5.4 5.4 0 01-5.4-5.4c0-1.81.89-3.42 2.26-4.4-.44-.06-.9-.1-1.36-.1z"/></svg>`}
            <span class="flex-1">Modo escuro</span>
            <span class="w-9 h-5 rounded-full transition-colors relative shrink-0 ${dark ? 'bg-wa-teal' : 'bg-wa-border'}">
              <span class="absolute top-0.5 w-4 h-4 bg-white rounded-full shadow transition-all ${dark ? 'left-[18px]' : 'left-0.5'}"></span>
            </span>
          </button>

          ${hasPassword ? html`
            <div class="border-t border-wa-border my-1"></div>
            <button
              onClick=${() => { onLogout(); close(); }}
              class="w-full text-left px-4 py-2.5 text-[14px] hover:bg-red-50 transition-colors flex items-center gap-2 text-red-600"
            >
              <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z"/></svg>
              Sair
            </button>
          ` : null}
        </div>
      ` : null}
    </div>
  `;
}

function PageHeader({ title, onBack }) {
  return html`
    <div class="flex items-center gap-3 mb-4">
      <button
        onClick=${onBack}
        class="w-[36px] h-[36px] flex items-center justify-center rounded-full hover:bg-wa-hover transition-colors"
      >
        <svg viewBox="0 0 24 24" width="22" height="22" fill="#54656f">
          <path d="M12 4l1.4 1.4L7.8 11H20v2H7.8l5.6 5.6L12 20l-8-8 8-8z"/>
        </svg>
      </button>
      <h1 class="text-[20px] font-medium text-wa-text">${title}</h1>
    </div>
  `;
}

function App({ onLogout, hasPassword }) {
  const [status, setStatus] = useState({ connected: false, msg_count: 0, auto_reply_running: false });
  const [qrAvailable, setQrAvailable] = useState(false);
  const [qrVersion, setQrVersion] = useState(0);
  const [notification, setNotification] = useState('Iniciando...');
  const [wsConnected, setWsConnected] = useState(true);
  const [pluginScreens, setPluginScreens] = useState([]);
  const [tab, setTabState] = useState(() => tabFromPath([]));
  const [unreadConvos, setUnreadConvos] = useState(0);  // conversations with unread msgs (tab-title badge)
  const [newMessage, setNewMessage] = useState(null);
  const [chatPresence, setChatPresence] = useState(null);
  const [contactInfoUpdated, setContactInfoUpdated] = useState(null);
  const [tagsChanged, setTagsChanged] = useState(null);
  const [contactTagsUpdated, setContactTagsUpdated] = useState(null);
  const [contactAiToggled, setContactAiToggled] = useState(null);
  const [messagesRead, setMessagesRead] = useState(null);
  const [messageStatus, setMessageStatus] = useState(null);
  const [messageAction, setMessageAction] = useState(null);
  const [messageReaction, setMessageReaction] = useState(null);
  const [avatarUpdated, setAvatarUpdated] = useState(null);
  const [groupParticipantsChanged, setGroupParticipantsChanged] = useState(null);
  const [lowBalance, setLowBalance] = useState(null);
  const [gowaUpdate, setGowaUpdate] = useState(null);
  const [whatsNew, setWhatsNew] = useState(null);
  const [whatsbotUpdating, setWhatsbotUpdating] = useState(false);
  const [whatsbotUpdateError, setWhatsbotUpdateError] = useState('');
  const [gowaProgress, setGowaProgress] = useState(null);
  const [gowaInstalling, setGowaInstalling] = useState(false);
  const [initialContactId, setInitialContactId] = useState(contactIdFromPath);
  const [wizardManual, setWizardManual] = useState(() => window.location.pathname === '/wizard');
  const wizardLatchRef = useRef(false);

  // Open/close the setup wizard, keeping the /wizard URL in sync so it can be
  // reached directly (and bookmarked / shared).
  const openWizard = useCallback(() => {
    setWizardManual(true);
    if (window.location.pathname !== '/wizard') history.pushState(null, '', '/wizard');
  }, []);
  const closeWizard = useCallback(() => {
    wizardLatchRef.current = false;
    setWizardManual(false);
    if (window.location.pathname === '/wizard') {
      history.pushState(null, '', '/');
      window.dispatchEvent(new PopStateEvent('popstate'));
    }
  }, []);

  // Fetch the public plugin manifest once at boot. Errors are non-fatal —
  // the core app keeps running even if plugins fail to load.
  useEffect(() => {
    fetch('/api/plugins/manifest', { headers: authHeaders() })
      .then(r => r.json())
      .then(res => {
        if (!res || !res.ok) return;
        loadPluginExtensions(res.data.plugins);
        const screens = (res.data.plugins || []).flatMap(p =>
          (p.screens || [])
            .filter(s => !s.config)  // config screens live in the Plugins tab, not the gear menu
            .map(s => ({ ...s, pluginId: s.pluginId || p.id }))
        );
        setPluginScreens(screens);
        // Re-evaluate tab now that we know about plugin paths.
        setTabState(tabFromPath(screens));
      })
      .catch(() => { /* ignore */ });
  }, []);

  const setTab = useCallback((t) => {
    setTabState(t);
    const path = pathForTab(t, pluginScreens);
    if (window.location.pathname !== path) {
      history.pushState(null, '', path);
      // pushState doesn't fire popstate; notify listeners (e.g. Executions
      // syncs its detail view with the URL).
      window.dispatchEvent(new PopStateEvent('popstate'));
    }
  }, [pluginScreens]);

  useEffect(() => {
    function onPopState() {
      setTabState(tabFromPath(pluginScreens));
      setInitialContactId(contactIdFromPath());
      setWizardManual(window.location.pathname === '/wizard');
    }
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, [pluginScreens]);

  const { config, loading, saving, save } = useConfig();

  const configRef = useRef(config);
  useEffect(() => { configRef.current = config; }, [config]);

  // First run (no API key, setup not completed): reflect the wizard in the
  // URL so a hard reload / share lands back on /wizard.
  useEffect(() => {
    const firstRun = config && config.setup_completed !== true && !config.openrouter_api_key;
    if (firstRun && window.location.pathname !== '/wizard') {
      history.replaceState(null, '', '/wizard');
    }
  }, [config]);

  // Check for a newer stable release on page load and reconnect. The backend
  // caches GitHub responses and persists both "skip this version" and the
  // global notification preference for the whole installation.
  const checkWhatsBotUpdate = useCallback(() => {
    checkForUpdates()
      .then(res => {
        const d = res && res.ok && res.data;
        if (!d) return;
        if (!d.should_notify) {
          setWhatsNew(null);
          setWhatsbotUpdating(false);
          setWhatsbotUpdateError('');
          return;
        }
        setWhatsNew({
          currentVersion: d.current_version,
          version: d.latest_version,
          description: d.latest_description,
        });
      })
      .catch(() => { /* ignore */ });
  }, []);

  useEffect(() => { checkWhatsBotUpdate(); }, [checkWhatsBotUpdate]);

  async function handleWhatsBotUpdateNow() {
    setWhatsbotUpdating(true);
    setWhatsbotUpdateError('');
    try {
      const res = await performUpdate();
      if (!res || !res.ok) {
        setWhatsbotUpdateError((res && res.error) || 'Não foi possível atualizar o WhatsBot-Lite.');
        setWhatsbotUpdating(false);
        return;
      }
      setNotification(res.data?.message || 'Atualização instalada. Reiniciando o WhatsBot-Lite...');
      // Keep the modal locked while the server restarts. The WebSocket
      // reconnect runs checkWhatsBotUpdate again and closes it.
    } catch (_) {
      setWhatsbotUpdateError('Erro de conexão ao atualizar o WhatsBot-Lite.');
      setWhatsbotUpdating(false);
    }
  }

  async function handleWhatsBotLater() {
    const version = whatsNew && whatsNew.version;
    if (!version) return setWhatsNew(null);
    try {
      const res = await skipWhatsBotVersion(version);
      if (!res || !res.ok) {
        setWhatsbotUpdateError((res && res.error) || 'Não foi possível guardar esta escolha.');
        return;
      }
      setWhatsNew(null);
      setWhatsbotUpdateError('');
    } catch (_) {
      setWhatsbotUpdateError('Erro de conexão ao guardar esta escolha.');
    }
  }

  async function handleWhatsBotNever() {
    const result = await save({ whatsbot_update_notifications_enabled: false });
    if (!result || !result.ok) {
      setWhatsbotUpdateError((result && result.message) || 'Não foi possível desativar os avisos.');
      return;
    }
    setWhatsNew(null);
    setWhatsbotUpdateError('');
  }

  useWebSocket({
    onStatus: useCallback((data) => setStatus(data), []),
    onQrUpdate: useCallback((data) => {
      setQrAvailable(data.available);
      if (data.version) setQrVersion(data.version);
    }, []),
    onGowaStatus: useCallback((data) => setNotification(data.message), []),
    onConfigSaved: useCallback(() => setNotification('Configurações salvas!'), []),
    onNewMessage: useCallback((data) => setNewMessage(data), []),
    onChatPresence: useCallback((data) => setChatPresence(data), []),
    onContactInfoUpdated: useCallback((data) => setContactInfoUpdated(data), []),
    onTagsChanged: useCallback((data) => setTagsChanged(data), []),
    onContactTagsUpdated: useCallback((data) => setContactTagsUpdated(data), []),
    onHumanTransferAlert: useCallback(() => {
      const cfg = configRef.current;
      if (cfg && cfg.transfer_alert_enabled === false) return;
      const duration = cfg?.transfer_alert_duration || 5;
      playTransferAlert(duration);
    }, []),
    onContactAiToggled: useCallback((data) => setContactAiToggled(data), []),
    onMessagesRead: useCallback((data) => setMessagesRead(data), []),
    onMessageStatus: useCallback((data) => setMessageStatus(data), []),
    onMessageAction: useCallback((data) => setMessageAction(data), []),
    onMessageReaction: useCallback((data) => setMessageReaction(data), []),
    onAvatarUpdated: useCallback((data) => setAvatarUpdated(data), []),
    onGroupParticipantsChanged: useCallback((data) => setGroupParticipantsChanged({ ...data, _t: Date.now() }), []),
    onLowBalance: useCallback((data) => {
      if (lowBalanceIsSnoozed()) return;
      setLowBalance(data);
    }, []),
    onGowaUpdateAvailable: useCallback((data) => {
      if (gowaUpdateIsSnoozed()) return;
      if (!data || data.whatsapp_update_recommended !== true) return;
      setGowaUpdate(data);
    }, []),
    onGowaUpdateProgress: useCallback((data) => setGowaProgress(data), []),
    onGowaUpdateDone: useCallback((data) => {
      setGowaInstalling(false);
      setGowaProgress(null);
      setNotification(data && data.message ? data.message : '');
      if (data && data.ok) setGowaUpdate(null);
    }, []),
    onWsConnect: useCallback(() => { setWsConnected(true); checkWhatsBotUpdate(); }, [checkWhatsBotUpdate]),
    onWsDisconnect: useCallback(() => setWsConnected(false), []),
  });

  // One-shot balance check on boot — covers the case where the app opens while
  // already below the threshold but no LLM call has happened since the last
  // broadcast. Skipped when the user has snoozed the popup.
  useEffect(() => {
    if (!config || !config.openrouter_api_key) return;
    if (lowBalanceIsSnoozed()) return;
    fetch('/api/balance', { headers: authHeaders() })
      .then(r => r.json())
      .then(res => {
        if (res && res.ok && res.data && res.data.low_balance_enabled && res.data.below_threshold) {
          setLowBalance({
            remaining: res.data.remaining,
            total_credits: res.data.total_credits,
            total_usage: res.data.total_usage,
            threshold: res.data.threshold,
            account_url: res.data.account_url,
          });
        }
      })
      .catch(() => { /* ignore */ });
  }, [config && config.openrouter_api_key]);

  // One-shot GOWA update check on boot. The daily background task broadcasts
  // over WS, but a panel opened between two checks would otherwise never learn
  // about a release that is already out.
  useEffect(() => {
    if (gowaUpdateIsSnoozed()) return;
    fetch('/api/gowa/update/check', { headers: authHeaders() })
      .then(r => r.json())
      .then(res => {
        const d = res && res.ok && res.data;
        if (!d || !d.update_available || !d.latest_supported) return;
        if (d.whatsapp_update_recommended !== true) return;
        if (d.latest_version && d.latest_version === d.skipped_version) return;
        setGowaUpdate({
          latest_version: d.latest_version,
          installed_version: d.installed_version,
          supported: d.latest_supported,
          release_url: d.release_url,
          update_risk_level: d.update_risk_level,
          update_reason: d.update_reason,
          update_evidence: d.update_evidence,
          assessed_versions: d.assessed_versions,
        });
      })
      .catch(() => { /* ignore */ });
  }, []);

  const handleGowaUpdateNow = useCallback(async (forceUnsupported) => {
    if (!gowaUpdate) return;
    setGowaInstalling(true);
    setGowaProgress({ phase: 'downloading', progress: 0 });
    try {
      const res = await installGowaUpdate(gowaUpdate.latest_version, forceUnsupported);
      if (!res || !res.ok) {
        setNotification((res && res.error) || 'Falha ao atualizar o GOWA.');
      }
    } catch (e) {
      setNotification('Falha ao atualizar o GOWA.');
    } finally {
      // gowa_update_done normally clears these; this is the safety net for a
      // request that never got a broadcast (e.g. the connection dropped).
      setGowaInstalling(false);
      setGowaProgress(null);
    }
  }, [gowaUpdate]);

  const handleGowaSkip = useCallback(async (version) => {
    setGowaUpdate(null);
    try { await skipGowaVersion(version); } catch { /* ignore */ }
  }, []);

  // ── Browser-tab unread badge ("(3) WhatsBot-Lite"), like WhatsApp Web ──────────
  // Single source of truth is the backend count; we refresh it (debounced) on
  // boot, on WS events that change unread state, and when the contacts list
  // reports a change (e.g. the operator opened/read a chat — no WS event fires
  // for that on the same client).
  const unreadTimerRef = useRef(null);
  const refreshUnreadCount = useCallback(() => {
    if (unreadTimerRef.current) clearTimeout(unreadTimerRef.current);
    unreadTimerRef.current = setTimeout(async () => {
      try {
        const res = await getUnreadCount();
        if (res && res.ok) setUnreadConvos(res.data.count || 0);
      } catch (_) { /* ignore */ }
    }, 250);
  }, []);

  useEffect(() => { refreshUnreadCount(); }, [newMessage, messagesRead, refreshUnreadCount]);

  // Bumped when notification prefs change in the config panel, so the effects
  // below re-evaluate (e.g. turning the tab badge off should apply at once).
  const [notifVersion, setNotifVersion] = useState(0);
  useEffect(() => {
    const onPrefs = () => setNotifVersion(v => v + 1);
    window.addEventListener('whatsbot:notif-prefs', onPrefs);
    return () => window.removeEventListener('whatsbot:notif-prefs', onPrefs);
  }, []);

  // Tab-title badge — gated by the "tab notification" preference.
  useEffect(() => {
    const tabBadge = getNotifPref('tab');
    document.title = (tabBadge && unreadConvos > 0) ? `(${unreadConvos}) WhatsBot-Lite` : 'WhatsBot-Lite';
  }, [unreadConvos, notifVersion]);

  // Browser notification + sound on a new INBOUND message (from a contact).
  // Sound plays whenever enabled; the desktop notification only shows when the
  // tab isn't visible (you're away), like Telegram/WhatsApp Web.
  useEffect(() => {
    if (!newMessage) return;
    const m = newMessage.message;
    if (!m || m.role !== 'user') return;
    if (getNotifPref('sound')) playNotificationSound();
    const away = document.hidden || !document.hasFocus();
    if (getNotifPref('browser') && away) {
      let preview = (m.content || '').trim();
      if (!preview) {
        preview = m.media_type ? 'Enviou uma mídia' : 'Nova mensagem';
      }
      showBrowserNotification('WhatsBot-Lite — nova mensagem', preview.slice(0, 140));
    }
  }, [newMessage]);

  async function handleSave(data) {
    const result = await save(data);
    setNotification(result.message);
  }

  function handleNotify(msg) {
    setNotification(msg);
  }

  if (loading) {
    return html`
      <div class="h-screen flex items-center justify-center">
        <div class="text-center text-wa-secondary animate-pulse-slow">Carregando...</div>
      </div>
    `;
  }

  // First-run setup wizard — takes over the whole screen until completed.
  // Also reopenable on demand via the "Refazer configuração" button on /painel.
  // An install that already has an API key configured is NOT a first run —
  // never ambush an existing/configured user with the wizard after an update.
  const needsSetup = config
    && config.setup_completed !== true
    && !config.openrouter_api_key;
  // Once opened, the wizard stays mounted until the user finishes or closes
  // it — provisioning a key sets openrouter_api_key, which would otherwise
  // flip needsSetup to false mid-flow and unmount the wizard before step 3.
  if (needsSetup || wizardManual) wizardLatchRef.current = true;
  if (wizardLatchRef.current) {
    return html`<${SetupWizard}
      status=${status}
      qrAvailable=${qrAvailable}
      qrVersion=${qrVersion}
      config=${config}
      canClose=${!needsSetup}
      onClose=${closeWizard}
      onConfigSave=${save}
      onComplete=${async () => {
        await save({ setup_completed: true });
        closeWizard();
      }}
    />`;
  }

  // Resolve plugin screen for the current tab id, if any.
  const activePluginScreen = (tab && tab.startsWith('plugin:'))
    ? pluginScreens.find(s => pluginTabId(s) === tab)
    : null;

  return html`
    <div class="h-dvh overflow-hidden flex flex-col relative">
      <${GearMenu} tab=${tab} onTabChange=${setTab} pluginScreens=${pluginScreens} hasPassword=${hasPassword} onLogout=${onLogout} accountUrl=${config && config.account_url} />

      <main class="flex-1 min-h-0 ${tab === 'chat' ? 'overflow-hidden' : 'overflow-auto'} ${tab !== 'contacts' ? 'bg-wa-panel' : ''}">
        ${activePluginScreen
          ? html`<div class="w-full min-w-0 p-4">
              <${PageHeader} title=${activePluginScreen.title} onBack=${() => setTab('contacts')} />
              <${PluginScreen} screen=${activePluginScreen} />
            </div>`
          : tab === 'tools'
            ? html`<div class="max-w-5xl mx-auto p-4">
                <${PageHeader} title="Tools" onBack=${() => setTab('contacts')} />
                <${ToolsManager} />
              </div>`
            : tab === 'chat'
              ? html`<div class="h-full pt-12 md:p-3 md:pt-3"><${Chat} /></div>`
            : tab === 'plugins'
            ? html`<div class="max-w-5xl mx-auto p-4">
                <${PageHeader} title="Plugins" onBack=${() => setTab('contacts')} />
                <${PluginsManager} onPluginsChanged=${() => {
                  fetch('/api/plugins/manifest', { headers: authHeaders() }).then(r => r.json()).then(res => {
                    if (res && res.ok) {
                      const sc = (res.data.plugins || []).flatMap(p =>
                        (p.screens || [])
                          .filter(s => !s.config)
                          .map(s => ({ ...s, pluginId: s.pluginId || p.id }))
                      );
                      setPluginScreens(sc);
                    }
                  });
                }} />
              </div>`
            : tab === 'dashboard'
              ? html`<div class="max-w-5xl mx-auto p-4">
                  <${PageHeader} title="Painel" onBack=${() => setTab('contacts')} />
                  <${Dashboard}
                    status=${status}
                    qrAvailable=${qrAvailable}
                    qrVersion=${qrVersion}
                    config=${config}
                    saving=${saving}
                    onSave=${handleSave}
                    onNotify=${handleNotify}
                    onReopenSetup=${openWizard}
                  />
                </div>`
              : tab === 'contacts'
                ? html`<${Contacts} newMessage=${newMessage} chatPresence=${chatPresence} contactInfoUpdated=${contactInfoUpdated} tagsChanged=${tagsChanged} contactTagsUpdated=${contactTagsUpdated} contactAiToggled=${contactAiToggled} messagesRead=${messagesRead} messageStatus=${messageStatus} messageAction=${messageAction} messageReaction=${messageReaction} avatarUpdated=${avatarUpdated} groupParticipantsChanged=${groupParticipantsChanged} initialContactId=${initialContactId} wsConnected=${wsConnected} config=${config} onConfigSave=${save} onUnreadChange=${refreshUnreadCount} />`
                : tab === 'costs'
                  ? html`<div class="max-w-5xl mx-auto p-4">
                      <${PageHeader} title="Custos de IA" onBack=${() => setTab('contacts')} />
                      <${CostsDashboard} />
                    </div>`
                  : tab === 'executions'
                    ? html`<div class="max-w-5xl mx-auto p-4 h-full">
                        <${PageHeader} title="Execuções" onBack=${() => {
                          if (window.location.pathname.match(/^\/executions\/\d+$/)) {
                            history.pushState(null, '', '/executions');
                            window.dispatchEvent(new PopStateEvent('popstate'));
                          } else {
                            setTab('contacts');
                          }
                        }} />
                        <${Executions} />
                      </div>`
                    : html`<${Sandbox} newMessage=${newMessage} />`
        }
      </main>

      ${lowBalance ? html`<${LowBalanceModal}
        balance=${lowBalance.remaining}
        threshold=${lowBalance.threshold}
        accountUrl=${lowBalance.account_url || (config && config.account_url)}
        onClose=${() => setLowBalance(null)}
        onSnooze=${(ms) => snoozeLowBalance(ms)}
      />` : null}

      ${gowaUpdate ? html`<${GowaUpdateModal}
        latestVersion=${gowaUpdate.latest_version}
        installedVersion=${gowaUpdate.installed_version}
        supported=${gowaUpdate.supported !== false}
        releaseUrl=${gowaUpdate.release_url}
        riskLevel=${gowaUpdate.update_risk_level}
        reason=${gowaUpdate.update_reason}
        evidence=${gowaUpdate.update_evidence}
        installing=${gowaInstalling}
        progress=${gowaProgress}
        onUpdateNow=${handleGowaUpdateNow}
        onLater=${() => { snoozeGowaUpdate(24 * 60 * 60 * 1000); setGowaUpdate(null); }}
        onSkip=${handleGowaSkip}
      />` : null}

      ${whatsNew ? html`<${WhatsNewModal}
        currentVersion=${whatsNew.currentVersion}
        version=${whatsNew.version}
        description=${whatsNew.description}
        updating=${whatsbotUpdating}
        error=${whatsbotUpdateError}
        onUpdate=${handleWhatsBotUpdateNow}
        onLater=${handleWhatsBotLater}
        onNever=${handleWhatsBotNever}
      />` : null}
    </div>
  `;
}

function AuthGate() {
  const [authState, setAuthState] = useState('checking'); // 'checking' | 'login' | 'ready'
  const [hasPassword, setHasPassword] = useState(false);

  useEffect(() => {
    checkAuth().then(res => {
      if (res.ok) {
        setHasPassword(res.data.has_password);
        setAuthState('ready');
      } else {
        setHasPassword(true);
        setAuthState('login');
      }
    }).catch(() => {
      setAuthState('ready');
    });
  }, []);

  useEffect(() => {
    function onUnauthorized() {
      setHasPassword(true);
      setAuthState('login');
    }
    window.addEventListener('whatsbot:unauthorized', onUnauthorized);
    return () => window.removeEventListener('whatsbot:unauthorized', onUnauthorized);
  }, []);

  function handleLogin() {
    setAuthState('ready');
    setHasPassword(true);
  }

  function handleLogout() {
    localStorage.removeItem('whatsbot_token');
    setAuthState('login');
  }

  if (authState === 'checking') {
    return html`
      <div class="h-screen flex items-center justify-center">
        <div class="text-center text-wa-secondary animate-pulse-slow">Carregando...</div>
      </div>
    `;
  }

  if (authState === 'login') {
    return html`<${LoginScreen} onLogin=${handleLogin} />`;
  }

  return html`<${App} onLogout=${handleLogout} hasPassword=${hasPassword} />`;
}

render(html`<${AuthGate} />`, document.getElementById('app'));
