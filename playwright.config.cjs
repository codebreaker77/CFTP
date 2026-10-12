const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "./tests/browser",
  workers: 1,
  timeout: 30000,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:8791",
    viewport: { width: 1440, height: 1000 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH }
      : {},
  },
  webServer: {
    command: `python -m cftp.web --demo --port 8791 --cftp-port 9001 --storage-dir .web-run/browser-${process.pid}`,
    url: "http://127.0.0.1:8791/api/status",
    reuseExistingServer: false,
    timeout: 15000,
  },
});
