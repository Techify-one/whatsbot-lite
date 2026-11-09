// Back-button model for the INSTALLED app (PWA, display-mode: standalone).
//
// Problem it solves: the in-app "←" arrows used to pushState('/') — a NEW history
// entry — so the stack became [/, /contacts/5, /] and the phone's back button
// walked back into the conversation (2 backs to reach the list). And when the app
// was opened on a deep link (notification, shortcut) the stack had a single entry,
// so one back closed the app instead of showing the contact list.
//
// Model (standalone only):
//   - Every entry we create carries `history.state = { wbDepth: n }`; the root
//     entry ("/", the contact list) is depth 0.
//   - A cold start on a non-root URL first inserts the root entry underneath
//     ("root guard"), so back from any screen lands on the contact list.
//   - Switching between top-level screens REPLACES the entry unless we are on "/",
//     so the stack never grows into a chain of screens.
//   - The "←" arrows call goToRoot()/goBack(), which move through history instead
//     of pushing, so they behave exactly like the system back button.
//   - Overlays (contact info panel) push one entry flagged `wbOverlay`, so back
//     closes the panel instead of leaving the conversation.
//
// Outside standalone (regular browser tab) every helper returns false / does the
// old plain pushState(null, …), so the web version behaves exactly as before.
//
// Pure logic with an injectable `window`: tested by tests/navigation.test.cjs.

