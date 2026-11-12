import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { expect, test } from "@playwright/test";

test.use({ baseURL: "http://127.0.0.1:8412" });

test("HTTP profile controls snapshot a capture and survive offline comparison", async ({
  page,
  browser,
}, testInfo) => {
  await page.goto("/");
  await page.locator(".query-panel").evaluate((node) => node.setAttribute("open", ""));
  await page.getByLabel("Input file").selectOption({ label: "pruning-lab-hive · 8 files" });
  await page.getByLabel("SQL query").fill("SELECT sum(amount_cents) FROM input WHERE month = 3");
  const delay = page.getByLabel("Response delay (ms)");
  const rate = page.getByLabel("Shared body rate (B/s)");
  await delay.fill("1.5");
  await expect(page.getByRole("button", { name: "Run query" })).toBeDisabled();
  await delay.fill("20");
  await rate.fill("1048576");
  await expect(page.locator("#http-profile-help")).toContainText("deadline still applies");
  await page.getByLabel("Appearance").selectOption("dark");
  await page.screenshot({
    path: testInfo.outputPath("http-profile-controls-dark.png"),
    fullPage: true,
  });
  await page.getByLabel("Appearance").selectOption("light");
  const sent = page.waitForRequest(
    (request) => request.url().endsWith("/api/runs") && request.method() === "POST",
  );
  await page.getByRole("button", { name: "Run query" }).click();
  expect((await sent).postDataJSON().http_profile).toEqual({
    response_delay_ms: 20,
    body_bytes_per_second: 1048576,
  });
  // Draft edits belong to the next capture; the running request already owns its profile.
  await delay.fill("40");
  await rate.fill("2097152");
  await expect(page.getByRole("status")).toContainText("Run finished");
  const firstDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export JSON" }).click();
  const firstPath = testInfo.outputPath("first.json");
  await (await firstDownload).saveAs(firstPath);
  const first = JSON.parse(await readFile(firstPath, "utf8"));
  expect(first.schema_version).toBe("4");
  expect(first.sql_status).toBe("succeeded");
  expect(first.capture.complete).toBe(true);
  expect(first.server_observations.length).toBeGreaterThan(0);
  expect(first.http_profile.requested).toEqual({
    response_delay_ms: 20,
    body_bytes_per_second: 1048576,
  });
  expect(first.http_profile.effective).toEqual(first.http_profile.requested);
  expect(
    first.server_observations.every(
      (s: { profile_delay_ms: number; reserved_body_bytes: string; written_body_bytes: string }) =>
        s.profile_delay_ms > 0 && BigInt(s.reserved_body_bytes) >= BigInt(s.written_body_bytes),
    ),
  ).toBe(true);
  await page.locator(".query-panel").evaluate((node) => node.setAttribute("open", ""));
  await page.getByRole("button", { name: "Run query" }).click();
  await expect(page.getByRole("status")).toContainText("Run finished");
  await expect(page.locator(".http-profile-details > summary")).toContainText("40 ms");
  await page.locator(".http-profile-details > summary").click();
  await page.getByText(/Server request timings ·/).click();
  await expect(page.locator(".http-profile-details table")).toContainText("completed");
  await page.screenshot({ path: testInfo.outputPath("http-profile-wide.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("http-profile-mobile.png"), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.getByRole("button", { name: "Compare runs", exact: true }).click();
  await expect(
    page.locator(".difference-row.differs", { hasText: "HTTP profile (effective)" }),
  ).toBeVisible();
  const htmlDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Offline viewer" }).click();
  const htmlPath = testInfo.outputPath("http-profile.html");
  await (await htmlDownload).saveAs(htmlPath);
  const offline = await browser.newContext({ offline: true });
  const viewer = await offline.newPage();
  await viewer.goto(pathToFileURL(htmlPath).href);
  await expect(viewer.locator(".http-profile-details > summary")).toContainText("40 ms");
  await viewer.locator(".http-profile-details > summary").click();
  await expect(viewer.locator(".http-profile-details")).toContainText(
    "capture_shared_token_bucket_v1",
  );
  await viewer.getByText(/Server request timings ·/).click();
  await expect(viewer.locator(".http-profile-details table")).toContainText("completed");
  await viewer.getByLabel("Import artifact").setInputFiles(firstPath);
  await viewer.getByRole("button", { name: "Compare runs", exact: true }).click();
  await expect(
    viewer.locator(".difference-row.differs", { hasText: "HTTP profile (effective)" }),
  ).toBeVisible();
  await offline.close();
});

test("historical artifacts explicitly show an unrecorded HTTP profile", async ({ page }) => {
  await page.goto("/");
  await page
    .getByLabel("Import artifact")
    .setInputFiles("../docs/prefetch-lab/captures/duckdb-1.5.6-prefetch-off-1.parquetscope.json");
  await expect(page.locator(".http-profile-details > summary")).toContainText(
    "Not recorded (artifact before v4)",
  );
});
