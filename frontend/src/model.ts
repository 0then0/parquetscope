import Ajv from "ajv";
import schema1 from "./artifact-schema.json";
import schema2 from "./artifact-schema-v2.json";
import schema3 from "./artifact-schema-v3.json";
import schema4 from "./artifact-schema-v4.json";

export type Span = { start: string; end: string };
export type Structure = {
  id: string;
  kind: "header" | "footer" | "column_chunk" | "unclassified";
  label: string;
  span: Span;
  row_group: number | null;
  column: string | null;
  rows: string | null;
  dictionary_offset: string | null;
  data_offset: string | null;
  compression: string | null;
};
export type Phase = "view_creation" | "select" | "unattributed";
export type Access = "none" | "head_only" | "get";
export type Observation = {
  id: string;
  /** v1 artifacts have exactly one file; its ID is "input" there. */
  file_id: string;
  /** Not recorded by v1 artifacts. */
  phase: Phase | null;
  method: "GET" | "HEAD";
  url: string;
  requested_range: string | null;
  requested_span: Span | null;
  response_span: Span | null;
  status: number;
  start_ms: number;
  duration_ms: number;
  declared_body_bytes: string | null;
  request_headers: Record<string, string>;
  response_headers: Record<string, string>;
  engine_start_time: string;
};
export type ServerObservation = {
  id: string;
  file_id: string;
  method: "GET" | "HEAD";
  requested_range: string | null;
  response_span: Span | null;
  status: number;
  start_ms: number;
  duration_ms: number;
  declared_body_bytes: string;
  written_body_bytes: string;
  complete: boolean;
  error: string | null;
  /** v4 server timings use the server's monotonic origin, independently of engine timings. */
  profile_delay_started_ms?: number | null;
  profile_delay_ms?: number;
  headers_sent_ms?: number | null;
  body_budget_wait_ms?: number;
  body_write_started_ms?: number | null;
  body_write_finished_ms?: number | null;
  body_write_ms?: number;
  reserved_body_bytes?: string;
  termination?: "completed" | "cancelled" | "timeout" | "disconnect" | "write_error";
};
export type HTTPProfile = {
  response_delay_ms: number;
  body_bytes_per_second: number | null;
};
export type HTTPProfileEvidence = {
  requested: HTTPProfile;
  effective: HTTPProfile;
  semantics: "capture_shared_token_bucket_v1";
  burst_bytes: string;
  write_chunk_bytes: number;
};
export type Settings = {
  threads: number;
  memory_limit_mb: number;
  deadline_seconds: number;
  preview_rows: number;
  preview_bytes: number;
  external_file_cache: boolean;
  cold_client: boolean;
  /** v3 only. Null: not set by the capture, the engine default applied. Absent before v3. */
  disable_parquet_prefetching?: boolean | null;
};
export type PartitionSegment = { key: string; value: string };
export type FileEntry = {
  id: string;
  name: string;
  /** Relative manifest location; null for v1, which records only a basename. */
  logical_path: string | null;
  partition: PartitionSegment[];
  sha256: string;
  size: string;
  rows: string;
  row_groups: number;
  schema_fields: string[];
  structures: Structure[];
};
export type FileMetrics = {
  file_id: string;
  observed_access: Access;
  get_count: number;
  head_count: number;
  requested_range_bytes: string | null;
  confirmed_range_bytes: string | null;
  declared_body_bytes: string | null;
  server_written_bytes: string;
  unique_coverage_bytes: string | null;
  repeated_coverage_bytes: string | null;
  coverage: Span[] | null;
  unknown_reasons: string[];
};
export type Totals = {
  get_count: number;
  head_count: number;
  requested_range_bytes: string | null;
  confirmed_range_bytes: string | null;
  declared_body_bytes: string | null;
  server_written_bytes: string;
  unique_coverage_bytes: string | null;
  repeated_coverage_bytes: string | null;
  totals_scope: "complete" | "observed_subset";
  unknown_reasons: string[];
  /** Interval union of the only file; v2 totals never merge intervals across files. */
  coverage: Span[] | null;
  files_without_observed_access: number;
  files_head_only: number;
  files_with_get: number;
};
export type Prefetch = {
  requested: boolean | null;
  /** Read back from the engine after the SET; null when it could not be read. */
  applied: "true" | "false" | null;
  status: "engine_default" | "verified" | "unverified";
};
export type Engine = {
  httpfs_version: string | null;
  effective_settings: Record<string, string>;
  bound_schema: { name: string; type: string }[] | null;
  /** The next four are recorded by v3 artifacts only. */
  environment?: "project" | "isolated";
  python_version?: string;
  request_correlation?: "response_header_id" | "ordered_tuple_match";
  prefetch?: Prefetch;
};
export type Capture = {
  complete: boolean;
  reason: string | null;
  reconciled: boolean;
  scope: string;
};
/** One validated run of either artifact version, normalized to a file list. */
export type Run = {
  schema_version: "1" | "2" | "3" | "4";
  /** The validated artifact exactly as imported; exports use this, never the normalized view. */
  artifact: unknown;
  app_version: string;
  duckdb_version: string;
  pyarrow_version: string;
  platform: string;
  id: string;
  created_at: string;
  sql: string;
  /** Display name: the input basename (v1) or the manifest label (v2). */
  label: string;
  files: FileEntry[];
  /** Null when the artifact does not record it (v1). */
  read: { hive_partitioning: boolean; union_by_name: boolean } | null;
  engine: Engine | null;
  settings: Settings;
  /** Null means not recorded by this artifact, never a confirmed unrestricted profile. */
  http_profile: HTTPProfileEvidence | null;
  sql_status: "succeeded" | "failed" | "cancelled" | "timeout";
  query_duration_ms: number | null;
  elapsed_ms: number;
  result_rows: string | null;
  result_digest: string | null;
  observations: Observation[];
  server_observations: ServerObservation[];
  capture: Capture;
  metrics: Totals;
  file_metrics: FileMetrics[];
  errors: string[];
  limitations: string[];
  byFile: Map<string, { observations: Observation[]; server: ServerObservation[] }>;
};
type Raw1 = Omit<
  Run,
  | "artifact"
  | "label"
  | "files"
  | "read"
  | "engine"
  | "file_metrics"
  | "byFile"
  | "observations"
  | "server_observations"
  | "metrics"
  | "http_profile"
