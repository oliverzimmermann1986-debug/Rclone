const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, '../../ios/RcloneMobile/StorePreviewData.json'), 'utf8'));

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function openRecovery(page) {
  const config = structuredClone(fixture.config);
  config._revision = 'a'.repeat(64);
  const snapshots = [
    { name: 'config-first.yaml', sha256: '1'.repeat(64), size: 128, mtime: 1900000000 },
    { name: 'config-second.yaml', sha256: '2'.repeat(64), size: 256, mtime: 1900001000 },
  ];
  const snapshotWrites = [];
  const drillWrites = [];
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  let drillHandler = null;
  let jobs = [];
  let status = { backup: null, check: null, quicksync: null, restoretest: null, pbs: null };
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    let data = {};
    if (url.pathname === '/api/config') data = config;
    else if (url.pathname === '/api/maintenance/config/snapshots') data = { snapshots, max_snapshots: 30 };
    else if (url.pathname.endsWith('/snapshots/restore')) {
      snapshotWrites.push(request.postDataJSON());
      return route.fulfill({ status: 409, json: { detail: 'Snapshot wurde zwischenzeitlich geändert.' } });
    } else if (url.pathname === '/api/jobs/backup/restore-test') {
      drillWrites.push(request.url());
      if (drillHandler) return drillHandler(route);
      data = { ok: true, job_id: 123 };
    } else if (url.pathname === '/api/diagnostics/overview') data = fixture.overview;
    else if (url.pathname === '/api/storage/overview') data = fixture.storage;
    else if (url.pathname === '/api/jobs/list') data = jobs;
    else if (url.pathname === '/api/jobs/search') data = { items: jobs, total: jobs.length };
    else if (url.pathname === '/api/jobs/status/current') data = status;
    else if (url.pathname === '/api/jobs/progress/stream') return route.fulfill({ status: 404, body: '' });
    else if (url.pathname === '/api/jobs/progress') data = { running: false };
    else if (url.pathname.includes('/scheduler')) data = { enabled: true, paused: false };
    else if (url.pathname === '/api/diagnostics/copies') data = { totals: { sources: 0 }, sources: [] };
    return route.fulfill({ json: data });
  });
  await page.goto('/#doctor');
  const section = page.locator('.snapshot-restore');
  await expect(section.getByRole('combobox')).toBeVisible();
  await section.getByRole('combobox').selectOption('config-first.yaml');
  await section.getByLabel('Aktuelles Passwort').fill('test-only-current-password');
  const snapshotButton = section.getByRole('button', { name: 'Wiederherstellen', exact: true });
  const startButton = page.locator('.panel').filter({
    has: page.getByRole('heading', { name: 'Kopien je Datenbestand', exact: true }),
  }).getByRole('button', { name: /Restore-Drill starten|Start wird geprüft/ });
  return {
    section, snapshotButton, startButton, snapshots, config, snapshotWrites, drillWrites, errors,
    setDrillHandler: (handler) => { drillHandler = handler; },
    setJobs: (value) => { jobs = value; },
    setStatus: (value) => { status = value; },
  };
}

async function startAndConfirm(page, button) {
  await button.click();
  await page.getByRole('dialog').getByRole('button', { name: 'Restore-Drill starten', exact: true }).click();
}

test('snapshot confirmation shows its name and freezes the checked payload', async ({ page }) => {
  const { snapshotButton, snapshotWrites, config, snapshots, errors } = await openRecovery(page);
  const releaseConfig = deferred();
  const configStarted = deferred();
  const releaseList = deferred();
  const listStarted = deferred();
  await page.route('**/api/config', async (route) => {
    configStarted.resolve();
    await releaseConfig.promise;
    await route.fulfill({ json: config });
  });
  await page.route('**/api/maintenance/config/snapshots', async (route) => {
    listStarted.resolve();
    await releaseList.promise;
    await route.fulfill({ json: { snapshots: structuredClone(snapshots) } });
  });
  await page.evaluate(() => {
    const state = Alpine.$data(document.body);
    state.loadConfig(true);
    state.loadSnapshots();
  });
  await Promise.all([configStarted.promise, listStarted.promise]);
  await snapshotButton.click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('config-first.yaml');
  releaseConfig.resolve();
  releaseList.resolve();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect.poll(() => page.evaluate(() => Alpine.$data(document.body).snapshots.loading)).toBe(false);
  await dialog.getByRole('button', { name: 'Wiederherstellen', exact: true }).click();
  await expect.poll(() => snapshotWrites.length).toBe(1);
  expect(snapshotWrites).toEqual([{
    name: 'config-first.yaml', sha256: '1'.repeat(64), expected_revision: 'a'.repeat(64),
    current_password: 'test-only-current-password',
  }]);
  expect(errors).toEqual([]);
});

