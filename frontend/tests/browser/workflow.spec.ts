import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { expect, type Page, test } from "@playwright/test";

test("real demo, linked selections, comparison, JSON roundtrip and offline HTML", async ({
  page,
  browser,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await page.getByLabel("Appearance").selectOption("dark");
  await expect(page.getByRole("heading", { name: "Range inspector" })).toBeVisible();
  await page.getByRole("button", { name: "Run layout demo" }).click();
  await expect(page.getByRole("status")).toContainText("Demo results match", { timeout: 30000 });
  await expect(page.getByRole("heading", { name: "Range lab" })).toBeVisible();
  await page.getByRole("button", { name: "Compare runs", exact: true }).click();
  await expect(page.locator(".compare-values")).toHaveCount(2);
  await page
    .getByRole("navigation", { name: "Query runs" })
    .getByRole("button", { name: /original.parquet/ })
    .click();
  await page.getByRole("button", { name: "Compare runs", exact: true }).click();
  await expect(page.locator(".compare-values h3").nth(0)).toContainText("original.parquet");
  await expect(page.locator(".compare-values h3").nth(1)).toContainText("sorted.parquet");
  await page
    .getByRole("navigation", { name: "Query runs" })
    .getByRole("button", { name: /sorted.parquet/ })
    .click();
  await page.getByRole("button", { name: "Range lab", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Range lab" })).toBeVisible();
  await page
    .getByRole("button", { name: /Request GET/ })
    .first()
    .click();
  await expect(page.getByRole("heading", { name: "Request details", exact: true })).toBeVisible();
  await expect(page.locator(".chunk-links button").first()).toBeVisible();
  const exactSelection = await page.locator(".details dd code").nth(1).textContent();
  await page.getByRole("button", { name: "Zoom to selected range" }).click();
  await expect(page.getByTestId("zoom-domain")).toHaveText(exactSelection ?? "");
  await expect(page.locator(".plot-caption")).toContainText("ZOOMED RANGE");
  await expect(page.getByRole("button", { name: "Whole file", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Whole file", exact: true }).click();
  await expect(page.locator(".plot-caption")).toContainText("WHOLE FILE");
  await expect(page.locator(".details")).toContainText("Completed");
  await page
    .getByRole("button", { name: /Request HEAD/ })
    .first()
    .click();
  await expect(page.getByRole("button", { name: "Zoom to selected range" })).toBeDisabled();
  await page.getByRole("button", { name: "Structure", exact: true }).click();
  await page.getByRole("textbox", { name: "Filter structures" }).fill("customer_id");
  await page.getByRole("button", { name: "RG 0 · customer_id", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Chunk details" })).toBeVisible();
  await page.getByRole("checkbox", { name: "Related requests only" }).check();
  await expect(page.locator(".request-row").first()).toHaveClass(/related/);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath("range-inspector.png"), fullPage: true });
  await page
    .getByRole("navigation", { name: "Query runs" })
    .getByRole("button", { name: /original.parquet/ })
    .click();
  const jsonDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export JSON" }).click();
  const jsonPath = testInfo.outputPath("export.parquetscope.json");
  await (await jsonDownload).saveAs(jsonPath);
  const data = JSON.parse(await readFile(jsonPath, "utf8"));
  expect(data.capture.complete).toBe(true);
  expect(data.preview).toBeUndefined();
  await page.getByLabel("Import artifact").setInputFiles(jsonPath);
  await expect(page.getByRole("status")).toContainText("Artifact opened locally");
  const screenshotContext = await browser.newContext({
    viewport: { width: 1920, height: 1280 },
    deviceScaleFactor: 2,
  });
  const shot = await screenshotContext.newPage();
  await shot.goto("/");
  await shot.getByLabel("Import artifact").setInputFiles(jsonPath);
  await shot.getByLabel("Appearance").selectOption("light");
  await shot.getByRole("button", { name: "Structure", exact: true }).click();
  await shot.getByRole("textbox", { name: "Filter structures" }).fill("customer_id");
  await shot.getByRole("button", { name: "RG 0 · customer_id", exact: true }).click();
  await shot.getByRole("checkbox", { name: "Related requests only" }).check();
  await shot
    .getByRole("button", { name: /Request GET/ })
    .first()
    .click();
  await expect(shot.locator(".details")).toContainText("Completed");
  await shot.getByRole("checkbox", { name: "Related requests only" }).uncheck();
  await shot.screenshot({ path: testInfo.outputPath("range-lab-hires.png"), fullPage: true });
  await shot.getByRole("button", { name: "Zoom to selected range" }).click();
  await expect(shot.locator(".plot-caption")).toContainText("ZOOMED RANGE");
  await shot.screenshot({ path: testInfo.outputPath("range-lab-zoom-hires.png"), fullPage: true });
  await shot.setViewportSize({ width: 390, height: 844 });
  expect(await shot.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await shot.setViewportSize({ width: 1920, height: 1280 });
  await shot.getByLabel("Appearance").selectOption("dark");
  await shot.screenshot({ path: testInfo.outputPath("range-lab-dark-hires.png"), fullPage: true });
  await screenshotContext.close();
  const htmlDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Offline viewer" }).click();
  const htmlPath = testInfo.outputPath("offline.html");
  await (await htmlDownload).saveAs(htmlPath);
  const offline = await browser.newContext({ offline: true });
  const offlinePage = await offline.newPage();
  const external: string[] = [];
  offlinePage.on("request", (request) => {
    if (/^https?:/.test(request.url())) external.push(request.url());
  });
  await offlinePage.goto(pathToFileURL(htmlPath).href);
  await expect(offlinePage.getByRole("heading", { name: "Range lab" })).toBeVisible();
  await expect(offlinePage.getByText("Offline viewer", { exact: true })).toBeVisible();
  await offlinePage.getByLabel("Import artifact").setInputFiles(jsonPath);
  await expect(offlinePage.getByRole("status")).toContainText("Artifact opened locally");
  await offlinePage
    .getByRole("button", { name: /Request GET/ })
    .first()
    .click();
  await expect(
    offlinePage.getByRole("heading", { name: "Request details", exact: true }),
  ).toBeVisible();
  const reexport = offlinePage.waitForEvent("download");
  await offlinePage.getByRole("button", { name: "Offline viewer" }).click();
  const reexportPath = testInfo.outputPath("reexported.html");
  await (await reexport).saveAs(reexportPath);
  const reexported = await offline.newPage();
  await reexported.goto(pathToFileURL(reexportPath).href);
  await expect(reexported.getByRole("heading", { name: "Range lab" })).toBeVisible();
  expect(external).toEqual([]);
  expect(errors).toEqual([]);
  await offline.close();
});

test("cancel and deadline show separate SQL and capture state", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Run layout demo" }).click();
  await expect(page.getByRole("status")).toContainText("Demo results match", { timeout: 30000 });
  await page.locator(".query-panel").evaluate((node) => node.setAttribute("open", ""));
  await page.getByLabel("SQL query").fill("SELECT sum(sin(i)) FROM range(10000000000) t(i)");
  await page.getByRole("button", { name: "Run query" }).click();
  await expect(page.getByRole("button", { name: "Cancel run" })).toBeVisible();
  await page.getByRole("button", { name: "Cancel run" }).click();
  await expect(page.getByText("SQL cancelled", { exact: true })).toBeVisible({ timeout: 10000 });
  await expect(page.getByRole("alert")).toContainText("Observed subset");
  await page.getByText("Execution settings", { exact: true }).click();
  await page.getByLabel("Deadline (s)").fill("0.5");
  await page.getByText("Execution settings", { exact: true }).click();
  await page.getByRole("button", { name: "Run query" }).click();
  await expect(page.getByText("SQL timeout", { exact: true })).toBeVisible({ timeout: 10000 });
});

test("invalid artifacts, themes, keyboard controls and mobile width", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Import artifact").setInputFiles({
    name: "bad.json",
    mimeType: "application/json",
    buffer: Buffer.from('{"schema_version":"99"}'),
  });
  await expect(page.getByRole("alert")).toContainText("Invalid or unsupported");
  await page.getByLabel("Appearance").selectOption("light");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.getByLabel("Appearance").selectOption("dark");
  await page.getByRole("button", { name: "Open artifact" }).focus();
  await expect(page.getByRole("button", { name: "Open artifact" })).toBeFocused();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("heading", { name: "Range inspector" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
});

test("offline HTML preserves SQL containing closing script tags", async ({
  page,
  browser,
}, testInfo) => {
  await page.request.post("/api/demo");
  await page.goto("/");
  const sql =
    "SELECT '</script><script>window.unexpectedScript = true</script>' AS text FROM input LIMIT 1";
  await page.locator(".query-panel").evaluate((node) => node.setAttribute("open", ""));
  await page.getByLabel("SQL query").fill(sql);
  await page.getByRole("button", { name: "Run query" }).click();
  await expect(page.getByRole("status")).toContainText("Run finished");
  await expect(page.getByText("SQL succeeded", { exact: true })).toBeVisible();
  const pending = page.waitForEvent("download");
  await page.getByRole("button", { name: "Offline viewer" }).click();
  const path = testInfo.outputPath("escaped.html");
  await (await pending).saveAs(path);
  const context = await browser.newContext({ offline: true });
  const viewer = await context.newPage();
  await viewer.goto(pathToFileURL(path).href);
  await expect(viewer.getByRole("heading", { name: "Range lab" })).toBeVisible();
  await viewer.getByText("Captured SQL & measurement sources", { exact: true }).click();
  await expect(viewer.locator(".measurement-notes code")).toHaveText(sql);

  expect(await viewer.evaluate(() => Reflect.get(window, "unexpectedScript"))).toBeUndefined();
  await context.close();
});

async function importRealCapture(page: Page) {
  const demo = await page.request.post("/api/demo");
  expect(demo.ok()).toBe(true);
  const started = await page.request.post("/api/runs", {
    data: { input_id: "demo-sorted", sql: "SELECT sum(amount_cents) FROM input" },
  });
  expect(started.status()).toBe(202);
  const id: string = (await started.json()).id;
  await expect
    .poll(async () => (await (await page.request.get(`/api/jobs/${id}`)).json()).status)
    .toBe("finished");
  const { run } = await (await page.request.get(`/api/jobs/${id}`)).json();
  expect(run.capture.complete).toBe(true);
  await page.goto("/");
  await page.getByLabel("Import artifact").setInputFiles({
    name: "measured.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify(run)),
  });
  await expect(page.getByRole("status")).toContainText("Artifact opened locally");
}

for (const templateState of ["delayed", "failed"] as const) {
  test(`offline export handles a ${templateState} template without producing broken HTML`, async ({
    page,
    browser,
  }, testInfo) => {
    let release = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    await page.route("**/offline.html", async (route) => {
      if (templateState === "failed") await route.fulfill({ status: 503, body: "Unavailable" });
      else {
        await gate;
        await route.continue();
      }
    });
    try {
      await importRealCapture(page);
      const downloads: string[] = [];
      page.on("download", (download) => downloads.push(download.suggestedFilename()));
      await page.getByRole("button", { name: "Offline viewer" }).click();
      await expect(page.getByRole("alert")).toContainText(
        "Offline template is loading or unavailable",
      );
      expect(downloads).toEqual([]);
      if (templateState === "delayed") {
        const response = page.waitForResponse((r) => r.url().endsWith("/offline.html"));
        release();
        await (await response).finished();
        await page.waitForLoadState("networkidle");
        const pending = page.waitForEvent("download");
        await page.getByRole("button", { name: "Offline viewer" }).click();
        const path = testInfo.outputPath("ready.html");
        await (await pending).saveAs(path);
        const context = await browser.newContext({ offline: true });
        try {
          const viewer = await context.newPage();
          const external: string[] = [];
          viewer.on("request", (r) => {
            if (/^https?:/.test(r.url())) external.push(r.url());
          });
          await viewer.goto(pathToFileURL(path).href);
          await expect(viewer.getByRole("heading", { name: "Range lab" })).toBeVisible();
          expect(external).toEqual([]);
        } finally {
          await context.close();
        }
      }
    } finally {
      release();
    }
  });
}

test("demo can be cancelled while the SQL workspace remains collapsed", async ({ page }) => {
  await importRealCapture(page);
  const workspace = page.locator(".query-panel");
  if ((await workspace.getAttribute("open")) !== null)
    await workspace.locator(":scope > summary").click();
  await expect(workspace).not.toHaveAttribute("open");
  let release = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let started = () => {};
  const requestStarted = new Promise<void>((resolve) => {
    started = resolve;
  });
  await page.route("**/api/runs", async (route) => {
    started();
    await gate;
    await route.continue();
  });
  try {
    await page.getByRole("button", { name: "Run layout demo" }).click();
    await requestStarted;
    await expect(page.getByRole("button", { name: "Cancel run" })).toBeVisible();
    await expect(workspace).not.toHaveAttribute("open");
    await page.getByRole("button", { name: "Cancel run" }).click();
    release();
    await expect(page.getByRole("button", { name: "Run layout demo" })).toBeEnabled();
    await expect(page.getByText("SQL cancelled", { exact: true })).toBeVisible();
  } finally {
    release();
  }
});
