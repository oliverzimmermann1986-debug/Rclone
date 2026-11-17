'use strict';

// This entry point deliberately refuses a Windows-host browser launch. It may
// run only in GitHub Actions or a Linux container explicitly marked isolated.
if (process.platform !== 'linux' || (process.env.GITHUB_ACTIONS !== 'true' && process.env.UI_VISUAL_ISOLATED !== '1')) {
  throw new Error('Visual rendering is allowed only on the isolated Linux CI runner/container.');
}

const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');
const { apiFixture, pairs, progressFixture } = require('./ui-visual-fixtures.cjs');
const root = path.resolve(__dirname, '../..');
const staticRoot = path.join(root, 'app/static');
const output = path.resolve(process.env.UI_VISUAL_OUTPUT || path.join(__dirname, 'artifacts/web-visual'));
const report = { syntheticOnly: true, playwright: '1.65.1', expectedChromium: '159.0.8154.6', zoomMethod: '200% layout reflow equivalent: 1440x1000 physical area / 720x500 CSS viewport at DPR 2; not a claim of browser-UI zoom or mobile pinch-zoom testing.', cases: [], unexpectedRequests: [], blockedExternalRequests: [] };
const pages = [
  ['dashboard', 'Lagebild'], ['pairs', 'Datenwege'], ['definitions', 'Jobs'], ['runs', 'Läufe'], ['doctor', 'System'],
];
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.ico': 'image/x-icon' };

function json(response, value, status = 200) {
  response.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  response.end(JSON.stringify(value));
}

async function serve(request, response) {
  const url = new URL(request.url, 'http://127.0.0.1');
  const caseName = request.headers['x-ui-case'] || '';
  if (url.pathname === '/api/jobs/progress/stream' && request.method === 'GET') {
    response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', Connection: 'keep-alive' });
    const frame = () => response.write(`data: ${JSON.stringify(progressFixture(caseName))}\n\n`);
    frame();
    const heartbeat = setInterval(frame, 2000);
    request.on('close', () => clearInterval(heartbeat));
    return;
  }
  if (url.pathname.startsWith('/api/')) {
    const fixture = apiFixture(request.method, url, caseName);
    if (fixture === undefined) {
      report.unexpectedRequests.push({ method: request.method, path: url.pathname, case: request.headers['x-ui-case'] || '' });
      json(response, { detail: 'UI fixture has no route; no backend exists here.' }, 501);
    } else json(response, fixture);
    return;
  }
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    report.unexpectedRequests.push({ method: request.method, path: url.pathname });
    json(response, { detail: 'Fixture server accepts no mutations or logins.' }, 405);
    return;
  }
  let file;
  if (url.pathname === '/' || url.pathname === '/login') file = path.join(staticRoot, url.pathname === '/' ? 'index.html' : 'login.html');
  else if (url.pathname === '/favicon.ico') file = path.join(staticRoot, 'app-icon-192.png');
  else if (url.pathname.startsWith('/static/')) file = path.resolve(staticRoot, decodeURIComponent(url.pathname.slice('/static/'.length)));
  if (!file || !file.startsWith(staticRoot + path.sep)) return json(response, { detail: 'Not a static fixture route.' }, 404);
  try {
    let body = await fs.readFile(file);
    if (path.extname(file) === '.html') {
      body = Buffer.from(body.toString('utf8').replaceAll('__APP_VERSION__', 'ui-fixture').replace('<!--LOGIN_ERROR-->', '').replace('<!--LOGIN_CSRF-->', 'synthetic-ui-csrf-not-a-session').replace('<!--LOGIN_NEXT-->', ''));
    }
    response.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store', 'Set-Cookie': 'rclone_sync_csrf=synthetic-ui-csrf; Path=/; SameSite=Strict' });
    response.end(body);
  } catch (error) {
    report.unexpectedRequests.push({ method: request.method, path: url.pathname, error: error.code || String(error) });
    json(response, { detail: 'Static fixture asset is missing.' }, 404);
  }
}