for (const mutation of ['config revision', 'selected hash', 'selected snapshot']) {
  test(`a late ${mutation} change cancels snapshot consent without a POST`, async ({ page }) => {
    const { snapshotButton, snapshotWrites, config, snapshots } = await openRecovery(page);
    const started = deferred();
    const release = deferred();
    const endpoint = mutation === 'config revision' ? '**/api/config' : '**/api/maintenance/config/snapshots';
    await page.route(endpoint, async (route) => {
      started.resolve();
      await release.promise;
      if (mutation === 'config revision') config._revision = 'b'.repeat(64);
      else if (mutation === 'selected hash') snapshots[0].sha256 = '3'.repeat(64);
      await route.fulfill({ json: mutation === 'config revision' ? config : { snapshots } });
    });
    await page.evaluate((configChange) => {
      const state = Alpine.$data(document.body);
      if (configChange) state.loadConfig(true);
      else state.loadSnapshots();
    }, mutation === 'config revision');
    await started.promise;
    await snapshotButton.click();
    await expect(page.getByRole('dialog')).toBeVisible();
    if (mutation === 'selected snapshot') {
      await page.evaluate(() => { Alpine.$data(document.body).snapshots.restoreName = 'config-second.yaml'; });
    }
    release.resolve();
    await expect(page.getByRole('dialog')).toBeHidden();
    await expect(page.getByText('Auswahl oder Konfigurationsstand geändert. Bitte erneut prüfen und bestätigen.', { exact: true })).toBeVisible();
    expect(snapshotWrites).toHaveLength(0);
    await expect(snapshotButton).toBeEnabled();
  });
}

test('history navigation discards snapshot consent even when returning to the same page', async ({ page }) => {
  const { snapshotButton, snapshotWrites } = await openRecovery(page);
  await snapshotButton.click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.evaluate(() => { window.location.hash = 'runs'; });
  await expect(page.getByRole('dialog')).toBeHidden();
  await page.evaluate(() => { window.location.hash = 'doctor'; });
  await expect(snapshotButton).toBeVisible();
  expect(snapshotWrites).toHaveLength(0);
  await expect(snapshotButton).toBeEnabled();
});

test('a snapshot without a known hash cannot ask for consent or POST', async ({ page }) => {
  const { snapshotButton, snapshotWrites } = await openRecovery(page);
  await page.evaluate(() => { Alpine.$data(document.body).snapshots.items[0].sha256 = null; });
  await snapshotButton.click();
  await expect(page.getByRole('dialog')).toBeHidden();
  await expect(page.getByText('Snapshot-Prüfsumme oder Konfigurationsstand fehlt. Bitte neu laden und erneut auswählen.', { exact: true })).toBeVisible();
  expect(snapshotWrites).toHaveLength(0);
});

test('an authentication change discards snapshot consent without a POST', async ({ page }) => {
  const { snapshotButton, snapshotWrites } = await openRecovery(page);
  await snapshotButton.click();
  await page.evaluate(() => { document.cookie = 'rclone_sync_csrf=changed-session; path=/'; });
  await page.getByRole('dialog').getByRole('button', { name: 'Wiederherstellen', exact: true }).click();
  await expect(page.getByText('Auswahl oder Konfigurationsstand geändert. Bitte erneut prüfen und bestätigen.', { exact: true })).toBeVisible();
  expect(snapshotWrites).toHaveLength(0);
});

test('restore-drill consent and dispatch each exclude a second start, and cancellation releases the guard', async ({ page }) => {
  const { startButton, drillWrites, setDrillHandler } = await openRecovery(page);
  await startButton.click();
  await page.evaluate(() => { Alpine.$data(document.body).runRestoreTest('second'); });
  await expect(page.getByRole('dialog')).toContainText('alle aktiven Pairs');
  await page.getByRole('dialog').getByRole('button', { name: 'Abbrechen', exact: true }).click();
  await expect(startButton).toBeEnabled();
  expect(drillWrites).toHaveLength(0);
  const started = deferred();
  const release = deferred();
  setDrillHandler(async (route) => {
    started.resolve();
    await release.promise;
    await route.fulfill({ json: { ok: true, job_id: 123 } });
  });
  await startAndConfirm(page, startButton);
  await started.promise;
  await page.evaluate(() => { Alpine.$data(document.body).runRestoreTest('second'); });
  await expect(page.getByRole('dialog')).toBeHidden();
  await expect(startButton).toBeDisabled();
  expect(drillWrites).toHaveLength(1);
  release.resolve();
  await expect(startButton).toBeEnabled();
});

