const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, '../../ios/RcloneMobile/StorePreviewData.json'), 'utf8'));

const providers = [
  { name: 'pcloud', description: 'Pcloud' },
  { name: 'drive', description: 'Google Drive' },
  { name: 'webdav', description: 'WebDAV' },
  { name: 'onedrive', description: 'Microsoft OneDrive' },
  { name: 's3', description: 'Amazon S3 Compliant Storage Providers including AWS, Alibaba, Ceph, GCS, Hetzner, HuaweiOBS, IBM COS, IDrive, IONOS, Minio. '.repeat(40) },
  { name: 'box', description: 'Box' },
  { name: 'azureblob', description: 'Microsoft Azure Blob Storage' },
  { name: 'b2', description: 'Backblaze B2' },
  { name: 'dropbox', description: 'Dropbox' },
  { name: 'ftp', description: 'FTP' },
  { name: 'sftp', description: 'SSH/SFTP' },
  { name: 'crypt', description: 'Encrypt/Decrypt a remote' },
  { name: 'local', description: 'Local Disk' },
  { name: 'protondrive', description: 'Proton Drive' },
  { name: 'swift', description: 'OpenStack Swift' },
  { name: 'yandex', description: 'Yandex Disk' },
  { name: 'zoho', description: 'Zoho WorkDrive' },
];

async function openRemoteSetup(page, { choices = false, token = false, providerError = false, available = providers } = {}) {
  const writes = [];
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const config = structuredClone(fixture.config);
  config._revision = 'a'.repeat(64);
  const option = token ? {
    name: 'config_token', exclusive: false, required: true, secret: true, default: '', examples: [],
    help: 'For more help see https://rclone.org/remote_setup/.\nExecute the following:\n    rclone authorize "pcloud"\nThen paste the result.\njavascript:alert(1) <img src=x onerror=alert(1)>',
  } : choices ? {
    name: 'vendor', help: 'Choose the service used by your server.', exclusive: true,
    required: true, secret: false, default: 'other', examples: [
      { value: 'nextcloud', help: 'Nextcloud' },
      { value: 'other', help: 'Other WebDAV service. ' + 'LongProviderInformationWithoutAnySpaces'.repeat(30) },
    ],
  } : { name: 'client_secret', help: 'Enter a client secret, or leave empty to use the default.', exclusive: false, required: false, secret: true, default: '', examples: [] };
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    let data = {};
    if (url.pathname === '/api/remotes/providers') {
      if (providerError) return route.fulfill({ status: 503, json: { detail: 'Speicheranbieter vorübergehend nicht verfügbar.' } });
      data = { providers: available };
    } else if (url.pathname === '/api/remotes/setup' && request.method() === 'POST') {
      writes.push({ path: url.pathname, body: request.postDataJSON() });
      data = { id: 'fixture-setup', name: request.postDataJSON().name, step: 1, ready: false, error: '', option };
    } else if (url.pathname.endsWith('/answer')) {
      writes.push({ path: url.pathname, body: request.postDataJSON() });
      data = { id: 'fixture-setup', name: 'mein-speicher', step: 2, ready: true, error: '', option: {} };
    } else if (url.pathname.endsWith('/save')) {
      writes.push({ path: url.pathname, body: request.postDataJSON() });
      data = { ok: true, name: 'mein-speicher' };
    } else if (url.pathname.startsWith('/api/remotes/setup/') && request.method() === 'DELETE') {
      writes.push({ path: url.pathname, method: 'DELETE' });
      data = { ok: true };
    } else if (url.pathname === '/api/config') data = config;
    else if (url.pathname === '/api/diagnostics/overview') data = fixture.overview;
    else if (url.pathname === '/api/storage/overview') data = fixture.storage;
    else if (url.pathname === '/api/jobs/list') data = fixture.jobs;
    else if (url.pathname === '/api/jobs/search') data = { items: fixture.jobs, total: fixture.jobs.length };
    else if (url.pathname === '/api/jobs/status/current') data = { backup: null, restoretest: null };
    else if (url.pathname === '/api/jobs/progress/stream') return route.fulfill({ status: 404, body: '' });
    else if (url.pathname === '/api/jobs/progress') data = { running: false };
    else if (url.pathname === '/api/diagnostics/copies') data = { totals: { sources: 0 }, sources: [] };
    else if (url.pathname.includes('/scheduler')) data = { enabled: true, paused: false };
    return route.fulfill({ json: data });
  });
  await page.goto('/#doctor');
  const opener = page.getByRole('button', { name: 'Neues Remote einbinden', exact: true });
  await opener.click();
  const dialog = page.getByRole('dialog', { name: 'Neues Remote einbinden' });
  await expect(dialog).toBeVisible();
  return { dialog, opener, writes, errors };
}