async function settle(page) {
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.waitForTimeout(150);
}

async function noOverflow(page) {
  const dimensions = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth, body: document.body.scrollWidth }));
  assert(dimensions.scroll <= dimensions.width + 2 && dimensions.body <= dimensions.width + 2, `Horizontal page overflow: ${JSON.stringify(dimensions)}`);
}

async function schedulerPreviewIsContained(page, activePage) {
  if (activePage !== 'settings') {
    assert.equal(await page.locator('.scheduler-summary-card:visible').count(), 0, `${activePage}: the settings-only scheduler preview must be hidden.`);
  }
  const misplaced = await page.evaluate((id) => {
    const visible = (element) => element && element.getClientRects().length > 0 && getComputedStyle(element).visibility !== 'hidden';
    const main = document.querySelector('main');
    const activeSection = [...(main?.querySelectorAll('section[x-show]') || [])].find((section) => section.getAttribute('x-show') === `page==='${id}'`);
    return [...document.querySelectorAll('.scheduler-summary-card')].filter(visible).filter((card) => {
      if (visible(activeSection) && activeSection.contains(card)) return false;
      const dialog = card.closest('[role="dialog"]');
      const title = dialog?.getAttribute('aria-label') || (dialog?.getAttribute('aria-labelledby') || '').split(/\s+/).map((label) => document.getElementById(label)?.textContent || '').join(' ');
      return !(visible(dialog) && /\bEinstellungen\b/.test(title));
    }).map((card) => card.textContent.trim().slice(0, 160));
  }, activePage);
  assert.deepEqual(misplaced, [], `${activePage}: scheduler preview leaked outside the active page/settings dialog.`);
}

async function reducedMotionIsRespected(page) {
  const motion = await page.evaluate(() => ({
    scroll: getComputedStyle(document.documentElement).scrollBehavior,
    unbounded: document.getAnimations().filter((animation) => {
      const timing = animation.effect?.getComputedTiming();
      return animation.playState === 'running' && timing?.iterations === Infinity;
    }).length,
  }));
  assert.notEqual(motion.scroll, 'smooth', 'Reduced-motion pages must not force smooth document scrolling.');
  assert.equal(motion.unbounded, 0, 'Reduced-motion page still runs an unbounded animation.');
}

async function focusedControlIsVisible(page) {
  const focus = await page.evaluate(() => {
    const element = document.activeElement;
    const rect = element?.getBoundingClientRect();
    const style = element && getComputedStyle(element);
    return { tag: element?.tagName, width: rect?.width, height: rect?.height, hidden: style?.visibility === 'hidden' || style?.display === 'none', outline: style?.outlineStyle, outlineWidth: parseFloat(style?.outlineWidth || 0), shadow: style?.boxShadow };
  });
  assert(focus.tag !== 'BODY' && focus.width > 0 && focus.height > 0 && !focus.hidden, `Focus lost or invisible: ${JSON.stringify(focus)}`);
  assert((focus.outline !== 'none' && focus.outlineWidth > 0) || focus.shadow !== 'none', 'Focused control has no visible outline/shadow.');
}

async function screenshot(page, name) {
  await page.screenshot({ path: path.join(output, `${name}.png`), fullPage: true, animations: 'disabled' });
}

async function checkCase(page, name, action) {
  const item = { name, passed: false, error: null };
  report.cases.push(item);
  try {
    await action();
    item.passed = true;
  } catch (error) {
    item.error = error.stack || String(error);
    console.error(`${name}: ${item.error}`);
  } finally {
    await screenshot(page, item.passed ? name : `${name}-failure`).catch((error) => { item.error = `${item.error || ''}\nScreenshot: ${error}`; item.passed = false; });
  }
}

