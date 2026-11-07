import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { comparabilityNotes, compareComposition, readDifferences } from "./compare";
import { isSetRun, parseArtifact, parseArtifactValue, prefetchText } from "./model";

const capture = (name: string, directory = "prefetch-lab") =>
  readFileSync(
    new URL(`../../docs/${directory}/captures/${name}.parquetscope.json`, import.meta.url),
    "utf8",
  );
const lab = (config: string) => parseArtifact(capture(`duckdb-${config}-1`));
const row = (rows: ReturnType<typeof readDifferences>, label: string) =>
  rows.find((r) => r.label === label);

describe("v3 captures of the prefetch lab", () => {
  it("open with engine, setting and evidence, and obey the file-set totals", () => {
    const run = lab("2.0.0-prefetch-off");
    expect(run.schema_version).toBe("3");
    expect(isSetRun(run)).toBe(true);
    expect(run.duckdb_version).toBe("2.0.0");
    expect(run.settings.disable_parquet_prefetching).toBe(true);
    expect(run.engine?.prefetch).toEqual({ requested: true, applied: "true", status: "verified" });
    expect(run.engine?.request_correlation).toBe("ordered_tuple_match");
    expect(run.metrics.get_count).toBe(run.file_metrics.reduce((n, m) => n + m.get_count, 0));
    const serverGets = run.server_observations.filter((s) => s.method === "GET").length;
    expect(serverGets).toBe(run.metrics.get_count);
    expect(prefetchText(run)).toContain("set to true");
  });

  it("keeps server-written bytes apart from engine-confirmed ranges", () => {
    const run = lab("1.5.6-prefetch-off");
    expect(run.metrics.server_written_bytes).toBeTruthy();
    expect(run.metrics.confirmed_range_bytes).toBeTruthy();
    expect(run.metrics.unique_coverage_bytes).not.toBe(run.metrics.confirmed_range_bytes);
  });
});

describe("comparing captures across engines and settings", () => {
  it("shows prefetch and engine differences, and counts the requests", () => {
    const off = lab("2.0.0-prefetch-off");
    const on = lab("2.0.0-prefetch-on");
    const diffs = readDifferences(on, off);
    expect(row(diffs, "Parquet prefetch")?.differs).toBe(true);
    expect(row(diffs, "disable_parquet_prefetching")).toEqual(
      expect.objectContaining({ a: "False", b: "True", differs: true }),
    );
    expect(row(diffs, "DuckDB")?.differs).toBe(false);
    expect(off.metrics.get_count).toBeGreaterThan(on.metrics.get_count * 10);
    expect(
      comparabilityNotes(on, off, compareComposition(on, off)).some((n) =>
        n.includes("disable_parquet_prefetching request differs"),
      ),
    ).toBe(true);
  });

  it("flags different DuckDB, httpfs and request correlation", () => {
    const a = lab("1.5.6-prefetch-off");
    const b = lab("2.0.0-prefetch-off");
    const diffs = readDifferences(a, b);
    expect(row(diffs, "DuckDB")).toEqual(
      expect.objectContaining({ a: "1.5.6", b: "2.0.0", differs: true }),
    );
    expect(row(diffs, "httpfs")?.differs).toBe(true);
    expect(row(diffs, "Request correlation")).toEqual(
      expect.objectContaining({
        a: "response_header_id",
        b: "ordered_tuple_match",
        differs: true,
      }),
    );
    const composition = compareComposition(a, b);
    expect(composition.identicalContent && composition.sameLocations).toBe(true);
    const notes = comparabilityNotes(a, b, composition);
    expect(notes.some((n) => n.includes("DuckDB or httpfs versions differ"))).toBe(true);
    expect(notes.some((n) => n.includes("redacts the request ID"))).toBe(true);
  });

  it("compares a historical v2 capture with a v3 capture without inventing evidence", () => {
    const v2 = parseArtifact(capture("hive-selective-1", "pruning-lab"));
    const v3 = lab("1.5.6-prefetch-on");
    expect(v2.engine?.prefetch).toBeUndefined();
    const diffs = readDifferences(v2, v3);
    expect(row(diffs, "Parquet prefetch")?.a).toContain("Not recorded");
    expect(row(diffs, "Request correlation")?.a).toContain("Not recorded");
    expect(row(diffs, "Parquet prefetch")?.b).toContain("set to false");
    expect(() => compareComposition(v2, v3)).not.toThrow();
  });
});

describe("v3 consistency checks match the backend", () => {
  const base = () => JSON.parse(capture("duckdb-1.5.6-prefetch-off-1"));
  it("reads an omitted prefetch setting as null, like the backend", () => {
    const value = JSON.parse(capture("duckdb-1.5.6-prefetch-off-1"));
    value.settings.disable_parquet_prefetching = undefined;
    expect(() => parseArtifactValue(value)).toThrow("disagree");
    const unset = JSON.parse(capture("duckdb-1.5.6-prefetch-off-1"));
    unset.settings.disable_parquet_prefetching = undefined;
    unset.engine.prefetch = { requested: null, applied: "false", status: "engine_default" };
    expect(parseArtifactValue(unset).engine?.prefetch?.status).toBe("engine_default");
  });
  it("rejects settings that disagree with the evidence", () => {
    const value = base();
    value.settings.disable_parquet_prefetching = false;
    expect(() => parseArtifactValue(value)).toThrow("disagree");
  });
  it("rejects a complete capture whose prefetch was never verified", () => {
    const value = base();
    value.engine.prefetch = { requested: true, applied: null, status: "unverified" };
    expect(() => parseArtifactValue(value)).toThrow("verified prefetch");
  });
  it("rejects verified evidence that contradicts the request", () => {
    const value = base();
    value.engine.prefetch.applied = "false";
    expect(() => parseArtifactValue(value)).toThrow("disagrees");
  });
});
