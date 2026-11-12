import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { comparabilityNotes, compareComposition, readDifferences } from "./compare";
import { httpProfileText, parseArtifact, parseArtifactValue } from "./model";

const historical = () =>
  JSON.parse(
    readFileSync(
      new URL(
        "../../docs/prefetch-lab/captures/duckdb-1.5.6-prefetch-off-1.parquetscope.json",
        import.meta.url,
      ),
      "utf8",
    ),
  );
function fixture() {
  const value = historical();
  value.schema_version = "4";
  value.http_profile = {
    requested: { response_delay_ms: 5, body_bytes_per_second: 655360 },
    effective: { response_delay_ms: 5, body_bytes_per_second: 655360 },
    semantics: "capture_shared_token_bucket_v1",
    burst_bytes: "16384",
    write_chunk_bytes: 16384,
  };
  value.server_observations = value.server_observations.map(
    (s: { start_ms: number; duration_ms: number; written_body_bytes: string; method: string }) => ({
      ...s,
      profile_delay_started_ms: s.start_ms,
      profile_delay_ms: 0,
      headers_sent_ms: s.start_ms,
      body_budget_wait_ms: 0,
      body_write_started_ms: s.method === "GET" ? s.start_ms : null,
      body_write_finished_ms: s.method === "GET" ? s.start_ms + s.duration_ms : null,
      body_write_ms: 0,
      reserved_body_bytes: s.written_body_bytes,
      termination: "completed",
    }),
  );
  return value;
}

describe("HTTP profile artifacts", () => {
  it("keeps old profiles explicitly unknown and distinguishes recorded unrestricted service", () => {
    const old = parseArtifactValue(historical());
    expect(old.http_profile).toBeNull();
    expect(httpProfileText(old.http_profile?.effective)).toContain("Not recorded");
    expect(httpProfileText({ response_delay_ms: 0, body_bytes_per_second: null })).toBe(
      "Unrestricted (recorded)",
    );
  });

  it("roundtrips requested/effective profiles and server timing evidence", () => {
    const value = fixture();
    const run = parseArtifactValue(value);
    expect(run.http_profile).toEqual(value.http_profile);
    expect(run.server_observations[0].termination).toBe("completed");
    expect(parseArtifact(JSON.stringify(run.artifact)).http_profile).toEqual(run.http_profile);
    expect(
      readDifferences(parseArtifactValue(historical()), run).find(
        (r) => r.label === "HTTP profile (effective)",
      ),
    ).toMatchObject({ differs: true, a: "Not recorded (artifact before v4)" });
  });

  it("reports different profiles and unsuccessful SQL independently of complete observations", () => {
    const a = parseArtifactValue(fixture());
    const value = fixture();
    value.http_profile.requested.response_delay_ms =
      value.http_profile.effective.response_delay_ms = 30;
    value.sql_status = "timeout";
    const b = parseArtifactValue(value);
    const notes = comparabilityNotes(a, b, compareComposition(a, b));
    expect(notes.some((n) => n.includes("HTTP profiles differ"))).toBe(true);
    expect(notes.some((n) => n.includes("not a successful benchmark result"))).toBe(true);
  });

  it.each([-1, 60001, 0.5, "20", true])("rejects invalid response delay %s", (delay) => {
    const value = fixture();
    value.http_profile.requested.response_delay_ms = delay;
    expect(() => parseArtifactValue(value)).toThrow();
  });
  it.each([0, -1, 1000000001, 1.5, "1024", true])("rejects invalid body rate %s", (rate) => {
    const value = fixture();
    value.http_profile.requested.body_bytes_per_second = rate;
    expect(() => parseArtifactValue(value)).toThrow();
  });
  it("rejects mismatched effective profiles, burst evidence and byte accounting", () => {
    const mismatched = fixture();
    mismatched.http_profile.effective.response_delay_ms++;
    expect(() => parseArtifactValue(mismatched)).toThrow("profiles");
    const burst = fixture();
    burst.http_profile.burst_bytes = "65536";
    expect(() => parseArtifactValue(burst)).toThrow("limiter");
    const bytes = fixture();
    bytes.server_observations.find(
      (s: { method: string }) => s.method === "GET",
    ).reserved_body_bytes = "0";
    expect(() => parseArtifactValue(bytes)).toThrow("reserved");
  });
  it("rejects timing boundaries outside the request and completion/termination contradictions", () => {
    const time = fixture();
    time.server_observations[0].headers_sent_ms = -1;
    expect(() => parseArtifactValue(time)).toThrow();
    const termination = fixture();
    termination.server_observations[0].termination = "cancelled";
    expect(() => parseArtifactValue(termination)).toThrow("Completion");
  });
  it.each(["requested", "effective"])("requires every field in the recorded %s profile", (key) => {
    for (const field of ["response_delay_ms", "body_bytes_per_second"]) {
      const value = fixture();
      delete value.http_profile[key][field];
      expect(() => parseArtifactValue(value)).toThrow();
    }
  });
  it("requires recorded limiter semantics", () => {
    const value = fixture();
    delete value.http_profile.semantics;
    expect(() => parseArtifactValue(value)).toThrow();
  });
});
