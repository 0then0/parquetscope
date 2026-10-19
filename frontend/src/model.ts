import Ajv from "ajv";
import schema from "./artifact-schema.json";

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
export type Observation = {
  id: string;
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
};
export type Settings = {
  threads: number;
  memory_limit_mb: number;
  deadline_seconds: number;
  preview_rows: number;
  preview_bytes: number;
  external_file_cache: boolean;
  cold_client: boolean;
};
export type Run = {
  schema_version: "1";
  app_version: string;
  duckdb_version: string;
  pyarrow_version: string;
  platform: string;
  id: string;
  created_at: string;
  sql: string;
  input: {
    name: string;
    sha256: string;
    size: string;
    rows: string;
    row_groups: number;
    schema_fields: string[];
    structures: Structure[];
  };
  settings: Settings;
  sql_status: "succeeded" | "failed" | "cancelled" | "timeout";
  query_duration_ms: number | null;
  elapsed_ms: number;
  result_rows: string | null;
  result_digest: string | null;
  observations: Observation[];
  server_observations: ServerObservation[];
  capture: { complete: boolean; reason: string | null; reconciled: boolean; scope: string };
  metrics: {
    get_count: number;
    head_count: number;
    requested_range_bytes: string | null;
    confirmed_range_bytes: string | null;
    declared_body_bytes: string | null;
    server_written_bytes: string;
    unique_coverage_bytes: string | null;
    repeated_coverage_bytes: string | null;
    coverage: Span[] | null;
    totals_scope: "complete" | "observed_subset";
    unknown_reasons: string[];
  };
  errors: string[];
  limitations: string[];
};
export const MAX_BYTES = 32 * 1024 * 1024;
const validate = new Ajv({ strict: false, allErrors: false }).compile<Run>(schema);
export const intersects = (a: Span | null, b: Span | null) =>
  a !== null && b !== null && BigInt(a.start) < BigInt(b.end) && BigInt(b.start) < BigInt(a.end);
export function union(spans: Span[]): Span[] {
  const sorted = [...spans].sort((a, b) => (BigInt(a.start) < BigInt(b.start) ? -1 : 1));
  const result: Span[] = [];
  for (const span of sorted) {
    const last = result.at(-1);
    if (last && BigInt(span.start) <= BigInt(last.end)) {
      if (BigInt(span.end) > BigInt(last.end)) last.end = span.end;
    } else result.push({ ...span });
  }
  return result;
}
export const length = (s: Span) => BigInt(s.end) - BigInt(s.start);
export const sumSpans = (spans: Span[]) => spans.reduce((sum, span) => sum + length(span), 0n);
export function parseArtifact(text: string): Run {
  if (new TextEncoder().encode(text).length > MAX_BYTES)
    throw new Error("Artifact exceeds 32 MiB limit");
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error("Invalid JSON artifact");
  }
  if (!validate(value))
    throw new Error(
      `Invalid or unsupported artifact: ${validate.errors?.[0]?.instancePath} ${validate.errors?.[0]?.message}`,
    );
  const run = value;
  const spans = [
    ...run.input.structures.map((s) => s.span),
    ...run.observations.map((o) => o.response_span),
    ...run.server_observations.map((o) => o.response_span),
  ];
  if (
    spans.some(
      (s) => s && (BigInt(s.start) >= BigInt(s.end) || BigInt(s.end) > BigInt(run.input.size)),
    )
  )
    throw new Error("Invalid byte interval");
  if (
    run.observations.some(
      (o) => o.requested_span && BigInt(o.requested_span.start) >= BigInt(o.requested_span.end),
    )
  )
    throw new Error("Invalid requested interval");
  for (const records of [run.observations, run.server_observations, run.input.structures]) {
    if (new Set(records.map((o) => o.id)).size !== records.length)
      throw new Error("Duplicate record IDs");
  }
  if (run.capture.complete) {
    const byId = new Map(run.server_observations.map((o) => [o.id, o]));
    if (
      !run.capture.reconciled ||
      run.capture.reason !== null ||
      byId.size !== run.observations.length ||
      run.server_observations.some((o) => !o.complete)
    )
      throw new Error("Complete capture has incomplete observations");
    for (const o of run.observations) {
      const server = byId.get(o.id);
      if (
        !server ||
        o.method !== server.method ||
        o.status !== server.status ||
        o.requested_range !== server.requested_range ||
        o.declared_body_bytes !== server.declared_body_bytes ||
        o.response_span?.start !== server.response_span?.start ||
        o.response_span?.end !== server.response_span?.end
      )
        throw new Error("Complete capture does not reconcile");
    }
  }
  const gets = run.observations.filter((o) => o.method === "GET");
  const requested = gets.flatMap((o) => (o.requested_span ? [o.requested_span] : []));
  const confirmed = gets.flatMap((o) => (o.response_span ? [o.response_span] : []));
  const knownRequests = gets.every((o) => o.requested_span !== null);
  const knownResponses = gets.every((o) => o.response_span !== null);
  const covered = union(confirmed);
  const expected = {
    get_count: gets.length,
    head_count: run.observations.length - gets.length,
    requested_range_bytes: knownRequests ? sumSpans(requested).toString() : null,
    confirmed_range_bytes: knownResponses ? sumSpans(confirmed).toString() : null,
    declared_body_bytes: gets.every((o) => o.declared_body_bytes !== null)
      ? gets.reduce((s, o) => s + BigInt(o.declared_body_bytes ?? 0), 0n).toString()
      : null,
    server_written_bytes: run.server_observations
      .reduce((s, o) => s + BigInt(o.written_body_bytes), 0n)
      .toString(),
    unique_coverage_bytes: knownResponses ? sumSpans(covered).toString() : null,
    repeated_coverage_bytes: knownResponses
      ? (sumSpans(confirmed) - sumSpans(covered)).toString()
      : null,
    totals_scope: run.capture.complete ? "complete" : "observed_subset",
  };
  for (const key of Object.keys(expected) as (keyof typeof expected)[]) {
    if (expected[key] !== run.metrics[key]) throw new Error(`Artifact metrics disagree: ${key}`);
  }
  if (JSON.stringify(knownResponses ? covered : null) !== JSON.stringify(run.metrics.coverage))
    throw new Error("Artifact coverage disagrees");
  return run;
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