> & {
  input: Omit<FileEntry, "id" | "logical_path" | "partition">;
  observations: Omit<Observation, "file_id" | "phase">[];
  server_observations: Omit<ServerObservation, "file_id">[];
  metrics: Omit<Totals, "files_without_observed_access" | "files_head_only" | "files_with_get">;
};
type Raw2 = Omit<
  Run,
  "artifact" | "label" | "files" | "read" | "metrics" | "file_metrics" | "byFile" | "engine"
> & {
  file_set: {
    label: string | null;
    read: { hive_partitioning: boolean; union_by_name: boolean };
    files: Omit<FileEntry, never>[];
  };
  engine: Engine;
  metrics: Omit<Totals, "coverage"> & { files: FileMetrics[] };
};
export const MAX_BYTES = 32 * 1024 * 1024;
export const MAX_FILES = 256;
const MAX_STRUCTURES = 100000;
const ajv = new Ajv({ strict: false, allErrors: false });
const validate1 = ajv.compile(schema1);
const validate2 = ajv.compile(schema2);
const validate3 = ajv.compile(schema3);
const validate4 = ajv.compile(schema4);
/** v2 and v3 artifacts are file sets; v1 holds exactly one file. */
export const isSetRun = (run: { schema_version: string }) => run.schema_version !== "1";
export const intersects = (a: Span | null, b: Span | null) =>
  a !== null && b !== null && BigInt(a.start) < BigInt(b.end) && BigInt(b.start) < BigInt(a.end);
