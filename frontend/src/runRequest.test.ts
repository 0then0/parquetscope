import { describe, expect, it } from "vitest";
import { runRequest } from "./runRequest";

const settings = {
  threads: 4,
  memory_limit_mb: 512,
  deadline_seconds: 30,
  preview_rows: 30,
  preview_bytes: 65536,
  external_file_cache: false,
  cold_client: true,
};

describe("run request", () => {
  it("sends engine and prefetch for a file set", () => {
    expect(runRequest("set-0", "set", "SELECT 1", settings, "2.0.0", "off")).toEqual({
      input_id: "set-0",
      sql: "SELECT 1",
      engine: "2.0.0",
      settings: { ...settings, disable_parquet_prefetching: true },
    });
    expect(runRequest("set-0", "set", "SELECT 1", settings, "", "default")).toMatchObject({
      engine: null,
      settings: { disable_parquet_prefetching: null },
    });
    expect(runRequest("set-0", "set", "q", settings, "", "on").settings).toMatchObject({
      disable_parquet_prefetching: false,
    });
  });

  it("leaves a stale selection out of single-file and demo runs", () => {
    for (const kind of ["file", undefined] as const) {
      const body = runRequest("demo-original", kind, "SELECT 1", settings, "2.0.0", "off");
      expect(body).toEqual({ input_id: "demo-original", sql: "SELECT 1", settings });
      expect(body).not.toHaveProperty("engine");
    }
  });
});
