import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { expect, test } from "@playwright/test";

test("comparison controls and analysis tabs survive refresh and browser history", async ({
  page,
}) => {
  await page.goto("./#compare-B-C");
  await expect(page.locator(".compare-panel .tag")).toHaveText("B ↔ C");
  const first = page
    .getByLabel("Compare with run")
    .locator("option")
    .filter({ hasText: "yellow_tripdata" });
  const firstId = await first.getAttribute("value");
  expect(firstId).not.toBeNull();
  await page.getByLabel("Compare with run").selectOption(firstId ?? "");
  await expect(page.locator(".compare-panel .tag")).toHaveText("B ↔ A");
  await expect(page).toHaveURL(/#compare-B-A$/);
  await page.reload();
  await expect(page.locator(".compare-panel .tag")).toHaveText("B ↔ A");
  await page.getByRole("button", { name: "Range lab", exact: true }).click();
  await expect(page).toHaveURL(/#B$/);
  await page.reload();
  await expect(page.getByRole("heading", { name: "Range lab", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Structure", exact: true }).click();
  await expect(page).toHaveURL(/#B-structure$/);
  await page.getByRole("button", { name: "Method & downloads", exact: true }).click();
  await expect(page.locator("#lab-method")).toBeFocused();
  await expect(page).toHaveURL(/#B-structure$/);
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Physical structure", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Result preview", exact: true }).click();
  await expect(page).toHaveURL(/#B-preview$/);
  await page.reload();
  await expect(page.getByRole("heading", { name: "Result preview", exact: true })).toBeVisible();
  await page.goBack();
  await expect(
    page.getByRole("heading", { name: "Physical structure", exact: true }),
  ).toBeVisible();
});

test("Explore the selected layout always returns to Range lab", async ({ page }) => {
  await page.goto("./#B");
  await page.getByRole("button", { name: "Structure", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Physical structure", exact: true }),
  ).toBeVisible();
  const explore = page.getByRole("link", { name: "Explore B", exact: false });
  await explore.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Range lab", exact: true })).toBeVisible();
  await expect(page).toHaveURL(/#B$/);
  // The same target must also work when the hash is already unchanged.
  await explore.click();
  await expect(page.getByRole("heading", { name: "Range lab", exact: true })).toBeVisible();
  await expect(page.locator(".lab-card.selected")).toContainText("LAYOUT B");
});

test("static three-run lab, linked selection, B/C comparison, project path and themes", async ({
  page,
}, info) => {
  const requests: string[] = [];
  const errors: string[] = [];
  page.on("request", (r) => requests.push(r.url()));
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("./#B");
  await expect(
    page.getByRole("heading", { name: "How Parquet layout changes HTTP reads" }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "repacked.parquet", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Range lab", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /Run query/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Run layout demo" })).toHaveCount(0);
  await page
    .getByRole("button", { name: /Request GET/ })
    .first()
    .click();
  await expect(page.getByRole("heading", { name: "Request details", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Zoom to selected range" }).click();
  await expect(page.getByRole("button", { name: "Whole file", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Structure", exact: true }).click();
  await page.getByLabel("Filter structures").fill("PULocationID");
  await page
    .getByRole("button", { name: /PULocationID/ })
    .first()
    .click();
  await expect(page.getByRole("heading", { name: "Range lab", exact: true })).toBeVisible();
  await page.getByRole("checkbox", { name: "Related requests only" }).check();
  await page.getByRole("link", { name: "Compare B ↔ C", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Run comparison", exact: true })).toBeVisible();
  await expect(page.locator(".compare-panel .tag")).toHaveText("B ↔ C");
  await expect(page.locator(".compare-values").nth(0)).toContainText("118");
  await expect(page.locator(".compare-values").nth(1)).toContainText("10");
  await page.reload();
  await expect(page.locator(".compare-panel .tag")).toHaveText("B ↔ C");
  for (const id of ["A", "B", "C"]) {
    const link = page.getByRole("link", { name: `Explore ${id}`, exact: false });
    await link.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("heading", { name: "Range lab", exact: true })).toBeVisible();
    await expect(page.locator(".lab-card.selected")).toContainText(`LAYOUT ${id}`);
  }
  await page.getByLabel("Appearance").selectOption("light");
  await page.screenshot({ path: info.outputPath("lab-desktop-light.png"), fullPage: true });
  await page.getByLabel("Appearance").selectOption("dark");
  await page.screenshot({ path: info.outputPath("lab-desktop-dark.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: info.outputPath("lab-narrow-dark.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
  expect(requests.filter((url) => /\/api\/|\.parquet(?:$|\?)/.test(url))).toEqual([]);
  expect(requests.filter((url) => !url.startsWith("http://127.0.0.1:8418/parquetscope/"))).toEqual(
    [],
  );
});

test("all nine native captures download", async ({ page }) => {
  await page.goto("./#C");
  await expect(page.getByRole("heading", { name: "Range lab", exact: true })).toBeVisible();
  const captures = page.locator('.lab-downloads a[download][href$=".json"]');
  await expect(captures).toHaveCount(9);
  for (const link of await captures.all()) {
    const pending = page.waitForEvent("download");
    await link.click();
    const download = await pending;
    const path = await download.path();
    const capture = JSON.parse(await readFile(path, "utf8"));
    expect(capture.sql_status).toBe("succeeded");
    expect(capture.capture.complete).toBe(true);
    expect(capture.capture.reconciled).toBe(true);
  }
});

test("standalone viewers and UI export open without network", async ({ page, browser }, info) => {
  await page.goto("./#C");
  await expect(page.getByRole("heading", { name: "Range lab", exact: true })).toBeVisible();
  const viewers = page.getByRole("link", { name: "Standalone viewer · repetition 1" });
  await expect(viewers).toHaveCount(3);
  const paths: string[] = [];
  for (const [i, link] of (await viewers.all()).entries()) {
    const pending = page.waitForEvent("download");
    await link.click();
    const target = info.outputPath(`viewer-${i}.html`);
    await (await pending).saveAs(target);
    paths.push(target);
  }
  const exported = page.waitForEvent("download");
  await page.getByRole("button", { name: "Offline viewer", exact: false }).click();
  const target = info.outputPath("current-export.html");
  await (await exported).saveAs(target);
  paths.push(target);
  const offline = await browser.newContext({ offline: true });
  const network: string[] = [];
  for (const path of paths) {
    const viewer = await offline.newPage();
    viewer.on("request", (r) => {
      if (/^https?:/.test(r.url())) network.push(r.url());
    });
    await viewer.goto(pathToFileURL(path).href);
    await expect(viewer.getByRole("heading", { name: "Range lab", exact: true })).toBeVisible();
    await viewer
      .getByRole("button", { name: /Request GET/ })
      .first()
      .click();
    await expect(
      viewer.getByRole("heading", { name: "Request details", exact: true }),
    ).toBeVisible();
    // Native offline import and comparison use no execution backend either.
    await viewer
      .getByLabel("Import artifact")
      .setInputFiles("../docs/real-data/captures/B-2.parquetscope.json");
    await viewer.getByRole("button", { name: "Compare runs", exact: true }).click();
    await expect(
      viewer.getByRole("heading", { name: "Run comparison", exact: true }),
    ).toBeVisible();
    await viewer.close();
  }
  expect(network).toEqual([]);
  await offline.close();
  for (const name of ["Machine-readable report", "File manifest", "SHA-256 checksums"]) {
    const pending = page.waitForEvent("download");
    await page.getByRole("link", { name, exact: true }).click();
    expect(await (await pending).failure()).toBeNull();
  }
});

test("static capture failure is visible and never falls back to execution API", async ({
  page,
}) => {
  const backend: string[] = [];
  page.on("request", (r) => {
    if (r.url().includes("/api/")) backend.push(r.url());
  });
  await page.route("**/captures/B-1.parquetscope.json", (route) => route.fulfill({ status: 404 }));
  await page.goto("./");
  await expect(page.getByRole("alert")).toContainText("Cannot open real-data lab");
  expect(backend).toEqual([]);
});
