import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { comparabilityNotes, compareComposition, readDifferences } from "./compare";
import {
  computeFileMetrics,
  deriveSetTotals,
  parseArtifact,
  parseArtifactValue,
  partitionPairs,
  splitLogicalPath,
} from "./model";

const lab = (name: string) =>
  readFileSync(
    new URL(`../../docs/pruning-lab/captures/${name}.parquetscope.json`, import.meta.url),
    "utf8",
  );
const legacy = readFileSync(
  new URL("../../docs/real-data/captures/A-1.parquetscope.json", import.meta.url),
  "utf8",
);
const raw = (name: string) => JSON.parse(lab(name));

describe("v2 file-set artifacts", () => {
  it("opens real captures and derives consistent per-file and total metrics", () => {
    const run = parseArtifact(lab("hive-selective-1"));
    expect(run.schema_version).toBe("2");
    expect(run.files).toHaveLength(12);
    expect(run.file_metrics.reduce((n, m) => n + m.get_count, 0)).toBe(run.metrics.get_count);
    expect(run.metrics.files_with_get + run.metrics.files_without_observed_access).toBe(12);
    expect(run.metrics.files_without_observed_access).toBe(9);
    const untouched = run.file_metrics.filter((m) => m.observed_access === "none");
    expect(untouched.every((m) => m.server_written_bytes === "0" && m.coverage?.length === 0)).toBe(
      true,
    );
    expect(run.read).toEqual({ hive_partitioning: true, union_by_name: false });
    expect(run.observations.some((o) => o.phase === "view_creation")).toBe(true);
    expect(run.artifact).toBeTruthy();
  });

  it("keeps same-basename files and equal offsets separate", () => {
    const run = parseArtifact(lab("hive-control-1"));
    expect(new Set(run.files.map((f) => f.name)).size).toBe(2);
    expect(new Set(run.files.map((f) => f.logical_path)).size).toBe(12);
    const starts = run.observations
      .filter((o) => o.response_span?.start === "4")
      .map((o) => o.file_id);
    expect(starts.length).toBeGreaterThan(1);
    expect(new Set(starts).size).toBe(starts.length);
  });

  it("still opens v1 artifacts without inventing v2 evidence", () => {
    const run = parseArtifact(legacy);
    expect(run.schema_version).toBe("1");
    expect(run.files).toHaveLength(1);
    expect(run.read).toBeNull();
    expect(run.engine).toBeNull();
    expect(run.observations.every((o) => o.phase === null)).toBe(true);
    expect(run.files[0].logical_path).toBeNull();
    expect(JSON.stringify(run.artifact)).not.toContain("file_set");
  });

  const corruptions: [string, (v: ReturnType<typeof raw>) => void, RegExp][] = [
    ["unknown file", (v) => (v.observations[0].file_id = "f099"), /unknown file|Invalid/],
    [
      "interval past its file",
      (v) => {
        const o = v.observations.find((x: { response_span: unknown }) => x.response_span);
        o.response_span.end = "999999999999";
      },
      /Invalid byte interval/,
    ],
    [
      "duplicate observation ids",
      (v) => (v.observations[1].id = v.observations[0].id),
      /Duplicate|reconcile|incomplete/,
    ],
    [
      "client/server file disagreement",
      (v) => {
        v.observations.find((x: { file_id: string }) => x.file_id === "f005").file_id = "f001";
      },
      /reconcile|disagree/,
    ],
    ["set total", (v) => (v.metrics.get_count += 1), /disagree/],
    ["file total", (v) => (v.metrics.files[1].server_written_bytes = "1"), /disagree/],
    ["file access state", (v) => (v.metrics.files[2].observed_access = "get"), /disagree/],
    ["non-sequential ids", (v) => (v.file_set.files[1].id = "f007"), /File IDs|Invalid/],
    [
      "duplicate paths",
      (v) => (v.file_set.files[1].logical_path = v.file_set.files[0].logical_path),
      /Duplicate/,
    ],
    [
      "path traversal",
      (v) => (v.file_set.files[0].logical_path = "../x/data-0.parquet"),
      /must not contain/,
    ],
    ["partition mismatch", (v) => (v.file_set.files[0].partition[0].value = "9"), /Partition/],
    ["name mismatch", (v) => (v.file_set.files[0].name = "other.parquet"), /disagrees/],
    ["unknown field", (v) => (v.file_set.files[0].absolute_path = "/x"), /Invalid/],
    ["bad phase", (v) => (v.observations[0].phase = "later"), /Invalid/],
    ["missing server observation", (v) => v.server_observations.pop(), /incomplete|reconcile/],
  ];
  it.each(corruptions)("rejects %s", (_name, mutate, message) => {
    const value = raw("hive-selective-1");
    mutate(value);
    expect(() => parseArtifactValue(value)).toThrow(message);
  });

  it("rejects unsupported versions", () => {
    for (const version of ["0", "3", 2, null]) {
      const value = raw("hive-selective-1");
      value.schema_version = version;
      expect(() => parseArtifactValue(value)).toThrow("unsupported");
    }
  });
});

describe("path rules mirror the backend", () => {
  it("accepts and rejects the same shapes", () => {
    expect(splitLogicalPath("month=1/data.parquet")).toEqual(["month=1", "data.parquet"]);
    expect(partitionPairs("a=1/plain/b=/x.parquet")).toEqual([
      { key: "a", value: "1" },
      { key: "b", value: "" },
    ]);
    for (const bad of ["/abs", "a/../b", "a//b", "a\\b", "", "a/‮b", "é", "a/./b"])
      expect(() => splitLogicalPath(bad), bad).toThrow();
    expect(() => splitLogicalPath(Array(33).fill("d").join("/"))).toThrow("32 segments");
  });
});

