/* Pure-logic tests for the keyboard/visualViewport sync; no browser needed.
 * Run: node --test tests/viewport.test.cjs
 */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/static/js/utils/viewport.js'), 'utf8');
const load = () => import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

test('app height follows the visual viewport, rounded', async () => {
  const {computeAppHeight} = await load();
  assert.equal(computeAppHeight({height: 412.4, scale: 1}), 412);
  assert.equal(computeAppHeight({height: 700, scale: 1}), 700);
});

test('invalid or pinch-zoomed viewports fall back to 100dvh (null)', async () => {
  const {computeAppHeight} = await load();
  assert.equal(computeAppHeight(null), null);
  assert.equal(computeAppHeight({height: 0, scale: 1}), null);
  assert.equal(computeAppHeight({height: NaN, scale: 1}), null);
  assert.equal(computeAppHeight({height: 500, scale: 1.5}), null);
});

test('scroll reset is requested only when the page was panned and not zoomed', async () => {
  const {needsScrollReset} = await load();
  assert.equal(needsScrollReset({scale: 1, offsetTop: 0}, {scrollY: 0, scrollX: 0}), false);
  assert.equal(needsScrollReset({scale: 1, offsetTop: 0}, {scrollY: 120, scrollX: 0}), true);
  assert.equal(needsScrollReset({scale: 1, offsetTop: 80}, {scrollY: 0, scrollX: 0}), true);
  assert.equal(needsScrollReset({scale: 2, offsetTop: 80}, {scrollY: 120, scrollX: 0}), false, 'never fight pinch-zoom');
});

test('installViewportSync sets --app-h on keyboard resize, cancels the pan, and cleans up', async () => {
  const {installViewportSync} = await load();
  const listeners = {};
  const vv = {height: 800, scale: 1, offsetTop: 0,
    addEventListener(n, f) {listeners[n] = f;}, removeEventListener(n) {delete listeners[n];}};
  const style = new Map();
  const win = {visualViewport: vv, scrollY: 0, scrollX: 0, scrolled: 0,
    document: {documentElement: {style: {setProperty: (k, v) => style.set(k, v), removeProperty: k => style.delete(k)}}},
    requestAnimationFrame: fn => { fn(); return 1; },
    addEventListener() {}, removeEventListener() {}, scrollTo() {this.scrolled++;}};
  const stop = installViewportSync(win);
  assert.equal(style.get('--app-h'), '800px');
  vv.height = 480; win.scrollY = 200; listeners.resize();
  assert.equal(style.get('--app-h'), '480px');
  assert.equal(win.scrolled, 1);
  stop();
  assert.equal(style.has('--app-h'), false);
});

test('without visualViewport support the helper is a harmless no-op', async () => {
  const {installViewportSync} = await load();
  assert.equal(typeof installViewportSync({document: {documentElement: {}}}), 'function');
});