async function navigate(page, id, label) {
  const mobile = await page.locator('.mobile-nav').isVisible();
  const nav = page.getByRole('navigation', { name: mobile ? 'Mobile Navigation' : 'Hauptnavigation', exact: true });
  await nav.getByRole('button', { name: new RegExp(`^${label}`) }).click();
  await page.waitForFunction((next) => window.Alpine?.$data(document.body)?.page === next, id);
  await page.waitForFunction(() => {
    const model = window.Alpine.$data(document.body);
    return !model.configLoading && !model.refreshing && !model.jobs.loading && !model.jobDefinitions.loading && !model.jobTimeline.loading && !model.doctor.loading && !model.cloudQuota.loading;
  });
  await settle(page);
  const section = page.locator(`main section[x-show="page==='${id}'"]`);
  assert(await section.isVisible(), `${label}: real page section is not visible.`);
  const action = section.locator('.page-heading button:visible:not([disabled])').first();
  assert(await action.count(), `${label}: no visible main action.`);
  await action.click({ trial: true });
  const tabs = section.getByRole('tab');
  if (await tabs.count() > 1) {
    await tabs.first().focus();
    await page.keyboard.press('ArrowRight');
    await settle(page);
    assert.equal(await tabs.nth(1).getAttribute('aria-selected'), 'true', `${label}: ArrowRight did not select the next workspace tab.`);
    await page.keyboard.press('Home');
    await settle(page);
    assert.equal(await tabs.first().getAttribute('aria-selected'), 'true', `${label}: Home did not restore the first workspace tab.`);
    await focusedControlIsVisible(page);
  }
  await page.evaluate(() => window.scrollTo(0, 0));
  await settle(page);
  await noOverflow(page);
  await schedulerPreviewIsContained(page, id);
  await reducedMotionIsRespected(page);
}

async function dialogCase(page) {
  const dashboard = page.locator('main section[x-show="page===\'dashboard\'"]');
  const opener = dashboard.locator('button[\\@click="openQuick()"]');
  if (!(await opener.isVisible())) {
    const menu = opener.locator('xpath=ancestor::details[1]');
    assert.equal(await menu.count(), 1, 'Hidden Quick-Sync action must have one owning disclosure menu.');
    await menu.locator(':scope > summary').click();
  }
  await opener.click();
  const dialog = page.getByRole('dialog', { name: 'Quick-Sync', exact: true });
  await dialog.waitFor({ state: 'visible' });
  await page.waitForFunction(() => document.querySelector('[role="dialog"][aria-labelledby="quick-dialog-title"]')?.contains(document.activeElement));
  const controls = dialog.locator('button:visible:not([disabled]), input:visible:not([type="hidden"]):not([disabled]), select:visible:not([disabled]), textarea:visible:not([disabled])');
  await controls.first().focus();
  await page.keyboard.press('Shift+Tab');
  assert(await controls.last().evaluate((element) => element === document.activeElement), 'Dialog reverse Tab did not wrap to its last control.');
  await page.keyboard.press('Tab');
  assert(await controls.first().evaluate((element) => element === document.activeElement), 'Dialog Tab did not wrap to its first control.');
  await focusedControlIsVisible(page);
  await noOverflow(page);
  await schedulerPreviewIsContained(page, 'dashboard');
}

async function menuPanelFitsViewport(page, menu) {
  const panel = menu.locator(':scope > .console-action-menu-items');
  await panel.waitFor({ state: 'visible' });
  const bounds = await panel.boundingBox();
  const viewport = page.viewportSize();
  assert(bounds && bounds.width > 0 && bounds.height > 0, 'Action menu has no visible panel.');
  assert(bounds.x >= -1 && bounds.y >= -1 && bounds.x + bounds.width <= viewport.width + 1 && bounds.y + bounds.height <= viewport.height + 1, `Action menu leaves viewport: ${JSON.stringify({ bounds, viewport })}`);
  await noOverflow(page);
}

