/* Pure DOM contract checks; no browser, backend, network, or credentials. */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { parseHTML } = require('linkedom');
const root = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(root, 'app/static/index.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'app/static/workspace.js'), 'utf8');

function allElements(rootNode) {
  const result = [];
  for (const child of Array.from(rootNode.children || [])) {
    result.push(child, ...allElements(child));
    if (child.tagName === 'TEMPLATE') result.push(...allElements(child.content));
  }
  return [...new Set(result)];
}

function runScenario(newerServer = false) {
  const { document, window } = parseHTML(html);
  const recovery = Array.from(document.querySelectorAll('main section[x-show]'))
    .find((section) => section.getAttribute('x-show') === "page==='recovery'");
  if (newerServer) {
    const extra = document.createElement('article');
    extra.id = 'newer-live-restore-plan';
    extra.innerHTML = '<h3>Restore-Plan</h3><button @click="confirmRestorePlan()">Geprüften Plan ausführen</button>';
    recovery.append(extra);
    const jobTemplate = document.querySelector('.definition-list > template');
    const options = jobTemplate.content.querySelector('.definition-options');
    if (options && options.tagName !== 'DETAILS') {
      const container = document.createElement('details');
      container.innerHTML = '<summary>Ausführung und Wiederholung</summary>';
      options.before(container);
      container.append(options);
    }
  }
  const originals = allElements(document.documentElement);
  const controls = originals.filter((node) => Array.from(node.attributes)
    .some((attribute) => attribute.name.startsWith('@') || ['x-model', 'x-for', 'x-if'].includes(attribute.name)));
  const signatures = new Map(controls.map((node) => [node, Array.from(node.attributes)
    .filter((attribute) => attribute.name.startsWith('@') || ['x-model', 'x-for', 'x-if', ':id', 'id'].includes(attribute.name))
    .map((attribute) => [attribute.name, attribute.value])]));
  const watches = new Map();
  const context = { window, document, console };
  window.app = () => ({
    page: 'dashboard',
    overview: { data: { pairs: { health: [] } } },
    recovery: { quarantine: [], diff: null },
    $watch(key, callback) { watches.set(key, callback); },
    $nextTick(callback) { callback(); },
    init() { this.originalInitCalled = true; },
    async navigate(next) { if (next !== 'rejected') this.page = next; },
    statusLabel(status) { return status; },
    storagePairs() { return []; },
  });
  // Browser setAttribute rejects '@' although HTML parsing accepts Alpine's
  // shorthand. LinkeDOM is permissive, so enforce the browser boundary here.
  const originalSetAttribute = window.Element.prototype.setAttribute;
  window.Element.prototype.setAttribute = function checkedAttribute(name, value) {
    assert.match(name, /^[A-Za-z_:][A-Za-z0-9_.:-]*$/, 'Dynamic attribute must be a valid DOM XMLName');
    return originalSetAttribute.call(this, name, value);
  };
  try {
    vm.runInNewContext(script, context, { filename: 'workspace.js' });
  } finally {
    window.Element.prototype.setAttribute = originalSetAttribute;
  }
  assert.equal(document.body.dataset.workspaceReady, 'true');
  const retained = new Set(allElements(document.documentElement));
  for (const control of controls) {
    assert(retained.has(control), `Original control was removed: ${control.outerHTML.slice(0, 160)}`);
    for (const [name, value] of signatures.get(control)) assert.equal(control.getAttribute(name), value, `Binding changed: ${name}`);
  }
  const ids = allElements(document.documentElement).map((node) => node.id).filter(Boolean);
  assert.equal(new Set(ids).size, ids.length, 'Duplicate static IDs');
  const model = window.app();
  model.init();
  assert.equal(model.originalInitCalled, true);
  const groupCounts = { dashboard: 4, doctor: 4, recovery: 4 };
  for (const [name, count] of Object.entries(groupCounts)) {
    const tabs = Array.from(document.querySelectorAll(`[data-workspace-page="${name}"] > .workspace-tabs button`));
    assert.equal(tabs.length, count, `${name} view count`);
    for (const tab of tabs) {
      const panel = document.getElementById(tab.getAttribute('aria-controls'));
      assert(panel, 'Tab must have a panel');
      assert.equal(panel.getAttribute('aria-labelledby'), tab.id);
      const expression = panel.getAttribute('x-show');
      const evaluate = () => Function('workspaceViews', `return (${expression})`)(model.workspaceViews);
      const id = tab.id.replace(`workspace-tab-${name}-`, '');
      model.workspaceSelect(name, id);
      assert.equal(evaluate(), true);
      assert.equal(Array.from(document.querySelectorAll(`[data-workspace-page="${name}"] > .workspace-view`))
        .filter((item) => Function('workspaceViews', `return (${item.getAttribute('x-show')})`)(model.workspaceViews)).length, 1);
    }
    const key = { key: 'Home', preventDefault() { this.prevented = true; } };
    model.workspaceTabKey(key, name);
    assert.equal(key.prevented, true);
    assert.equal(model.workspaceViews[name], tabs[0].id.replace(`workspace-tab-${name}-`, ''));
    model.workspaceTabKey({ key: 'ArrowLeft', preventDefault() {} }, name);
    assert.equal(model.workspaceViews[name], tabs.at(-1).id.replace(`workspace-tab-${name}-`, ''));
    model.workspaceTabKey({ key: 'ArrowRight', preventDefault() {} }, name);
    assert.equal(model.workspaceViews[name], tabs[0].id.replace(`workspace-tab-${name}-`, ''));
  }
  const progress = document.querySelector('.workspace-live');
  assert(progress);
  assert.equal(progress.closest('.workspace-view'), null, 'Active progress must remain outside optional views');
  const alertStack = document.querySelector('[data-workspace-page="dashboard"] .alert-stack');
  assert.equal(alertStack.closest('.workspace-view'), null, 'Server alerts must remain visible');
  assert.equal(document.querySelector('.workspace-quarantine-notice').closest('.workspace-view'), null);
  assert(document.querySelector('.workspace-disclosure .definition-assignments') ||
    allElements(document.documentElement).some((node) => node.matches('.workspace-disclosure .definition-assignments')),
  'Job assignments should be disclosed deliberately');
  if (newerServer) assert.equal(document.getElementById('newer-live-restore-plan').closest('.workspace-view').id,
    'workspace-panel-recovery-restore', 'New live restore controls must remain in the default restore view');
  const state = { ...model.workspaceViews };
  vm.runInNewContext(script, context);
  assert.equal(JSON.stringify(model.workspaceViews), JSON.stringify(state), 'Enhancement is idempotent');
  return { model, watches, document, originalControls: controls.length };
}

(async () => {
  const normal = runScenario();
  runScenario(true);
  const deferred = [];
  const originalTick = normal.model.$nextTick;
  const originalActive = Object.getOwnPropertyDescriptor(normal.document, 'activeElement');
  const destination = normal.document.getElementById('workspace-tab-doctor-audit');
  const originalFocus = destination.focus;
  let focusCalls = 0;
  destination.focus = () => { focusCalls += 1; };
  normal.model.$nextTick = (callback) => deferred.push(callback);
  Object.defineProperty(normal.document, 'activeElement', {
    value: normal.document.getElementById('workspace-panel-doctor-health'), writable: true, configurable: true,
  });
  normal.model.workspaceSelect('doctor', 'audit');
  normal.document.activeElement = normal.document.body;
  deferred.forEach((callback) => callback());
  assert.equal(focusCalls, 1, 'Focus returns to the selected tab even if hiding the old panel first focuses body');
  destination.focus = originalFocus;
  normal.model.$nextTick = originalTick;
  if (originalActive) Object.defineProperty(normal.document, 'activeElement', originalActive);
  else delete normal.document.activeElement;
  normal.model.overview.data.pairs.health = [
    { name: 'Fotos', last_status: 'stale' },
    { name: 'Archiv', last_status: 'skipped', error: 'Quarantäne aktiv' },
    { name: 'Dokumente', last_status: 'ok' },
  ];
  normal.model.storagePairs = () => [{ name: 'Cloud', restore_evidence: { state: 'failed' } }];
  assert.deepEqual(Array.from(normal.model.workspaceIssues()), [
    'Fotos: stale', 'Archiv: Quarantäne aktiv', 'Cloud: Letzter Restore-Test fehlgeschlagen',
  ]);
  normal.model.workspaceSelect('doctor', 'audit');
  await normal.model.navigate('doctor');
  assert.equal(normal.model.workspaceViews.doctor, 'health', 'Entering a page starts in its primary view');
  normal.model.page = 'dashboard';
  normal.model.workspaceDestination = { page: 'doctor', view: 'copies' };
  await normal.model.navigate('doctor');
  assert.equal(normal.model.workspaceViews.doctor, 'copies', 'Copy-matrix link reaches its intended detail');
  assert.equal(normal.model.workspaceDestination, null, 'Destination is consumed once');
  normal.model.workspaceSelect('doctor', 'audit');
  await normal.model.navigate('rejected');
  assert.equal(normal.model.workspaceViews.doctor, 'audit', 'Rejected navigation must preserve current view');
  normal.model.page = 'recovery';
  normal.model.workspaceSelect('recovery', 'handover');
  normal.watches.get('recovery.diff')({ counts: { changed: 1 } });
  assert.equal(normal.model.workspaceViews.recovery, 'restore', 'Loaded comparison is shown immediately');
  console.log(`Workspace DOM checks passed: ${normal.originalControls} original controls/bindings retained, 12 views, newer live restore UI, notices, keyboard navigation, and model hooks.`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
