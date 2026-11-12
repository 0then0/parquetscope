import { readFile, writeFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { expect, type Page, test } from "@playwright/test";

test.use({ baseURL: "http://127.0.0.1:8412" });

const SELECTIVE = "SELECT count(*) AS n, sum(amount_cents) AS total FROM input WHERE month = 3";
const HIVE = "pruning-lab-hive · 8 files";
const FLAT = "pruning-lab-flat · 8 files";

async function capture(page: Page, input: string, sql: string) {
  await page.locator(".query-panel").evaluate((node) => node.setAttribute("open", ""));
  await page.getByLabel("Input file").selectOption({ label: input });
  await page.getByLabel("SQL query").fill(sql);
  await page.getByRole("button", { name: "Run query" }).click();
  await expect(page.getByRole("status")).toContainText("Run finished", { timeout: 30000 });
}
async function exportJson(page: Page, path: string) {
  const pending = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export JSON" }).click();
  await (await pending).saveAs(path);
}

test("file-set capture: overview, per-file inspector, comparison and offline viewer", async ({
  page,
  browser,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await capture(page, HIVE, SELECTIVE);
  const overview = page.locator(".file-overview");
  await expect(overview.getByRole("heading", { name: "File set overview" })).toBeVisible();
  // Binding reads the first file; the selective predicate then needs only month=3.
  await expect(overview).toContainText("8 files · 3 with GET · 0 HEAD only · 5 without observed");
  await page.getByLabel("Filter files by observed access").selectOption("none");
  await expect(overview.locator("tbody tr")).toHaveCount(5);
  await expect(overview.locator("tbody")).not.toContainText("month=3");
  await expect(overview.locator("tbody tr").first()).toContainText("No observed requests");
  await page.getByLabel("Filter files by observed access").selectOption("get");
  await expect(overview.locator("tbody tr")).toHaveCount(3);
  await page.getByLabel("Filter files by path").fill("month=3/data-1");
  await expect(overview.locator("tbody tr")).toHaveCount(1);
  await page.screenshot({ path: testInfo.outputPath("file-overview.png"), fullPage: true });
  await page.getByRole("button", { name: "Inspect month=3/data-1.parquet" }).click();
  await expect(page.getByRole("heading", { name: "Range lab" })).toBeVisible();
  await expect(page.getByLabel("Inspected file")).toContainText("month=3/data-1.parquet");
  await page
    .getByRole("button", { name: /Request GET/ })
    .first()
    .click();
  await expect(page.locator(".details")).toContainText("Completed");
  await page.getByLabel("Inspected file").selectOption({ label: "month=1/data-1.parquet" });
  await expect(page.locator(".plot-caption")).toContainText("0 HTTP observations");
  await page.getByLabel("Inspected file").selectOption({ label: "month=3/data-0.parquet" });
  await page.getByRole("button", { name: "Structure", exact: true }).click();
  await expect(page.locator(".panel-heading")).toContainText("month=3/data-0.parquet");

  await capture(page, FLAT, SELECTIVE);
  await expect(page.locator(".file-overview")).toContainText("8 files · 8 with GET");
  await page.getByRole("button", { name: "Compare runs", exact: true }).click();
  await expect(page.locator(".compare-values")).toHaveCount(2);
  await expect(page.locator(".compare-panel .panel-heading").first()).toContainText(
    "Identical file bytes (SHA-256) at different logical locations",
  );
  await expect(page.locator(".compare-files tbody tr")).toHaveCount(8);
  await expect(
    page.locator(".difference-row.differs", { hasText: "hive_partitioning" }),
  ).toBeVisible();
  await expect(page.locator(".reading-notes")).toContainText("do not make two file sets logically");
  await page.screenshot({ path: testInfo.outputPath("compare-sets.png"), fullPage: true });

  const hivePath = testInfo.outputPath("hive.parquetscope.json");
  await page
    .getByRole("navigation", { name: "Query runs" })
    .getByRole("button", { name: /pruning-lab-hive/ })
    .click();
  await exportJson(page, hivePath);
  const exported = JSON.parse(await readFile(hivePath, "utf8"));
  expect(exported.schema_version).toBe("4");
  expect(exported.file_set.files).toHaveLength(8);
  expect(exported.preview).toBeUndefined();
  // Paths stay relative to the manifest: no absolute source location is exported.
  expect(JSON.stringify(exported)).not.toMatch(/parquetscope-fixture|\/private\/|\/tmp\//);

  await page
    .getByRole("navigation", { name: "Query runs" })
    .getByRole("button", { name: /pruning-lab-flat/ })
    .click();
  const htmlDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Offline viewer" }).click();
  const htmlPath = testInfo.outputPath("flat.viewer.html");
  await (await htmlDownload).saveAs(htmlPath);

  const offline = await browser.newContext({ offline: true });
  const viewer = await offline.newPage();
  const external: string[] = [];
  viewer.on("request", (request) => {
    if (/^https?:/.test(request.url())) external.push(request.url());
  });
  viewer.on("pageerror", (e) => errors.push(e.message));
  await viewer.goto(pathToFileURL(htmlPath).href);
  await expect(viewer.getByRole("heading", { name: "File set overview" })).toBeVisible();
  await viewer.getByLabel("Import artifact").setInputFiles(hivePath);
  await expect(viewer.getByRole("status")).toContainText("Artifact opened locally");
  await viewer.getByRole("button", { name: "Compare runs", exact: true }).click();
  await expect(viewer.locator(".compare-values")).toHaveCount(2);
  await expect(viewer.locator(".compare-files tbody tr")).toHaveCount(8);
  expect(external).toEqual([]);
  expect(errors).toEqual([]);
  await offline.close();
});

test("a v1 single-file artifact opens without file-set views and compares with a set", async ({
  page,
}) => {
  const legacy = "../docs/real-data/captures/A-1.parquetscope.json";
  const legacyId: string = JSON.parse(await readFile(legacy, "utf8")).id;
  await page.goto("/");
  await page.getByLabel("Import artifact").setInputFiles(legacy);
  await expect(page.getByRole("status")).toContainText("Artifact opened locally");
  await expect(page.getByRole("heading", { name: "Range lab" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Files", exact: true })).toHaveCount(0);
  await capture(page, HIVE, SELECTIVE);
  await page.getByRole("button", { name: "Compare runs", exact: true }).click();
  await page.getByLabel("Compare with run").selectOption(legacyId);
  await expect(page.locator(".reading-notes")).toContainText("One artifact is v1");
  await expect(page.locator(".difference-row", { hasText: "httpfs" })).toContainText(
    "Not recorded (v1 artifact)",
  );
});

test("corrupted file-set artifacts are rejected before display", async ({ page }, testInfo) => {
  await page.goto("/");
  await capture(page, HIVE, SELECTIVE);
  const source = testInfo.outputPath("source.json");
  await exportJson(page, source);
  const good = JSON.parse(await readFile(source, "utf8"));
  const cases: Record<string, [(value: typeof good) => void, string]> = {
    "unknown file": [
      (v) => {
        v.observations[0].file_id = "f099";
      },
      "Observation refers to an unknown file",
    ],
    "interval outside its file": [
      (v) => {
        const o = v.observations.find((x: { response_span: unknown }) => x.response_span);
        o.response_span.end = "999999999";
      },
      "Invalid byte interval",
    ],
    "inflated total": [
      (v) => {
        v.metrics.get_count += 1;
      },
      "Artifact metrics disagree",
    ],
    "duplicate logical path": [
      (v) => {
        v.file_set.files[1].logical_path = v.file_set.files[0].logical_path;
      },
      "Duplicate logical paths",
    ],
    "unsafe path": [
      (v) => {
        v.file_set.files[0].logical_path = "../escape/data-0.parquet";
      },
      "must not contain empty",
    ],
  };
  for (const [name, [mutate, message]] of Object.entries(cases)) {
    const bad = structuredClone(good);
    mutate(bad);
    const path = testInfo.outputPath(`${name.replaceAll(" ", "-")}.json`);
    await writeFile(path, JSON.stringify(bad));
    await page.getByLabel("Import artifact").setInputFiles(path);
    await expect(page.getByRole("alert").first(), name).toContainText(message);
  }
});

test("engine and prefetch selection are captured, recorded and compared", async ({
  page,
  request,
}, testInfo) => {
  const session = await (await request.get("/api/session")).json();
  const prepared = (session.engines as { version: string; prepared: boolean }[]).filter(
    (e) => e.prepared,
  );
  test.skip(prepared.length < 2, "Prepare both engines with `parquetscope setup --engine X`");
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  // The selectors apply to file sets only: they stay disabled while a single file is selected.
  await page.locator(".query-panel").evaluate((node) => node.setAttribute("open", ""));
  await page.getByLabel("Input file").selectOption({ label: HIVE });
  for (const [engine, prefetch] of [
    ["1.5.6", "default"],
    ["2.0.0", "off"],
  ]) {
    await page.locator(".query-panel").evaluate((node) => node.setAttribute("open", ""));
    await page
      .locator(".settings")
      .evaluate((node) => node.closest("details")?.setAttribute("open", ""));
    await page
      .locator(".settings label", { hasText: /^Engine/ })
      .locator("select")
      .selectOption(engine);
    await page
      .locator(".settings label", { hasText: /^Parquet prefetch/ })
      .locator("select")
      .selectOption(prefetch);
    await capture(page, HIVE, SELECTIVE);
  }
  await page.getByRole("button", { name: "Compare runs", exact: true }).click();
  // Earlier tests leave runs in the shared session: compare the two made here: the latest other run is the first engine run.
  const partner = page.getByLabel("Compare with run");
  await partner.selectOption({ index: (await partner.locator("option").count()) - 1 });
  const compare = page.locator(".compare-panel");
  await expect(compare.locator(".compare-values")).toHaveCount(2);
  await expect(compare).toContainText("1.5.6 · httpfs");
  await expect(compare).toContainText("2.0.0 · httpfs");
  await expect(compare).toContainText("default → false");
  await expect(compare).toContainText("true → true");
  await expect(
    compare.locator(".difference-row.differs", { hasText: "DuckDB" }).first(),
  ).toBeVisible();
  await expect(
    compare.locator(".difference-row.differs", { hasText: "Parquet prefetch" }),
  ).toBeVisible();
  await expect(compare.getByText("Server records (GET / HEAD)")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("compare-engines.png"), fullPage: true });
  const path = testInfo.outputPath("engine.parquetscope.json");
  await exportJson(page, path);
  const exported = JSON.parse(await readFile(path, "utf8"));
  expect(exported.duckdb_version).toBe("2.0.0");
  expect(exported.settings.disable_parquet_prefetching).toBe(true);
  expect(exported.engine.prefetch).toEqual({
    requested: true,
    applied: "true",
    status: "verified",
  });
  expect(errors).toEqual([]);
});
