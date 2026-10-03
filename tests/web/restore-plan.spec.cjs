const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, '../../ios/RcloneMobile/StorePreviewData.json'), 'utf8'));

async function openPlan(page, { previewDelay = 0, conflict = false } = {}) {
  const writes = [];
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const config = structuredClone(fixture.config);
  config._revision = 'a'.repeat(64);
  let settings = { enabled: true, schedule: '0 5 * * 0', sample_files: 20, max_total_mb: 256, max_scan_files: 20000 };
  const response = () => ({
    ok: true, revision: config._revision, settings, timezone: 'Europe/Berlin',
    generated_at: 1900000000, next_runs: [1900007200, 1900266400], due_now: false,
    data_paths: [{ id: 'photos', name: 'Fotos', evidence_state: 'never', valid_until: null, coverage_gap: true }],
    warnings: ['Fotos hat noch keinen aktuellen Restore-Nachweis.'],
  });
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    let data = {};
    if (url.pathname === '/api/recovery/restore-plan') {
      if (request.method() === 'PUT') {
        writes.push(request.postDataJSON());
        if (conflict) return route.fulfill({ status: 409, json: { detail: 'Konfiguration wurde parallel geändert. Plan neu laden.' } });
        settings = request.postDataJSON().settings; config._revision = 'b'.repeat(64);
      }
      data = response();
    } else if (url.pathname.endsWith('/restore-plan/preview')) {
      const draft = request.postDataJSON().settings;
      if (previewDelay) await new Promise((resolve) => setTimeout(resolve, previewDelay));
      if (draft.schedule === '0 5 31 2 *') return route.fulfill({ status: 422, json: { detail: 'Zeitplan hat keinen erreichbaren Prüftermin' } });
      data = { ...response(), settings: draft };
    } else if (url.pathname === '/api/config') data = config;
    else if (url.pathname === '/api/diagnostics/overview') data = fixture.overview;
    else if (url.pathname === '/api/storage/overview') data = fixture.storage;
    else if (url.pathname === '/api/jobs/list') data = fixture.jobs;
    else if (url.pathname === '/api/jobs/progress/stream') return route.fulfill({ status: 404, body: '' });
    else if (url.pathname === '/api/jobs/progress') data = { running: false };
    else if (url.pathname === '/api/jobs/status/current') data = { backup: null, restoretest: null };
    else if (url.pathname === '/api/config/filter-file') data = { content: '', revision: 'filters' };
    else if (url.pathname === '/api/config/schedule-preview') data = { enabled: false, next_runs: [] };
    else if (url.pathname.includes('/scheduler')) data = { enabled: true, paused: false };
    return route.fulfill({ json: data });
  });
  await page.goto('/#settings');
  await page.locator('#settings-tab-scheduler').click();
  const section = page.locator('section[aria-labelledby="restore-plan-title"]');
  await expect(section.getByLabel('Rhythmus')).toHaveValue('weekly');
  return { section, writes, errors, response, config };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function previewTwiceWeekly(section) {
  await section.getByLabel('Rhythmus').selectOption('twice');
  await section.getByRole('button', { name: 'Vorschau prüfen' }).click();
  const save = section.getByRole('button', { name: 'Prüfplan übernehmen', exact: true });
  await expect(save).toBeEnabled();
  return save;
}

async function confirmPlan(page, save) {
  await save.click();
  await page.getByRole('dialog').getByRole('button', { name: 'Prüfplan übernehmen', exact: true }).click();
}

test('preview and confirmation save only the restore plan', async ({ page }, testInfo) => {
  const { section, writes, errors } = await openPlan(page);
  await section.getByLabel('Rhythmus').selectOption('twice');
  const save = section.getByRole('button', { name: 'Prüfplan übernehmen', exact: true });
  await expect(save).toBeDisabled();
  await section.getByRole('button', { name: 'Vorschau prüfen' }).click();
  await expect(save).toBeEnabled();
  await expect(section).toContainText('Fotos hat noch keinen aktuellen Restore-Nachweis.');
  const overflow = await section.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const main = element.closest('.scheduler-main').getBoundingClientRect();
    if (bounds.right > main.right + 1) return true;
    return Array.from(element.querySelectorAll('fieldset, input, select, button')).some((child) => {
      const box = child.getBoundingClientRect();
      return box.width > 0 && (box.left < bounds.left - 1 || box.right > bounds.right + 1);
    });
  });
  expect(overflow).toBe(false);
  const screenshot = testInfo.outputPath('restore-plan-preview.png');
  await section.screenshot({ path: screenshot, style: '.topbar, .mobile-nav, .skip-link { visibility: hidden !important; }' });
  await testInfo.attach('restore-plan-preview', { path: screenshot, contentType: 'image/png' });
  await save.click();
  await page.getByRole('dialog').getByRole('button', { name: 'Abbrechen', exact: true }).click();
  expect(writes).toHaveLength(0);
  await save.click();
  await page.getByRole('dialog').getByRole('button', { name: 'Prüfplan übernehmen', exact: true }).click();
  await expect(page.getByText('Restore-Prüfplan gespeichert', { exact: true })).toBeVisible();
  expect(writes).toEqual([{ revision: 'a'.repeat(64), settings: {
    enabled: true, schedule: '0 5 * * 0,3', sample_files: 20, max_total_mb: 256, max_scan_files: 20000,
  } }]);
  expect(errors).toEqual([]);
});