async function menusCase(page) {
  const dashboard = page.locator('main section[x-show="page===\'dashboard\'"]');
  const menus = [dashboard.locator('.page-heading .console-action-menu'), page.locator('.console-utilities')];
  for (const menu of menus) {
    const summary = menu.locator(':scope > summary');
    await summary.focus();
    await page.keyboard.press('Enter');
    await menuPanelFitsViewport(page, menu);
    await menu.locator('.console-action-menu-items button:visible').first().focus();
    await focusedControlIsVisible(page);
    await page.keyboard.press('Escape');
    assert.equal(await menu.evaluate((element) => element.open), false, 'Escape must close the action menu.');
    assert(await summary.evaluate((element) => element === document.activeElement), 'Escape must return focus to the menu summary.');
    await summary.click();
    await menuPanelFitsViewport(page, menu);
    await dashboard.locator('h2').click({ position: { x: 8, y: 8 } });
    assert.equal(await menu.evaluate((element) => element.open), false, 'Outside click must close the action menu.');
  }
  // Keep one real menu open for the screenshot after checking both disclosures.
  await menus[1].locator(':scope > summary').click();
  await menuPanelFitsViewport(page, menus[1]);
  await schedulerPreviewIsContained(page, 'dashboard');
}

async function settingsSchedulerCase(page) {
  await page.evaluate(() => window.Alpine.$data(document.body).navigate('settings'));
  await page.waitForFunction(() => {
    const model = window.Alpine.$data(document.body);
    return model.page === 'settings' && !model.configLoading && !model.filterFile.loading;
  });
  const section = page.locator('main section[x-show="page===\'settings\'"]');
  assert(await section.isVisible(), 'The real settings page must be visible.');
  const tab = page.locator('#settings-tab-scheduler');
  await tab.click();
  await page.waitForFunction(() => {
    const model = window.Alpine.$data(document.body);
    return model.settingsTab === 'scheduler' && model.schedulePreview.valid === true && !model.schedulePreview.loading;
  });
  await settle(page);
  assert.equal(await tab.getAttribute('aria-selected'), 'true', 'Scheduler tab must be selected.');
  const preview = section.locator('#settings-panel-scheduler > .scheduler-layout > .scheduler-summary-card');
  assert.equal(await preview.count(), 1, 'The scheduler preview must remain in its settings layout.');
  assert(await preview.isVisible(), 'The scheduler preview must be visible on its selected settings tab.');
  assert(await preview.getByRole('heading', { name: 'So arbeitet der Scheduler', exact: true }).isVisible());
  await schedulerPreviewIsContained(page, 'settings');
  await noOverflow(page);
  await reducedMotionIsRespected(page);
  assert.equal(await page.evaluate(() => window.Alpine.$data(document.body).configDirty), false, 'Rendering settings must not mutate the saved configuration.');
  await page.evaluate(() => window.scrollTo(0, 0));
  await settle(page);
}