export function union(spans: Span[]): Span[] {
  const pairs = spans
    .map((s) => [BigInt(s.start), BigInt(s.end)] as const)
    .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  const result: [bigint, bigint][] = [];
  for (const [start, end] of pairs) {
    const last = result.at(-1);
    if (last && start <= last[1]) {
      if (end > last[1]) last[1] = end;
    } else result.push([start, end]);
  }
  return result.map(([start, end]) => ({ start: start.toString(), end: end.toString() }));
}
export const length = (s: Span) => BigInt(s.end) - BigInt(s.start);
export const sumSpans = (spans: Span[]) => spans.reduce((sum, span) => sum + length(span), 0n);
export const fileTitle = (f: FileEntry) => f.logical_path ?? f.name;

const FORBIDDEN = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}]/u;
const utf8 = new TextEncoder();
/** Mirrors models_v2.split_logical_path so both validators accept the same paths. */
export function splitLogicalPath(value: string): string[] {
  if (!value || utf8.encode(value).length > 1024) throw new Error("Path must be 1 to 1024 bytes");
  if (value.normalize("NFC") !== value) throw new Error("Path must be Unicode NFC-normalized");
  if (FORBIDDEN.test(value)) throw new Error("Path contains control or invisible characters");
  if (value.includes("\\") || value.startsWith("/"))
    throw new Error("Path must be relative and use '/' separators");
  const segments = value.split("/");
  if (segments.length > 32) throw new Error("Path has more than 32 segments");
  for (const segment of segments) {
    if (segment === "" || segment === "." || segment === "..")
      throw new Error("Path must not contain empty, '.' or '..' segments");
    if (utf8.encode(segment).length > 255) throw new Error("Path segment exceeds 255 bytes");
  }
  return segments;
}
export function partitionPairs(path: string): PartitionSegment[] {
  const pairs: PartitionSegment[] = [];
  for (const segment of splitLogicalPath(path).slice(0, -1)) {
    const at = segment.indexOf("=");
    if (at > 0) pairs.push({ key: segment.slice(0, at), value: segment.slice(at + 1) });
  }
  return pairs;
}