test('an accepted completed drill with a lost response stays blocked until status review and separate consent', async ({ page }) => {
  const { startButton, drillWrites, setDrillHandler, setJobs, errors } = await openRecovery(page);
  setDrillHandler(async (route) => {
    setJobs([{ id: 123, kind: 'restoretest', status: 'ok', started_at: 1900000000, finished_at: 1900000020 }]);
    await route.abort('failed');
  });
  await startAndConfirm(page, startButton);
  const alert = page.getByRole('alert').filter({ hasText: 'Startstatus unbekannt' });
  await expect(alert).toBeVisible();
  await page.evaluate(() => { Alpine.$data(document.body).runRestoreTest(); });
  expect(drillWrites).toHaveLength(1);
  await expect(startButton).toBeDisabled();
  await page.reload();
  await expect(alert).toBeVisible();
  await expect(page.getByRole('button', { name: 'Restore-Drill starten', exact: true })).toBeDisabled();
  await alert.getByRole('button', { name: 'Aufträge prüfen' }).click();
  await expect(page).toHaveURL(/#runs$/);
  await expect(page.getByRole('button', { name: '#123' })).toBeVisible();
  const retry = alert.getByRole('button', { name: 'Weiteren Drill starten', exact: true });
  await expect(retry).toBeVisible();
  await retry.click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('Der erste Auftrag kann bereits abgeschlossen sein.');
  await dialog.getByRole('button', { name: 'Abbrechen', exact: true }).click();
  await expect(alert).toBeVisible();
  expect(drillWrites).toHaveLength(1);
  setDrillHandler(null);
  await retry.click();
  await dialog.getByRole('button', { name: 'Bewusst erneut starten' }).click();
  await expect(alert).toBeHidden();
  expect(drillWrites).toHaveLength(2);
  expect(errors).toEqual([]);
});

test('unknown start review cannot unlock a new drill while one is running or status is unavailable', async ({ page }) => {
  const { startButton, drillWrites, setDrillHandler, setStatus } = await openRecovery(page);
  setDrillHandler((route) => route.abort('failed'));
  await startAndConfirm(page, startButton);
  const alert = page.getByRole('alert').filter({ hasText: 'Startstatus unbekannt' });
  await expect(alert).toBeVisible();
  await page.route('**/api/jobs/status/current', (route) => route.abort('failed'));
  await alert.getByRole('button', { name: 'Aufträge prüfen' }).click();
  await expect(alert).toContainText('Aufträge konnten nicht zuverlässig geprüft werden.');
  await expect(alert.getByRole('button', { name: 'Weiteren Drill starten' })).toBeHidden();
  await page.unroute('**/api/jobs/status/current');
  setStatus({ backup: null, check: null, quicksync: null, restoretest: { id: 123 }, pbs: null });
  await alert.getByRole('button', { name: 'Aufträge prüfen' }).click();
  await expect(alert).toContainText('Ein Restore-Drill läuft.');
  await expect(alert.getByRole('button', { name: 'Weiteren Drill starten' })).toBeHidden();
  expect(drillWrites).toHaveLength(1);
});

test('a definite HTTP rejection releases the restore-drill start guard', async ({ page }) => {
  const { startButton, drillWrites, setDrillHandler } = await openRecovery(page);
  setDrillHandler((route) => route.fulfill({ status: 409, json: { detail: 'Ein Auftrag läuft bereits.' } }));
  await startAndConfirm(page, startButton);
  await expect(page.getByText('Restore-Drill nicht gestartet: Ein Auftrag läuft bereits.', { exact: true })).toBeVisible();
  await expect(startButton).toBeEnabled();
  await expect(page.getByRole('alert').filter({ hasText: 'Startstatus unbekannt' })).toBeHidden();
  expect(drillWrites).toHaveLength(1);
});

for (const status of [408, 499]) {
  test(`an accepted drill followed by HTTP ${status} keeps its uncertain-start guard`, async ({ page }) => {
    const { startButton, drillWrites, setDrillHandler, setJobs } = await openRecovery(page);
    setDrillHandler((route) => {
      setJobs([{ id: 123, kind: 'restoretest', status: 'ok', started_at: 1900000000, finished_at: 1900000020 }]);
      return route.fulfill({ status, json: { detail: 'Proxy did not deliver the start response.' } });
    });
    await startAndConfirm(page, startButton);
    const alert = page.getByRole('alert').filter({ hasText: 'Startstatus unbekannt' });
    await expect(alert).toBeVisible();
    await expect(startButton).toBeDisabled();
    await page.evaluate(() => { Alpine.$data(document.body).runRestoreTest(); });
    expect(drillWrites).toHaveLength(1);
    await page.reload();
    await expect(alert).toBeVisible();
    await expect(page.getByRole('button', { name: 'Restore-Drill starten', exact: true })).toBeDisabled();
    expect(drillWrites).toHaveLength(1);
  });
}

for (const firstResolved of ['current-status', 'jobs']) {
  test(`a compound review cannot overwrite newer ${firstResolved} data after its other response arrives`, async ({ page }) => {
    const { startButton, drillWrites, setDrillHandler, setStatus, setJobs, errors } = await openRecovery(page);
    setDrillHandler((route) => route.abort('failed'));
    await startAndConfirm(page, startButton);
    const alert = page.getByRole('alert').filter({ hasText: 'Startstatus unbekannt' });
    await expect(alert).toBeVisible();
    await page.evaluate((key) => {
      const state = Alpine.$data(document.body);
      state.stopPolling();
      // Distinguish navigation's page read from the fixed-size review read,
      // even if the navigation request is aborted before route interception.
      state.jobs.limit = 10;
      const api = state.api.bind(state);
      state.api = (method, url, body, options = {}) => {
        const request = api(method, url, body, options);
        if (options.captureError && options.requestKey === key) {
          return request.then((result) => { window.reviewFastResponseSettled = true; return result; });
        }
        return request;
      };
    }, firstResolved);
    const slowStarted = deferred();
    const releaseSlow = deferred();
    if (firstResolved === 'current-status') {
      await page.route('**/api/jobs/search?*', async (route) => {
        if (new URL(route.request().url()).searchParams.get('limit') !== '25') return route.fallback();
        slowStarted.resolve();
        await releaseSlow.promise;
        await route.fulfill({ json: { items: [], total: 0 } });
      });
    } else {
      await page.route('**/api/jobs/status/current', async (route) => {
        slowStarted.resolve();
        await releaseSlow.promise;
        await route.fulfill({ json: { backup: null, check: null, quicksync: null, restoretest: null, pbs: null } });
      });
    }
    await alert.getByRole('button', { name: 'Aufträge prüfen' }).click();
    await slowStarted.promise;
    await expect.poll(() => page.evaluate(() => window.reviewFastResponseSettled === true)).toBe(true);
    if (firstResolved === 'current-status') {
      setStatus({ backup: null, check: null, quicksync: null, restoretest: { id: 456 }, pbs: null });
      await page.evaluate(() => Alpine.$data(document.body).refreshStatus(true));
      await expect.poll(() => page.evaluate(() => Alpine.$data(document.body).status.restoretest?.id)).toBe(456);
    } else {
      setJobs([{ id: 456, kind: 'restoretest', status: 'running', started_at: 1900000100 }]);
      await page.evaluate(() => Alpine.$data(document.body).loadJobs(true));
      await expect(page.getByRole('button', { name: /#456/ })).toBeVisible();
    }
    releaseSlow.resolve();
    await expect(alert).toContainText('Status oder Aufträge wurden inzwischen aktualisiert. Bitte erneut prüfen.');
    await expect(alert.getByRole('button', { name: 'Weiteren Drill starten' })).toBeHidden();
    if (firstResolved === 'current-status') {
      expect(await page.evaluate(() => Alpine.$data(document.body).status.restoretest?.id)).toBe(456);
    } else {
      await expect(page.getByRole('button', { name: /#456/ })).toBeVisible();
    }
    await page.evaluate(() => { Alpine.$data(document.body).retryRestoreTestStart(); });
    await expect(page.getByRole('dialog')).toBeHidden();
    expect(drillWrites).toHaveLength(1);
    expect(errors).toEqual([]);
  });
}

test('logout invalidates pending recovery consent and clears the password', async ({ page }) => {
  const { section, snapshotButton, snapshotWrites } = await openRecovery(page);
  await page.route('**/logout', (route) => route.fulfill({ status: 503, json: { detail: 'Test logout unavailable' } }));
  await snapshotButton.click();
  await page.evaluate(() => { Alpine.$data(document.body).logout(); });
  await expect(page.getByRole('dialog')).toBeHidden();
  await expect(section.getByLabel('Aktuelles Passwort', { exact: true })).toHaveValue('');
  expect(snapshotWrites).toHaveLength(0);
  await expect(snapshotButton).toBeDisabled();
});
