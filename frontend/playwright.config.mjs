import { defineConfig } from "@playwright/test";

if (!process.env.MARKET_SENTINEL_BROWSER_TEST_ORIGIN) {
  throw new Error("Run python scripts/verify_browser_workflows.py to start an isolated test backend.");
}

export default defineConfig({
  testDir: "./browser-tests",
  workers: 1,
  retries: 0,
  forbidOnly: Boolean(process.env.CI),
  timeout: 90_000,
  expect: { timeout: 10_000 },
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
    baseURL: process.env.MARKET_SENTINEL_BROWSER_TEST_ORIGIN,
    browserName: "chromium",
    channel: process.env.MARKET_SENTINEL_BROWSER_TEST_CHANNEL || undefined,
    serviceWorkers: "block",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop-light", use: { viewport: { width: 1440, height: 1000 }, colorScheme: "light" } },
    { name: "desktop-dark", use: { viewport: { width: 1440, height: 1000 }, colorScheme: "dark" } },
    { name: "mobile-light", use: { viewport: { width: 390, height: 844 }, colorScheme: "light" } },
    { name: "mobile-dark", use: { viewport: { width: 390, height: 844 }, colorScheme: "dark" } },
  ],
});