async function busyCase(page) {
  await page.waitForFunction(() => {
    const model = window.Alpine?.$data(document.body);
    return model?.configLoaded && document.body.dataset.workspaceReady && model.progress?.running && model.status?.backup;
  });
  await settle(page);
  const dashboard = page.locator('main section[x-show="page===\'dashboard\'"]');
  const progress = dashboard.locator('.overview-live .live-progress');
  assert(await progress.isVisible(), 'Running transfer must expose its live progress panel.');
  const cancel = progress.getByRole('button', { name: 'Abbrechen', exact: true });
  assert(await cancel.isVisible() && await cancel.isEnabled(), 'Running transfer must expose an enabled cancel action.');
  assert(await dashboard.locator('button[\\@click="runBackup(true)"]').isDisabled(), 'Main start action must be disabled while a transfer is running.');
  assert(await dashboard.locator('button[\\@click="loadPlan(true)"]').isDisabled(), 'Plan/start preparation must be disabled while a transfer is running.');
  assert.equal(await progress.getByRole('progressbar', { name: `Fortschritt ${pairs[0].name}`, exact: true }).getAttribute('aria-valuenow'), '62');
  assert(await progress.getByText('11,2 GiB / 18,0 GiB', { exact: true }).isVisible(), 'Synthetic transfer quantities must be rendered.');
  const banner = page.locator('.global-progress');
  assert(await banner.isVisible(), 'Running transfer must expose the persistent progress bar.');
  assert(await banner.getByRole('button', { name: 'Abbrechen', exact: true }).isVisible());
  const bounds = await banner.boundingBox();
  const viewport = page.viewportSize();
  assert(bounds && bounds.x >= -1 && bounds.y >= -1 && bounds.x + bounds.width <= viewport.width + 1 && bounds.y + bounds.height <= viewport.height + 1, `Persistent progress leaves viewport: ${JSON.stringify({ bounds, viewport })}`);
  await noOverflow(page);
  await schedulerPreviewIsContained(page, 'dashboard');
  await reducedMotionIsRespected(page);
  await page.evaluate(() => window.scrollTo(0, 0));
  await settle(page);
}

