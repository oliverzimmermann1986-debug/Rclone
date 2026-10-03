const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests/web',
  fullyParallel: true,
  retries: 0,
  timeout: 30000,
  use: { baseURL: 'http://127.0.0.1:4183', timezoneId: 'UTC', screenshot: 'only-on-failure', trace: 'retain-on-failure' },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile-webkit', use: { ...devices['iPhone 13'], defaultBrowserType: 'webkit' } },
  ],
  webServer: { command: 'node scripts/web_ui_test_server.cjs', url: 'http://127.0.0.1:4183', reuseExistingServer: false },
});
