import { expect, test } from "@playwright/test";

const views = ["Overview", "Markets", "Analytics", "Live Safety", "Alerts", "Wallets", "Paper", "Settings"];
const headings = { Markets: "Market Operations", Analytics: "Polymarket Analytics", Wallets: "Wallets & Copy", Paper: "Paper Trading" };

async function navigate(page, name) {
  const button = page.getByRole("navigation", { name: "Primary" }).getByRole("button", { name, exact: true });
  await button.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: headings[name] || name, level: 1, exact: true })).toBeVisible();
}

test.beforeEach(async ({ page, context, baseURL }, testInfo) => {
  const errors = [];
  const blocked = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await context.route("**/*", (route) => {
    if (new URL(route.request().url()).origin === baseURL) return route.continue();
    blocked.push(route.request().url());
    return route.abort();
  });
  testInfo.errorsFromPage = errors;
  testInfo.blockedRequests = blocked;
  await page.goto("/");
  await expect(page.getByText("API ok", { exact: true })).toBeVisible();
  await navigate(page, "Settings");
  await page.getByRole("combobox", { name: "Theme", exact: true }).selectOption(testInfo.project.use.colorScheme);
  await expect(page.locator("html")).toHaveAttribute("data-theme", testInfo.project.use.colorScheme);
  await expect(page.locator(".app-shell")).toHaveCSS("background-color",
    testInfo.project.use.colorScheme === "dark" ? "rgb(20, 25, 27)" : "rgb(241, 244, 243)");
});

test.afterEach(async ({}, testInfo) => {
  expect(testInfo.errorsFromPage).toEqual([]);
  expect(testInfo.blockedRequests).toEqual([]);
});

test("leaderboard category selection is sent without changing its meaning", async ({ page }, testInfo) => {
  await navigate(page, "Analytics");
  const form = page.locator(".leaderboard-form");
  const category = form.getByRole("combobox", { name: "Category", exact: true });
  await expect(form.getByRole("checkbox", { name: "Accounting snapshot", exact: true })).toBeVisible();
  await expect(category).toHaveValue("OVERALL");
  await expect(category.locator("option")).toHaveCount(11);
  await category.selectOption("ESPORTS");
  await page.route("**/api/polymarket/users/leaderboard?*", async (route) => {
    expect(new URL(route.request().url()).searchParams.get("category")).toBe("ESPORTS");
    await route.fulfill({ json: {
      category: "ESPORTS", rows: [], warnings: [],
      counts: { returned: 0, filtered: 0, scanned: 0, mdd_computed: 0 },
      mdd_available: false, mdd_note: "No history requested.", rate_limit: { limited: false },
      completion_reason: "end_of_results", source_enumeration_complete: true,
      source_scope_note: "Fixture category results."
    } });
  });
  const request = page.waitForRequest((request) => new URL(request.url()).pathname === "/api/polymarket/users/leaderboard");
  await form.locator('button[type="submit"]').click();
  expect(new URL((await request).url()).searchParams.get("category")).toBe("ESPORTS");
  await expect(page.getByText("No leaderboard rows matched the filters.")).toBeVisible();
  await expect(category).toHaveValue("ESPORTS");
  await form.screenshot({ path: testInfo.outputPath("leaderboard-category.png") });
  await navigate(page, "Overview");
  await navigate(page, "Analytics");
  await expect(category).toHaveValue("ESPORTS");
});

test("all views have named controls, current navigation and responsive layout", async ({ page }, testInfo) => {
  for (const name of views) {
    await navigate(page, name);
    const navigation = page.getByRole("navigation", { name: "Primary" });
    await expect(navigation.locator('[aria-current="page"]')).toHaveCount(1);
    await expect(navigation.getByRole("button", { name, exact: true })).toHaveAttribute("aria-current", "page");
    for (const role of ["textbox", "combobox", "checkbox", "button"]) {
      for (const control of await page.getByRole(role).all()) {
        await expect(control).toHaveAccessibleName(/\S/);
      }
    }
    const width = await page.evaluate(() => ({ document: document.documentElement.scrollWidth, viewport: innerWidth }));
    expect(width.document).toBeLessThanOrEqual(width.viewport);
    await page.screenshot({ path: testInfo.outputPath(`${name.toLowerCase().replaceAll(" ", "-")}.png`), fullPage: true });
  }
  await page.reload();
  await expect(page.getByRole("heading", { name: "Settings", level: 1 })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Theme", exact: true })).toHaveValue(testInfo.project.use.colorScheme);
  await expect(page.locator("html")).toHaveAttribute("data-theme", testInfo.project.use.colorScheme);
  await navigate(page, "Markets");
  await page.goBack();
  await expect(page.getByRole("heading", { name: "Settings", level: 1 })).toBeVisible();
  await page.goForward();
  await expect(page.getByRole("heading", { name: "Market Operations", level: 1 })).toBeVisible();
  await page.getByRole("navigation", { name: "Primary" }).getByRole("button", { name: "Overview", exact: true }).focus();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("navigation", { name: "Primary" }).getByRole("button", { name: "Markets", exact: true })).toBeFocused();
  await page.getByRole("textbox", { name: "Search markets" }).fill("polymarket");
  await expect(page.getByRole("combobox", { name: "Selected market" })).toHaveValue("polymarket");
});

