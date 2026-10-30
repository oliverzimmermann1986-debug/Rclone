const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, '../../ios/RcloneMobile/StorePreviewData.json'), 'utf8'));
const revision = 'a'.repeat(64);
const confirmedPassword = 'test-only-confirmed-password';

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

// A small valid PDF exercises authenticated binary transport and browser download.
// The production report renderer has separate content and visual-layout tests.
function transportPDF() {
  const content = 'BT /F1 18 Tf 50 780 Td (Sicherpfad protection report) Tj ET';
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    `<< /Length ${content.length} >>\nstream\n${content}\nendstream`,
  ];
  let pdf = '%PDF-1.4\n';
  const offsets = [0];
  for (let index = 0; index < objects.length; index += 1) {
    offsets.push(Buffer.byteLength(pdf));
    pdf += `${index + 1} 0 obj\n${objects[index]}\nendobj\n`;
  }
  const xref = Buffer.byteLength(pdf);
  pdf += `xref\n0 ${offsets.length}\n0000000000 65535 f \n`;
  pdf += offsets.slice(1).map((offset) => `${String(offset).padStart(10, '0')} 00000 n \n`).join('');
  pdf += `trailer\n<< /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(pdf);
}

function weeklyReport() {
  return {
    id: 12, week_start: '2026-10-19', week_end: '2026-10-26',
    period_label: '19.10.2026 bis 25.10.2026', timezone: 'Europe/Berlin', generated_at: 1793361600,
    summary: {
      jobs: { total: 30, successful: 27, failed: 1, warnings: 2, cancelled: 0, dry_runs: 3 },
      protection: { configured_paths: 3, paths_with_success: 3, restore_tests: 4, verified_restore_tests: 3, valid_restore_paths: 2 },
      storage_forecast: [], notes: ['Der Bericht enthält keine Dateinamen, Pfade oder Anmeldedaten.'],
    },
  };
}

function searchItem(overrides = {}) {
  return {
    identity: 'photos', pair: 'Fotos', point_id: 'full-fixture', point_kind: 'full', point_label: 'Geprüfter Vollstand',
    path: '<img src=x onerror="window.searchInjected=true">.pdf', size_bytes: 2048,
    verification: 'manifest', restorable: true,
    restore_request: { identity: 'photos', point_id: 'full-fixture', paths: ['<img src=x onerror="window.searchInjected=true">.pdf'] },
    ...overrides,
  };
}

async function openTools(page, tool = 'Dateisuche') {
  const config = structuredClone(fixture.config);
  config._revision = revision;
  config.backup.pairs = [{ ...config.backup.pairs[0], id: 'photos', name: 'Fotos', direction: 'push', mode: 'copy',
    remote: 'old-cloud:photos', local: '/mnt/photos', enabled: true }];
  const state = {
    config, errors: [], writes: [], reads: [], hooks: {}, reports: [], allowSwitched: false, forecastRevision: revision,
    settings: {
      _revision: revision,
      heartbeat: { enabled: true, url_configured: true, period_seconds: 60, timeout_seconds: 5, allow_private_hosts: false },
      weekly_report: { enabled: true, schedule: '0 8 * * 1', timezone: 'Europe/Berlin', retention_weeks: 52,
        email: { enabled: true, host: 'smtp.example.org', port: 587, encryption: 'starttls', username: 'operator', password_set: true,
          from_address: 'backup@example.org', to_addresses: ['owner@example.org'], timeout_seconds: 10 } },
      capabilities: { heartbeat: true, email: true, pdf: true },
    },
    heartbeat: { enabled: true, configured: true, state: 'ok', period_seconds: 60, last_success_at: 1793361600, last_error: null },
    searchFirst: { items: [searchItem()], scanned_points: 2, scanned_files: 70, warnings: [], truncated: false, next_cursor: null },
    searchNext: { items: [searchItem({ path: 'second.pdf', restore_request: { identity: 'photos', point_id: 'full-fixture', paths: ['second.pdf'] } })],
      scanned_points: 3, scanned_files: 90, warnings: [], truncated: false, next_cursor: null },
    forecast: [
      { identity: 'unknown:remote', label: 'Cloud ohne Quote', kind: 'remote', used_bytes: null, total_bytes: null, free_bytes: null,
        state: 'unknown', reason: 'Der Anbieter liefert keine Kapazität.', samples: 0, observed_at: null, days_to_full: null,
        capacity_bytes: null, measurement_scope: 'remote_account' },
      { identity: 'sparse:local', label: 'Neue Messreihe', kind: 'local', used_bytes: 20 * 1024 ** 3, total_bytes: 100 * 1024 ** 3,
        free_bytes: 80 * 1024 ** 3, state: 'insufficient_samples', reason: 'Mindestens drei Tageswerte erforderlich.', samples: 1,
        observed_at: 1793361600, days_to_full: null, capacity_bytes: null, measurement_scope: 'filesystem' },
      { identity: 'photos:remote', label: 'Fotos-Ziel', kind: 'remote', used_bytes: 80 * 1024 ** 3, total_bytes: 100 * 1024 ** 3,
        free_bytes: 20 * 1024 ** 3, state: 'warning', reason: 'Stetiges Wachstum über mehrere Tage.', samples: 7,
        observed_at: 1793361600, days_to_full: 18.2, capacity_bytes: null, measurement_scope: 'remote_account' },
    ],
    history: [1, 2, 3].map((index) => ({ observed_at: 1793102400 + index * 86400, used_bytes: (60 + index * 5) * 1024 ** 3, free_bytes: (40 - index * 5) * 1024 ** 3 })),
    plan: null, migrationPolls: 0,
  };
  page.on('pageerror', (error) => state.errors.push(error.message));
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    const body = method === 'GET' ? undefined : request.postDataJSON();
    if (method === 'GET') state.reads.push(url);
    else state.writes.push({ path: url.pathname, method, body });
    if (state.hooks[url.pathname]) return route.fulfill(await state.hooks[url.pathname]({ request, url, method, body, state }));
    let data = {};
    if (url.pathname === '/api/config') data = state.config;
    else if (url.pathname === '/api/lockdown') data = { active: false };
    else if (url.pathname === '/api/diagnostics/overview') data = fixture.overview;
    else if (url.pathname === '/api/storage/overview') data = fixture.storage;
    else if (url.pathname === '/api/jobs/list') data = [];
    else if (url.pathname === '/api/jobs/search') data = { items: [], total: 0 };
    else if (url.pathname === '/api/jobs/status/current') data = { backup: null, restoretest: null };
    else if (url.pathname === '/api/jobs/progress/stream') return route.fulfill({ status: 404, body: '' });
    else if (url.pathname === '/api/jobs/progress') data = { running: false };
    else if (url.pathname.includes('/scheduler')) data = { enabled: true, paused: false };
    else if (url.pathname === '/api/diagnostics/copies') data = { totals: { sources: 0 }, sources: [] };
    else if (url.pathname === '/api/recovery/files/search') data = url.searchParams.has('cursor') ? state.searchNext : state.searchFirst;
    else if (url.pathname === '/api/recovery/restore') data = { ok: true, job_id: 88 };
    else if (url.pathname === '/api/storage/forecast') data = { _revision: state.forecastRevision, enabled: true, items: state.forecast };
    else if (url.pathname === '/api/storage/forecast/history') data = { identity: url.searchParams.get('identity'), items: state.history };
    else if (url.pathname === '/api/storage/forecast/sample') data = { _revision: state.forecastRevision, sampled: 0, errors: 1, items: state.forecast };
    else if (url.pathname === '/api/storage/forecast/capacity') {
      if (body.expected_revision !== state.forecastRevision) return route.fulfill({ status: 409, json: { detail: 'Konfiguration wurde parallel geändert.' } });
      state.forecastRevision = 'd'.repeat(64);
      state.config._revision = state.forecastRevision;
      const target = state.forecast.find((item) => item.identity === body.identity);
      target.capacity_bytes = body.capacity_bytes;
      target.measurement_scope = body.capacity_bytes == null ? 'remote_account' : 'configured_target';
      // Configuring a budget is not a measurement. Existing unknown evidence remains unknown.
      data = { ok: true, _revision: state.forecastRevision, enabled: true, items: state.forecast };
    }
    else if (url.pathname === '/api/operations/settings') {
      if (method === 'PUT') {
        const invalidMetadata = Object.hasOwn(body.heartbeat, 'url_configured') || Object.hasOwn(body.weekly_report.email, 'password_set');
        if (invalidMetadata) return route.fulfill({ status: 422, json: { detail: 'Nur bearbeitbare Einstellungen dürfen gespeichert werden.' } });
        state.settings = { ...state.settings, _revision: 'b'.repeat(64), heartbeat: { ...state.settings.heartbeat, ...body.heartbeat },
          weekly_report: { ...state.settings.weekly_report, ...body.weekly_report, email: { ...state.settings.weekly_report.email, ...body.weekly_report.email } } };
        delete state.settings.heartbeat.secret_url;
        delete state.settings.weekly_report.email.password;
        state.config._revision = state.settings._revision;
      }
      data = state.settings;
    } else if (url.pathname === '/api/operations/heartbeat') data = state.heartbeat;
    else if (url.pathname === '/api/operations/heartbeat/test') {
      state.heartbeat = { ...state.heartbeat, state: 'error', last_error: 'TLS-Zertifikat des Heartbeat-Empfängers ist nicht vertrauenswürdig.' };
      data = { ok: false, attempted: true, status: state.heartbeat };
    } else if (url.pathname === '/api/reports/weekly/history') data = { items: state.reports, total: state.reports.length };
    else if (url.pathname === '/api/reports/weekly/generate') {
      const report = weeklyReport();
      state.reports = [report];
      data = { ok: true, report, delivery: { state: 'disabled', attempts: 0 } };
    } else if (url.pathname === '/api/reports/weekly/12/pdf') {
      return route.fulfill({ contentType: 'application/pdf', headers: { 'Cache-Control': 'no-store' }, body: transportPDF() });
    } else if (url.pathname === '/api/reports/weekly/12/send') data = { ok: true, delivery: { state: 'sent', attempts: 1, last_success_at: 1793361600 } };
    else if (url.pathname === '/api/migrations') data = { items: state.plan ? [state.plan] : [] };
    else if (url.pathname === '/api/migrations/preview') {
      state.plan = { id: 'migration-fixture', pair: 'Fotos', identity: body.identity, source: 'old-cloud:photos', target: body.target,
        files: 3, total_bytes: 16384, config_revision: body.expected_revision, expires_at: 1900000000, status: 'preview', warnings: [] };
      data = state.plan;
    } else if (url.pathname === '/api/migrations/migration-fixture/run') {
      state.plan.status = 'running'; state.migrationPolls = 0;
      return route.fulfill({ status: 202, json: state.plan });
    } else if (url.pathname === '/api/migrations/migration-fixture/switch') {
      state.plan.status = 'switching';
      return route.fulfill({ status: 202, json: state.plan });
    } else if (url.pathname === '/api/migrations/migration-fixture') {
      if (state.plan.status === 'running' && ++state.migrationPolls >= 2) state.plan.status = 'verified';
      if (state.plan.status === 'switching' && state.allowSwitched) {
        state.plan.status = 'switched'; state.config._revision = 'c'.repeat(64);
        state.config.backup.pairs[0].remote = state.plan.target;
      }
      data = state.plan;
    }
    return route.fulfill({ json: data });
  });
  await page.goto('/#doctor');
  await page.getByRole('tab', { name: /^Betriebswerkzeuge/ }).click();
  const panel = page.locator('#protection-tools-panel');
  await expect(panel).toBeVisible();
  if (tool !== 'Dateisuche') await panel.getByRole('button', { name: tool, exact: true }).click();
  return { panel, state };
}

async function confirmPassword(page, password = confirmedPassword) {
  const dialog = page.getByRole('dialog', { name: 'Aktuelles Passwort bestätigen' });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel('Aktuelles Passwort', { exact: true }).fill(password);
  await dialog.getByRole('button', { name: 'Bestätigen und speichern', exact: true }).click();
  await expect(dialog).toBeHidden();
  expect(await page.evaluate(() => Alpine.$data(document.body).currentPasswordDialog.password)).toBe('');
}

async function prepareMigration(panel) {
  // These labels are scoped to the tool because other console forms also select a data path.
  await panel.getByRole('combobox', { name: 'Datenweg', exact: true }).selectOption('photos');
  await panel.getByLabel('Neues leeres Ziel', { exact: true }).fill('new-cloud:photos');
  await panel.getByLabel('Maximaler Umfang in MiB', { exact: true }).fill('128');
  await panel.getByRole('button', { name: 'Vorschau erstellen', exact: true }).click();
  await expect(panel.getByRole('heading', { name: 'Vorschau bereit', exact: true })).toBeVisible();
}

test('search escapes filenames and restores the exact selected point only after consent', async ({ page }) => {
  const { panel, state } = await openTools(page);
  await panel.getByLabel('Dateiname enthält').fill('Rechnung');
  await panel.getByLabel('Datenweg eingrenzen').selectOption('photos');
  await panel.getByRole('button', { name: 'Sicherungsstände durchsuchen' }).click();
  await expect(panel.locator('.protection-file-path')).toHaveText(state.searchFirst.items[0].path);
  await expect(panel.locator('.protection-file-path img')).toHaveCount(0);
  expect(await page.evaluate(() => window.searchInjected)).toBeUndefined();
  const search = state.reads.find((url) => url.pathname === '/api/recovery/files/search');
  expect(search.searchParams.get('identity')).toBe('photos');
  expect(search.searchParams.get('query')).toBe('Rechnung');
  await expect(panel.getByRole('button', { name: 'In Staging zurückholen', exact: true })).toBeEnabled();
  await panel.getByRole('button', { name: 'In Staging zurückholen', exact: true }).click();
  const confirmation = page.getByRole('dialog', { name: 'Datei wiederherstellen', exact: true });
  await expect(confirmation).toContainText('Geprüfter Vollstand');
  expect(state.writes).toEqual([]);
  await confirmation.getByRole('button', { name: 'Abbrechen', exact: true }).click();
  expect(state.writes).toEqual([]);
  await panel.getByRole('button', { name: 'In Staging zurückholen', exact: true }).click();
  await confirmation.getByRole('button', { name: 'In Staging zurückholen', exact: true }).click();
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0]).toEqual({ path: '/api/recovery/restore', method: 'POST', body: { ...state.searchFirst.items[0].restore_request, max_total_mb: 512 } });
  await expect(panel).toContainText('Lauf #88');
  expect(state.errors).toEqual([]);
});

test('search continuation uses the signed cursor and retains prior matches', async ({ page }) => {
  const { panel, state } = await openTools(page);
  state.searchFirst.next_cursor = 'signed-next-cursor'; state.searchFirst.truncated = true;
  await panel.getByLabel('Dateiname enthält').fill('pdf');
  await panel.getByRole('button', { name: 'Sicherungsstände durchsuchen' }).click();
  await panel.getByRole('button', { name: 'Weitere Stände durchsuchen', exact: true }).click();
  await expect(panel.locator('.protection-file-path')).toHaveCount(2);
  expect(state.reads.filter((url) => url.pathname === '/api/recovery/files/search')[1].searchParams.get('cursor')).toBe('signed-next-cursor');
  await expect(panel.getByRole('button', { name: 'Weitere Stände durchsuchen', exact: true })).toBeHidden();
  expect(state.errors).toEqual([]);
});

test('changing the query cannot reuse a signed continuation from an older search', async ({ page }) => {
  const { panel, state } = await openTools(page);
  state.searchFirst.next_cursor = 'query-bound-cursor';
  await panel.getByLabel('Dateiname enthält').fill('initial.pdf');
  await panel.getByRole('button', { name: 'Sicherungsstände durchsuchen' }).click();
  await panel.getByLabel('Dateiname enthält').fill('different.pdf');
  await panel.getByRole('button', { name: 'Weitere Stände durchsuchen', exact: true }).click();
  await expect(panel.getByRole('alert')).toContainText('zuerst eine neue Suche starten');
  expect(state.reads.filter((url) => url.pathname === '/api/recovery/files/search')).toHaveLength(1);
  expect(state.errors).toEqual([]);
});

test('forecast distinguishes unknown quota, sparse evidence and measured growth, with history', async ({ page }, testInfo) => {
  const { panel, state } = await openTools(page, 'Speicherprognose');
  const unknown = panel.getByRole('row').filter({ hasText: 'Cloud ohne Quote' });
  await expect(unknown).toContainText('Quote unbekannt');
  await expect(unknown).toContainText('Kapazität unbekannt');
  await expect(unknown.locator('small').filter({ hasText: /^Voraussichtlich voll/ })).toBeHidden();
  await expect(panel.getByRole('row').filter({ hasText: 'Neue Messreihe' })).toContainText('Messreihe wird aufgebaut');
  const measured = panel.getByRole('row').filter({ hasText: 'Fotos-Ziel' });
  await expect(measured).toContainText('Kapazität wird knapp');
  await expect(measured).toContainText('18 Tagen');
  await measured.getByRole('button', { name: 'Fotos-Ziel', exact: true }).click();
  await expect(panel.getByRole('img', { name: 'Verlauf der gemessenen Speicherbelegung' })).toBeVisible();
  await expect(panel.locator('.protection-history tbody tr')).toHaveCount(3);
  const screenshot = path.resolve(__dirname, '../../.planning/protection-tools', `forecast-${testInfo.project.name}.png`);
  fs.mkdirSync(path.dirname(screenshot), { recursive: true });
  const layout = await panel.evaluate((element) => ({ right: element.getBoundingClientRect().right,
    viewport: window.innerWidth, document: document.documentElement.scrollWidth }));
  expect(layout.right).toBeLessThanOrEqual(layout.viewport + 1);
  expect(layout.document).toBeLessThanOrEqual(layout.viewport + 1);
  await page.evaluate(() => window.scrollTo({ top: 0, left: 0, behavior: 'instant' }));
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
  await page.screenshot({ path: screenshot.replace('.png', '-page.png'), fullPage: true });
  await panel.screenshot({ path: screenshot });
  expect(state.reads.find((url) => url.pathname.endsWith('/forecast/history')).searchParams.get('identity')).toBe('photos:remote');
  await panel.getByRole('button', { name: 'Jetzt messen', exact: true }).click();
  await expect(panel.getByRole('alert')).toContainText('1 Messung(en) fehlgeschlagen.');
  await expect(measured).toContainText('Kapazität wird knapp');
  expect(state.errors).toEqual([]);
});

test('an unmeasured remote can store its first target budget without inventing capacity evidence', async ({ page }) => {
  const { panel, state } = await openTools(page, 'Speicherprognose');
  state.history = [];
  const unknown = panel.getByRole('row').filter({ hasText: 'Cloud ohne Quote' });
  await unknown.getByRole('button', { name: 'Cloud ohne Quote', exact: true }).click();
  await expect(panel).toContainText('Für dieses Ziel liegen noch keine Messwerte vor.');
  const budget = panel.getByLabel('Budget des Zielordners in GiB');
  await expect(budget).toBeVisible();
  await budget.fill('128.5');
  await panel.getByRole('button', { name: 'Kapazität speichern', exact: true }).click();
  await expect(panel).toContainText('Kapazitätsvorgabe gespeichert.');
  expect(state.writes).toEqual([{ path: '/api/storage/forecast/capacity', method: 'PUT',
    body: { identity: 'unknown:remote', capacity_bytes: 128.5 * 1024 ** 3, expected_revision: revision } }]);
  await expect(unknown).toContainText('Kapazität unbekannt');
  await expect(unknown).toContainText('0 Messwerte');
  await expect(unknown.locator('small').filter({ hasText: /^Voraussichtlich voll/ })).toBeHidden();
  expect(state.forecast[0].used_bytes).toBeNull();
  expect(state.forecast[0].state).toBe('unknown');
  expect(state.errors).toEqual([]);
});

test('saving operational settings confirms the password and omits read-only metadata and unchanged secrets', async ({ page }) => {
  const { panel, state } = await openTools(page, 'Ausfallwächter');
  await expect(panel.getByLabel('Intervall in Sekunden')).toHaveValue('60');
  await panel.getByLabel('Intervall in Sekunden').fill('120');
  await panel.getByRole('button', { name: 'Einstellungen speichern', exact: true }).filter({ visible: true }).click();
  expect(state.writes).toEqual([]);
  await page.getByRole('dialog', { name: 'Aktuelles Passwort bestätigen' }).getByRole('button', { name: 'Abbrechen', exact: true }).click();
  expect(state.writes).toEqual([]);
  await panel.getByRole('button', { name: 'Einstellungen speichern', exact: true }).filter({ visible: true }).click();
  await confirmPassword(page);
  await expect(panel).toContainText('Einstellungen gespeichert.');
  const write = state.writes.find((item) => item.path === '/api/operations/settings');
  expect(write.body.expected_revision).toBe(revision);
  expect(write.body.current_password).toBe(confirmedPassword);
  expect(write.body.heartbeat.period_seconds).toBe(120);
  expect(write.body.heartbeat).not.toHaveProperty('secret_url');
  expect(write.body.heartbeat).not.toHaveProperty('url_configured');
  expect(write.body.weekly_report.email).not.toHaveProperty('password');
  expect(write.body.weekly_report.email).not.toHaveProperty('password_set');
  expect(await page.evaluate((password) => Object.values(localStorage).some((value) => value.includes(password)), confirmedPassword)).toBe(false);
  expect(state.errors).toEqual([]);
});

test('heartbeat testing requires consent and displays an untrusted TLS receiver failure', async ({ page }) => {
  const { panel, state } = await openTools(page, 'Ausfallwächter');
  await panel.getByRole('button', { name: 'Gespeicherten Empfänger testen', exact: true }).click();
  expect(state.writes).toEqual([]);
  await confirmPassword(page);
  await expect(panel).toContainText('TLS-Zertifikat des Heartbeat-Empfängers ist nicht vertrauenswürdig.');
  await expect(panel).toContainText('Versandfehler');
  expect(state.writes[0].body).toEqual({ current_password: confirmedPassword });
  expect(state.errors).toEqual([]);
});

test('weekly report generation, preview and PDF download do not send email without a separate confirmation', async ({ page }, testInfo) => {
  const { panel, state } = await openTools(page, 'Schutzberichte');
  await panel.getByRole('button', { name: 'Bericht jetzt erstellen', exact: true }).click();
  const preview = panel.locator('.protection-report-preview');
  await expect(preview).toContainText('19.10.2026 bis 25.10.2026');
  await expect(preview).toContainText('27');
  expect(state.writes.map((item) => item.path)).toEqual(['/api/reports/weekly/generate']);
  const downloading = page.waitForEvent('download');
  await preview.getByRole('button', { name: 'PDF herunterladen', exact: true }).click();
  const download = await downloading;
  expect(download.suggestedFilename()).toBe('Sicherpfad-Schutzbericht-2026-10-19.pdf');
  const target = testInfo.outputPath('weekly-report.pdf');
  await download.saveAs(target);
  expect(fs.readFileSync(target).subarray(0, 5).toString()).toBe('%PDF-');
  await preview.getByRole('button', { name: 'Per E-Mail senden', exact: true }).click();
  expect(state.writes).toHaveLength(1);
  await page.getByRole('dialog', { name: 'Aktuelles Passwort bestätigen' }).getByRole('button', { name: 'Abbrechen', exact: true }).click();
  expect(state.writes).toHaveLength(1);
  await preview.getByRole('button', { name: 'Per E-Mail senden', exact: true }).click();
  await confirmPassword(page);
  await expect(panel).toContainText('Schutzbericht per E-Mail versendet.');
  expect(state.writes[1]).toEqual({ path: '/api/reports/weekly/12/send', method: 'POST', body: { current_password: confirmedPassword } });
  expect(state.errors).toEqual([]);
});

test('weekly email delivery failure is visible instead of a success claim', async ({ page }) => {
  const { panel, state } = await openTools(page, 'Schutzberichte');
  await panel.getByRole('button', { name: 'Bericht jetzt erstellen', exact: true }).click();
  state.hooks['/api/reports/weekly/12/send'] = async () => ({ json: { ok: false, delivery: { state: 'retrying', attempts: 1,
    last_error: 'SMTP-Anmeldung wurde abgewiesen.', next_attempt_at: 1793361900 } } });
  await panel.locator('.protection-report-preview').getByRole('button', { name: 'Per E-Mail senden', exact: true }).click();
  await confirmPassword(page);
  await expect(panel.getByRole('alert')).toContainText('SMTP-Anmeldung wurde abgewiesen.');
  await expect(panel).not.toContainText('Schutzbericht per E-Mail versendet.');
  expect(state.errors).toEqual([]);
});

test('target migration separates preview, verified copy and asynchronously confirmed cutover', async ({ page }) => {
  const { panel, state } = await openTools(page, 'Zielwechsel');
  await prepareMigration(panel);
  expect(state.writes[0].body).toEqual({ identity: 'photos', target: 'new-cloud:photos', expected_revision: revision, max_total_mb: 128 });
  const switchButton = panel.getByRole('button', { name: 'Geprüftes Ziel umschalten', exact: true });
  await expect(switchButton).toBeHidden();
  await panel.getByRole('button', { name: 'Kopieren und prüfen', exact: true }).click();
  expect(state.writes).toHaveLength(1);
  await page.getByRole('dialog', { name: 'Zielwechsel vorbereiten', exact: true }).getByRole('button', { name: 'Kopieren und prüfen', exact: true }).click();
  expect(state.writes).toHaveLength(1);
  await confirmPassword(page);
  await expect(panel.getByRole('heading', { name: 'Neue Kopie geprüft', exact: true })).toBeVisible({ timeout: 10000 });
  expect(state.writes.filter((item) => item.path.endsWith('/run'))[0].body).toEqual({ current_password: confirmedPassword });
  expect(state.writes.filter((item) => item.path.endsWith('/switch'))).toEqual([]);
  await switchButton.click();
  await page.getByRole('dialog', { name: 'Sicherungsziel umschalten', exact: true }).getByRole('button', { name: 'Ziel umschalten', exact: true }).click();
  await confirmPassword(page);
  await expect.poll(() => state.writes.filter((item) => item.path.endsWith('/switch')).length).toBe(1);
  expect(state.writes.find((item) => item.path.endsWith('/switch')).body).toEqual({ current_password: confirmedPassword, expected_revision: revision });
  expect(await page.evaluate(() => Alpine.$data(document.body).protectionTools.migration.plan.status)).toBe('switching');
  await expect(panel).not.toContainText('Sicherungsziel umgeschaltet.');
  state.allowSwitched = true;
  await expect(panel.getByRole('heading', { name: 'Ziel umgeschaltet', exact: true })).toBeVisible({ timeout: 10000 });
  await expect(panel).toContainText('Sicherungsziel umgeschaltet.');
  expect(await page.evaluate(() => Alpine.$data(document.body).config.backup.pairs[0].remote)).toBe('new-cloud:photos');
  expect(state.errors).toEqual([]);
});

test('a target edit invalidates a migration preview and late previews cannot restore obsolete consent', async ({ page }) => {
  const { panel, state } = await openTools(page, 'Zielwechsel');
  await prepareMigration(panel);
  await panel.getByLabel('Neues leeres Ziel', { exact: true }).fill('changed-cloud:photos');
  await expect(panel.getByRole('button', { name: 'Kopieren und prüfen', exact: true })).toBeHidden();
  const started = deferred(), release = deferred();
  state.hooks['/api/migrations/preview'] = async ({ body }) => {
    started.resolve(); await release.promise;
    return { json: { ...state.plan, target: body.target, status: 'preview' } };
  };
  await panel.getByRole('button', { name: 'Vorschau erstellen', exact: true }).click();
  await started.promise;
  await panel.getByLabel('Neues leeres Ziel', { exact: true }).fill('latest-cloud:photos');
  release.resolve();
  await expect(panel.getByRole('button', { name: 'Vorschau erstellen', exact: true })).toBeEnabled();
  await expect(panel.getByRole('button', { name: 'Kopieren und prüfen', exact: true })).toBeHidden();
  expect(state.writes.some((item) => /\/(run|switch)$/.test(item.path))).toBe(false);
  expect(state.errors).toEqual([]);
});
