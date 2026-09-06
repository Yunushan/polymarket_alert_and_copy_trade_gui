import assert from "node:assert/strict";
import test from "node:test";

async function loadConfig(channel) {
  const originalOrigin = process.env.MARKET_SENTINEL_BROWSER_TEST_ORIGIN;
  const originalChannel = process.env.MARKET_SENTINEL_BROWSER_TEST_CHANNEL;
  process.env.MARKET_SENTINEL_BROWSER_TEST_ORIGIN = "http://127.0.0.1:1";
  if (channel) process.env.MARKET_SENTINEL_BROWSER_TEST_CHANNEL = channel;
  else delete process.env.MARKET_SENTINEL_BROWSER_TEST_CHANNEL;
  try {
    return (await import(`../playwright.config.mjs?channel=${channel || "default"}`)).default;
  } finally {
    if (originalOrigin === undefined) delete process.env.MARKET_SENTINEL_BROWSER_TEST_ORIGIN;
    else process.env.MARKET_SENTINEL_BROWSER_TEST_ORIGIN = originalOrigin;
    if (originalChannel === undefined) delete process.env.MARKET_SENTINEL_BROWSER_TEST_CHANNEL;
    else process.env.MARKET_SENTINEL_BROWSER_TEST_CHANNEL = originalChannel;
  }
}

test("every browser engine runs every viewport and theme profile", async () => {
  const config = await loadConfig();
  assert.equal(config.projects.length, 12);
  assert.equal(new Set(config.projects.map((project) => project.name)).size, 12);
  for (const browser of ["chromium", "firefox", "webkit"]) {
    const projects = config.projects.filter((project) => project.use.browserName === browser);
    assert.equal(projects.length, 4);
    for (const theme of ["light", "dark"]) {
      for (const width of [1440, 390]) {
        assert.equal(projects.filter((project) => project.use.colorScheme === theme && project.use.viewport.width === width).length, 1);
      }
    }
  }
  assert.equal(config.retries, 0);
  assert.equal(config.workers, 1);
  assert.equal(config.globalTimeout, 540_000);
  assert.equal(config.use.serviceWorkers, "block");
  assert.equal(config.use.trace, "retain-on-failure");
  assert.ok(config.reporter.some(([reporter, options]) => reporter === "json" && options.outputFile === "test-results/results.json"));
});

test("a local branded-browser override only affects Chromium projects", async () => {
  const config = await loadConfig("msedge");
  assert.equal(config.use.channel, undefined);
  for (const project of config.projects) {
    assert.equal(project.use.channel, project.use.browserName === "chromium" ? "msedge" : undefined);
  }
  assert.deepEqual(config.projects.filter((project) => project.use.browserName === "chromium").map((project) => project.name),
    ["desktop-light", "desktop-dark", "mobile-light", "mobile-dark"]);
});
