/* Alpine integration in a pure DOM VM. No browser or backend is started. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { parseHTML } = require('linkedom');
const root = path.resolve(__dirname, '../..');
const actualBody = fs.readFileSync(path.join(root, 'app/static/index.html'), 'utf8').match(/<body\b[^>]*>/)?.[0];
assert(actualBody, 'Production HTML has a body');
const bodyInit = [actualBody.match(/\bx-data="[^"]*"/)?.[0], actualBody.match(/\bx-init="[^"]*"/)?.[0]]
  .filter(Boolean).join(' ');
const fixture = `<!doctype html><html><body ${bodyInit}><main>
<section x-show="page==='dashboard'">
  <div class="page-heading"><h2>Lagebild</h2></div>
  <div class="alert-stack" x-show="false">Serverwarnung</div>
  <article class="scheduler-control-strip">Automatik</article>
  <div class="metric-grid"><article>Status</article></div>
  <article class="copy-overview">Kopien</article>
  <article class="copy-matrix-summary"><button @click="navigate('doctor')">Kopiendetails</button></article>
  <div class="dashboard-grid"><article class="control-panel"><div class="live-progress" x-show="progress.running">Fortschritt</div></article><article class="system-panel">Serverdaten</article></div>
  <article><h3>Datenweg-Status</h3><div class="health-grid"></div></article>
</section>
<section x-show="page==='doctor'">
  <div class="page-heading"><h2>System</h2><button @click="loadDoctor()">Prüfen</button></div>
  <div class="metric-grid">Dienste</div>
  <article><h3>Kopien je Datenbestand</h3></article>
  <article><h3>Logs und Datenbank</h3></article>
  <article class="audit-panel">Protokoll</article>
</section>
<section x-show="page==='recovery'">
  <div class="page-heading"><h2>Wiederherstellung</h2></div>
  <article class="recovery-browser-panel">Restore-Browser</article>
  <div class="recovery-secondary-grid"><article><h3>Staging-Bereich</h3></article><article><h3>Quarantäne</h3></article></div>
  <article class="handover-panel">Notfallpaket</article>
</section>
</main></body></html>`;

const { document, window } = parseHTML(fixture);
const errors = [];
const sandbox = {
  window, document,
  MutationObserver: window.MutationObserver,
  CustomEvent: window.CustomEvent,
  Event: window.Event,
  Element: window.Element,
  HTMLElement: window.HTMLElement,
  ShadowRoot: window.ShadowRoot,
  Node: window.Node,
  queueMicrotask,
  setTimeout,
  clearTimeout,
  requestAnimationFrame: (callback) => setTimeout(callback, 0),
  cancelAnimationFrame: clearTimeout,
  console: { ...console, warn: (...args) => errors.push(args.join(' ')), error: (...args) => errors.push(args.join(' ')) },
};
window.requestAnimationFrame = sandbox.requestAnimationFrame;
window.app = () => ({
  page: 'dashboard',
  overview: { data: { pairs: { health: [] } } },
  recovery: { quarantine: [], diff: null },
  progress: { running: false },
  calls: [],
  init() { this.calls.push('init'); },
  async navigate(next) {
    if (this.blockNavigation) return;
    this.calls.push(`navigate:${next}`);
    this.page = next;
  },
  rcloneBusy() { return false; },
  storagePairs() { return []; },
  statusLabel(value) { return value; },
  loadDoctor() { this.calls.push('loadDoctor'); },
});
vm.createContext(sandbox);
const originalSetAttribute = window.Element.prototype.setAttribute;
window.Element.prototype.setAttribute = function checkedAttribute(name, value) {
  assert.match(name, /^[A-Za-z_:][A-Za-z0-9_.:-]*$/, 'Dynamic attribute must be a valid DOM XMLName');
  return originalSetAttribute.call(this, name, value);
};
try {
  vm.runInContext(fs.readFileSync(path.join(root, 'app/static/workspace.js'), 'utf8'), sandbox, { filename: 'workspace.js' });
} finally {
  window.Element.prototype.setAttribute = originalSetAttribute;
}
sandbox.app = window.app;
vm.runInContext(fs.readFileSync(path.join(root, 'app/static/alpine.min.js'), 'utf8'), sandbox, { filename: 'alpine.min.js' });
const settle = () => new Promise((resolve) => setTimeout(resolve, 15));

(async () => {
  await settle();
  const model = window.Alpine.$data(document.body);
  assert.deepEqual(Array.from(model.calls), ['init']);
  const tab = document.getElementById('workspace-tab-doctor-audit');
  assert.equal(tab.getAttribute('aria-selected'), 'false');
  tab.dispatchEvent(new window.Event('click', { bubbles: true, cancelable: true }));
  await settle();
  assert.equal(model.workspaceViews.doctor, 'audit');
  assert.equal(tab.getAttribute('aria-selected'), 'true');
  assert.equal(document.getElementById('workspace-panel-doctor-audit').style.display, undefined);
  assert.equal(document.getElementById('workspace-panel-doctor-health').style.display, 'none');
  model.recovery.quarantine.push({ id: 1 });
  await settle();
  assert.equal(document.querySelector('.workspace-tab-count').textContent, '1');
  const copies = document.querySelector('.copy-matrix-summary button');
  copies.dispatchEvent(new window.Event('click', { bubbles: true, cancelable: true }));
  await settle();
  assert.equal(model.page, 'doctor');
  assert.equal(model.workspaceViews.doctor, 'copies');
  assert.equal(model.workspaceDestination, null);
  assert.equal(model.calls.filter((value) => value === 'navigate:doctor').length, 1);
  const doctor = document.querySelector('button[x-on\\:click\\.capture*="workspaceSelect"]');
  doctor.dispatchEvent(new window.Event('click', { bubbles: true, cancelable: true }));
  await settle();
  assert.equal(model.workspaceViews.doctor, 'health');
  assert.equal(model.calls.filter((value) => value === 'loadDoctor').length, 1);
  const key = new window.Event('keydown', { bubbles: true, cancelable: true });
  Object.defineProperty(key, 'key', { value: 'ArrowRight' });
  document.getElementById('workspace-tab-doctor-health').dispatchEvent(key);
  await settle();
  assert.equal(model.workspaceViews.doctor, 'copies');
  assert.equal(document.getElementById('workspace-tab-doctor-copies').getAttribute('tabindex'), '0');
  assert.equal(errors.length, 0, errors.join('\n'));
  console.log('Workspace Alpine VM checks passed: initialization before Alpine, reactive panels/badges, original handlers, and capture navigation.');
})().catch((error) => { console.error(error); process.exitCode = 1; });