function sumKnown(values: (string | null)[]): string | null {
  return values.some((v) => v === null)
    ? null
    : values.reduce((s, v) => s + BigInt(v ?? 0), 0n).toString();
}
/** Intervals belong to one file: callers pass only that file's observations. */
export function computeFileMetrics(
  fileId: string,
  observations: Observation[],
  server: ServerObservation[],
): FileMetrics {
  const gets = observations.filter((o) => o.method === "GET");
  const requested = gets.flatMap((o) => (o.requested_span ? [o.requested_span] : []));
  const confirmed = gets.flatMap((o) => (o.response_span ? [o.response_span] : []));
  const knownRequests = gets.every((o) => o.requested_span !== null);
  const knownResponses = gets.every((o) => o.response_span !== null);
  const knownLengths = gets.every((o) => o.declared_body_bytes !== null);
  const covered = union(confirmed);
  const reasons: string[] = [];
  if (!knownRequests) reasons.push("Some GETs have no interpretable single requested range");
  if (!knownResponses) reasons.push("Some GET responses have no confirmed file interval");
  if (!knownLengths) reasons.push("Some GET responses have no declared Content-Length");
  const access: Access =
    gets.length || server.some((s) => s.method === "GET")
      ? "get"
      : observations.length || server.length
        ? "head_only"
        : "none";
  return {
    file_id: fileId,
    observed_access: access,
    get_count: gets.length,
    head_count: observations.length - gets.length,
    requested_range_bytes: knownRequests ? sumSpans(requested).toString() : null,
    confirmed_range_bytes: knownResponses ? sumSpans(confirmed).toString() : null,
    declared_body_bytes: knownLengths
      ? gets.reduce((s, o) => s + BigInt(o.declared_body_bytes ?? 0), 0n).toString()
      : null,
    server_written_bytes: server.reduce((s, o) => s + BigInt(o.written_body_bytes), 0n).toString(),
    unique_coverage_bytes: knownResponses ? sumSpans(covered).toString() : null,
    repeated_coverage_bytes: knownResponses
      ? (sumSpans(confirmed) - sumSpans(covered)).toString()
      : null,
    coverage: knownResponses ? covered : null,
    unknown_reasons: reasons,
  };
}
function groupByFile(
  ids: string[],
  observations: Observation[],
  server: ServerObservation[],
): Run["byFile"] {
  const groups: Run["byFile"] = new Map(
    ids.map((id) => [id, { observations: [], server: [] }] as const),
  );
  for (const o of observations) groups.get(o.file_id)?.observations.push(o);
  for (const s of server) groups.get(s.file_id)?.server.push(s);
  return groups;
}
function checkReconciled(
  capture: Capture,
  observations: Observation[],
  server: ServerObservation[],
) {
  if (!capture.complete) return;
  const byId = new Map(server.map((o) => [o.id, o]));
  if (
    !capture.reconciled ||
    capture.reason !== null ||
    byId.size !== observations.length ||
    server.some((o) => !o.complete)
  )
    throw new Error("Complete capture has incomplete observations");
  for (const o of observations) {
    const s = byId.get(o.id);
    if (
      !s ||
      o.method !== s.method ||
      o.status !== s.status ||
      o.file_id !== s.file_id ||
      o.requested_range !== s.requested_range ||
      o.declared_body_bytes !== s.declared_body_bytes ||
      o.response_span?.start !== s.response_span?.start ||
      o.response_span?.end !== s.response_span?.end
    )
      throw new Error("Complete capture does not reconcile");
  }
}
function checkIntervals(
  files: FileEntry[],
  observations: Observation[],
  server: ServerObservation[],
) {
  const sizes = new Map(files.map((f) => [f.id, BigInt(f.size)]));
  for (const f of files) {
    for (const s of f.structures)
      if (BigInt(s.span.start) >= BigInt(s.span.end) || BigInt(s.span.end) > BigInt(f.size))
        throw new Error("Invalid byte interval");
    if (new Set(f.structures.map((s) => s.id)).size !== f.structures.length)
      throw new Error("Duplicate record IDs");
  }
  for (const o of [...observations, ...server]) {
    const size = sizes.get(o.file_id);
    if (size === undefined) throw new Error("Observation refers to an unknown file");
    const s = o.response_span;
    if (s && (BigInt(s.start) >= BigInt(s.end) || BigInt(s.end) > size))
      throw new Error("Invalid byte interval");
  }
  if (
    observations.some(
      (o) => o.requested_span && BigInt(o.requested_span.start) >= BigInt(o.requested_span.end),
    )
  )
    throw new Error("Invalid requested interval");
  for (const records of [observations, server]) {
    if (new Set(records.map((o) => o.id)).size !== records.length)
      throw new Error("Duplicate record IDs");
  }
}
function same<T>(a: T, b: T, message: string) {
  if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(message);
}
const TOTAL_KEYS = [
  "get_count",
  "head_count",
  "requested_range_bytes",
  "confirmed_range_bytes",
  "declared_body_bytes",
  "server_written_bytes",
  "unique_coverage_bytes",
  "repeated_coverage_bytes",
] as const;