export function createNavigation(win) {
  const w = () => win || globalThis.window;

  function isStandalone() {
    try {
      const x = w();
      return !!(x.matchMedia && x.matchMedia('(display-mode: standalone)').matches)
        || x.navigator?.standalone === true;
    } catch {
      return false;
    }
  }

  function depth() {
    const s = w().history.state;
    return s && Number.isInteger(s.wbDepth) ? s.wbDepth : 0;
  }

  function notify() {
    const x = w();
    x.dispatchEvent(new x.PopStateEvent('popstate'));
  }

  /** pushState that keeps the depth bookkeeping when installed. */
  function push(path) {
    const x = w();
    x.history.pushState(isStandalone() ? { wbDepth: depth() + 1 } : null, '', path);
  }

  /** replaceState that keeps the depth of the entry it replaces. */
  function replace(path) {
    const x = w();
    x.history.replaceState(isStandalone() ? { wbDepth: depth() } : null, '', path);
  }

  /**
   * Rewrite only the URL (filters, search, query string) and KEEP the current
   * history.state — a plain replaceState(null, …) would erase wbDepth/wbOverlay and
   * the installed app would lose track of where the contact list is.
   */
  function keepState(url) {
    const x = w();
    x.history.replaceState(x.history.state, '', url);
  }

  /**
   * Back to the contact list. Installed app: jump down the stack (like the system
   * back button). Web: the old pushState('/'). Returns true when handled by history.
   */
  function toList() {
    if (goToRoot()) return true;
    w().history.pushState(null, '', '/');
    return false;
  }

  /**
   * Only point the URL at "/" WITHOUT a popstate and without moving through history.
   * For the one case where a conversation stays selected in state but its URL is gone
   * (a contact that has no conversation yet). Installed app: rewrites the current entry
   * (never stacks another "/"); at depth > 0 that leaves a duplicate "/" below, so do
   * NOT use it for the back arrow — use toList()/goToRoot() there.
   */
  function rootSilently() {
    const x = w();
    if (isStandalone()) {
      if (x.location.pathname !== '/') replace('/');
    } else {
      x.history.pushState(null, '', '/');
    }
  }

  /**
   * Navigation INSIDE a screen (config tab, chat project, execution detail → list).
   * Installed app, away from "/": replace the entry so one back still reaches the
   * contact list. Everywhere else: a normal push, exactly as before.
   */
  function swap(path) {
    const x = w();
    if (isStandalone() && x.location.pathname !== '/') replace(path);
    else push(path);
  }

  /**
   * Installed app only: make every pushState/replaceState written elsewhere in the
   * code base keep the depth bookkeeping, without editing each call site.
   *   pushState(null, …)    -> state { wbDepth: current + 1 }
   *   replaceState(null, …) -> keeps the current state (URL-only rewrites such as
   *                            filters, search and ?query sync must not lose depth)
   * An explicit state is passed through untouched. Idempotent. The web version never
   * calls this, so its history behaves exactly as before.
   */
  function trackHistory() {
    const h = w().history;
    if (h.__wbTracked) return;
    const rawPush = h.pushState.bind(h);
    const rawReplace = h.replaceState.bind(h);
    h.pushState = (state, title, url) =>
      rawPush(state == null ? { wbDepth: depth() + 1 } : state, title, url);
    h.replaceState = (state, title, url) =>
      rawReplace(state == null ? (h.state || { wbDepth: depth() }) : state, title, url);
    h.__wbTracked = true;
  }

  /**
   * Make sure an entry for "/" exists under the current one. Call once at boot,
   * before rendering. Idempotent: entries we created are recognised by wbDepth.
   */
  function installRootGuard() {
    if (!isStandalone()) return false;
    trackHistory();
    const x = w();
    const s = x.history.state;
    if (s && Number.isInteger(s.wbDepth)) return false; // reload of an entry we made
    const { pathname, search, hash } = x.location;
    const original = pathname + search + hash;
    if (pathname === '/') {
      x.history.replaceState({ wbDepth: 0 }, '', original);
      return true;
    }
    x.history.replaceState({ wbDepth: 0 }, '', '/');
    x.history.pushState({ wbDepth: 1 }, '', original);
    return true;
  }

  /**
   * Go to the contact list ("/") the way the system back button would: a single
   * jump down to the root entry. Returns true when handled (installed app).
   */
  function goToRoot() {
    if (!isStandalone()) return false;
    const x = w();
    const d = depth();
    if (d > 0) {
      x.history.go(-d); // real popstate follows
    } else if (x.location.pathname !== '/') {
      x.history.replaceState({ wbDepth: 0 }, '', '/');
      notify();
    }
    return true;
  }

  /**
   * One step back, like the system button. At depth 0 (nothing below us) it
   * rewrites the current entry to `fallback` instead of leaving the app.
   */
  function goBack(fallback = '/') {
    if (!isStandalone()) return false;
    const x = w();
    if (depth() > 0) {
      x.history.back();
    } else if (x.location.pathname !== fallback) {
      x.history.replaceState({ wbDepth: 0 }, '', fallback);
      notify();
    }
    return true;
  }

  /**
   * Top-level screen switch (gear menu, arrows). Returns false outside the
   * installed app so the caller keeps its original pushState code.
   */
  function tabNavigate(path) {
    if (!isStandalone()) return false;
    const x = w();
    if (path === '/') return goToRoot();
    if (x.location.pathname === '/') push(path);
    else replace(path);
    notify();
    return true;
  }

  /** Open a conversation: replace when already inside one, push from the list. */
  function openContactEntry(path) {
    if (!isStandalone()) return false;
    const x = w();
    if (x.location.pathname === '/') push(path);
    else replace(path);
    return true;
  }

  /**
   * True when `ev` is a REAL back/forward traversal (not one of the app's synthetic
   * popstates) that landed on the contact list. Our entries carry `wbDepth`, synthetic
   * events carry no state. Components use it to close the open conversation when the
   * phone's back button returns to "/", without relying on App-level URL state (which
   * stays null when a conversation is opened with pushState, since that fires no popstate).
   */
  function backedToRoot(ev) {
    if (!isStandalone()) return false;
    const st = ev && ev.state;
    if (!st || !Number.isInteger(st.wbDepth)) return false;
    return w().location.pathname === '/';
  }

  // ── Overlays (info panel): one flagged entry so back closes just the overlay ──
  function hasOverlay(name) {
    const s = w().history.state;
    return !!(s && s.wbOverlay === name);
  }

  function pushOverlay(name) {
    if (!isStandalone() || hasOverlay(name)) return false;
    const x = w();
    const { pathname, search, hash } = x.location;
    x.history.pushState({ wbDepth: depth() + 1, wbOverlay: name }, '', pathname + search + hash);
    return true;
  }

  /** Close an overlay through history (popstate then updates the UI). */
  function popOverlay(name) {
    if (!isStandalone() || !hasOverlay(name)) return false;
    w().history.back();
    return true;
  }

  return {
    isStandalone, depth, push, replace, swap, keepState, toList, rootSilently, installRootGuard,
    goToRoot, goBack, tabNavigate, openContactEntry,
    hasOverlay, pushOverlay, popOverlay, backedToRoot,
  };
}

// Shared instance bound to the real window (lazy: safe to import under Node).
const nav = createNavigation(null);
export default nav;