test('an unreachable schedule blocks saving and shows recovery', async ({ page }) => {
  const { section, writes } = await openPlan(page);
  await section.getByLabel('Rhythmus').selectOption('custom');
  await section.getByLabel('Zeitplan (Cron)').fill('0 5 31 2 *');
  await section.getByRole('button', { name: 'Vorschau prüfen' }).click();
  await expect(section.getByRole('alert')).toHaveText('Zeitplan hat keinen erreichbaren Prüftermin');
  await expect(section.getByRole('button', { name: 'Prüfplan übernehmen', exact: true })).toBeDisabled();
  expect(writes).toHaveLength(0);
});

test('old previews cannot enable saving a changed draft; conflicts retain the draft', async ({ page }) => {
  const { section, writes } = await openPlan(page, { previewDelay: 250, conflict: true });
  await section.getByLabel('Rhythmus').selectOption('twice');
  await section.getByRole('button', { name: 'Vorschau prüfen' }).click();
  await section.getByLabel('Dateien je Datenweg').fill('30');
  await expect(section.getByRole('button', { name: 'Prüfplan übernehmen', exact: true })).toBeDisabled();
  await section.getByRole('button', { name: 'Vorschau prüfen' }).click();
  const save = section.getByRole('button', { name: 'Prüfplan übernehmen', exact: true });
  await expect(save).toBeEnabled();
  await save.click();
  await page.getByRole('dialog').getByRole('button', { name: 'Prüfplan übernehmen', exact: true }).click();
  await expect(section.getByRole('alert')).toContainText('parallel geändert');
  await expect(section.getByLabel('Dateien je Datenweg')).toHaveValue('30');
  await expect(save).toBeDisabled();
  expect(writes).toHaveLength(1);
});

test('reloading evidence discards an older preview even when settings and revision match', async ({ page }) => {
  const { section, response } = await openPlan(page);
  let releasePreview;
  const release = new Promise((resolve) => { releasePreview = resolve; });
  let previewStarted;
  const started = new Promise((resolve) => { previewStarted = resolve; });
  await page.route('**/api/recovery/restore-plan/preview', async (route) => {
    const obsolete = { ...response(), warnings: ['Veraltete Vorschau'] };
    previewStarted();
    await release;
    await route.fulfill({ json: obsolete });
  });
  await section.getByRole('button', { name: 'Vorschau prüfen' }).click();
  await started;
  await page.route('**/api/recovery/restore-plan', (route) => route.fulfill({
    json: { ...response(), warnings: ['Neuer Prüfsummenfehler nach der Vorschau'] },
  }));
  await section.getByRole('button', { name: 'Plan neu laden' }).click();
  await expect(section).toContainText('Neuer Prüfsummenfehler nach der Vorschau');
  const finished = page.waitForResponse('**/api/recovery/restore-plan/preview');
  releasePreview();
  await finished;
  await page.evaluate(() => new Promise(requestAnimationFrame));
  await expect(section).toContainText('Neuer Prüfsummenfehler nach der Vorschau');
  await expect(section).not.toContainText('Veraltete Vorschau');
});

