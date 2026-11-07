import { type FileEntry, fileTitle, prefetchText, type Run } from "./model";

export type ContentRow = {
  sha256: string;
  a: FileEntry[];
  b: FileEntry[];
  aGet: number;
  aHead: number;
  aWrote: bigint;
  bGet: number;
  bHead: number;
  bWrote: bigint;
};
export type Composition = {
  /** Same multiset of (SHA-256, size): identical stored bytes, whatever the locations. */
  identicalContent: boolean;
  sameLocations: boolean;
  rows: ContentRow[];
};
export type Difference = { label: string; a: string; b: string; differs: boolean };

const NOT_RECORDED = "Not recorded (v1 artifact)";
const NOT_RECORDED_V3 = "Not recorded (artifact before v3)";

function tally(run: Run, files: FileEntry[]) {
  const metrics = new Map(run.file_metrics.map((m) => [m.file_id, m]));
  let get = 0;
  let head = 0;
  let wrote = 0n;
  for (const f of files) {
    const m = metrics.get(f.id);
    if (!m) continue;
    get += m.get_count;
    head += m.head_count;
    wrote += BigInt(m.server_written_bytes);
  }
  return { get, head, wrote };
}
function groupByContent(run: Run) {
  const groups = new Map<string, FileEntry[]>();
  for (const f of run.files) {
    const key = `${f.sha256}:${f.size}`;
    const group = groups.get(key);
    if (group) group.push(f);
    else groups.set(key, [f]);
  }
  return groups;
}

/** Linear in the number of files: groups by content hash, never compares every pair. */
export function compareComposition(a: Run, b: Run): Composition {
  const ga = groupByContent(a);
  const gb = groupByContent(b);
  const keys = [...new Set([...ga.keys(), ...gb.keys()])];
  const rows = keys.map((key) => {
    const fa = ga.get(key) ?? [];
    const fb = gb.get(key) ?? [];
    const ta = tally(a, fa);
    const tb = tally(b, fb);
    return {
      sha256: key.split(":")[0],
      a: fa,
      b: fb,
      aGet: ta.get,
      aHead: ta.head,
      aWrote: ta.wrote,
      bGet: tb.get,
      bHead: tb.head,
      bWrote: tb.wrote,
    };
  });
  rows.sort((x, y) => fileTitle(x.a[0] ?? x.b[0]).localeCompare(fileTitle(y.a[0] ?? y.b[0])));
  const locations = (r: Run) => r.files.map((f) => f.logical_path ?? f.name).sort();
  return {
    identicalContent: rows.every((r) => r.a.length === r.b.length),
    sameLocations: JSON.stringify(locations(a)) === JSON.stringify(locations(b)),
    rows,
  };
}

const flag = (v: boolean | undefined) => (v === undefined ? NOT_RECORDED : v ? "true" : "false");

export function readDifferences(a: Run, b: Run): Difference[] {
  const rows: [string, string, string][] = [
    ["hive_partitioning", flag(a.read?.hive_partitioning), flag(b.read?.hive_partitioning)],
    ["union_by_name", flag(a.read?.union_by_name), flag(b.read?.union_by_name)],
    ["DuckDB", a.duckdb_version, b.duckdb_version],
    [
      "httpfs",
      a.engine ? (a.engine.httpfs_version ?? "Unknown") : NOT_RECORDED,
      b.engine ? (b.engine.httpfs_version ?? "Unknown") : NOT_RECORDED,
    ],
    [
      "Engine environment",
      a.engine?.environment ?? NOT_RECORDED_V3,
      b.engine?.environment ?? NOT_RECORDED_V3,
    ],
    [
      "Worker Python",
      a.engine?.python_version ?? NOT_RECORDED_V3,
      b.engine?.python_version ?? NOT_RECORDED_V3,
    ],
    [
      "Request correlation",
      a.engine?.request_correlation ?? NOT_RECORDED_V3,
      b.engine?.request_correlation ?? NOT_RECORDED_V3,
    ],
    ["Parquet prefetch", prefetchText(a), prefetchText(b)],
    [
      "Bound columns",
      a.engine?.bound_schema?.map((c) => `${c.name} ${c.type}`).join(", ") ?? NOT_RECORDED,
      b.engine?.bound_schema?.map((c) => `${c.name} ${c.type}`).join(", ") ?? NOT_RECORDED,
    ],
  ];
  const keys = [
    ...new Set([
      ...Object.keys(a.engine?.effective_settings ?? {}),
      ...Object.keys(b.engine?.effective_settings ?? {}),
    ]),
  ].sort();
  for (const key of keys)
    rows.push([
      key,
      a.engine ? (a.engine.effective_settings[key] ?? "Unknown") : NOT_RECORDED,
      b.engine ? (b.engine.effective_settings[key] ?? "Unknown") : NOT_RECORDED,
    ]);
  rows.push(["Execution settings", JSON.stringify(a.settings), JSON.stringify(b.settings)]);
  return rows.map(([label, x, y]) => ({ label, a: x, b: y, differs: x !== y }));
}

/** Facts that limit what the comparison can support. Not advice, and no equivalence claim. */
export function comparabilityNotes(a: Run, b: Run, composition: Composition): string[] {
  const notes: string[] = [];
  if (!composition.identicalContent)
    notes.push(
      "The file contents differ (SHA-256). Request totals describe different stored bytes.",
    );
  else if (!composition.sameLocations)
    notes.push("The file bytes are identical but their logical locations differ.");
  if (a.sql !== b.sql) notes.push("The captured SQL texts differ.");
  if (a.read?.hive_partitioning !== b.read?.hive_partitioning)
    notes.push(
      "Read options differ or are not recorded in one artifact; they can change which columns, rows and files the view exposes.",
    );
  if (
    a.duckdb_version !== b.duckdb_version ||
    a.engine?.httpfs_version !== b.engine?.httpfs_version
  )
    notes.push("DuckDB or httpfs versions differ or are not recorded.");
  if (a.engine?.prefetch?.requested !== b.engine?.prefetch?.requested)
    notes.push(
      "The disable_parquet_prefetching request differs or is not recorded. Its effect depends on the engine version and the storage, and a loopback result does not carry over to S3.",
    );
  if (a.engine?.request_correlation !== b.engine?.request_correlation)
    notes.push(
      "Engine log rows were matched to server records differently: DuckDB 2.0 redacts the request ID header, so those rows were paired by request content.",
    );
  if (JSON.stringify(a.settings) !== JSON.stringify(b.settings))
    notes.push("Execution settings differ.");
  if (a.schema_version !== b.schema_version)
    notes.push(
      "One artifact is v1, which has no read options, engine details or per-request phases.",
    );
  if (!a.capture.complete || !b.capture.complete)
    notes.push("At least one capture is partial; its metrics describe an observed subset.");
  if (a.result_digest !== null && b.result_digest !== null)
    notes.push(
      a.result_digest === b.result_digest
        ? "The ordered result digests are equal. This does not establish unordered relational equivalence."
        : "The ordered result digests differ. Row order alone can cause this; it is not proof of different data.",
    );
  notes.push(
    "Similar schemas or row counts do not make two file sets logically equivalent. Byte positions belong to their own file.",
  );
  return notes;
}
