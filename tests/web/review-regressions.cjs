'use strict';

// Run only in the project's isolated test environment. Production methods are
// loaded unchanged; time, transport, navigation and DOM boundaries are fixtures.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const { parseHTML } = require('linkedom');
const root = path.resolve(__dirname, '../..');
const plain = (value) => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };

function harness() {
  let now = Date.UTC(2026, 2, 28, 12);
  let nextTimer = 0;
  const timers = new Map();
  const elements = new Map();
  const calls = { toasts: [], requests: [], navigation: [] };
  const clock = {
    setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, at: now + delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  const window = {
    RcloneUI: { storedChoice: (_key, _choices, fallback) => fallback, prefersCompact: () => false, store() {} },
    location: { hash: '#dashboard', protocol: 'https:' },
    matchMedia: () => ({ matches: false }),
    scrollTo() {},
  };
  const document = {
    cookie: 'rclone_sync_csrf=fixture', hidden: false, activeElement: null,
    getElementById: (id) => elements.get(id) || null,
    querySelector: () => null, querySelectorAll: () => [],
  };
  const history = {
    pushState(_state, _title, url) { window.location.hash = url; calls.navigation.push(['push', url]); },
    replaceState(_state, _title, url) { window.location.hash = url; calls.navigation.push(['replace', url]); },
  };
  const context = vm.createContext({
    window, document, history, navigator: { onLine: true }, console, URLSearchParams, Intl,
    AbortController, DOMException, ...clock,
    Date: class extends Date { constructor(...args) { super(...(args.length ? args : [now])); } static now() { return now; } },
    fetch: async () => { throw new Error('Unexpected transport'); },
  });
  for (const file of ['app-core.js', 'app-dashboard.js', 'app-pairs.js', 'app-jobs.js', 'app-system.js', 'app-recovery.js', 'app-settings.js', 'app.js']) {
    const source = fs.readFileSync(path.join(root, 'app/static', file), 'utf8')
      .replace(/^import[\s\S]*?from\s+['"][^'"]+['"];\r?$/gm, '')
      .replace(/\bexport const /g, 'const ');
    vm.runInContext(source, context, { filename: file });
  }
  const model = window.app();
  Object.assign(model, {
    $refs: {}, $nextTick: (callback) => callback(),
    showToast: (...args) => calls.toasts.push(args),
    requestConfirmation: async () => true,
    api: async (...args) => { calls.requests.push(args); return null; },
    configLoaded: true, configLoading: false,
  });
  return {
    model, calls, elements, window, document, context, timers,
    setNow(value) { now = value; },
    async advance(milliseconds) {
      const until = now + milliseconds;
      for (;;) {
        const item = [...timers].sort((a, b) => a[1].at - b[1].at).find(([, timer]) => timer.at <= until);
        if (!item) break;
        timers.delete(item[0]); now = item[1].at; item[1].callback();
        await Promise.resolve();
      }
      now = until;
    },
  };
}

test('Google setup requires its own OAuth client before starting login', async () => {
  const { model: m, calls } = harness();
  Object.assign(m.remoteSetup, { provider: 'drive', name: 'gdrive' });
  await m.submitRemoteSetup();
  assert.equal(calls.requests.length, 0);
  assert.match(m.remoteSetup.error, /Client-ID.*Client-Secret/);
  assert.equal(m.remoteSetup.busy, false);
});

test('Google authorization stays a draft until an explicit save', async () => {
  const { model: m } = harness();
  const requests = [];
  m.restoreDialogFocus = () => {};
  m.$refs.remoteAnswer = { focus() {} };
  Object.assign(m.remoteSetup, { show: true, provider: 'drive', name: 'gdrive', clientId: 'fixture-id', clientSecret: 'fixture-secret' });
  m.api = async (method, url, body) => {
    requests.push([method, url, plain(body)]);
    if (url.endsWith('/save')) return { ok: true, name: 'gdrive' };
    return { id: 'draft', name: 'gdrive', ready: url.endsWith('/answer'), step: 1, option: { default: '' } };
  };
  await m.submitRemoteSetup();
  assert.equal(requests[0][2].require_own_client, true);
  assert.equal(requests[0][2].client_id, 'fixture-id');
  assert.equal(requests[0][2].client_secret, 'fixture-secret');
  assert.equal(m.remoteSetup.clientSecret, '');
  m.remoteSetup.answer = 'fixture-callback';
  await m.submitRemoteSetup();
  assert.equal(requests.length, 2);
  assert.equal(m.remoteSetup.session.ready, true);
  assert.equal(m.remoteSetup.show, true);
  assert.equal(requests.some((request) => request[1].endsWith('/save')), false);
  await m.submitRemoteSetup();
  assert.equal(requests.length, 3);
  assert.equal(requests[2][1], '/api/remotes/setup/draft/save');
  assert.equal(m.remoteSetup.show, false);
});

test('Google reconnect retains server-side options and can use its stored own client', async () => {
  const { model: m } = harness();
  m.remoteSetup.driveReconnectSupported = true;
  m.selectDriveReconnect({ name: 'existing', has_own_client: true });
  let submitted;
  m.api = async (_method, _url, body) => { submitted = plain(body); return { id: 'draft', ready: true }; };
  await m.submitRemoteSetup();
  assert.equal(submitted.name, 'existing');
  assert.equal(submitted.reconnect, true);
  assert.equal(submitted.require_own_client, true);
  assert.equal(submitted.client_id, '');
  assert.equal(submitted.client_secret, null);
  for (const key of ['scope', 'root_folder_id', 'team_drive']) assert.equal(key in submitted, false, key);
  assert.equal(m.remoteSetup.session.ready, true);
});

test('a busy-run save refusal keeps the authenticated Google draft retryable', async () => {
  const { model: m } = harness();
  Object.assign(m.remoteSetup, { provider: 'drive', session: { id: 'draft', ready: true } });
  m.api = async () => ({ __error: true, detail: 'Sicherung läuft. Nach deren Abschluss erneut speichern.' });
  await m.submitRemoteSetup();
  assert.equal(m.remoteSetup.session.id, 'draft');
  assert.equal(m.remoteSetup.session.ready, true);
  assert.equal(m.remoteSetup.busy, false);
  assert.match(m.remoteSetup.error, /Sicherung läuft/);
});

test('changing provider and closing setup clear the OAuth client secret', async () => {
  const { model: m } = harness();
  Object.assign(m.remoteSetup, { provider: 'drive', reconnect: true, useStoredDriveClient: true, clientSecret: 'fixture' });
  m.selectRemotePreset('webdav');
  assert.equal(m.remoteSetup.reconnect, false);
  assert.equal(m.remoteSetup.useStoredDriveClient, false);
  assert.equal(m.remoteSetup.clientSecret, '');
  m.remoteSetup.clientSecret = 'fixture-again';
  m.restoreDialogFocus = () => {};
  await m.closeRemoteSetup();
  assert.equal(m.remoteSetup.clientSecret, '');
});

test('pending Google starts cannot be reassigned to another provider', async () => {
  const { model: m } = harness();
  const pending = deferred();
  Object.assign(m.remoteSetup, { provider: 'drive', name: 'gdrive', clientId: 'fixture-id', clientSecret: 'fixture-secret' });
  m.api = async () => pending.promise;
  const request = m.submitRemoteSetup();
  assert.equal(m.remoteSetup.busy, true);
  m.selectRemotePreset('webdav', 'nextcloud');
  assert.equal(m.remoteSetup.provider, 'drive');
  assert.equal(m.remoteSetup.name, 'gdrive');
  pending.resolve({ id: 'draft', ready: true });
  await request;
  assert.equal(m.remoteSetup.session.id, 'draft');
  m.selectRemotePreset('webdav');
  assert.equal(m.remoteSetup.provider, 'drive');
});

test('failed and interrupted Google starts clear the client secret and release busy state', async () => {
  for (const throws of [false, true]) {
    const { model: m } = harness();
    Object.assign(m.remoteSetup, { provider: 'drive', name: 'gdrive', clientId: 'fixture-id', clientSecret: 'fixture-secret' });
    m.api = async () => { if (throws) throw new Error('fixture'); return { __error: true, detail: 'fixture failure' }; };
    await m.submitRemoteSetup();
    assert.equal(m.remoteSetup.clientSecret, '');
    assert.equal(m.remoteSetup.busy, false);
    assert.ok(m.remoteSetup.error);
  }
});

test('same-page navigation preserves drafts and rejected browser navigation restores the URL', async () => {
  const { model: m, window, calls } = harness();
  m.page = 'pairs'; m.configDirty = true;
  let loads = 0;
  m.loadPage = () => { loads += 1; };
  m.closeNavigation = () => {};
  m.requestConfirmation = async () => false;
  await m.navigate('pairs');
  assert.equal(loads, 0);
  window.location.hash = '#dashboard';
  await m.navigate('dashboard', false);
  assert.equal(m.page, 'pairs');
  assert.equal(window.location.hash, '#pairs');
  assert.deepEqual(calls.navigation, [['replace', '#pairs']]);
});

test('the API deadline covers stalled JSON response bodies', async () => {
  const h = harness();
  const bodyStarted = deferred();
  const originalApi = vm.runInContext('coreMethods.api', h.context);
  h.context.fetch = async (_url, options) => ({
    ok: true, headers: { get: () => 'application/json' },
    json: () => new Promise((_resolve, reject) => {
      bodyStarted.resolve();
      const abort = () => reject(new DOMException('aborted', 'AbortError'));
      if (options.signal.aborted) abort();
      else options.signal.addEventListener('abort', abort, { once: true });
    }),
  });
  const pending = originalApi.call(h.model, 'GET', '/fixture', undefined, { timeoutMs: 100, captureError: true });
  await bodyStarted.promise;
  assert.equal(h.timers.size, 1, 'Deadline remains active during body reading');
  await h.advance(100);
  const result = await pending;
  assert.equal(result.__error, true);
  assert.match(result.detail, /Zeitüberschreitung/);
  assert.equal(h.timers.size, 0);
});

test('palette keyboard selection is scrolled into view and has an accessible DOM target', () => {
  const { model: m, elements } = harness();
  const scrolls = [];
  m.commandItems = () => [{ label: 'A' }, { label: 'B' }];
  elements.set('command-option-1', { scrollIntoView: (options) => scrolls.push(options.block) });
  m.moveCommandSelection(1);
  assert.equal(m.commandPalette.selectedIndex, 1);
  assert.deepEqual(scrolls, ['nearest']);
  const { document } = parseHTML(fs.readFileSync(path.join(root, 'app/static/index.html'), 'utf8'));
  const input = document.getElementById('command-palette-input');
  assert(input.getAttribute('aria-label'));
  assert.equal(input.getAttribute('role'), 'combobox');
  assert(document.getElementById(input.getAttribute('aria-controls')));
  assert.match(input.getAttribute(':aria-activedescendant'), /command-option-/);
});

test('palette focus has priority over an underlying remote setup', () => {
  const { model: m } = harness();
  m.commandPalette.show = true; m.remoteSetup.show = true;
  m.$refs.commandPaletteDialog = { id: 'palette' };
  m.$refs.remoteSetupDialog = { id: 'remote' };
  assert.equal(m.activeDialog().id, 'palette');
});

test('filter tests preserve filename whitespace and expire when either input changes', async () => {
  const { model: m, calls } = harness();
  m.filterFile.content = '- *.tmp';
  m.filterTester.pathsText = ' leading.txt\ntrailing.txt \n';
  m.api = async (_method, _url, body) => { calls.requests.push(body); return { results: [{ path: ' leading.txt', included: true }], parse_errors: [] }; };
  await m.runFilterTest();
  assert.deepEqual(plain(calls.requests[0].paths), [' leading.txt', 'trailing.txt ']);
  assert.equal(m.filterTestIsCurrent(), true);
  m.filterFile.content = '- *.txt';
  assert.equal(m.filterTestIsCurrent(), false);
  m.filterFile.content = '- *.tmp'; m.filterTester.pathsText += 'other.txt';
  assert.equal(m.filterTestIsCurrent(), false);
});

test('filter edits made during save remain dirty against the new revision', async () => {
  const { model: m } = harness();
  const wait = deferred();
  m.filterFile = { content: '- *.tmp', revision: 'old', dirty: true };
  m.api = async () => wait.promise;
  const saving = m.saveFilterFile();
  m.filterFile.content = '- *.cache';
  wait.resolve({ ok: true, revision: 'new', bytes: 7 });
  assert.equal(await saving, true);
  assert.equal(m.filterFile.content, '- *.cache');
  assert.equal(m.filterFile.revision, 'new');
  assert.equal(m.filterFile.dirty, true);
});

test('changing recovery search points drops old file selection', async () => {
  const { model: m } = harness();
  m.recovery.pointId = 'old'; m.recovery.selectedPaths = ['old.txt'];
  m.loadRecoveryBrowse = async () => {};
  await m.selectRecoverySearchResult({ point_id: 'new', path: 'new.txt' });
  assert.deepEqual(plain(m.recovery.selectedPaths), ['new.txt']);
});

test('changing recovery pair invalidates an already running search', async () => {
  const { model: m } = harness();
  const wait = deferred();
  m.recovery.pair = 'A'; m.recovery.searchQuery = 'dummy';
  m.api = async () => wait.promise;
  m.loadRecovery = async () => {};
  const searching = m.searchRecoveryFiles();
  await m.selectRecoveryPair('B');
  wait.resolve({ results: [{ point_id: 'A-point', path: 'dummy.txt' }] });
  await searching;
  assert.equal(m.recovery.searchLoading, false);
  assert.deepEqual(plain(m.recovery.searchResults), []);
});

test('a pending recovery hit selection cannot add its file to a different pair', async () => {
  const { model: m } = harness(); const wait = deferred();
  m.recovery.pair = 'A'; m.loadRecoveryBrowse = async () => wait.promise;
  const selecting = m.selectRecoverySearchResult({ point_id: 'A-point', path: 'old.txt' });
  m.loadRecovery = async () => {}; await m.selectRecoveryPair('B');
  wait.resolve(); await selecting;
  assert.deepEqual(plain(m.recovery.selectedPaths), []);
});

test('editing the query while recovery search is pending invalidates its reply', async () => {
  const { model: m } = harness(); const wait = deferred();
  m.recovery.pair = 'A'; m.recovery.searchQuery = 'old'; m.api = async () => wait.promise;
  const searching = m.searchRecoveryFiles(); m.recovery.searchQuery = 'new';
  wait.resolve({ results: [{ point_id: 'A-point', path: 'old.txt' }] }); await searching;
  assert.equal(m.recovery.searchLoading, false);
  assert.deepEqual(plain(m.recovery.searchResults), []);
});

test('PBS draft paths are included in validation and diff payloads', () => {
  const { model: m } = harness();
  m.config.pbs.targets = [{ paths: ['/old'], pathsText: '/new\n/next\n' }];
  const payload = m.configPayload();
  assert.deepEqual(plain(payload.pbs.targets[0].paths), ['/new', '/next']);
  assert.equal('pathsText' in payload.pbs.targets[0], false);
  assert.deepEqual(plain(m.config.pbs.targets[0].paths), ['/old']);
});

test('failed discard reload keeps the existing draft dirty without a success toast', async () => {
  const { model: m, calls } = harness();
  m.configDirty = true; m.configDiffOpen = true;
  m.loadConfig = async () => false;
  await m.discardConfigChanges();
  assert.equal(m.configDirty, true);
  assert.equal(m.configDiffOpen, true);
  assert.equal(calls.toasts.length, 0);
});

test('validation responses never certify a newer draft', async () => {
  const { model: m, calls } = harness();
  const wait = deferred(); m.api = async () => wait.promise;
  const checking = m.validateConfigDraft();
  m.markConfigDirty();
  wait.resolve({ ok: true, warnings: [] });
  assert.equal(await checking, false);
  assert.equal(m.configValidation.ok, null);
  assert.equal(m.configValidation.loading, false);
  assert.equal(calls.toasts.length, 0);
});

test('missing bytes produce no coverage ratio while measured zero remains valid', () => {
  const { model: m } = harness();
  assert.equal(m.copyDelta({ source_size: { bytes: null }, target_size: { bytes: 10 } }), null);
  assert.equal(m.copyDelta({ source_size: { bytes: 10 }, target_size: { bytes: null } }), null);
  assert.equal(m.copyDelta({ source_size: { bytes: 10 }, target_size: { bytes: 0 } }).coverage, 0);
});

test('recovery and scrub jobs are busy and cancellable in the proper scope', () => {
  const { model: m } = harness(); const scopes = [];
  m.cancelPbs = () => scopes.push('pbs'); m.cancelBackup = () => scopes.push('backup');
  m.status.recovery = { id: 1 };
  assert.equal(m.busy(), true); assert.equal(m.runningKind(), 'Wiederherstellung');
  assert.equal(m.canCancelCurrentJob(), true); m.cancelCurrentJob();
  m.status.recovery = null; m.status.bitrotscrub = { id: 2 };
  assert.equal(m.canCancelCurrentJob(), true); m.cancelCurrentJob();
  m.status.bitrotscrub = null; m.status.bootdrill = { id: 3 };
  assert.equal(m.canCancelCurrentJob(), true); m.cancelCurrentJob();
  assert.deepEqual(scopes, ['backup', 'backup', 'pbs']);
});

test('system bundle partial failures keep valid data and do not clear their error', async () => {
  const { model: m } = harness();
  const old = { sources: [{ name: 'A' }] }; m.copies.data = old;
  m.api = async () => ({ copies: { error: 'DUMMY_COPY_FAILURE' }, doctor: { checks: [] }, vpn: { wireguard: {} } });
  await m.loadSystemBundle();
  assert.equal(m.copies.data, old);
  assert.equal(m.copies.error, 'DUMMY_COPY_FAILURE');
  assert.match(m.systemBundleErrors[0], /DUMMY_COPY_FAILURE/);
  assert.deepEqual(plain(m.doctor.data), { checks: [] });
});

test('system page relies on the bundled doctor and VPN instead of duplicating probes', async () => {
  const { model: m } = harness(); let bundles = 0;
  m.loadConfig = async () => true;
  m.loadSystemBundle = () => { bundles += 1; };
  m.loadCloudQuota = () => {};
  m.loadDoctor = m.loadVpn = () => { throw new Error('duplicate probe'); };
  await m.loadPage('doctor');
  assert.equal(bundles, 1);
});

test('pause until tomorrow uses the configured timezone across spring DST', () => {
  const { model: m } = harness(); let until;
  m.config.backup.timezone = 'Europe/Berlin';
  m.pauseScheduler = (_minutes, timestamp) => { until = timestamp; };
  m.pauseSchedulerUntilTomorrow();
  assert.equal(until, Date.UTC(2026, 2, 29, 4) / 1000);
});

test('timeline includes frequent jobs for 24 hours with matching axis labels and duration contract', async () => {
  const { model: m, calls } = harness();
  const start = Date.UTC(2026, 2, 28, 12) / 1000;
  m.config.backup.jobs = [{ id: 'A', name: 'A', enabled: true, schedule: '* * * * *' }];
  m.config.backup.timezone = 'Europe/Berlin';
  m.api = async (_method, url, body) => {
    if (url.includes('durations')) return { durations: { A: { avg_duration_sec: 120 } } };
    calls.requests.push(body);
    return { next_runs: Array.from({ length: 1441 }, (_, index) => ({ timestamp: start + index * 60 })) };
  };
  await m.loadJobScheduleTimeline();
  assert.equal(calls.requests[0].count, 1441);
  assert.equal(m.jobTimeline.rows[0].runs.length, 1440);
  assert.equal(m.jobTimeline.rows[0].avgDurationSec, 120);
  assert.equal(m.timelineHourLabel(0), '13:00');
  assert.equal(m.timelineHourPosition(6), 25);
});

test('an old timeline cannot reintroduce a removed job', async () => {
  const { model: m } = harness(); const wait = deferred();
  m.config.backup.jobs = [{ id: 'A', name: 'A', enabled: true, schedule: '0 3 * * *' }];
  m.api = async (_method, url) => url.includes('durations') ? { durations: {} } : wait.promise;
  const old = m.loadJobScheduleTimeline();
  m.config.backup.jobs = [];
  await m.loadJobScheduleTimeline();
  wait.resolve({ next_runs: [] }); await old;
  assert.deepEqual(plain(m.jobTimeline.rows), []);
});

test('wizard preserves zero delete limit and assigns bisync archives to actual endpoint order', () => {
  const { model: m } = harness();
  m.pairWizard = { name: 'A', source: '/local', target: 'cloud:/remote', twoWay: true, mode: 'bisync', maxDelete: 0, allowDelete: true };
  m.applyPairWizard();
  const pair = m.config.backup.pairs[0];
  assert.equal(pair.max_delete, 0);
  assert.equal(pair.backup_dir1, 'cloud:/remote-versions/{date}');
  assert.equal(pair.backup_dir2, '/local-versions/{date}');
  const draft = { direction: 'bisync', local: '/local', remote: 'cloud:/remote' };
  m.applyBackupDirSuggestion(draft);
  assert.equal(draft.backup_dir1, pair.backup_dir1);
  assert.equal(draft.backup_dir2, pair.backup_dir2);
});

test('removing a data path cleans job references and preserves jobs with remaining paths', async () => {
  const { model: m } = harness();
  m.config.backup.pairs = [{ id: 'A', name: 'A' }, { id: 'B', name: 'B' }];
  m.config.backup.jobs = [{ name: 'only-A', data_path_ids: ['A'] }, { name: 'shared', data_path_ids: ['A', 'B'] }];
  await m.removePair(0);
  assert.deepEqual(plain(m.config.backup.jobs), [{ name: 'shared', data_path_ids: ['B'] }]);
});

test('PBS conversion also removes obsolete data-path job references', () => {
  const { model: m } = harness();
  m.config.backup.pairs = [{ id: 'A', name: 'A', source: '/source' }];
  m.config.backup.jobs = [{ name: 'only-A', data_path_ids: ['A'] }];
  m.convertPairToPbs(0);
  assert.equal(m.config.pbs.targets.length, 1);
  assert.deepEqual(plain(m.config.backup.jobs), []);
});

test('quick direction reversal keeps the cloud endpoint out of the local field', () => {
  const { model: m } = harness();
  m.quick = { local: '/local', remote: 'cloud:/remote', direction: 'push', mode: 'copy' };
  m.swapQuickPaths();
  assert.equal(m.quick.local, '/local'); assert.equal(m.quick.remote, 'cloud:/remote');
  assert.equal(m.quick.direction, 'pull');
});

test('pair-test replies follow the tested pair rather than its former row index', async () => {
  const { model: m } = harness(); const wait = deferred();
  const a = { id: 'A', name: 'A', local: '/a', remote: 'cloud:/a' };
  const b = { id: 'B', name: 'B', local: '/b', remote: 'cloud:/b' };
  m.config.backup.pairs = [a, b]; m.api = async () => wait.promise;
  const testing = m.testPair(0);
  m.movePair(0, 1);
  wait.resolve({ ok: true, message: 'A verified' }); await testing;
  assert.equal(m.pairTestResult(b), undefined);
  assert.equal(m.pairTestResult(a).message, 'A verified');
  a.local = '/changed';
  assert.equal(m.pairTestResult(a), undefined);
});

test('bulk actions preserve edits made while the server request was pending', async () => {
  const { model: m } = harness(); const wait = deferred();
  m.config._revision = 'old'; m.config.backup.pairs = [{ id: 'A', name: 'A', enabled: true }];
  m.pairSelection = { A: true }; m.api = async () => wait.promise; m.loadOverview = () => {};
  const changing = m.bulkPairAction('disable');
  m.config.backup.pairs[0].name = 'new draft name'; m.markConfigDirty();
  wait.resolve({ ok: true, changed: 1, config: { _revision: 'new', backup: { pairs: [{ id: 'A', name: 'A', enabled: false }] } } });
  await changing;
  assert.equal(m.config.backup.pairs[0].name, 'new draft name');
  assert.equal(m.config._revision, 'new'); assert.equal(m.configDirty, true);
});

test('a rejected bulk action cannot reload over a newer draft', async () => {
  const { model: m } = harness(); const wait = deferred(); let reloads = 0;
  m.config._revision = 'old'; m.pairSelection = { A: true };
  m.api = async () => wait.promise; m.loadConfig = async () => { reloads += 1; };
  const changing = m.bulkPairAction('disable');
  m.config.backup.pairs.push({ name: 'new draft' }); m.markConfigDirty();
  wait.resolve({ __error: true, status: 409, detail: 'Parallel geändert' });
  await changing;
  assert.equal(reloads, 0);
  assert.equal(m.config.backup.pairs[0].name, 'new draft');
  assert.equal(m.configDirty, true);
});

test('deselecting a snapshot invalidates an in-flight diff response', async () => {
  const { model: m } = harness(); const wait = deferred();
  m.snapshots.restoreName = 'A'; m.api = async () => wait.promise;
  const comparing = m.loadSnapshotDiff();
  m.snapshots.restoreName = ''; await m.loadSnapshotDiff();
  wait.resolve({ changes: ['old result'] }); await comparing;
  assert.equal(m.snapshots.diff.data, null);
});

test('HTTP-successful log-prune errors are displayed as errors', async () => {
  const { model: m, calls } = harness();
  m.api = async () => ({ ok: false, error: 'DUMMY_ACTIVE_LOGS_UNKNOWN', deleted: 0 });
  await m.pruneLogs(false);
  assert.deepEqual(calls.toasts, [['DUMMY_ACTIVE_LOGS_UNKNOWN', 'err']]);
});

test('productive preflight requires the original revision and never routes quicksync as configured backup', async () => {
  const { model: m, calls } = harness();
  m.preflight = { kind: 'quicksync', data: { pairs: [{ name: 'quick' }], config_revision: 'old' }, excluded: {} };
  await m.startPreflightRun();
  assert.equal(calls.requests.length, 0);
  m.preflight = { kind: 'backup', data: { pairs: [{ name: 'A' }], config_revision: 'original' }, excluded: {}, show: true };
  m.api = async (...args) => { calls.requests.push(args); return { ok: true }; };
  await m.startPreflightRun();
  assert.equal(calls.requests[0][2].expected_revision, 'original');
});

test('retry preserves dry-run mode and explicitly describes it in confirmation', async () => {
  const { model: m, calls } = harness(); let message;
  m.requestConfirmation = async (text) => { message = text; return true; };
  const job = { id: 1, kind: 'backup', status: 'error', dry_run: true, definition_id: 'A', config_revision: 'r' };
  await m.retryJob(job);
  assert.match(calls.requests[0][1], /dry_run=true$/);
  assert.match(message, /Dry-Run/);
});

test('overview refresh retains measured storage rows', async () => {
  const { model: m } = harness();
  const rows = [{ name: 'A', source_size: { bytes: 123 } }];
  m.overview.data = { storage_pairs: rows };
  m.api = async () => ({ pairs: { health: [] } });
  await m.loadOverview(true);
  assert.equal(m.storagePairs(), rows);
});

test('PBS failures do not produce a green completion toast', async () => {
  const { model: m, calls } = harness();
  m.pbs.status = { running: true, running_job: { id: 4 } };
  m.status.pbs = { id: 4 };
  m.api = async (_method, url) => url === '/api/pbs/status' ? { running: false } : { id: 4, status: 'error' };
  m.loadRecent = m.loadOverview = () => {};
  await m.loadPbsStatus(true);
  assert.match(calls.toasts[0][0], /fehlgeschlagen/);
  assert.equal(calls.toasts[0][1], 'warn');
});

test('reachable crypt remotes are only verified after actual decryption', async () => {
  const { model: m } = harness();
  m.crypt.result = { name: 'encrypted' };
  m.api = async () => ({ ok: true, verified: false, message: 'Erreichbar; ohne Datei ungeprüft.' });
  await m.verifyCrypt();
  assert.equal(m.crypt.verify.ok, true);
  assert.equal(m.crypt.verify.verified, false);
  assert.equal(m.crypt.verify.text, 'Erreichbar; ohne Datei ungeprüft.');
  m.api = async () => ({ ok: true, verified: true, message: 'Entschlüsselung geprüft.' });
  await m.verifyCrypt();
  assert.equal(m.crypt.verify.verified, true);
});

test('desktop settings tabs support vertical navigation and mobile tabs horizontal navigation', () => {
  const { model: m, window } = harness(); let prevented = 0;
  m.refreshSchedulePreview = () => {};
  m.settingsTab = 'general';
  m.settingsTabKey({ key: 'ArrowDown', preventDefault() { prevented += 1; } });
  assert.equal(m.settingsTab, 'scheduler');
  window.matchMedia = () => ({ matches: true });
  m.settingsTabKey({ key: 'ArrowRight', preventDefault() { prevented += 1; } });
  assert.equal(m.settingsTab, 'security'); assert.equal(prevented, 2);
});

test('status text colors meet normal-text contrast on their light status surfaces', () => {
  const css = fs.readFileSync(path.join(root, 'app/static/style.css'), 'utf8');
  const variables = Object.fromEntries([...css.match(/:root\s*\{([\s\S]*?)\n\}/)[1].matchAll(/--([\w-]+):\s*(#[0-9a-f]{6})/gi)].map((match) => [match[1], match[2]]));
  const luminance = (hex) => {
    const channels = hex.slice(1).match(/../g).map((part) => parseInt(part, 16) / 255).map((v) => v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  };
  for (const name of ['ok', 'running', 'warning', 'danger']) {
    const values = [luminance(variables[name]), luminance(variables[name + '-soft'])].sort((a, b) => a - b);
    assert((values[1] + 0.05) / (values[0] + 0.05) >= 4.5, name + ' contrast');
  }
  assert.match(css, /\.toast\.err/);
  assert.match(css, /\.command-palette-results button\.active/);
});

test('G-01: stacked remoteSetup sits above picker, Escape closes only the top layer and restores focus cleanly', async () => {
  const css = fs.readFileSync(path.join(root, 'app/static/style.css'), 'utf8');
  assert.match(css, /\.modal\.remote-setup-modal[^{]*\{\s*z-index:\s*105\s*;/);
  const { document: domDoc } = parseHTML(fs.readFileSync(path.join(root, 'app/static/index.html'), 'utf8'));
  const remoteModal = domDoc.querySelector('.modal.remote-setup-modal');
  assert(remoteModal, 'remoteSetup modal has elevated stacking class');
  assert(remoteModal.querySelector('.remote-setup-dialog'));

  const { model: m, document, window, context } = harness();
  m.requestConfirmation = vm.runInContext('coreMethods.requestConfirmation', context);
  window.getComputedStyle = () => ({ display: 'block', visibility: 'visible' });
  const focusLog = [];
  const pageTrigger = { id: 'page-cloud-btn', isConnected: true, getClientRects: () => [{}], focus: () => focusLog.push('pageTrigger') };
  const pickerRemoteBtn = { id: 'picker-new-remote-btn', isConnected: true, getClientRects: () => [{}], focus: () => focusLog.push('pickerRemoteBtn') };
  const pickerFallbackBtn = { id: 'picker-fallback-btn', isConnected: true, offsetParent: {}, getClientRects: () => [{}], getAttribute: () => null, focus: () => focusLog.push('pickerFallbackBtn') };
  const pickerDialog = {
    id: 'picker-dialog',
    isConnected: true,
    getClientRects: () => [{}],
    focus() {},
    contains: (node) => node === pickerRemoteBtn || node === pickerFallbackBtn,
    querySelector: () => null,
    querySelectorAll: () => [pickerFallbackBtn],
  };
  const remoteDialog = { id: 'remote-dialog', isConnected: true, getClientRects: () => [{}], focus() {}, contains: () => false, querySelector: () => null, querySelectorAll: () => [] };
  const confirmDialog = { id: 'confirm-dialog', isConnected: true, getClientRects: () => [{}], focus() {}, contains: () => false, querySelector: () => null, querySelectorAll: () => [] };
  m.$refs.pickerDialog = pickerDialog;
  m.$refs.remoteSetupDialog = remoteDialog;
  m.$refs.confirmationDialog = confirmDialog;
  m.api = async (_method, url) => {
    if (url.startsWith('/api/browse/')) return { entries: [], parent: null, path: '', is_root: true };
    if (url === '/api/remotes/providers') return { providers: [{ name: 'drive', description: 'Google Drive' }] };
    return { ok: true };
  };

  document.activeElement = pageTrigger;
  m.openPicker('target-remote', 0);
  assert.equal(m.picker.show, true);
  assert.equal(m.activeDialog(), pickerDialog);

  document.activeElement = pickerRemoteBtn;
  await m.openRemoteSetup();
  assert.equal(m.remoteSetup.show, true);
  assert.equal(m.picker.show, true);
  assert.equal(m.activeDialog(), remoteDialog);

  // Confirmation dialog opened above remoteSetup takes top priority and closes first on Escape
  const pendingConfirm = m.requestConfirmation('Abbrechen?');
  assert.equal(m.activeDialog(), confirmDialog);
  m.closeOverlays();
  assert.equal(await pendingConfirm, false);
  assert.equal(m.remoteSetup.show, true);
  assert.equal(m.picker.show, true);

  // First Escape on remoteSetup closes ONLY remoteSetup and restores focus inside picker
  await m.closeOverlays();
  assert.equal(m.remoteSetup.show, false);
  assert.equal(m.picker.show, true);
  assert.equal(focusLog.at(-1), 'pickerRemoteBtn');

  // If the trigger inside picker was disconnected, focus falls back inside picker (never outside)
  document.activeElement = pickerRemoteBtn;
  await m.openRemoteSetup();
  pickerRemoteBtn.isConnected = false;
  await m.closeRemoteSetup();
  assert.equal(m.picker.show, true);
  assert.equal(focusLog.at(-1), 'pickerFallbackBtn');

  // Next Escape closes picker and restores focus to the page trigger
  m.closeOverlays();
  assert.equal(m.picker.show, false);
  assert.equal(focusLog.at(-1), 'pageTrigger');
});

test('G-03 & U-04: System nav active states, explicit workspace groups, wizard picker, flat job disclosures and PBS copy', () => {
  const html = fs.readFileSync(path.join(root, 'app/static/index.html'), 'utf8');
  const { document, window } = parseHTML(html);

  const desktopSystem = [...document.querySelectorAll('.main-nav button')].find((btn) => btn.textContent.trim() === 'System');
  const mobileSystem = [...document.querySelectorAll('.mobile-nav button')].find((btn) => btn.textContent.trim() === 'System');
  for (const btn of [desktopSystem, mobileSystem]) {
    assert(btn, 'System nav button exists');
    const cls = btn.getAttribute(':class');
    const cur = btn.getAttribute(':aria-current');
    for (const subpage of ['doctor', 'recovery', 'settings']) {
      assert(cls.includes(subpage), `active class includes ${subpage}`);
      assert(cur.includes(subpage), `aria-current includes ${subpage}`);
    }
  }

  for (const sectionName of ['doctor', 'recovery']) {
    const section = [...document.querySelectorAll('main > section[x-show]')].find((s) => s.getAttribute('x-show') === `page==='${sectionName}'`);
    const panels = [...section.querySelectorAll(':scope > article.panel, :scope > .metric-grid, :scope > .recovery-secondary-grid > article.panel')];
    assert(panels.length >= 4);
    for (const panel of panels) assert(panel.getAttribute('data-workspace-group'), `${sectionName} panel has explicit data-workspace-group`);
  }

  const wizard = document.querySelector('[aria-labelledby="pair-wizard-heading"]');
  const wizardClicks = [...wizard.querySelectorAll('button')].map((btn) => btn.getAttribute('@click') || '');
  assert(wizardClicks.some((v) => v.includes("openPicker('wizard-source-local'")));
  assert(wizardClicks.some((v) => v.includes("openPicker('wizard-source-remote'")));
  assert(wizardClicks.some((v) => v.includes("openPicker('wizard-target-local'")));
  assert(wizardClicks.some((v) => v.includes("openPicker('wizard-target-remote'")));
  assert(wizardClicks.filter((v) => v.includes('openRemoteSetup(')).length >= 2);

  const pbsPanel = document.getElementById('settings-panel-pbs');
  assert(!pbsPanel.textContent.includes('Seite „Pairs"'));
  assert(pbsPanel.textContent.includes('unter Datenwege'));
  assert(pbsPanel.querySelector('button[\\@click="navigate(\'pairs\')"]'));

  window.app = () => ({ page: 'dashboard', init() {}, navigate() {} });
  vm.runInNewContext(fs.readFileSync(path.join(root, 'app/static/workspace.js'), 'utf8'), { window, document, console });
  const jobTemplate = document.querySelector('.definition-list > template');
  assert(jobTemplate.content.querySelector('.workspace-disclosure .definition-assignments'));
  assert.equal(jobTemplate.content.querySelector('.workspace-disclosure details'), null, 'No nested double <details> in job definition row');

  const { model: m } = harness();
  m.openPairWizard();
  m.picker = { show: true, mode: 'wizard-source-local', idx: -2 };
  m.pickPath('/mnt/nas/quelle');
  assert.equal(m.pairWizard.source, '/mnt/nas/quelle');
  m.picker = { show: true, mode: 'wizard-target-remote', idx: -2 };
  assert.equal(m.pickerCanSwitchKind(), true);
  m.pickPath('gdrive:/Backup/Ziel');
  assert.equal(m.pairWizard.target, 'gdrive:/Backup/Ziel');
});

test('G-04..G-07: WCAG contrast tokens, security.html palette, webauthn theme sync, non-overlapping bars, reflow and inert bindings', () => {
  const luminance = (hex) => {
    const channels = hex.slice(1).match(/../g).map((part) => parseInt(part, 16) / 255).map((v) => v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  };
  const contrast = (a, b) => {
    const [lo, hi] = [luminance(a), luminance(b)].sort((x, y) => x - y);
    return (hi + 0.05) / (lo + 0.05);
  };

  const css = fs.readFileSync(path.join(root, 'app/static/style.css'), 'utf8');
  const lightVars = Object.fromEntries([...css.match(/:root\s*\{([\s\S]*?)\r?\n\}/)[1].matchAll(/--([\w-]+):\s*(#[0-9a-f]{6})/gi)].map((m) => [m[1], m[2]]));
  const darkVars = Object.fromEntries([...css.match(/html\[data-theme="dark"\]\s*\{([\s\S]*?)\r?\n\}/)[1].matchAll(/--([\w-]+):\s*(#[0-9a-f]{6})/gi)].map((m) => [m[1], m[2]]));
  assert(contrast(lightVars['text-faint'], lightVars['surface-3']) >= 5.4, 'Light --text-faint contrast >= 5.4:1 on --surface-3');
  assert(contrast(lightVars['border-strong'], lightVars.surface) >= 3.0, 'Light --border-strong contrast >= 3:1');
  assert(contrast(darkVars['border-strong'], darkVars['surface-2']) >= 3.1, 'Dark --border-strong contrast >= 3.1:1');
  assert.match(css, /\.summary-json,\s*\.live-log\s*\{\s*height:\s*min\(430px,\s*45dvh\)/);
  assert.match(css, /\.unsaved-bar\.above-progress/);

  const workspaceCss = fs.readFileSync(path.join(root, 'app/static/workspace.css'), 'utf8');
  assert.match(workspaceCss, /@media\s*\(min-width:\s*961px\)/);
  assert.match(workspaceCss, /@media\s*\(max-width:\s*960px\)/);

  const securityHtml = fs.readFileSync(path.join(root, 'app/static/security.html'), 'utf8');
  const secDarkVars = Object.fromEntries([...securityHtml.match(/html\[data-theme="dark"\]\s*\{([\s\S]*?)\n    \}/)[1].matchAll(/--([\w-]+):\s*(#[0-9a-f]{6})/gi)].map((m) => [m[1], m[2]]));
  assert(contrast(secDarkVars.primary, secDarkVars['on-primary']) >= 4.5, 'security.html dark primary button contrast >= 4.5:1');

  for (const htmlFile of ['index.html', 'login.html', 'security.html']) {
    const { document } = parseHTML(fs.readFileSync(path.join(root, 'app/static', htmlFile), 'utf8'));
    for (const scriptTag of document.querySelectorAll('script')) {
      assert(scriptTag.getAttribute('src'), `${htmlFile} must not contain inline <script> tags`);
    }
  }

  const { document: indexDoc } = parseHTML(fs.readFileSync(path.join(root, 'app/static/index.html'), 'utf8'));
  assert.equal(indexDoc.querySelector('.sidebar').getAttribute(':inert'), 'Boolean(activeDialog())');
  assert.equal(indexDoc.querySelector('.workspace').getAttribute(':inert'), 'navigationIsModal() || Boolean(activeDialog())');
  assert.match(indexDoc.querySelector('.unsaved-bar').getAttribute(':class'), /above-progress/);

  const { document: secDoc, window: secWin } = parseHTML(securityHtml);
  secWin.localStorage = { getItem: (key) => (key === 'rclone-sync-theme' ? 'dark' : null) };
  const webauthnCtx = vm.createContext({
    window: secWin,
    document: secDoc,
    Headers,
    URLSearchParams,
    fetch: async () => ({ ok: true, json: async () => ({ credentials: [] }) }),
  });
  vm.runInContext(fs.readFileSync(path.join(root, 'app/static/webauthn.js'), 'utf8'), webauthnCtx, { filename: 'webauthn.js' });
  assert.equal(secDoc.documentElement.dataset.theme, 'dark');
});

test('U-03: pair advanced protection fields and zfs_snapshot direction guard', () => {
  const { document } = parseHTML(fs.readFileSync(path.join(root, 'app/static/index.html'), 'utf8'));
  const pairTpl = document.querySelector('.pair-card-list > template').content;
  assert(pairTpl.querySelector('input[x-model="pair.recovery_snapshots"]'));
  assert(pairTpl.querySelector('input[x-model\\.trim="pair.failure_domain"]'));
  assert(pairTpl.querySelector('input[x-model\\.trim="pair.airgap_device"]'));
  const zfsInput = pairTpl.querySelector('input[x-model="pair.zfs_snapshot"]');
  assert(zfsInput);
  assert.equal(zfsInput.getAttribute(':disabled'), "pair.direction !== 'push'");

  const { model: m } = harness();
  const pair = {
    name: 'ZFS-Test',
    source: '/tank/data',
    target: 'cloud:/backup',
    local: '/tank/data',
    remote: 'cloud:/backup',
    direction: 'push',
    mode: 'copy',
    two_way: false,
    recovery_snapshots: true,
    failure_domain: 'cloud-eu',
    location_id: 'legacy-conflict',
    airgap_device: '/dev/disk/by-uuid/1234',
    zfs_snapshot: true,
  };
  m.normalizePair(pair);
  assert.equal(pair.zfs_snapshot, true);

  // Switching to bisync immediately clears zfs_snapshot
  pair.two_way = true;
  m.syncPairDirection(pair);
  assert.equal(pair.direction, 'bisync');
  assert.equal(pair.zfs_snapshot, false);

  // Switching back to push, enabling zfs_snapshot, then switching endpoints to pull clears zfs_snapshot
  pair.two_way = false;
  m.syncPairDirection(pair);
  assert.equal(pair.direction, 'push');
  pair.zfs_snapshot = true;
  pair.source = 'cloud:/backup';
  pair.target = '/tank/data';
  m.syncPairDirection(pair);
  assert.equal(pair.direction, 'pull');
  assert.equal(pair.zfs_snapshot, false);

  // Even if zfs_snapshot was forced on a non-push pair in memory, configPayload() clears it and removes legacy location_id
  pair.zfs_snapshot = true;
  m.config.backup.pairs = [pair];
  const payloadPair = m.configPayload().backup.pairs[0];
  assert.equal(payloadPair.direction, 'pull');
  assert.equal(payloadPair.zfs_snapshot, false);
  assert.equal(payloadPair.failure_domain, 'cloud-eu');
  assert.equal('location_id' in payloadPair, false);
});

test('U-02 & U-08: handover preview/import, manual canary verify, and time-travel file history wire to exact backend routes', async () => {
  const { model: m } = harness();
  const requests = [];
  m.config.backup.pairs = [{ id: 'p1', name: 'Fotos' }];
  m.requestCurrentPassword = async () => 'reauth-secret';
  m.loadVaultLibrary = async () => { requests.push(['RELOAD_VAULT']); };
  m.api = async (method, url, body) => {
    requests.push([method, url, body ? plain(body) : undefined]);
    if (url === '/api/recovery/handover/preview') {
      return { hostname: 'alt-srv', created_at: 1700000000, total: 3, groups: [{ key: 'vault-fotos', name: 'Fotos', count: 3 }] };
    }
    if (url === '/api/recovery/handover/import') {
      return { imported: 3, already_present: 1 };
    }
    if (url.endsWith('/verify') && url.startsWith('/api/security/canary/')) {
      return { pair: 'Fotos & Archiv', ok: true, canary: { configured: true, ok: true, canary_file: '.canary' }, extension_scan: { blocked: false, suspect_count: 0 } };
    }
    if (url.startsWith('/api/recovery/timetravel/history?')) {
      return { versions: [{ point_id: '2026-03-01', label: '01.03.2026', size: 512, modified_at: 1700000000 }] };
    }
    return { ok: true };
  };

  // Handover preview & import
  m.recovery.handoverImport.envelopeText = JSON.stringify({ schema: 'rclone-recovery-handover-v1', ciphertext_b64: 'abc' });
  m.recovery.handoverImport.passphrase = 'sichere-passphrase-123';
  await m.previewRecoveryHandover();
  assert.equal(requests[0][0], 'POST');
  assert.equal(requests[0][1], '/api/recovery/handover/preview');
  assert.equal(m.recovery.handoverImport.mappings['vault-fotos'], 'Fotos');

  await m.importRecoveryHandover();
  assert.equal(requests[1][0], 'POST');
  assert.equal(requests[1][1], '/api/recovery/handover/import');
  assert.deepEqual(requests[1][2].mappings, { 'vault-fotos': 'Fotos' });
  assert.equal(requests[1][2].current_password, 'reauth-secret');
  assert.equal(m.recovery.handoverImport.result.imported, 3);
  assert.deepEqual(requests[2], ['RELOAD_VAULT']);

  // Manual canary verification
  m.ransomwareStatus.data = { pairs: [{ name: 'Fotos & Archiv', canary: { configured: true, ok: false } }] };
  await m.verifyPairCanary('Fotos & Archiv');
  const canaryReq = requests.find((r) => String(r[1]).startsWith('/api/security/canary/'));
  assert(canaryReq);
  assert.equal(canaryReq[0], 'POST');
  assert.equal(canaryReq[1], '/api/security/canary/Fotos%20%26%20Archiv/verify');
  assert.equal(m.canaryVerify['Fotos & Archiv'].ok, true);
  assert.match(m.canaryVerifySummary('Fotos & Archiv'), /Canary OK/);

  // Time-travel file history uses identity=...&path=...
  m.recovery.pair = 'Fotos';
  await m.loadTimeTravelHistory({ path: 'urlaub/notiz.txt' });
  const historyReq = requests.find((r) => String(r[1]).startsWith('/api/recovery/timetravel/history?'));
  assert(historyReq);
  assert.equal(historyReq[0], 'GET');
  assert.equal(historyReq[1], '/api/recovery/timetravel/history?identity=Fotos&path=urlaub%2Fnotiz.txt');
  assert.equal(m.recovery.fileHistory.versions.length, 1);
  assert.equal(m.recovery.fileHistory.versions[0].point_id, '2026-03-01');
});

test('U-05 & U-06: lockdown WebAuthn passkey step-up and dry-run preflight matrix shortcut', async () => {
  const h = harness();
  const { model: m, window } = h;
  const requests = [];
  window.atob = (b64) => Buffer.from(b64, 'base64').toString('binary');
  window.btoa = (bin) => Buffer.from(bin, 'binary').toString('base64');
  window.PublicKeyCredential = function PublicKeyCredential() {};
  h.context.navigator.credentials = {
    get: async () => ({
      id: 'cred-1',
      rawId: Uint8Array.from([1, 2, 3]).buffer,
      type: 'public-key',
      authenticatorAttachment: 'platform',
      response: {
        authenticatorData: Uint8Array.from([4, 5]).buffer,
        clientDataJSON: Uint8Array.from([6, 7]).buffer,
        signature: Uint8Array.from([8, 9]).buffer,
        userHandle: null,
      },
    }),
  };

  m.api = async (method, url, body) => {
    requests.push([method, url, body ? plain(body) : undefined]);
    if (url === '/api/webauthn/status') return { enabled: true, passkey: true, security_key: false };
    if (url === '/api/webauthn/reauth/options') {
      return { challenge_id: 'challenge-12345678901234567890', publicKey: { challenge: 'AQIDBA', allowCredentials: [] } };
    }
    if (url === '/api/webauthn/reauth/verify') return { ok: true, reauth_token: 'webauthn-stepup-token' };
    if (url === '/api/lockdown') return { active: true };
    if (url.startsWith('/api/jobs/backup/run?dry_run=true')) return { ok: true, job_id: 77 };
    if (url === '/api/jobs/77/preflight') return { config_revision: 'rev1', pairs: [{ name: 'Fotos', direction: 'push', mode: 'copy', matrix: { copies: [{ path: 'a.jpg', size_bytes: 10 }] } }] };
    return { ok: true };
  };

  m.openLockdown();
  await m.loadWebAuthnStatus();
  assert.equal(m.hasWebAuthnCredentials(), true);
  m.lockdownDialog.reason = 'Verdachtsfall';
  await m.triggerLockdownWithWebAuthn();
  const verifyCall = requests.find((r) => r[1] === '/api/webauthn/reauth/verify');
  assert.equal(verifyCall[2].challenge_id, 'challenge-12345678901234567890');
  const lockdownCall = requests.find((r) => r[1] === '/api/lockdown');
  assert.equal(lockdownCall[2].password, 'webauthn-stepup-token');
  assert.equal(lockdownCall[2].reason, 'Verdachtsfall');

  // Dry-run records lastDryRun and openPreflightMatrix opens preflight matrix once completed
  await m.runBackup(true);
  assert.equal(m.lastDryRun.id, 77);
  m.recentJobs = [{ id: 77, kind: 'backup', dry_run: true, status: 'ok' }];
  await m.openPreflightMatrix();
  assert.equal(m.preflight.show, true);
  assert.equal(m.preflight.jobId, 77);
  assert.equal(m.preflight.data.pairs[0].rows.length, 1);
});