async function expectContained(dialog) {
  expect(await dialog.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const content = element.querySelector('.modal-content');
    const footer = element.querySelector('.modal-footer').getBoundingClientRect();
    return bounds.left >= -1 && bounds.right <= innerWidth + 1 && bounds.top >= -1 && bounds.bottom <= innerHeight + 1
      && content.scrollWidth <= content.clientWidth + 1 && footer.bottom <= innerHeight + 1;
  })).toBe(true);
  await expect(dialog.locator('select')).toHaveCount(0);
}

test('remote providers stay compact and searchable with long rclone descriptions', async ({ page }, testInfo) => {
  const { dialog, writes, errors } = await openRemoteSetup(page);
  const next = dialog.getByRole('button', { name: 'Weiter', exact: true });
  await expect(next).toBeDisabled();
  await expect(dialog.getByRole('radio')).toHaveCount(4);
  await expect(dialog.getByRole('radio', { name: 'pCloud', exact: true })).toBeVisible();
  await expect(dialog.getByRole('radio', { name: 'Google Drive', exact: true })).toBeVisible();
  await expectContained(dialog);
  await dialog.getByLabel('Remote-Name').fill('mein-speicher');
  await dialog.getByRole('radio', { name: 'pCloud', exact: true }).check();
  await expect(next).toBeEnabled();
  await dialog.locator('..').click({ position: { x: 2, y: 2 } });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel('Remote-Name')).toHaveValue('mein-speicher');
  await expect(dialog.getByRole('radio', { name: 'pCloud', exact: true })).toBeChecked();
  const screenshot = testInfo.outputPath('remote-provider-selection.png');
  await dialog.screenshot({ path: screenshot });
  await testInfo.attach('remote-provider-selection', { path: screenshot, contentType: 'image/png' });
  const search = dialog.getByRole('searchbox', { name: 'Anbieter suchen' });
  await search.fill('Hetzner');
  await search.press('Enter');
  expect(writes).toEqual([]);
  await expect(dialog.getByRole('radio')).toHaveCount(1);
  await dialog.getByRole('radio', { name: 'S3-kompatibler Speicher', exact: true }).check();
  await expectContained(dialog);
  await dialog.getByText('Anbieterhinweise', { exact: true }).click();
  await expectContained(dialog);
  await search.fill('Box');
  await dialog.getByRole('radio', { name: 'Box', exact: true }).check();
  await next.click();
  expect(writes[0]).toEqual({ path: '/api/remotes/setup', body: { name: 'mein-speicher', provider: 'box' } });
  await expect(dialog.locator('input[type="password"]')).toBeFocused();
  await next.click();
  await expect(dialog.getByRole('button', { name: 'Remote speichern' })).toBeVisible();
  expect(writes).toHaveLength(2);
  await dialog.getByRole('button', { name: 'Remote speichern' }).click();
  await expect(dialog).toContainText('Verbindung gespeichert');
  await expect(dialog.getByRole('button', { name: 'Fertig', exact: true })).toBeFocused();
  await dialog.getByRole('button', { name: 'Fertig', exact: true }).click();
  await expect(dialog).toBeHidden();
  expect(writes[2].path).toBe('/api/remotes/setup/fixture-setup/save');
  expect(errors).toEqual([]);
});

test('all providers scroll inside the dialog and cancellation creates nothing', async ({ page }) => {
  const { dialog, opener, writes, errors } = await openRemoteSetup(page);
  await dialog.getByRole('button', { name: /Weitere Anbieter anzeigen/ }).click();
  await expect(dialog.getByRole('radio')).toHaveCount(providers.length);
  expect(await dialog.locator('#remote-provider-options').evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true);
  await expectContained(dialog);
  await dialog.getByRole('searchbox').fill('KeinTreffer');
  await expect(dialog.getByRole('status')).toContainText('Kein passender Anbieter');
  await expect(dialog.getByRole('button', { name: 'Weiter', exact: true })).toBeDisabled();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});

test('small screens retain the footer and provider choices work with the keyboard', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 568 });
  const { dialog, errors } = await openRemoteSetup(page);
  await dialog.getByLabel('Remote-Name').fill('mein-speicher');
  const first = dialog.getByRole('radio', { name: 'pCloud', exact: true });
  await first.focus();
  await first.press('ArrowDown');
  await expect(dialog.getByRole('radio', { name: 'Google Drive', exact: true })).toBeChecked();
  await expectContained(dialog);
  await dialog.getByRole('button', { name: 'Abbrechen', exact: true }).click();
  expect(errors).toEqual([]);
});