function normalizeV1(value: Raw1): Run {
  const id = "input";
  const file: FileEntry = { ...value.input, id, logical_path: null, partition: [] };
  const observations = value.observations.map((o) => ({ ...o, file_id: id, phase: null }));
  const server = value.server_observations.map((o) => ({ ...o, file_id: id }));
  checkIntervals([file], observations, server);
  checkReconciled(value.capture, observations, server);
  const metrics = computeFileMetrics(id, observations, server);
  const expected = {
    ...metrics,
    totals_scope: value.capture.complete ? "complete" : "observed_subset",
  };
  for (const key of [...TOTAL_KEYS, "totals_scope"] as const) {
    if (expected[key] !== value.metrics[key]) throw new Error(`Artifact metrics disagree: ${key}`);
  }
  same(metrics.coverage, value.metrics.coverage, "Artifact coverage disagrees");
  return {
    ...value,
    artifact: value,
    schema_version: "1",
    label: file.name,
    files: [file],
    read: null,
    engine: null,
    http_profile: null,
    observations,
    server_observations: server,
    metrics: {
      ...value.metrics,
      files_without_observed_access: metrics.observed_access === "none" ? 1 : 0,
      files_head_only: metrics.observed_access === "head_only" ? 1 : 0,
      files_with_get: metrics.observed_access === "get" ? 1 : 0,
    },
    file_metrics: [metrics],
    byFile: groupByFile([id], observations, server),
  };
}

/** Set totals are sums of per-file values; intervals are never merged across files. */
export function deriveSetTotals(
  perFile: FileMetrics[],
  complete: boolean,
): Record<string, string | number | null> {
  return {
    get_count: perFile.reduce((n, m) => n + m.get_count, 0),
    head_count: perFile.reduce((n, m) => n + m.head_count, 0),
    requested_range_bytes: sumKnown(perFile.map((m) => m.requested_range_bytes)),
    confirmed_range_bytes: sumKnown(perFile.map((m) => m.confirmed_range_bytes)),
    declared_body_bytes: sumKnown(perFile.map((m) => m.declared_body_bytes)),
    server_written_bytes: sumKnown(perFile.map((m) => m.server_written_bytes)),
    unique_coverage_bytes: sumKnown(perFile.map((m) => m.unique_coverage_bytes)),
    repeated_coverage_bytes: sumKnown(perFile.map((m) => m.repeated_coverage_bytes)),
    totals_scope: complete ? "complete" : "observed_subset",
    files_without_observed_access: perFile.filter((m) => m.observed_access === "none").length,
    files_head_only: perFile.filter((m) => m.observed_access === "head_only").length,
    files_with_get: perFile.filter((m) => m.observed_access === "get").length,
  };
}