test('dates use server timezone and an expired proof stays invalid despite the browser clock', async ({ page }) => {
  const { section, response } = await openPlan(page);
  await page.route('**/api/recovery/restore-plan', (route) => route.fulfill({
    json: { ...response(), generated_at: 1791007200, next_runs: [1791003600], data_paths: [{
      id: 'photos', name: 'Fotos', evidence_state: 'passed', valid_until: 1791007199, coverage_gap: true,
    }] },
  }));
  await section.getByRole('button', { name: 'Plan neu laden' }).click();
  // Browser is explicitly UTC, server is Europe/Berlin (UTC+2 in October).
  await expect(section).toContainText('03.10.2026, 07:00');
  await expect(section).toContainText('Kein aktuell gültiger Nachweis');
  await expect(section).not.toContainText('Nachweis gültig bis');
});

test('a failed save retains the draft and retries only after a fresh preview', async ({ page }) => {
  const { section, writes, response, errors } = await openPlan(page);
  await page.route('**/api/recovery/restore-plan', async (route) => {
    if (route.request().method() !== 'PUT') return route.fallback();
    const request = route.request().postDataJSON();
    writes.push(request);
    if (writes.length === 1) {
      return route.fulfill({ status: 500, json: { detail: 'Der Server konnte den Prüfplan gerade nicht speichern.' } });
    }
    return route.fulfill({ json: { ...response(), revision: 'b'.repeat(64), settings: request.settings } });
  });
  await section.getByLabel('Rhythmus').selectOption('twice');
  await section.getByLabel('Dateien je Datenweg').fill('31');
  await section.getByLabel('Datenlimit je Datenweg (MiB)').fill('384');
  const save = section.getByRole('button', { name: 'Prüfplan übernehmen', exact: true });
  await section.getByRole('button', { name: 'Vorschau prüfen' }).click();
  await expect(save).toBeEnabled();
  await confirmPlan(page, save);
  await expect(section.getByRole('alert')).toHaveText('Der Server konnte den Prüfplan gerade nicht speichern.');
  await expect(section.getByLabel('Rhythmus')).toHaveValue('twice');
  await expect(section.getByLabel('Dateien je Datenweg')).toHaveValue('31');
  await expect(section.getByLabel('Datenlimit je Datenweg (MiB)')).toHaveValue('384');
  await expect(save).toBeDisabled();
  expect(writes).toHaveLength(1);

  await section.getByRole('button', { name: 'Vorschau prüfen' }).click();
  await expect(section.getByRole('alert')).toBeHidden();
  await expect(save).toBeEnabled();
  await confirmPlan(page, save);
  await expect(page.getByText('Restore-Prüfplan gespeichert', { exact: true })).toBeVisible();
  expect(writes).toHaveLength(2);
  expect(writes[1]).toEqual(writes[0]);
  expect(writes[1]).toEqual({ revision: 'a'.repeat(64), settings: {
    enabled: true, schedule: '0 5 * * 0,3', sample_files: 31, max_total_mb: 384, max_scan_files: 20000,
  } });
  expect(errors).toEqual([]);
});

test('general edits made during a plan save survive and use its new revision when saved later', async ({ page }) => {
  const { section, writes, response, config, errors } = await openPlan(page);
  const started = deferred();
  const release = deferred();
  const configWrites = [];
  await page.route('**/api/recovery/restore-plan', async (route) => {
    if (route.request().method() !== 'PUT') return route.fallback();
    const request = route.request().postDataJSON();
    writes.push(request);
    started.resolve();
    await release.promise;
    config._revision = 'b'.repeat(64);
    config.backup.restore_test = structuredClone(request.settings);
    return route.fulfill({ json: { ...response(), settings: request.settings } });
  });
  await page.route('**/api/config', async (route) => {
    if (route.request().method() !== 'PUT') return route.fallback();
    const request = route.request().postDataJSON();
    configWrites.push(request);
    Object.assign(config, request.config, { _revision: 'c'.repeat(64) });
    return route.fulfill({ json: { ok: true, config, warnings: [] } });
  });
  const save = await previewTwiceWeekly(section);
  await confirmPlan(page, save);
  await started.promise;
  await page.locator('#settings-tab-general').click();
  const timezone = page.locator('#settings-panel-general').getByLabel('Zeitzone');
  await timezone.fill('Europe/Paris');
  await expect(page.locator('.save-state')).toContainText('Ungespeicherte Änderungen');
  expect(configWrites).toHaveLength(0);
  release.resolve();
  await expect(page.getByText('Restore-Prüfplan gespeichert', { exact: true })).toBeVisible();
  await expect(timezone).toHaveValue('Europe/Paris');
  await expect(page.locator('.save-state')).toContainText('Ungespeicherte Änderungen');

  await page.getByRole('button', { name: 'Änderungen speichern', exact: true }).click();
  await expect(page.getByText('Einstellungen gespeichert', { exact: true })).toBeVisible();
  expect(writes).toHaveLength(1);
  expect(configWrites).toHaveLength(1);
  expect(configWrites[0].config._revision).toBe('b'.repeat(64));
  expect(configWrites[0].config.backup.timezone).toBe('Europe/Paris');
  expect(configWrites[0].config.backup.restore_test).toEqual(writes[0].settings);
  expect(errors).toEqual([]);
});

