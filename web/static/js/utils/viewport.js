// Keeps the app exactly as tall as the VISIBLE area, so the on-screen keyboard shrinks the
// layout instead of sliding it up (which pushed the contact header off-screen).
//
// Chrome/Android honours `interactive-widget=resizes-content` (index.html); iOS Safari ignores
// it and instead pans the page when an input is focused. Both are covered here: the height
// follows `visualViewport` and any pan of the layout viewport is cancelled.
//
// CSS side (custom.css): `height: var(--app-h, 100dvh)` on <body> and the app root.

const VAR_NAME = '--app-h';

/** Height to expose as --app-h, or null to fall back to 100dvh (pinch-zoomed / unknown). */
export function computeAppHeight(vv) {
  if (!vv || !Number.isFinite(vv.height) || vv.height <= 0) return null;
  // While the user pinch-zooms, visualViewport shrinks without any keyboard involved.
  if (vv.scale && Math.abs(vv.scale - 1) > 0.01) return null;
  return Math.round(vv.height);
}

/** True when the page was panned away from the origin by focus/keyboard (must be undone). */
export function needsScrollReset(vv, win) {
  if (!vv || (vv.scale && Math.abs(vv.scale - 1) > 0.01)) return false;
  return (win.scrollY || 0) > 0 || (win.scrollX || 0) > 0 || (vv.offsetTop || 0) > 0;
}

export function installViewportSync(win = window) {
  const vv = win.visualViewport;
  const root = win.document && win.document.documentElement;
  if (!vv || !root) return () => {};
  let frame = 0;
  const apply = () => {
    frame = 0;
    const height = computeAppHeight(vv);
    if (height === null) root.style.removeProperty(VAR_NAME);
    else root.style.setProperty(VAR_NAME, `${height}px`);
    if (needsScrollReset(vv, win)) win.scrollTo(0, 0);
  };
  const schedule = () => {
    if (frame) return;
    frame = (win.requestAnimationFrame || (fn => setTimeout(fn, 16)))(apply);
  };
  vv.addEventListener('resize', schedule);
  vv.addEventListener('scroll', schedule);
  win.addEventListener('orientationchange', schedule);
  apply();
  return () => {
    vv.removeEventListener('resize', schedule);
    vv.removeEventListener('scroll', schedule);
    win.removeEventListener('orientationchange', schedule);
    root.style.removeProperty(VAR_NAME);
  };
}

if (typeof window !== 'undefined' && window.visualViewport) installViewportSync(window);