/** Mirrors the v3 validators of models_v3.py. */
function checkPrefetch(value: Raw2) {
  const p = value.engine.prefetch;
  if (!p) throw new Error("Missing prefetch evidence");
  // Absent means null, as in the backend model where the field defaults to null.
  if ((value.settings.disable_parquet_prefetching ?? null) !== p.requested)
    throw new Error("Settings and engine evidence disagree about the prefetch request");
  if (p.status !== "unverified") {
    if (p.applied === null) throw new Error("A verified or default prefetch setting needs a value");
    if (p.status === "verified" && p.applied !== String(p.requested))
      throw new Error("Verified prefetch setting disagrees with the request");
    if (p.status === "engine_default" && p.requested !== null)
      throw new Error("A requested prefetch setting is verified or unverified");
  } else if (value.capture.complete) throw new Error("Complete capture needs a verified prefetch");
}
/** Mirrors models_v4.ServerObservation4 and HTTPProfileEvidence refinements. */
function checkHTTPProfile(value: Raw2) {
  const profile = value.http_profile;
  if (!profile) throw new Error("Missing HTTP profile evidence");
  if (
    profile.requested.response_delay_ms !== profile.effective.response_delay_ms ||
    profile.requested.body_bytes_per_second !== profile.effective.body_bytes_per_second
  )
    throw new Error("Requested and effective HTTP profiles disagree");
  const rate = profile.effective.body_bytes_per_second;
  const burst = rate === null ? 0 : Math.max(1, Math.min(16384, Math.ceil(rate / 10)));
  if (
    BigInt(profile.burst_bytes) !== BigInt(burst) ||
    profile.write_chunk_bytes !== (burst || 65536)
  )
    throw new Error("HTTP limiter evidence disagrees with its profile");
  const epsilon = 0.001;
  for (const s of value.server_observations) {
    const end = s.start_ms + s.duration_ms;
    const times = [
      s.profile_delay_started_ms,
      s.headers_sent_ms,
      s.body_write_started_ms,
      s.body_write_finished_ms,
    ];
    const recorded = times.filter((t): t is number => t !== null && t !== undefined);
    if (recorded.some((t) => t < s.start_ms - epsilon || t > end + epsilon))
      throw new Error("Server timing outside request duration");
    if (recorded.some((t, i) => i > 0 && t < recorded[i - 1]))
      throw new Error("Server timestamps are out of order");
    if ((s.body_write_started_ms === null) !== (s.body_write_finished_ms === null))
      throw new Error("Body write boundaries must both be recorded");
    if (s.body_write_ms && s.body_write_started_ms === null)
      throw new Error("Body write duration needs write boundaries");
    if (
      s.body_write_started_ms != null &&
      s.body_write_finished_ms != null &&
      (s.body_write_ms ?? 0) > s.body_write_finished_ms - s.body_write_started_ms + epsilon
    )
      throw new Error("Body write duration exceeds its boundaries");
    if (s.profile_delay_ms && s.profile_delay_started_ms === null)
      throw new Error("Profile delay needs its start timestamp");
    if (
      s.profile_delay_started_ms != null &&
      s.profile_delay_started_ms + (s.profile_delay_ms ?? 0) > end + epsilon
    )
      throw new Error("Profile delay exceeds request duration");
    if (
      (s.profile_delay_ms ?? 0) + (s.body_budget_wait_ms ?? 0) + (s.body_write_ms ?? 0) >
      s.duration_ms + epsilon
    )
      throw new Error("Sequential timing components exceed request duration");
    const written = BigInt(s.written_body_bytes),
      reserved = BigInt(s.reserved_body_bytes ?? "0"),
      declared = BigInt(s.declared_body_bytes);
    if (written > reserved || reserved > declared)
      throw new Error("Expected written <= reserved <= declared body bytes");
    if (s.method === "HEAD" && (declared !== 0n || reserved !== 0n || written !== 0n))
      throw new Error("HEAD does not transfer body bytes");
    if (s.complete !== (s.termination === "completed"))
      throw new Error("Completion disagrees with server termination");
    if (s.complete && (written !== declared || s.headers_sent_ms === null))
      throw new Error("Completed response needs headers and all declared body writes");
  }
}
function normalizeSet(value: Raw2, version: "2" | "3" | "4"): Run {
  const files = value.file_set.files;
  if (files.some((f, i) => f.id !== `f${String(i + 1).padStart(3, "0")}`))
    throw new Error("File IDs must be f001, f002, … in set order");
  if (new Set(files.map((f) => f.logical_path)).size !== files.length)
    throw new Error("Duplicate logical paths");
  if (files.reduce((n, f) => n + f.structures.length, 0) > MAX_STRUCTURES)
    throw new Error("File set exceeds the 100,000 structure limit");
  for (const f of files) {
    if (f.logical_path === null) throw new Error("Missing logical path");
    const segments = splitLogicalPath(f.logical_path);
    if (segments.at(-1) !== f.name) throw new Error("File name disagrees with its logical path");
    same(f.partition, partitionPairs(f.logical_path), "Partition segments disagree with the path");
  }
  const observations = value.observations;
  const server = value.server_observations;
  if (version === "3" || version === "4") checkPrefetch(value);
  if (version === "4") checkHTTPProfile(value);
  checkIntervals(files, observations, server);
  checkReconciled(value.capture, observations, server);
  const byFile = groupByFile(
    files.map((f) => f.id),
    observations,
    server,
  );
  const perFile = files.map((f) => {
    const group = byFile.get(f.id);
    return computeFileMetrics(f.id, group?.observations ?? [], group?.server ?? []);
  });
  same(
    perFile,
    value.metrics.files,
    "Artifact metrics disagree: per-file metrics differ from observations",
  );
  const expected = deriveSetTotals(perFile, value.capture.complete);
  for (const key of Object.keys(expected)) {
    if (expected[key] !== (value.metrics as unknown as Record<string, unknown>)[key])
      throw new Error(`Artifact metrics disagree: ${key}`);
  }
  const { files: _files, ...totals } = value.metrics;
  return {
    ...value,
    artifact: value,
    schema_version: version,
    http_profile: version === "4" ? value.http_profile : null,
    label: value.file_set.label ?? "File set",
    files,
    read: value.file_set.read,
    metrics: { ...totals, coverage: null },
    file_metrics: value.metrics.files,
    byFile,
  };
}

