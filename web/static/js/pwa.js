// Optional enhancement: installation must never block authentication or the app.
let installEvent = null;
let installed = false;
let registrationFailed = false;
// Chrome on Android can tell whether this origin's PWA is installed on the device
// (getInstalledRelatedApps). Everywhere else the answer stays "unknown" = false.
let installedElsewhere = false;
const listeners = new Set();
const displayMode = window.matchMedia('(display-mode: standalone)');
const notify = () => listeners.forEach(listener => listener(getInstallState()));

const PROMPT_SHOWN_KEY = 'whatsbot_install_prompt_shown';       // sessionStorage: once per session
const PROMPT_DISMISSED_KEY = 'whatsbot_install_prompt_dismissed'; // localStorage: "Agora não" timestamp
export const PROMPT_SNOOZE_MS = 7 * 24 * 60 * 60 * 1000;

function isIos() {
  return /iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}

/** Phone/tablet (touch) vs desktop. Install is only offered on the former. */
export function isMobileDevice() {
  if (isIos()) return true;
  const uaData = navigator.userAgentData;
  if (uaData && typeof uaData.mobile === 'boolean') return uaData.mobile;
  return /Android|Mobile|Tablet/i.test(navigator.userAgent || '');
}

export function getInstallState() {
  const standalone = displayMode.matches || navigator.standalone === true;
  return {
    installed: installed || standalone,
    standalone,
    installedElsewhere,
    mobile: isMobileDevice(),
    canPrompt: !!installEvent,
    secure: window.isSecureContext,
    registrationFailed,
    ios: isIos(),
  };
}

/** Whether the "install / open the app" pop-up should appear now. Never throws. */
export function installPromptDue(state = getInstallState(), now = Date.now()) {
  if (!state.mobile || state.standalone || !state.secure) return false;
  try {
    if (sessionStorage.getItem(PROMPT_SHOWN_KEY)) return false;
    const dismissedAt = Number(localStorage.getItem(PROMPT_DISMISSED_KEY));
    if (dismissedAt && now - dismissedAt < PROMPT_SNOOZE_MS) return false;
  } catch { /* storage blocked: show once per page load rather than never */ }
  return true;
}

export function markInstallPromptShown() {
  try { sessionStorage.setItem(PROMPT_SHOWN_KEY, '1'); } catch { /* ignore */ }
}

export function dismissInstallPrompt(now = Date.now()) {
  markInstallPromptShown();
  try { localStorage.setItem(PROMPT_DISMISSED_KEY, String(now)); } catch { /* ignore */ }
}

async function detectInstalledApp() {
  if (typeof navigator.getInstalledRelatedApps !== 'function') return;
  try {
    const apps = await navigator.getInstalledRelatedApps();
    const found = Array.isArray(apps) && apps.length > 0;
    if (found !== installedElsewhere) {
      installedElsewhere = found;
      notify();
    }
  } catch { /* unsupported or blocked: stay "unknown" */ }
}

export function subscribeInstall(listener) {
  listeners.add(listener);
  listener(getInstallState());
  return () => listeners.delete(listener);
}

window.addEventListener('beforeinstallprompt', event => {
  event.preventDefault();
  installEvent = event;
  notify();
});
window.addEventListener('appinstalled', () => {
  installed = true;
  installEvent = null;
  notify();
});
displayMode.addEventListener?.('change', notify);

export async function promptInstall() {
  const event = installEvent;
  if (!event) return 'unavailable';
  // Consume before awaiting: repeated clicks cannot open the same prompt twice.
  installEvent = null;
  notify();
  try {
    await event.prompt();
    const choice = await event.userChoice;
    return choice.outcome;
  } catch {
    return 'unavailable';
  }
}

async function register() {
  if (!window.isSecureContext || !('serviceWorker' in navigator)) return;
  try {
    // Root scope also covers existing deep links and plugin screens.
    await navigator.serviceWorker.register('/sw.js', { scope: '/', updateViaCache: 'none' });
  } catch {
    registrationFailed = true;
    notify();
  }
  detectInstalledApp();
}
if (document.readyState === 'complete') register();
else window.addEventListener('load', register, { once: true });