test("alert validation, creation, edit, toggle, delete confirmation and persistence", async ({ page }, testInfo) => {
  await navigate(page, "Alerts");
  const label = `Browser alert ${testInfo.project.name}`;
  const updated = `${label} edited`;
  await page.getByLabel("Contract/token ID", { exact: true }).fill("123456789");
  await page.getByLabel("Label", { exact: true }).fill(label);
  await page.getByLabel("Threshold", { exact: true }).fill("not-a-number");
  await page.getByRole("button", { name: "Add Alert", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByLabel("Label", { exact: true })).toHaveValue(label);
  await page.getByLabel("Threshold", { exact: true }).fill("0.5");
  await page.getByRole("button", { name: "Add Alert", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("Alert added.");
  let row = page.getByRole("row").filter({ hasText: label });
  await expect(row).toHaveCount(1);
  await page.screenshot({ path: testInfo.outputPath("alert-created.png"), fullPage: true });
  await page.reload();
  await expect(row).toHaveCount(1);
  await row.getByRole("button", { name: "Edit alert", exact: true }).click();
  await page.getByLabel("Label", { exact: true }).fill(updated);
  await page.getByRole("button", { name: "Save Alert", exact: true }).click();
  row = page.getByRole("row").filter({ hasText: updated });
  await expect(row).toContainText(updated);
  await row.getByRole("button", { name: "Disable alert", exact: true }).click();
  await expect(row.getByRole("button", { name: "Enable alert", exact: true })).toBeVisible();
  await page.reload();
  await expect(row.getByRole("button", { name: "Enable alert", exact: true })).toBeVisible();
  page.once("dialog", (dialog) => dialog.dismiss());
  await row.getByRole("button", { name: "Delete alert", exact: true }).click();
  await expect(row).toHaveCount(1);
  await row.getByRole("button", { name: "Edit alert", exact: true }).click();
  page.once("dialog", (dialog) => dialog.accept());
  await row.getByRole("button", { name: "Delete alert", exact: true }).click();
  await expect(row).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Add Alert", exact: true })).toBeVisible();
  await page.reload();
  await expect(row).toHaveCount(0);
});

test("wallet edit and delete persist, and failed saves preserve form input", async ({ page }, testInfo) => {
  await navigate(page, "Wallets");
  const name = `Browser wallet ${testInfo.project.name}`;
  const updated = `${name} edited`;
  const walletForm = page.locator("form.wallet-form");
  await walletForm.getByLabel("Activity identity").fill("0x1111111111111111111111111111111111111111");
  await walletForm.getByLabel("Name", { exact: true }).fill(name);
  await walletForm.getByLabel("Enabled", { exact: true }).uncheck();
  const failure = (route) => route.request().method() === "POST"
    ? route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: "Acceptance save failure" }) })
    : route.fallback();
  await page.route("**/api/wallets", failure);
  await page.getByRole("button", { name: "Add Wallet", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Acceptance save failure");
  await expect(walletForm.getByLabel("Name", { exact: true })).toHaveValue(name);
  await expect(page.getByRole("row").filter({ hasText: name })).toHaveCount(0);
  await page.unroute("**/api/wallets", failure);
  await page.getByRole("button", { name: "Add Wallet", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("Wallet watch added.");
  let row = page.getByRole("row").filter({ hasText: name });
  await expect(row).toHaveCount(1);
  await page.screenshot({ path: testInfo.outputPath("wallet-created.png"), fullPage: true });
  await row.getByRole("button", { name: "Edit wallet", exact: true }).click();
  await walletForm.getByLabel("Name", { exact: true }).fill(updated);
  await page.getByRole("button", { name: "Save Wallet", exact: true }).click();
  row = page.getByRole("row").filter({ hasText: updated });
  await expect(row).toContainText(updated);
  await row.getByRole("button", { name: "Enable wallet", exact: true }).click();
  await expect(row.getByRole("button", { name: "Disable wallet", exact: true })).toBeVisible();
  await page.reload();
  await expect(row.getByRole("button", { name: "Disable wallet", exact: true })).toBeVisible();
  page.once("dialog", (dialog) => dialog.dismiss());
  await row.getByRole("button", { name: "Delete wallet", exact: true }).click();
  await expect(row).toHaveCount(1);
  await row.getByRole("button", { name: "Edit wallet", exact: true }).click();
  page.once("dialog", (dialog) => dialog.accept());
  await row.getByRole("button", { name: "Delete wallet", exact: true }).click();
  await expect(row).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Add Wallet", exact: true })).toBeVisible();
  await expect(walletForm.getByLabel("Name", { exact: true })).toHaveValue("");
  await page.reload();
  await expect(row).toHaveCount(0);
});

test("invalid paper and analytics input cannot create orders or qualifying risk results", async ({ page }) => {
  await navigate(page, "Paper");
  await page.getByLabel("Metadata JSON (optional)", { exact: true }).fill("[]");
  await page.getByRole("button", { name: "Submit Paper Order", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Order metadata must be a JSON object.");
  await expect(page.getByText("No paper-order history.", { exact: true })).toBeVisible();
  await navigate(page, "Analytics");
  const mdd = page.locator("form.direct-mdd-form");
  await mdd.getByLabel("Wallet", { exact: true }).fill("not-a-wallet");
  await mdd.getByRole("button", { name: "Compute", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("valid 0x");
  await expect(mdd.getByLabel("Wallet", { exact: true })).toHaveValue("not-a-wallet");
  await expect(page.locator("section.panel").filter({ has: mdd }).getByText("Observed MDD %", { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByText("No cached MDD audit artifacts.", { exact: true })).toBeVisible();
});
