'use strict';

// Exercise production methods with controlled time and transport failures.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const root = path.resolve(__dirname, '../..');

function harness() {
  let now = 100000;
  let nextTimer = 0;
  const timers = new Map();
  const streams = [];
  const calls = { progress: 0, toasts: [] };
  const clock = {
    setTimeout(callback, delay) {
      const id = ++nextTimer;
      timers.set(id, { callback, at: now + delay });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
  };
  class EventSource {
    constructor(url) { this.url = url; this.closed = false; streams.push(this); }
    message(data) { this.onmessage?.({ data: JSON.stringify(data) }); }
    fail() { this.onerror?.(); }
    close() { this.closed = true; }
  }
  const window = { ...clock, EventSource, RcloneUI: { storedChoice() {} } };
  const document = { hidden: false };
  const navigator = { onLine: true };
  const context = vm.createContext({
    window, document, navigator, console, EventSource, ...clock,
    Date: class extends Date { static now() { return now; } },
    Math: Object.assign(Object.create(Math), { random: () => 0 }),
  });
  for (const [file, name] of [
    ['app-core.js', 'coreMethods'], ['app-dashboard.js', 'dashboardMethods'], ['app-jobs.js', 'jobsMethods'],
  ]) {
    const source = process.argv.includes('--baseline')
      ? require('node:child_process').execFileSync('git', ['show', `HEAD:app/static/${file}`], { cwd: root, encoding: 'utf8' })
      : fs.readFileSync(path.join(root, 'app/static', file), 'utf8');
    vm.runInContext(source.replace(/^import .+;\r?$/gm, '').replace(/\bexport const /g, 'const ')
      + `\nglobalThis.${name} = ${name};`, context, { filename: file });
  }
  const model = {
    ...context.coreMethods, ...context.dashboardMethods, ...context.jobsMethods,
    page: 'dashboard', refreshing: false, refreshFailures: 0,
    polling: { active: false, generation: 0, refreshTimer: null, activityTimer: null },
    sse: null, progressStream: { startedAt: null, lastMessageAt: null, retryTimer: null, retryAttempts: 0 },
    status: {}, progress: null, config: { pbs: { enabled: false } }, lockdownState: { active: false },
    pbs: { status: null }, jobModal: { show: false },
    copies: { loading: false, data: null, error: '' }, overview: { data: null },
    api: async () => ({}), rcloneBusy: () => false,
    loadSchedulerState: async () => true,
    loadProgress: async () => { calls.progress += 1; return true; },
    showToast: (...args) => calls.toasts.push(args),
  };
  async function advance(duration) {
    const until = now + duration;
    for (;;) {
      const next = [...timers].sort((a, b) => a[1].at - b[1].at).find(([, timer]) => timer.at <= until);
      if (!next) break;
      timers.delete(next[0]);
      now = next[1].at;
      await next[1].callback();
    }
    now = until;
  }
  return { model, calls, streams, timers, advance, window, document, navigator, context };
}

test('copies loading reports failures, keeps previous data and clears the error after recovery', async () => {
  const { model } = harness();
  model.api = async () => null;
  assert.equal(await model.loadCopies(true), false);
  assert.match(model.copies.error, /nicht geladen/);
  assert.equal(model.copies.loading, false);
  const previous = { sources: [{ id: 'photos' }] };
  model.api = async () => previous;
  assert.equal(await model.loadCopies(true), true);
  assert.equal(model.copies.error, '');
  model.api = async () => null;
  assert.equal(await model.loadCopies(true), false);
  assert.equal(model.copies.data, previous);
  assert.match(model.copies.error, /letzte geladene Stand/);
  model.api = async () => ({ __stale: true });
  assert.equal(await model.loadCopies(true), undefined);
  assert.equal(model.copies.data, previous);
  assert.match(model.copies.error, /letzte geladene Stand/);
});

test('a failed copies refresh stays degraded despite later successful requests, then recovers', async () => {
  const { model, calls } = harness();
  model.api = async (_method, url) => url === '/api/diagnostics/copies' ? null : {};
  assert.equal(await model.refreshAll(true), false);
  assert.equal(model.connectionState, 'degraded');
  assert.equal(model.refreshing, false);
  assert.deepEqual(calls.toasts.at(-1), ['Ansicht nur teilweise aktualisiert', 'err']);
  model.setConnectionState('online', 'Mit dem Server verbunden');
  assert.equal(model.connectionState, 'degraded');
  model.api = async () => ({});
  assert.equal(await model.refreshAll(true), true);
  assert.equal(model.connectionState, 'online');
  assert.equal(model.copies.error, '');
});

test('fallback progress failures participate in the refresh result', async () => {
  const { model, context } = harness();
  model.rcloneBusy = () => true;
  model.loadProgress = context.jobsMethods.loadProgress;
  model.api = async (_method, url) => url === '/api/jobs/backup/progress' ? null : {};
  assert.equal(await model.refreshAll(), false);
  assert.equal(model.connectionState, 'degraded');
});

test('fresh stream frames suppress redundant activity and full-refresh progress requests', async () => {
  const { model, streams, calls, advance } = harness();
  model.rcloneBusy = () => true;
  model.startPolling();
  streams[0].message({ running: true, current_status: { backup: { id: 7 } }, lockdown_active: true });
  assert.equal(model.status.backup.id, 7);
  assert.equal(model.lockdownState.active, true);
  await advance(6000);
  await model.refreshAll();
  assert.equal(calls.progress, 0);
  model.stopPolling();
});

test('stream errors reconnect, retain fallback polling and ignore callbacks from closed streams', async () => {
  const { model, streams, calls, advance, timers } = harness();
  model.rcloneBusy = () => true;
  model.startPolling();
  const old = streams[0];
  old.message({ running: true });
  const oldMessage = old.onmessage;
  const oldError = old.onerror;
  old.fail();
  assert.equal(old.closed, true);
  assert.equal(model.hasFreshProgressStream(), false);
  await advance(2000);
  assert.equal(streams.length, 2);
  assert.equal(calls.progress, 1);
  oldMessage({ data: JSON.stringify({ running: false }) });
  oldError();
  assert.equal(model.sse, streams[1]);
  streams[1].message({ running: true });
  await advance(2000);
  assert.equal(calls.progress, 1);
  model.stopPolling();
  oldError();
  assert.equal(timers.size, 0);
  assert.equal(streams[1].closed, true);
});

test('repeated failures back off and a healthy frame resets the retry delay', async () => {
  const { model, streams, advance } = harness();
  model.startPolling();
  for (const delay of [1000, 2000, 4000, 8000, 16000, 30000, 30000]) {
    const count = streams.length;
    streams.at(-1).fail();
    await advance(delay - 1);
    assert.equal(streams.length, count);
    await advance(1);
    assert.equal(streams.length, count + 1);
  }
  streams.at(-1).message({ running: false });
  assert.equal(model.progressStream.retryAttempts, 0);
  const count = streams.length;
  streams.at(-1).fail();
  await advance(1000);
  assert.equal(streams.length, count + 1);
  model.stopPolling();
});

test('a stalled or malformed stream cannot disable polling indefinitely', async () => {
  const { model, streams, calls, advance } = harness();
  model.rcloneBusy = () => true;
  model.startPolling();
  streams[0].message({ unexpected: true });
  assert.equal(model.hasFreshProgressStream(), false);
  await advance(10000);
  assert(calls.progress > 0);
  assert.equal(streams[0].closed, true);
  await advance(1000);
  assert.equal(streams.length, 2);
  model.stopPolling();
});

test('hiding, offline state and missing EventSource leave no reconnect loop', async () => {
  const { model, streams, timers, document, navigator, window, advance, calls } = harness();
  model.startPolling();
  streams[0].fail();
  document.hidden = true;
  model.stopPolling();
  await advance(60000);
  assert.equal(streams.length, 1);
  assert.equal(timers.size, 0);
  document.hidden = false;
  navigator.onLine = false;
  model.startPolling();
  assert.equal(model.sse, null);
  model.stopPolling();
  navigator.onLine = true;
  delete window.EventSource;
  model.rcloneBusy = () => true;
  model.startPolling();
  await advance(2000);
  assert.equal(calls.progress, 1);
  assert.equal(streams.length, 1);
  model.stopPolling();
});

test('completion from the stream updates history once without progress polling', async () => {
  const { model, streams } = harness();
  let refreshed = 0;
  let jobsLoaded = 0;
  model.page = 'runs';
  model.refreshAll = async () => { refreshed += 1; };
  model.loadJobs = () => { jobsLoaded += 1; };
  model.startPolling();
  streams[0].message({ running: true });
  streams[0].message({ running: false });
  streams[0].message({ running: false });
  assert.equal(refreshed, 1);
  assert.equal(jobsLoaded, 1);
  model.stopPolling();
});

test('a restore action retains confirmation and sends only the selected data path', async () => {
  const { model } = harness();
  const requests = [];
  model.requestConfirmation = async () => false;
  model.api = async (...args) => { requests.push(args); return { ok: true }; };
  model.loadJobs = () => {};
  model.loadOverview = () => {};
  await model.runRestoreTest('Fotos & Dokumente');
  assert.equal(requests.length, 0);
  model.requestConfirmation = async () => true;
  await model.runRestoreTest('Fotos & Dokumente');
  assert.equal(requests.length, 1);
  assert.equal(requests[0][0], 'POST');
  assert.equal(requests[0][1], '/api/jobs/backup/restore-test?pairs=Fotos%20%26%20Dokumente');
});
