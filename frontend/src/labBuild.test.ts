import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, it } from "vitest";

const frontend = fileURLToPath(new URL("../", import.meta.url));
const builder = fileURLToPath(new URL("../scripts/lab.mjs", import.meta.url));
const evidence = fileURLToPath(new URL("../../docs/real-data/", import.meta.url));

it.each(["report", "capture"])(
  "rejects inconsistent %s SQL before replacing the previous site",
  (changed) => {
    const temp = mkdtempSync(join(tmpdir(), "parquetscope-build-test-"));
    try {
      const input = join(temp, "evidence");
      const output = join(temp, "site");
      cpSync(evidence, input, { recursive: true });
      mkdirSync(output);
      writeFileSync(join(output, "previous.html"), "Keep the previous site");
      const reportPath = join(input, "report.json");
      const report = JSON.parse(readFileSync(reportPath, "utf8"));
      if (changed === "report") report.sql = "SELECT 1 AS wrong_workload";
      else {
        const repetition = report.layouts[0].repetitions[0];
        const capturePath = join(input, repetition.capture);
        const capture = JSON.parse(readFileSync(capturePath, "utf8"));
        capture.sql = "SELECT 1 AS wrong_workload";
        const data = JSON.stringify(capture);
        writeFileSync(capturePath, data);
        // A matching checksum must not hide SQL disagreement with the report.
        repetition.sha256 = createHash("sha256").update(data).digest("hex");
      }
      writeFileSync(reportPath, JSON.stringify(report));
      const result = spawnSync(process.execPath, [builder, input, output], {
        cwd: frontend,
        encoding: "utf8",
      });
      expect(result.status).toBe(1);
      expect(result.stderr).toContain("Capture SQL differs from report");
      expect(readFileSync(join(output, "previous.html"), "utf8")).toBe("Keep the previous site");
    } finally {
      rmSync(temp, { recursive: true, force: true });
    }
  },
);
