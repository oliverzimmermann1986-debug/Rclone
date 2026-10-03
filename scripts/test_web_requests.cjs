const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function modelFor(fetch) {
  const context = {
    window: { RcloneUI: { storedChoice: (_key, _choices, fallback) => fallback } },
    navigator: { onLine: true },
    AbortController, setTimeout, clearTimeout, fetch,
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  const model = context.app();
  model.showToast = () => {};
  return model;
}

function stalledBody(signal, ok = true) {
  const read = () => new Promise((_resolve, reject) => {
    const abort = () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
    if (signal.aborted) abort();
    else signal.addEventListener('abort', abort, { once: true });
  });
  return { ok, status: ok ? 200 : 503, headers: { get: () => 'application/json' }, json: read, text: read };
}

for (const ok of [true, false]) {
  test(`timeout covers stalled ${ok ? 'success' : 'error'} response body`, async () => {
    let signal;
    const model = modelFor(async (_url, options) => {
      signal = options.signal;
      return stalledBody(signal, ok);
    });
    const result = await model.api('GET', '/status', undefined, { timeoutMs: 15, captureError: true });
    assert.equal(signal.aborted, true);
    assert.equal(result.__error, true);
    assert.equal(result.status, 0);
    assert.match(result.detail, /Zeitüberschreitung/);
  });
}

test('completed body cancels timeout and retains result', async () => {
  let signal;
  const model = modelFor(async (_url, options) => {
    signal = options.signal;
    return { ok: true, headers: { get: () => 'application/json' }, json: async () => ({ value: 42 }) };
  });
  const result = await model.api('GET', '/status', undefined, { timeoutMs: 15 });
  await delay(35);
  assert.equal(result.value, 42);
  assert.equal(signal.aborted, false);
});

test('superseded body is aborted without overwriting the newer request', async () => {
  let calls = 0;
  const model = modelFor(async (_url, options) => ++calls === 1
    ? stalledBody(options.signal)
    : { ok: true, headers: { get: () => 'application/json' }, json: async () => ({ latest: true }) });
  const first = model.api('GET', '/status', undefined, { requestKey: 'status', timeoutMs: 100 });
  await delay(5);
  const second = await model.api('GET', '/status', undefined, { requestKey: 'status', timeoutMs: 100 });
  assert.equal((await first).__stale, true);
  assert.equal(second.latest, true);
  assert.equal(model.connectionState, 'online');
});

test('stalled overview releases refresh ownership so retry can run', async () => {
  let stall = true;
  const model = modelFor(async (_url, options) => stall
    ? stalledBody(options.signal)
    : { ok: true, headers: { get: () => 'application/json' }, json: async () => ({ ok: true }) });
  model.refreshStatus = async () => true;
  model.loadRecent = async () => true;
  model.loadSchedulerState = async () => true;
  model.loadOverview = async () => {
    const result = await model.api('GET', '/overview', undefined, { timeoutMs: 15, captureError: true });
    return !result.__error;
  };
  assert.equal(await model.refreshAll(), false);
  assert.equal(model.refreshing, false);
  stall = false;
  assert.equal(await model.refreshAll(), true);
  assert.equal(model.refreshing, false);
});
