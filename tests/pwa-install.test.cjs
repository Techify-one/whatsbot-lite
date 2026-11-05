/* Isolated install-event lifecycle tests; no browser or package install required.
 * Run: node --test tests/pwa-install.test.cjs
 */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/static/js/pwa.js'), 'utf8');
let sequence = 0;
async function setup({secure = true, serviceWorker = true, readyState = 'complete', ua = 'Android', platform = 'Linux', touch = 1, standalone = false, registerError = false, installedApps, userAgentData, storage = true} = {}) {
  const handlers = {};
  const registrations = [];
  const display = {matches: standalone, addEventListener(name, listener) {this[name] = listener;}};
  const window = {isSecureContext: secure, matchMedia: () => display,
    addEventListener(name, listener) {handlers[name] = listener;}};
  const navigator = {userAgent: ua, platform, maxTouchPoints: touch};
  if (userAgentData !== undefined) navigator.userAgentData = userAgentData;
  if (installedApps !== undefined) navigator.getInstalledRelatedApps = typeof installedApps === 'function' ? installedApps : async () => installedApps;
  const makeStore = () => { const m = new Map(); return {getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v))}; };
  if (storage) { Object.defineProperty(globalThis, 'sessionStorage', {value: makeStore(), configurable: true}); Object.defineProperty(globalThis, 'localStorage', {value: makeStore(), configurable: true}); }
  else { for (const k of ['sessionStorage', 'localStorage']) Object.defineProperty(globalThis, k, {get() { throw new Error('blocked'); }, configurable: true}); }
  if (serviceWorker) navigator.serviceWorker = {register: async (...args) => {
    registrations.push(args);
    if (registerError) throw new Error('unavailable');
    return {};
  }};
  Object.defineProperty(globalThis, 'window', {value: window, configurable: true});
  Object.defineProperty(globalThis, 'navigator', {value: navigator, configurable: true});
  Object.defineProperty(globalThis, 'document', {value: {readyState}, configurable: true});
  const module = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}#test-${sequence++}`);
  await Promise.resolve();
  return {module, handlers, registrations, display, navigator};
}
function promptEvent(outcome = 'dismissed') {
  return {prevented: 0, prompted: 0,
    preventDefault() {this.prevented++;},
    async prompt() {this.prompted++;},
    userChoice: Promise.resolve({outcome})};
}

test('secure registration uses root scope and bypasses worker HTTP cache', async () => {
  const {registrations} = await setup();
  assert.deepEqual(registrations, [['/sw.js', {scope: '/', updateViaCache: 'none'}]]);
});

test('insecure contexts and browsers without service workers do not register', async () => {
  for (const options of [{secure: false}, {serviceWorker: false}]) {
    const {registrations, module} = await setup(options);
    assert.deepEqual(registrations, []);
    assert.equal(module.getInstallState().registrationFailed, false);
  }
});

test('registration waits for load and reports failure without rejecting module', async () => {
  const {registrations, handlers, module} = await setup({readyState: 'loading', registerError: true});
  assert.deepEqual(registrations, []);
  await handlers.load();
  assert.equal(module.getInstallState().registrationFailed, true);
});

test('prompt is consumed once; cancellation permits a later new browser event', async () => {
  const {module, handlers} = await setup();
  assert.equal(await module.promptInstall(), 'unavailable');
  const event = promptEvent();
  handlers.beforeinstallprompt(event);
  assert.equal(event.prevented, 1);
  assert.equal(module.getInstallState().canPrompt, true);
  const first = module.promptInstall();
  assert.equal(module.getInstallState().canPrompt, false);
  assert.equal(await module.promptInstall(), 'unavailable');
  assert.equal(await first, 'dismissed');
  assert.equal(event.prompted, 1);
  assert.equal(module.getInstallState().installed, false);
  const next = promptEvent('accepted');
  handlers.beforeinstallprompt(next);
  assert.equal(await module.promptInstall(), 'accepted');
  assert.equal(next.prompted, 1);
  // Accepted prompt is not proof installation completed.
  assert.equal(module.getInstallState().installed, false);
});

test('failed prompt is consumed and resolves unavailable', async () => {
  const {module, handlers} = await setup();
  const event = promptEvent();
  event.prompt = async () => {throw new Error('prompt failed');};
  handlers.beforeinstallprompt(event);
  assert.equal(await module.promptInstall(), 'unavailable');
  assert.equal(module.getInstallState().canPrompt, false);
});

