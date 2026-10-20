// Reuse the production application and native artifact files. No second rendering model.

import { createHash } from "node:crypto";
import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const evidence = resolve(process.argv[2] ?? "../docs/real-data");
const out = resolve(process.argv[3] ?? "../.runtime/pages/parquetscope");
if (out === evidence || evidence.startsWith(`${out}/`) || out.startsWith(`${evidence}/`))
  throw new Error("Build output and evidence must be separate directories");
const report = JSON.parse(await readFile(`${evidence}/report.json`, "utf8"));
if (report.status !== "complete" || report.layouts.length !== 3)
  throw new Error("Build requires a complete real-data report");
for (const layout of report.layouts) {
  if (layout.repetitions.length !== 3) throw new Error("Missing repetitions");
  for (const run of layout.repetitions) {
    const data = await readFile(`${evidence}/${run.capture}`);
    if (createHash("sha256").update(data).digest("hex") !== run.sha256)
      throw new Error(`Capture checksum mismatch: ${run.capture}`);
    if (JSON.parse(data).sql !== report.sql)
      throw new Error(`Capture SQL differs from report: ${run.capture}`);
  }
}
await rm(out, { recursive: true, force: true });
await mkdir(out, { recursive: true });
await cp(evidence, out, { recursive: true });
const runtime = resolve("../src/parquetscope/static");
await cp(`${runtime}/assets`, `${out}/assets`, { recursive: true });
await cp(`${runtime}/offline.html`, `${out}/offline.html`);
let html = await readFile(`${runtime}/index.html`, "utf8");
html = html
  .replaceAll('"/assets/', '"./assets/')
  .replace(
    "<title>ParquetScope · Range inspector</title>",
    "<title>How Parquet layout changes HTTP reads · ParquetScope</title>",
  )
  .replace(
    '<div id="root">',
    '<script id="parquetscope-lab" type="application/json">true</script><div id="root">',
  );
await writeFile(`${out}/index.html`, html);
console.log(`Static real-data lab: ${out} (relative assets, hash navigation)`);
