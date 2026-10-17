// Optional enhancement: installation must never block authentication or the app.
let installEvent = null;
let installed = false;
let registrationFailed = false;
const listeners = new Set();
const displayMode = window.matchMedia('(display-mode: standalone)');
const notify = () => listeners.forEach(listener => listener(getInstallState()));

export function getInstallState() {
  return {
    installed: installed || displayMode.matches || navigator.standalone === true,
    standalone: displayMode.matches || navigator.standalone === true,
    canPrompt: !!installEvent,
    secure: window.isSecureContext,
    registrationFailed,
    ios: /iPad|iPhone|iPod/.test(navigator.userAgent)
      || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1),
  };
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
}
if (document.readyState === 'complete') register();
else window.addEventListener('load', register, { once: true });