test('exclusive answers wrap, keep defaults and submit the actual choice value', async ({ page }, testInfo) => {
  const { dialog, writes, errors } = await openRemoteSetup(page, { choices: true });
  await dialog.getByLabel('Remote-Name').fill('mein-speicher');
  await dialog.getByRole('radio', { name: 'WebDAV', exact: true }).check();
  await dialog.getByRole('button', { name: 'Weiter', exact: true }).click();
  const choices = dialog.getByRole('group', { name: 'Servertyp' });
  await expect(choices.getByRole('radio', { name: 'Anderer Anbieter', exact: true })).toBeChecked();
  await expect(choices.getByRole('radio', { name: 'Anderer Anbieter', exact: true })).toBeFocused();
  await dialog.locator('.remote-original-help > summary').click();
  await expectContained(dialog);
  const screenshot = testInfo.outputPath('remote-provider-question.png');
  await dialog.screenshot({ path: screenshot });
  await testInfo.attach('remote-provider-question', { path: screenshot, contentType: 'image/png' });
  await choices.getByRole('radio', { name: 'Nextcloud', exact: true }).check();
  await dialog.getByRole('button', { name: 'Weiter', exact: true }).click();
  expect(writes[1].body).toEqual({ step: 1, answer: 'nextcloud' });
  await dialog.getByRole('button', { name: 'Abbrechen', exact: true }).click();
  expect(writes.at(-1)).toEqual({ path: '/api/remotes/setup/fixture-setup', method: 'DELETE' });
  expect(writes.some((write) => write.path.endsWith('/save'))).toBe(false);
  expect(errors).toEqual([]);
});

test('login guidance is German, safe links open separately and copying works on local HTTP', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true });
    document.execCommand = (command) => { window.copiedLoginCommand = command === 'copy' ? document.activeElement.value : null; return true; };
  });
  const { dialog, writes, errors } = await openRemoteSetup(page, { token: true });
  await dialog.getByLabel('Remote-Name').fill('mein-speicher');
  await dialog.getByRole('radio', { name: 'pCloud', exact: true }).check();
  await dialog.getByRole('button', { name: 'Weiter', exact: true }).click();
  await expect(dialog).toContainText('Für diese Anmeldung brauchst du einmalig rclone auf einem Computer mit Browser.');
  await expect(dialog.locator('.remote-original-help')).not.toHaveAttribute('open');
  const links = dialog.getByRole('navigation', { name: 'Anmeldehilfen' });
  const helpLink = links.getByRole('link', { name: 'Anleitung bei rclone öffnen' });
  await expect(helpLink).toHaveAttribute('href', 'https://rclone.org/remote_setup/');
  await expect(helpLink).toHaveAttribute('target', '_blank');
  await expect(helpLink).toHaveAttribute('rel', 'noopener noreferrer');
  await dialog.getByRole('button', { name: 'Befehl kopieren' }).click();
  expect(await page.evaluate(() => window.copiedLoginCommand)).toBe('rclone authorize "pcloud"');
  await expect(dialog.getByRole('button', { name: 'Befehl kopieren' })).toBeFocused();
  await dialog.locator('.remote-original-help > summary').click();
  await expect(dialog.locator('.remote-original-help a')).toHaveAttribute('href', 'https://rclone.org/remote_setup/');
  await expect(dialog.locator('a[href^="javascript:"], img')).toHaveCount(0);
  await expectContained(dialog);
  await dialog.locator('..').click({ position: { x: 2, y: 2 } });
  await expect(dialog).toBeVisible();
  expect(writes).toHaveLength(1);
  await dialog.getByRole('button', { name: 'Abbrechen', exact: true }).click();
  expect(errors).toEqual([]);
});

for (const providerError of [false, true]) test(`unavailable providers show an error without enabling the next step (${providerError})`, async ({ page }) => {
  const { dialog, writes, errors } = await openRemoteSetup(page, { providerError, available: [] });
  await expect(dialog.getByRole('alert')).toContainText(providerError ? 'vorübergehend nicht verfügbar' : 'Keine Speicheranbieter verfügbar');
  await expect(dialog.getByRole('button', { name: 'Weiter', exact: true })).toBeDisabled();
  await expectContained(dialog);
  await dialog.getByRole('button', { name: 'Abbrechen', exact: true }).click();
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});