test('installation clears pending prompt; subscribers receive changes and unsubscribe', async () => {
  const {module, handlers, display} = await setup();
  const changes = [];
  const unsubscribe = module.subscribeInstall(state => changes.push(state));
  handlers.beforeinstallprompt(promptEvent());
  handlers.appinstalled();
  assert.equal(changes.length, 3);
  assert.equal(changes[2].installed, true);
  assert.equal(changes[2].canPrompt, false);
  unsubscribe();
  display.change();
  assert.equal(changes.length, 3);
});

test('standalone and iPad desktop-mode detection reflect the actual context', async () => {
  const {module, display, navigator} = await setup({ua: 'Macintosh', platform: 'MacIntel', touch: 5});
  assert.equal(module.getInstallState().ios, true);
  assert.equal(module.getInstallState().installed, false);
  navigator.standalone = true;
  assert.equal(module.getInstallState().installed, true);
  navigator.standalone = false;
  display.matches = true;
  assert.equal(module.getInstallState().installed, true);
});

test('mobile detection: userAgentData wins, UA is the fallback, iOS is always mobile', async () => {
  assert.equal((await setup({ua: 'Android', userAgentData: {mobile: true}})).module.isMobileDevice(), true);
  assert.equal((await setup({ua: 'X11; Linux x86_64', platform: 'Linux', touch: 0, userAgentData: {mobile: false}})).module.isMobileDevice(), false);
  assert.equal((await setup({ua: 'Mozilla/5.0 (Linux; Android 14) Mobile', userAgentData: undefined})).module.isMobileDevice(), true);
  assert.equal((await setup({ua: 'Windows NT 10.0', platform: 'Win32', touch: 0})).module.isMobileDevice(), false);
  assert.equal((await setup({ua: 'iPhone', platform: 'iPhone', touch: 5, userAgentData: {mobile: false}})).module.isMobileDevice(), true);
});

test('install pop-up is never due on desktop or inside the installed app', async () => {
  const desktop = await setup({ua: 'Windows NT 10.0', platform: 'Win32', touch: 0});
  assert.equal(desktop.module.installPromptDue(), false);
  const standalone = await setup({standalone: true});
  assert.equal(standalone.module.installPromptDue(), false);
  const insecure = await setup({secure: false});
  assert.equal(insecure.module.installPromptDue(), false);
});

test('install pop-up: once per session, and "Agora não" snoozes for 7 days', async () => {
  const {module} = await setup();
  const now = 1_700_000_000_000;
  assert.equal(module.installPromptDue(undefined, now), true);
  module.markInstallPromptShown();
  assert.equal(module.installPromptDue(undefined, now), false, 'already shown this session');
  const second = await setup();
  second.module.dismissInstallPrompt(now);
  const fresh = await setup();
  // new session (sessionStorage reset) but localStorage dismissal persists within the same store
  globalThis.localStorage.setItem('whatsbot_install_prompt_dismissed', String(now));
  assert.equal(fresh.module.installPromptDue(undefined, now + 6 * 24 * 3600 * 1000), false, 'still snoozed on day 6');
  assert.equal(fresh.module.installPromptDue(undefined, now + 8 * 24 * 3600 * 1000), true, 'due again after 7 days');
});

test('install pop-up still works (shown once per load) when storage is blocked', async () => {
  const {module} = await setup({storage: false});
  assert.equal(module.installPromptDue(), true);
  assert.doesNotThrow(() => module.markInstallPromptShown());
  assert.doesNotThrow(() => module.dismissInstallPrompt());
});

test('getInstalledRelatedApps: found, empty, throwing and absent are all handled', async () => {
  const found = await setup({installedApps: [{platform: 'webapp'}]});
  await new Promise(r => setImmediate(r));
  assert.equal(found.module.getInstallState().installedElsewhere, true);
  const empty = await setup({installedApps: []});
  await new Promise(r => setImmediate(r));
  assert.equal(empty.module.getInstallState().installedElsewhere, false);
  const boom = await setup({installedApps: () => Promise.reject(new Error('blocked'))});
  await new Promise(r => setImmediate(r));
  assert.equal(boom.module.getInstallState().installedElsewhere, false);
  const absent = await setup();
  assert.equal(absent.module.getInstallState().installedElsewhere, false);
});
