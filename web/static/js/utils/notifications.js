/**
 * Client-side notification preferences + helpers.
 *
 * These are per-device settings (the browser-notification permission itself is
 * per-browser), so they live in localStorage rather than the server config.
 * Changing one dispatches a `whatsbot:notif-prefs` window event so listeners
 * (e.g. the tab-title badge in app.js) can re-apply immediately.
 */

const KEYS = {
  tab: 'whatsbot_notif_tab',         // browser-tab unread badge "(N) WhatsBot-Lite"
  browser: 'whatsbot_notif_browser', // desktop/browser notifications
  sound: 'whatsbot_notif_sound',     // play a sound on new message
};

// Tab badge defaults ON (matches prior behavior); browser + sound default OFF
// since browser notifications need an explicit permission grant.
const DEFAULTS = { tab: true, browser: false, sound: false };

export function getNotifPref(key) {
  const v = localStorage.getItem(KEYS[key]);
  if (v === null) return DEFAULTS[key];
  return v === '1';
}

export function setNotifPref(key, value) {
  localStorage.setItem(KEYS[key], value ? '1' : '0');
  try { window.dispatchEvent(new Event('whatsbot:notif-prefs')); } catch (_) { /* ignore */ }
}

export function browserNotifSupported() {
  return typeof Notification !== 'undefined';
}

export function browserNotifPermission() {
  return browserNotifSupported() ? Notification.permission : 'unsupported';
}

export async function requestBrowserPermission() {
  if (!browserNotifSupported()) return 'unsupported';
  try {
    return await Notification.requestPermission();
  } catch (_) {
    return 'denied';
  }
}

export function showBrowserNotification(title, body) {
  if (!browserNotifSupported() || Notification.permission !== 'granted') return;
  const options = { body, tag: 'whatsbot-message', renotify: true, icon: '/static/icons/icon-192.png' };
  // Android (PWA instalado) rejeita `new Notification()`; precisa do service worker.
  const sw = typeof navigator !== 'undefined' ? navigator.serviceWorker : null;
  if (sw && sw.getRegistration) {
    sw.getRegistration('/').then((reg) => {
      if (reg && reg.active && reg.showNotification) return reg.showNotification(title, options);
      return _showPageNotification(title, options);
    }).catch(() => _showPageNotification(title, options));
    return;
  }
  _showPageNotification(title, options);
}

function _showPageNotification(title, options) {
  try {
    const n = new Notification(title, options);
    n.onclick = () => { try { window.focus(); } catch (_) {} n.close(); };
  } catch (_) { /* ignore */ }
}

// localStorage key for a custom notification sound (a data: URL). Plugins (e.g.
// the "Sons de Notificação" plugin) write here to override the default ding; the
// core reads it without knowing about any plugin. Empty/absent → default ding.
export const CUSTOM_SOUND_KEY = 'whatsbot_notif_sound_custom';
// Notification volume, 0..1 (default 1). Per-device, set by the custom-sounds plugin.
export const VOLUME_KEY = 'whatsbot_notif_volume';

export function getNotifVolume() {
  let v = 1;
  try {
    const raw = localStorage.getItem(VOLUME_KEY);
    if (raw !== null) v = parseFloat(raw);
  } catch (_) { /* ignore */ }
  if (!isFinite(v)) v = 1;
  return Math.max(0, Math.min(1, v));
}

let _customAudio = null;
let _customAudioSrc = '';

function _playCustom(src) {
  if (!_customAudio || _customAudioSrc !== src) {
    _customAudio = new Audio(src);
    _customAudioSrc = src;
  }
  try {
    _customAudio.currentTime = 0;
    _customAudio.volume = getNotifVolume();
  } catch (_) { /* ignore */ }
  return _customAudio.play();  // returns a promise
}

// Short, pleasant two-note "ding" via the Web Audio API (no asset needed). The
// context is created lazily and resumed on demand (autoplay policies allow it
// once the user has interacted with the page).
let _audioCtx = null;
function _playDefaultDing() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    if (!_audioCtx) _audioCtx = new Ctx();
    if (_audioCtx.state === 'suspended') _audioCtx.resume();
    const now = _audioCtx.currentTime;
    const vol = Math.max(0.0001, getNotifVolume());
    [[784, 0], [1047, 0.11]].forEach(([freq, offset]) => {  // G5 → C6
      const osc = _audioCtx.createOscillator();
      const gain = _audioCtx.createGain();
      osc.type = 'sine';
      osc.frequency.value = freq;
      const start = now + offset;
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.25 * vol, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.3);
      osc.connect(gain);
      gain.connect(_audioCtx.destination);
      osc.start(start);
      osc.stop(start + 0.32);
    });
  } catch (_) { /* ignore */ }
}

export function playNotificationSound() {
  let custom = '';
  try { custom = localStorage.getItem(CUSTOM_SOUND_KEY) || ''; } catch (_) { /* ignore */ }
  if (custom) {
    // Fall back to the built-in ding if the custom clip fails to play.
    Promise.resolve(_playCustom(custom)).catch(() => _playDefaultDing());
    return;
  }
  _playDefaultDing();
}
