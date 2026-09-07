import { defineConfig } from "@playwright/test";

if (!process.env.MARKET_SENTINEL_BROWSER_TEST_ORIGIN) {
  throw new Error("Run python scripts/verify_browser_workflows.py to start an isolated test backend.");
}

const browsers = ["chromium", "firefox", "webkit"];
const profiles = [
  { name: "desktop-light", viewport: { width: 1440, height: 1000 }, colorScheme: "light" },
  { name: "desktop-dark", viewport: { width: 1440, height: 1000 }, colorScheme: "dark" },
  { name: "mobile-light", viewport: { width: 390, height: 844 }, colorScheme: "light" },
  { name: "mobile-dark", viewport: { width: 390, height: 844 }, colorScheme: "dark" },
];

export default defineConfig({
  testDir: "./browser-tests",
  workers: 1,
  retries: 0,
  forbidOnly: Boolean(process.env.CI),
  timeout: 90_000,
  globalTimeout: 540_000,
  expect: { timeout: 10_000 },
  reporter: [["list"], ["html", { open: "never" }], ["json", { outputFile: "test-results/results.json" }]],
  use: {
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
    baseURL: process.env.MARKET_SENTINEL_BROWSER_TEST_ORIGIN,
    serviceWorkers: "block",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: browsers.flatMap((browserName) => profiles.map(({ name, ...profile }) => ({
    name: browserName === "chromium" ? name : `${browserName}-${name}`,
    use: {
      ...profile,
      browserName,
      ...(browserName === "chromium" && process.env.MARKET_SENTINEL_BROWSER_TEST_CHANNEL
        ? { channel: process.env.MARKET_SENTINEL_BROWSER_TEST_CHANNEL } : {}),
    },
  }))),
});
