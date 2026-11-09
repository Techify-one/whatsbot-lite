/* Back-button model of the installed app. Run: node --test tests/navigation.test.cjs */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL} = require('node:url');

const modulePath = pathToFileURL(
    path.join(__dirname, '../web/static/js/utils/navigation.js')).href;
const load = () => import(modulePath);

// Minimal browser: a real history stack, popstate on traversal, display-mode switch.
function fakeWindow(startUrl, {standalone}) {
    const listeners = [];
    const entries = [{url: startUrl, state: null}];
    let idx = 0;
    const parse = (u) => {
        const m = u.match(/^([^?#]*)(\?[^#]*)?(#.*)?$/);
        return {pathname: m[1], search: m[2] || '', hash: m[3] || ''};
    };
    const win = {
        navigator: {},
        matchMedia: () => ({matches: standalone}),
        PopStateEvent: class { constructor(type) { this.type = type; } },
        get location() { return parse(entries[idx].url); },
        history: {
            get state() { return entries[idx].state; },
            pushState(state, _t, url) { entries.splice(idx + 1); entries.push({url, state}); idx++; },
            replaceState(state, _t, url) { entries[idx] = {url, state}; },
            go(n) {
                const to = Math.max(0, Math.min(entries.length - 1, idx + n));
                if (to === idx) return;
                idx = to;
                win.dispatchEvent({type: 'popstate', state: entries[idx].state});
            },
            back() { this.go(-1); },
        },
        addEventListener(t, fn) { listeners.push(fn); },
        dispatchEvent(ev) { listeners.forEach(fn => fn(ev)); return true; },
        _stack: () => entries.map(e => e.url),
        _idx: () => idx,
    };
    return win;
}

const mk = async (url, standalone = true) => {
    const {createNavigation} = await load();
    const win = fakeWindow(url, {standalone});
    return {win, nav: createNavigation(win)};
};

test('web (not installed): helpers are inert and push keeps the old behaviour', async () => {
    const {win, nav} = await mk('/', false);
    assert.equal(nav.isStandalone(), false);
    assert.equal(nav.installRootGuard(), false);
    assert.equal(nav.goToRoot(), false);
    assert.equal(nav.goBack(), false);
    assert.equal(nav.tabNavigate('/painel'), false);
    assert.equal(nav.openContactEntry('/contacts/5'), false);
    assert.equal(nav.pushOverlay('info'), false);
    nav.push('/painel');
    assert.deepEqual(win._stack(), ['/', '/painel']);
    assert.equal(win.history.state, null);          // old code pushed null state
    nav.swap('/painel?aba=x');                      // outside the app swap == push
    assert.deepEqual(win._stack(), ['/', '/painel', '/painel?aba=x']);
});

test('root guard: cold start on a deep link puts "/" underneath', async () => {
    const {win, nav} = await mk('/contacts/7?msg=3');
    assert.equal(nav.installRootGuard(), true);
    assert.deepEqual(win._stack(), ['/', '/contacts/7?msg=3']);
    assert.equal(win._idx(), 1);
    win.history.back();                              // system back -> contact list, not exit
    assert.equal(win.location.pathname, '/');
});

test('root guard is idempotent (reload of an entry we created)', async () => {
    const {win, nav} = await mk('/painel');
    nav.installRootGuard();
    assert.equal(nav.installRootGuard(), false);
    assert.deepEqual(win._stack(), ['/', '/painel']);
});

test('opening a conversation then the app arrow = one step back to the list', async () => {
    const {win, nav} = await mk('/');
    nav.installRootGuard();
    nav.openContactEntry('/contacts/5');
    assert.deepEqual(win._stack(), ['/', '/contacts/5']);
    nav.goBack('/');                                 // the "←" in the conversation header
    assert.equal(win.location.pathname, '/');
    assert.equal(win._idx(), 0);
    assert.equal(win._stack().length, 2);            // nothing new was pushed (old bug pushed "/")
});

test('switching top-level screens never builds a chain', async () => {
    const {win, nav} = await mk('/');
    nav.installRootGuard();
    nav.tabNavigate('/painel');
    nav.tabNavigate('/plugins');
    nav.tabNavigate('/arquivos');
    assert.deepEqual(win._stack(), ['/', '/arquivos']);
    win.history.back();                              // phone back
    assert.equal(win.location.pathname, '/');
});

test('screen arrow from a top-level screen behaves like the system back', async () => {
    const {win, nav} = await mk('/');
    nav.installRootGuard();
    nav.tabNavigate('/painel');
    nav.goBack('/');                                 // PageHeader arrow
    assert.equal(win.location.pathname, '/');
    assert.equal(win._idx(), 0);
});

test('conversation opened from another screen: arrow goes to that screen, back again to the list', async () => {
    const {win, nav} = await mk('/');
    nav.installRootGuard();
    nav.tabNavigate('/arquivos');
    nav.push('/contacts/9?msg=1');                   // FileManager.openConversation
    nav.goBack('/');
    assert.equal(win.location.pathname, '/arquivos');
    nav.goBack('/');
    assert.equal(win.location.pathname, '/');
});

test('goToRoot jumps straight to the list from any depth', async () => {
    const {win, nav} = await mk('/');
    nav.installRootGuard();
    nav.tabNavigate('/arquivos');
    nav.push('/contacts/9');
    assert.equal(nav.goToRoot(), true);
    assert.equal(win.location.pathname, '/');
    assert.equal(win._idx(), 0);
});

test('goBack at depth 0 on a non-root URL rewrites instead of leaving the app', async () => {
    const {win, nav} = await mk('/executions');      // no guard installed
    assert.equal(nav.goBack('/'), true);
    assert.equal(win.location.pathname, '/');
    assert.equal(win._stack().length, 1);
});

test('swap inside a screen replaces (config tabs / execution detail)', async () => {
    const {win, nav} = await mk('/');
    nav.installRootGuard();
    nav.tabNavigate('/painel');
    nav.swap('/painel?aba=sistema');
    nav.swap('/painel?aba=agente');
    assert.deepEqual(win._stack(), ['/', '/painel?aba=agente']);
    win.history.back();
    assert.equal(win.location.pathname, '/');
});

test('swap from the list pushes (so back returns to the list)', async () => {
    const {win, nav} = await mk('/');
    nav.installRootGuard();
    nav.swap('/chat');
    assert.deepEqual(win._stack(), ['/', '/chat']);
});

test('overlay: system back closes only the overlay', async () => {
    const {win, nav} = await mk('/');
    nav.installRootGuard();
    nav.openContactEntry('/contacts/5');
    assert.equal(nav.pushOverlay('info'), true);
    assert.equal(nav.hasOverlay('info'), true);
    assert.equal(nav.pushOverlay('info'), false);    // no double entry
    win.history.back();                              // phone back
    assert.equal(nav.hasOverlay('info'), false);
    assert.equal(win.location.pathname, '/contacts/5');   // still in the conversation
});

test('overlay: closing from the panel button goes through history', async () => {
    const {win, nav} = await mk('/');
    nav.installRootGuard();
    nav.openContactEntry('/contacts/5');
    nav.pushOverlay('info');
    assert.equal(nav.popOverlay('info'), true);
    assert.equal(nav.hasOverlay('info'), false);
    assert.equal(win.location.pathname, '/contacts/5');
    assert.equal(nav.popOverlay('info'), false);     // nothing to pop
});

test('opening another conversation while the overlay is up replaces its entry', async () => {
    const {win, nav} = await mk('/');
    nav.installRootGuard();
    nav.openContactEntry('/contacts/5');
    nav.pushOverlay('info');
    nav.openContactEntry('/contacts/6');             // not on "/" -> replace
    assert.equal(nav.hasOverlay('info'), false);
    nav.goBack('/');
    assert.equal(win.location.pathname, '/contacts/5');
    nav.goBack('/');
    assert.equal(win.location.pathname, '/');
});

test('keepState rewrites the URL but preserves depth (filters/search in the list)', async () => {
    const {win, nav} = await mk('/');
    nav.installRootGuard();
    nav.openContactEntry('/contacts/5');
    nav.keepState('/contacts/5?panel=info');
    assert.equal(nav.depth(), 1);
    nav.goBack('/');
    assert.equal(win.location.pathname, '/');
});

test('keepState on the web is a plain replaceState (state stays null)', async () => {
    const {win, nav} = await mk('/', false);
    nav.keepState('/?q=abc');
    assert.equal(win.history.state, null);
    assert.deepEqual(win._stack(), ['/?q=abc']);
});

test('toList: installed app jumps down the stack, web pushes "/"', async () => {
    const a = await mk('/');
    a.nav.installRootGuard();
    a.nav.openContactEntry('/contacts/5');
    assert.equal(a.nav.toList(), true);
    assert.equal(a.win.location.pathname, '/');
    assert.equal(a.win._stack().length, 2);          // nothing pushed

    const b = await mk('/contacts/5', false);
    assert.equal(b.nav.toList(), false);
    assert.deepEqual(b.win._stack(), ['/contacts/5', '/']);   // old behaviour
});

test('rootSilently: no popstate in the installed app', async () => {
    const {win, nav} = await mk('/');
    nav.installRootGuard();
    nav.openContactEntry('/contacts/5');
    let fired = 0;
    win.addEventListener('popstate', () => fired++);
    nav.rootSilently();
    assert.equal(win.location.pathname, '/');
    assert.equal(fired, 0);
});

test('installed app: bare pushState/replaceState elsewhere in the code keep the depth', async () => {
    const {win, nav} = await mk('/');
    nav.installRootGuard();
    win.history.pushState(null, '', '/executions/4');     // legacy call site, no state
    assert.equal(nav.depth(), 1);
    win.history.replaceState(null, '', '/executions/4?step=2');   // URL-only rewrite
    assert.equal(nav.depth(), 1);                          // depth survived
    win.history.pushState(null, '', '/executions/5');
    assert.equal(nav.depth(), 2);
    nav.goToRoot();                                        // one jump to the list
    assert.equal(win.location.pathname, '/');
    assert.equal(win._idx(), 0);
});

test('installed app: an explicit state is passed through untouched', async () => {
    const {win, nav} = await mk('/');
    nav.installRootGuard();
    win.history.pushState({custom: 1}, '', '/x');
    assert.deepEqual(win.history.state, {custom: 1});
});

test('web: history is never patched', async () => {
    const {win, nav} = await mk('/', false);
    const before = win.history.pushState;
    nav.installRootGuard();
    assert.equal(win.history.pushState, before);
    assert.equal(win.history.__wbTracked, undefined);
});

test('trackHistory is idempotent', async () => {
    const {win, nav} = await mk('/');
    nav.installRootGuard();
    const patched = win.history.pushState;
    nav.installRootGuard();
    assert.equal(win.history.pushState, patched);
});

test('backedToRoot: real back to "/" yes; synthetic popstate no; other paths no; web no', async () => {
    const a = await mk('/');
    a.nav.installRootGuard();
    a.nav.openContactEntry('/c/5');
    let seen = [];
    a.win.addEventListener('popstate', ev => seen.push(a.nav.backedToRoot(ev)));
    a.win.history.back();                                  // real traversal to "/"
    assert.deepEqual(seen, [true]);
    a.win.dispatchEvent({type: 'popstate'});               // synthetic (setTab/navigate)
    assert.deepEqual(seen, [true, false]);

    const b = await mk('/');
    b.nav.installRootGuard();
    b.nav.tabNavigate('/x/1');
    b.nav.push('/c/9');
    const seenB = [];
    b.win.addEventListener('popstate', ev => seenB.push(b.nav.backedToRoot(ev)));
    b.win.history.back();                                  // lands on /x/1, not the list
    assert.deepEqual(seenB, [false]);

    const w = await mk('/', false);
    w.win.history.pushState({wbDepth: 1}, '', '/c/1');
    const seenW = [];
    w.win.addEventListener('popstate', ev => seenW.push(w.nav.backedToRoot(ev)));
    w.win.history.back();
    assert.deepEqual(seenW, [false]);                      // web: never
});