test('an unsaved scheduler edit blocks plan confirmation without losing either draft', async ({ page }) => {
  const { section, writes, errors } = await openPlan(page);
  const save = await previewTwiceWeekly(section);
  const automaticBackups = page.getByLabel('Automatische Zeitpläne grundsätzlich aktivieren');
  await automaticBackups.uncheck();
  await expect(page.locator('.save-state')).toContainText('Ungespeicherte Änderungen');
  await save.click();
  await expect(page.getByText('Offene Konfigurationsänderungen zuerst speichern oder verwerfen.', { exact: true })).toBeVisible();
  await expect(page.getByRole('dialog')).toBeHidden();
  await expect(automaticBackups).not.toBeChecked();
  await expect(section.getByLabel('Rhythmus')).toHaveValue('twice');
  expect(writes).toHaveLength(0);
  expect(errors).toEqual([]);
});

test('rapid repeated confirmation starts one plan write and locks editing until it finishes', async ({ page }) => {
  const { section, writes, response, errors } = await openPlan(page);
  const started = deferred();
  const release = deferred();
  await page.route('**/api/recovery/restore-plan', async (route) => {
    if (route.request().method() !== 'PUT') return route.fallback();
    const request = route.request().postDataJSON();
    writes.push(request);
    started.resolve();
    await release.promise;
    return route.fulfill({ json: { ...response(), revision: 'b'.repeat(64), settings: request.settings } });
  });
  const save = await previewTwiceWeekly(section);
  await save.click();
  await page.getByRole('dialog').getByRole('button', { name: 'Prüfplan übernehmen', exact: true }).dblclick();
  await started.promise;
  await expect(section.getByRole('button', { name: 'Speichert…', exact: true })).toBeDisabled();
  await expect(section.getByLabel('Rhythmus')).toBeDisabled();
  await expect(section.getByRole('button', { name: 'Plan neu laden' })).toBeDisabled();
  expect(writes).toHaveLength(1);
  release.resolve();
  await expect(page.getByText('Restore-Prüfplan gespeichert', { exact: true })).toBeVisible();
  await expect(section.getByLabel('Rhythmus')).toBeEnabled();
  expect(writes).toHaveLength(1);
  expect(errors).toEqual([]);
});

test('an obsolete preview error cannot replace evidence loaded with the same revision', async ({ page }) => {
  const { section, response, writes, errors } = await openPlan(page);
  const started = deferred();
  const release = deferred();
  await page.route('**/api/recovery/restore-plan/preview', async (route) => {
    started.resolve();
    await release.promise;
    return route.fulfill({ status: 422, json: { detail: 'Veralteter Fehler aus der vorherigen Vorschau' } });
  });
  await section.getByRole('button', { name: 'Vorschau prüfen' }).click();
  await started.promise;
  await page.route('**/api/recovery/restore-plan', (route) => route.fulfill({
    json: { ...response(), warnings: ['Aktueller Stand nach erfolgreicher Stichprobe'], data_paths: [{
      id: 'photos', name: 'Fotos', evidence_state: 'passed', valid_until: 1900604800, coverage_gap: false,
    }] },
  }));
  await section.getByRole('button', { name: 'Plan neu laden' }).click();
  await expect(section).toContainText('Aktueller Stand nach erfolgreicher Stichprobe');
  await expect(section).toContainText('Nachweis gültig bis');
  const finished = page.waitForResponse('**/api/recovery/restore-plan/preview');
  release.resolve();
  await (await finished).finished();
  await page.evaluate(() => new Promise(requestAnimationFrame));
  await expect(section.getByRole('alert')).toBeHidden();
  await expect(section).not.toContainText('Veralteter Fehler aus der vorherigen Vorschau');
  await expect(section).toContainText('Aktueller Stand nach erfolgreicher Stichprobe');
  await expect(section.getByRole('button', { name: 'Prüfplan übernehmen', exact: true })).toBeEnabled();
  expect(writes).toHaveLength(0);
  expect(errors).toEqual([]);
});
