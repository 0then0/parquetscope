import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { bytes, intersects, parseArtifact, percent, sumSpans, union } from "./model";

describe("exact byte arithmetic", () => {
  it("preserves offsets beyond Number.MAX_SAFE_INTEGER", () => {
    const a = { start: "9007199254740993", end: "9007199254741003" };
    expect(sumSpans([a])).toBe(10n);
    expect(intersects(a, { start: "9007199254741002", end: "9007199254741004" })).toBe(true);
    expect(percent("9007199254740993", "18014398509481986")).toBe(50);
    expect(bytes(null)).toBe("Unknown");
  });
  it("unions containment and adjacent ranges without mutating input", () => {
    const spans = [
      { start: "3", end: "7" },
      { start: "0", end: "10" },
      { start: "10", end: "12" },
      { start: "20", end: "25" },
    ];
    expect(union(spans)).toEqual([
      { start: "0", end: "12" },
      { start: "20", end: "25" },
    ]);
    expect(sumSpans(spans)).toBe(21n);
    expect(sumSpans(union(spans))).toBe(17n);
    expect(spans[1].end).toBe("10");
  });
  it("rejects invalid JSON and unsupported schema versions", () => {
    expect(() => parseArtifact("{bad")).toThrow("Invalid JSON");
    expect(() => parseArtifact('{"schema_version":"99"}')).toThrow("unsupported");
  });
  it("validates the generated artifact schema tracks the backend contract", () => {
    const schema = JSON.parse(
      readFileSync(new URL("./artifact-schema.json", import.meta.url), "utf8"),
    );
    expect(schema.properties.schema_version.const).toBe("1");
    expect(schema.$defs.Span.properties.start.type).toBe("string");
  });
});