async function main() {
  await fs.mkdir(output, { recursive: true });
  const server = http.createServer((request, response) => { serve(request, response).catch((error) => { console.error(error); json(response, { detail: 'Fixture server error' }, 500); }); });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    report.chromium = browser.version();
    assert.equal(browser.version(), report.expectedChromium, 'Installed browser must match the verified pinned Playwright release.');
    const scenarios = [
      { name: 'desktop', viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 },
      { name: 'mobile', viewport: { width: 390, height: 844 }, deviceScaleFactor: 1, isMobile: true, hasTouch: true },
      { name: 'desktop-200-percent-reflow', viewport: { width: 720, height: 500 }, deviceScaleFactor: 2 },
    ];
    for (const scenario of scenarios) for (const colorScheme of ['light', 'dark']) {
      const name = `${scenario.name}-${colorScheme}`;
      const deviceOptions = { ...scenario };
      delete deviceOptions.name;
      const context = await browser.newContext({ ...deviceOptions, colorScheme, reducedMotion: 'reduce', locale: 'de-DE', timezoneId: 'Europe/Berlin', serviceWorkers: 'block', extraHTTPHeaders: { 'X-UI-Case': name } });
      await context.route('**/*', async (route) => {
        const url = new URL(route.request().url());
        if (url.origin === origin) return route.continue();
        report.blockedExternalRequests.push({ case: name, url: url.href });
        return route.abort('blockedbyclient');
      });
      await context.addInitScript((theme) => localStorage.setItem('rclone-sync-theme', theme), colorScheme);
      await context.tracing.start({ screenshots: true, snapshots: true, sources: false });
      const page = await context.newPage();
      const jsErrors = [];
      page.on('pageerror', (error) => jsErrors.push(error.stack || error.message));
      page.on('console', (message) => { if (message.type() === 'error' || (message.type() === 'warning' && /Alpine|Expression Error/.test(message.text()))) jsErrors.push(message.text()); });
      page.setDefaultTimeout(15000);
      try {
        await page.goto(origin, { waitUntil: 'load' });
        await page.waitForFunction(() => window.Alpine?.$data(document.body)?.configLoaded && document.body.dataset.workspaceReady);
        await page.keyboard.press('Tab');
        assert(await page.locator('.skip-link').evaluate((element) => element === document.activeElement), 'First keyboard step must expose the skip link.');
        await focusedControlIsVisible(page);
        await page.keyboard.press('Enter');
        assert(await page.locator('#main-content').evaluate((element) => element === document.activeElement), 'Skip link must focus the main content.');
        for (const [id, label] of pages) {
          await checkCase(page, `${name}-${id}`, async () => {
            await navigate(page, id, label);
            assert.equal(jsErrors.length, 0, jsErrors.join('\n'));
          });
        }
        await navigate(page, 'dashboard', 'Lagebild');
        if (scenario.name !== 'desktop-200-percent-reflow') {
          await checkCase(page, `${name}-menus`, async () => { await menusCase(page); assert.equal(jsErrors.length, 0, jsErrors.join('\n')); });
          await page.keyboard.press('Escape');
          assert.equal(await page.locator('.console-utilities').evaluate((element) => element.open), false);
          await checkCase(page, `${name}-settings-scheduler`, async () => { await settingsSchedulerCase(page); assert.equal(jsErrors.length, 0, jsErrors.join('\n')); });
          await navigate(page, 'dashboard', 'Lagebild');
          await checkCase(page, `${name}-dialog`, async () => { await dialogCase(page); assert.equal(jsErrors.length, 0, jsErrors.join('\n')); });
          await page.keyboard.press('Escape');
          await page.getByRole('dialog', { name: 'Quick-Sync', exact: true }).waitFor({ state: 'hidden' });
          await page.keyboard.press('Control+k');
          const palette = page.getByRole('dialog', { name: 'Befehlssuche', exact: true });
          await palette.waitFor({ state: 'visible' });
          await page.getByRole('combobox', { name: 'Befehle und Ansichten suchen' }).fill('Läufe');
          await page.keyboard.press('Escape');
          await palette.waitFor({ state: 'hidden' });
          await page.goto(`${origin}/login`, { waitUntil: 'load' });
          await checkCase(page, `${name}-login`, async () => {
            const username = page.getByRole('textbox', { name: 'Benutzername', exact: true });
            await username.waitFor({ state: 'visible' });
            await username.focus();
            await page.keyboard.press('Tab');
            assert.equal(await page.locator('input[name="password"]').evaluate((element) => element === document.activeElement), true, 'Login keyboard order must reach password.');
            await focusedControlIsVisible(page);
            assert(await page.getByRole('button', { name: 'Anmelden', exact: true }).isVisible());
            await noOverflow(page);
            await reducedMotionIsRespected(page);
            assert.equal(jsErrors.length, 0, jsErrors.join('\n'));
          });
          if (colorScheme === 'light') {
            await context.setExtraHTTPHeaders({ 'X-UI-Case': `${name}-busy` });
            await page.goto(origin, { waitUntil: 'load' });
            await checkCase(page, `${name}-busy`, async () => { await busyCase(page); assert.equal(jsErrors.length, 0, jsErrors.join('\n')); });
          }
        }
      } catch (error) {
        report.cases.push({ name: `${name}-initialization-or-keyboard`, passed: false, error: error.stack || String(error) });
        await screenshot(page, `${name}-initialization-failure`).catch(() => {});
      } finally {
        await context.tracing.stop({ path: path.join(output, `${name}-trace.zip`) });
        await context.close();
      }
    }
  } finally {
    await browser?.close();
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    report.passed = report.cases.length > 0 && report.cases.every((item) => item.passed) && report.unexpectedRequests.length === 0 && report.blockedExternalRequests.length === 0;
    await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
    await fs.writeFile(path.join(output, 'README.txt'), 'Sicherpfad UI render QA\nAll API values are synthetic; no backend or account was connected.\nSee report.json for checks, pinned browser version and the explicit 200% reflow limitation.\nPNG files show actual production HTML/CSS/JavaScript rendered by Chromium. Traces contain synthetic data only.\n');
    console.log(JSON.stringify({ passed: report.passed, cases: report.cases.length, failures: report.cases.filter((item) => !item.passed).map((item) => item.name), unexpectedRequests: report.unexpectedRequests, blockedExternalRequests: report.blockedExternalRequests }, null, 2));
  }
  assert(report.passed, 'UI render QA failed; inspect the screenshot/trace/report artifacts.');
}

main().catch((error) => { console.error(error.stack || error); process.exitCode = 1; });
