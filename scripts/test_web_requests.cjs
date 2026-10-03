const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function modelFor(fetch, overrides = {}) {
  const context = {
    window: { RcloneUI: { storedChoice: (_key, _choices, fallback) => fallback } },
    navigator: { onLine: true },
    AbortController, setTimeout, clearTimeout, fetch, ...overrides,
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  const model = context.app();
  model.showToast = () => {};
  return model;
}

function manualTimers() {
  let nextID = 0;
  const pending = new Map();
  return {
    setTimeout: (callback) => { pending.set(++nextID, callback); return nextID; },
    clearTimeout: (id) => pending.delete(id),
    expire: () => { for (const [id, callback] of [...pending]) { pending.delete(id); callback(); } },
    get count() { return pending.size; },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
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

test('deadline also covers waiting for response headers', async () => {
  const timers = manualTimers();
  let signal;
  const model = modelFor(async (_url, options) => {
    signal = options.signal;
    return stalledBody(signal).json();
  }, timers);
  const pending = model.api('GET', '/status', undefined, { captureError: true });
  timers.expire();
  const result = await pending;
  assert.equal(signal.aborted, true);
  assert.equal(result.status, 0);
  assert.match(result.detail, /Zeitüberschreitung/);
  assert.equal(timers.count, 0);
});

test('deadline covers a non-JSON response body too', async () => {
  const timers = manualTimers();
  const bodyStarted = deferred();
  const model = modelFor(async (_url, options) => ({
    ok: true, headers: { get: () => 'text/plain' },
    text: () => { bodyStarted.resolve(); return stalledBody(options.signal).text(); },
  }), timers);
  const pending = model.api('GET', '/logs', undefined, { captureError: true });
  await bodyStarted.promise;
  timers.expire();
  const result = await pending;
  assert.equal(result.__error, true);
  assert.match(result.detail, /Zeitüberschreitung/);
  assert.equal(timers.count, 0);
});

test('invalid JSON clears the deadline and does not prevent another request', async () => {
  const timers = manualTimers();
  const signals = [];
  let call = 0;
  const model = modelFor(async (_url, options) => {
    signals.push(options.signal);
    return { ok: true, headers: { get: () => 'application/json' }, json: async () => {
      if (++call === 1) throw new SyntaxError('Malformed response');
      return { recovered: true };
    } };
  }, timers);
  const result = await model.api('GET', '/status', undefined, { requestKey: 'status', captureError: true });
  assert.equal(result.__error, true);
  assert.match(result.detail, /Malformed response/);
  assert.equal(timers.count, 0);
  assert.equal(signals[0].aborted, false);
  assert.equal((await model.api('GET', '/status', undefined, { requestKey: 'status' })).recovered, true);
  assert.equal(model.connectionState, 'online');
  assert.equal(timers.count, 0);
});

test('non-JSON server errors retain status and allow a later recovery', async () => {
  const timers = manualTimers();
  let fail = true;
  const model = modelFor(async () => fail
    ? { ok: false, status: 503, statusText: 'Service Unavailable', json: async () => { throw new SyntaxError('HTML instead of JSON'); } }
    : { ok: true, headers: { get: () => 'application/json' }, json: async () => ({ recovered: true }) }, timers);
  const result = await model.api('GET', '/status', undefined, { captureError: true });
  assert.equal(result.status, 503);
  assert.equal(result.detail, 'Service Unavailable');
  assert.equal(model.connectionState, 'degraded');
  fail = false;
  assert.equal((await model.api('GET', '/status')).recovered, true);
  assert.equal(model.connectionState, 'online');
  assert.equal(timers.count, 0);
});

test('an obsolete unauthorized response cannot redirect a newer successful request', async () => {
  const headers = deferred();
  const timers = manualTimers();
  const browser = { RcloneUI: { storedChoice: (_key, _choices, fallback) => fallback } };
  let call = 0;
  const model = modelFor(async () => ++call === 1 ? headers.promise
    : { ok: true, headers: { get: () => 'application/json' }, json: async () => ({ latest: true }) }, { ...timers, window: browser });
  const obsolete = model.api('GET', '/status', undefined, { requestKey: 'status' });
  assert.equal((await model.api('GET', '/status', undefined, { requestKey: 'status' })).latest, true);
  headers.resolve({ ok: false, status: 401 });
  assert.equal((await obsolete).__stale, true);
  assert.equal(browser.location, undefined);
  assert.equal(model.connectionState, 'online');
  assert.equal(timers.count, 0);
});

test('late cleanup of an obsolete request keeps the newer request abortable', async () => {
  const oldBody = deferred();
  const bodyStarted = deferred();
  const timers = manualTimers();
  const signals = [];
  let call = 0;
  const model = modelFor(async (_url, options) => {
    signals.push(options.signal);
    if (++call === 1) return { ok: true, headers: { get: () => 'application/json' }, json: () => { bodyStarted.resolve(); return oldBody.promise; } };
    if (call === 2) return stalledBody(options.signal);
    return { ok: true, headers: { get: () => 'application/json' }, json: async () => ({ final: true }) };
  }, timers);
  const first = model.api('GET', '/status', undefined, { requestKey: 'status' });
  await bodyStarted.promise;
  const second = model.api('GET', '/status', undefined, { requestKey: 'status' });
  oldBody.resolve({ old: true });
  assert.equal((await first).__stale, true);
  const third = await model.api('GET', '/status', undefined, { requestKey: 'status' });
  assert.equal((await second).__stale, true);
  assert.equal(signals[1].aborted, true);
  assert.equal(signals[2].aborted, false);
  assert.equal(third.final, true);
  assert.equal(timers.count, 0);
});

test('offline failures are recoverable without leaving a pending request timer', async () => {
  const timers = manualTimers();
  const network = { onLine: false };
  const model = modelFor(async () => {
    if (!network.onLine) throw new TypeError('Disconnected');
    return { ok: true, headers: { get: () => 'application/json' }, json: async () => ({ recovered: true }) };
  }, { ...timers, navigator: network });
  const result = await model.api('GET', '/status', undefined, { captureError: true });
  assert.equal(result.__error, true);
  assert.equal(model.connectionState, 'offline');
  assert.equal(timers.count, 0);
  network.onLine = true;
  assert.equal((await model.api('GET', '/status')).recovered, true);
  assert.equal(model.connectionState, 'online');
  assert.equal(timers.count, 0);
});

test('plan writes send cookies, CSRF and the exact revision-bound JSON body', async () => {
  const timers = manualTimers();
  let recorded;
  const model = modelFor(async (url, options) => {
    recorded = { url, options };
    return { ok: true, headers: { get: () => 'application/json' }, json: async () => ({ ok: true }) };
  }, { ...timers, document: { cookie: 'unrelated=1; rclone_sync_csrf=fixture%2Fcsrf' } });
  const body = { revision: 'a'.repeat(64), settings: { enabled: false, schedule: 'manual' } };
  await model.api('put', '/api/recovery/restore-plan', body);
  assert.equal(recorded.url, '/api/recovery/restore-plan');
  assert.equal(recorded.options.method, 'PUT');
  assert.equal(recorded.options.credentials, 'include');
  assert.equal(recorded.options.headers['X-CSRF-Token'], 'fixture/csrf');
  assert.equal(recorded.options.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(recorded.options.body), body);
  assert.equal(timers.count, 0);
});