export function parseArtifactValue(value: unknown): Run {
  const version = (value as { schema_version?: unknown } | null)?.schema_version;
  const check =
    version === "4"
      ? validate4
      : version === "3"
        ? validate3
        : version === "2"
          ? validate2
          : version === "1"
            ? validate1
            : null;
  if (!check)
    throw new Error(`Invalid or unsupported artifact: schema_version ${JSON.stringify(version)}`);
  if (!check(value))
    throw new Error(
      `Invalid or unsupported artifact: ${check.errors?.[0]?.instancePath} ${check.errors?.[0]?.message}`,
    );
  return version === "1"
    ? normalizeV1(value as Raw1)
    : normalizeSet(value as unknown as Raw2, version as "2" | "3" | "4");
}
export function parseArtifact(text: string): Run {
  if (new TextEncoder().encode(text).length > MAX_BYTES)
    throw new Error("Artifact exceeds 32 MiB limit");
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error("Invalid JSON artifact");
  }
  return parseArtifactValue(value);
}
export function bytes(value: string | null): string {
  if (value === null) return "Unknown";
  const n = BigInt(value);
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"];
  let unit = 0,
    divisor = 1n;
  while (n / divisor >= 1024n && unit < units.length - 1) {
    divisor *= 1024n;
    unit++;
  }
  if (!unit) return `${n} B`;
  const hundredths = (n * 100n) / divisor;
  return `${hundredths / 100n}.${(hundredths % 100n).toString().padStart(2, "0")} ${units[unit]}`;
}
export const exact = (value: string | null) =>
  value === null ? "Unknown" : `${BigInt(value).toLocaleString("en-US")} bytes`;
export const percent = (offset: string, size: string) =>
  Number((BigInt(offset) * 1000000n) / (BigInt(size) || 1n)) / 10000;
export const rangeText = (s: Span | null) =>
  s ? `[${s.start}, ${s.end})` : "Unknown / no file body";

/** Plain statement of the prefetch evidence an artifact holds. */
export function prefetchText(run: Run): string {
  const p = run.engine?.prefetch;
  if (!p) return "Not recorded (v1 and v2 artifacts)";
  const requested =
    p.requested === null ? "not set (engine default)" : `set to ${String(p.requested)}`;
  const applied = p.applied === null ? "could not be read back" : `engine reports ${p.applied}`;
  return `disable_parquet_prefetching ${requested}; ${applied}; ${p.status.replace("_", " ")}`;
}

export function httpProfileText(profile: HTTPProfile | null | undefined): string {
  if (!profile) return "Not recorded (artifact before v4)";
  const rate = profile.body_bytes_per_second;
  if (profile.response_delay_ms === 0 && rate === null) return "Unrestricted (recorded)";
  return `${profile.response_delay_ms} ms before headers; ${rate === null ? "unlimited body rate" : `${rate.toLocaleString("en-US")} B/s shared body rate`}`;
}