describe("run comparison", () => {
  const flat = parseArtifact(lab("flat-selective-1"));
  const hive = parseArtifact(lab("hive-selective-1"));
  it("matches files by content across different layouts", () => {
    const composition = compareComposition(flat, hive);
    expect(composition.identicalContent).toBe(true);
    expect(composition.sameLocations).toBe(false);
    expect(composition.rows).toHaveLength(12);
    expect(composition.rows.every((r) => r.a.length === 1 && r.b.length === 1)).toBe(true);
    const month3 = composition.rows.filter((r) => r.b[0].logical_path?.startsWith("month=3/"));
    expect(month3.every((r) => r.bGet > 0 && r.aGet > 0)).toBe(true);
    const pruned = composition.rows.filter((r) => r.b[0].logical_path?.startsWith("month=5/"));
    expect(pruned.every((r) => r.bGet === 0 && r.bHead === 0 && r.aGet > 0)).toBe(true);
  });
  it("reports read-option differences and limits without claiming equivalence", () => {
    const rows = readDifferences(flat, hive);
    expect(rows.find((r) => r.label === "hive_partitioning")).toMatchObject({
      a: "false",
      b: "true",
      differs: true,
    });
    const notes = comparabilityNotes(flat, hive, compareComposition(flat, hive)).join("\n");
    expect(notes).toContain("logical locations differ");
    expect(notes).toContain("does not establish unordered relational equivalence");
    expect(notes).toContain("do not make two file sets logically equivalent");
    expect(notes).not.toMatch(/faster|cheaper|savings|optimi[sz]e|cost/i);
  });
  it("flags a v1 artifact and different bytes", () => {
    const old = parseArtifact(legacy);
    const composition = compareComposition(old, hive);
    expect(composition.identicalContent).toBe(false);
    const notes = comparabilityNotes(old, hive, composition).join("\n");
    expect(notes).toContain("One artifact is v1");
    expect(notes).toContain("file contents differ");
    expect(readDifferences(old, hive).find((r) => r.label === "httpfs")?.a).toContain("v1");
  });
});

describe("scale", () => {
  it("validates a maximum-size set with 50,000 observations without quadratic work", () => {
    const files = 256;
    const perFile = 200;
    const size = 1_000_000;
    const base = {
      method: "GET" as const,
      status: 206,
      start_ms: 0,
      duration_ms: 1,
      declared_body_bytes: "100",
      request_headers: {},
      response_headers: {},
      engine_start_time: "2026-01-01 00:00:00+00",
      url: "http://127.0.0.1:1/t/x",
      phase: "select",
    };
    const fileEntries = Array.from({ length: files }, (_, i) => ({
      id: `f${String(i + 1).padStart(3, "0")}`,
      name: "data.parquet",
      logical_path: `month=${i}/data.parquet`,
      partition: [{ key: "month", value: String(i) }],
      sha256: "0".repeat(64),
      size: String(size),
      rows: "1",
      row_groups: 1,
      schema_fields: ["a"],
      structures: [
        {
          id: "footer",
          kind: "footer",
          label: "Footer",
          span: { start: "999000", end: String(size) },
          row_group: null,
          column: null,
          rows: null,
          dictionary_offset: null,
          data_offset: null,
          compression: null,
        },
      ],
    }));
    const observations = fileEntries.flatMap((f, i) =>
      Array.from({ length: perFile }, (_, j) => ({
        ...base,
        id: `o-${i}-${j}`,
        file_id: f.id,
        requested_range: `bytes=${j * 100}-${j * 100 + 99}`,
        requested_span: { start: String(j * 100), end: String(j * 100 + 100) },
        response_span: { start: String(j * 100), end: String(j * 100 + 100) },
      })),
    );
    const servers = observations.map((o) => ({
      id: o.id,
      file_id: o.file_id,
      method: "GET",
      requested_range: o.requested_range,
      response_span: o.response_span,
      status: 206,
      start_ms: 0,
      duration_ms: 1,
      declared_body_bytes: "100",
      written_body_bytes: "100",
      complete: true,
      error: null,
    }));
    const seed = raw("hive-selective-1");
    const group = new Map<string, unknown[]>();
    for (const o of observations) group.set(o.file_id, [...(group.get(o.file_id) ?? []), o]);
    const perFileMetrics = fileEntries.map((f) =>
      computeFileMetrics(
        f.id,
        (group.get(f.id) ?? []) as never,
        servers.filter((x) => x.file_id === f.id) as never,
      ),
    );
    const value = {
      ...seed,
      file_set: { ...seed.file_set, files: fileEntries },
      observations,
      server_observations: servers,
      capture: { ...seed.capture, complete: true, reason: null, reconciled: true },
      metrics: {
        ...deriveSetTotals(perFileMetrics, true),
        unknown_reasons: [],
        files: perFileMetrics,
      },
    };
    const started = performance.now();
    const run = parseArtifactValue(value);
    const elapsed = performance.now() - started;
    expect(run.files).toHaveLength(files);
    expect(run.observations).toHaveLength(files * perFile);
    expect(run.metrics.get_count).toBe(files * perFile);
    expect(elapsed).toBeLessThan(5000);
  });
});
