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
