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
      http_profile: { response_delay_ms: 0, body_bytes_per_second: null },
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
      expect(body).toEqual({
        input_id: "demo-original",
        sql: "SELECT 1",
        settings,
        http_profile: { response_delay_ms: 0, body_bytes_per_second: null },
      });
      expect(body).not.toHaveProperty("engine");
    }
  });

  it("snapshots the same HTTP profile for single-file and file-set captures", () => {
    const profile = { response_delay_ms: 30, body_bytes_per_second: 4096 };
    const requests = ["file", "set"].map((kind) =>
      runRequest("input", kind as "file" | "set", "SELECT 1", settings, "", "default", profile),
    );
    profile.response_delay_ms = 999;
    for (const request of requests)
      expect(request.http_profile).toEqual({ response_delay_ms: 30, body_bytes_per_second: 4096 });
  });
});
