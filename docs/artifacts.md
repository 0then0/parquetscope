# Artifact formats

ParquetScope writes and reads three versions. **v1** records one Parquet file; **v2** and **v3** record a manifest-defined file set. All open in the application and in standalone viewers. New file-set captures are v3; v1 and v2 files remain readable and are never rewritten. v1 files are never rewritten or upgraded, and the UI does not present v1 data as v2 evidence: fields that v1 never recorded (read options, engine settings, request phases, logical paths) show as not recorded. [Format v2](#format-v2-file-sets) follows the v1 reference.

## Format v1

A UTF-8 JSON object with `schema_version: "1"`. The authoritative structural schema is generated from `src/parquetscope/models.py` and checked in at [frontend/src/artifact-schema.json](../frontend/src/artifact-schema.json). Generate/check it with `scripts/generate_schema.py`. Both Python/Pydantic and the browser/Ajv validate it before opening a run; both recompute metrics to reject inconsistent totals. This is validation, not cryptographic proof that someone captured authentic traffic.

Top-level fields:

- `app_version`, `duckdb_version`, `pyarrow_version`, `platform`, `id`, `created_at`: provenance. v1 is verified only for DuckDB 1.5.6.
- `sql`, `settings`: one user statement and execution settings. `settings.memory_limit_mb` is expressed in MiB (2²⁰ bytes), applied to DuckDB with the explicit `MiB` suffix. Threads, deadline and preview limits are recorded. Cold-client conditions are explicit.
- `input`: basename (no original absolute path), SHA-256 of file bytes, byte size, row count, row-group count, schema field names, and known/unclassified layout regions.
- `sql_status`, `query_duration_ms`, `elapsed_ms`, `result_rows`, `result_digest`: execution outcome. The digest is a SHA-256 stream over length-prefixed JSON-encoded ordered row values; values are normalized as strings/bools/null. It is not an unordered relational equivalence proof.
- `observations`: normalized native engine request/response observations, original headers, raw requested Range, requested/confirmed intervals, engine timestamp and duration. IDs come from the server’s `X-ParquetScope-Request-ID` response header. No matching by ordinal.
- `server_observations`: independent server timings, response intervals, statuses, body declarations, successful stream writes and complete/interrupted transfer information.
- `capture`: completeness, reason, reconciliation and capture scope. `metrics`: derived values and unknown reasons; totals explicitly cover either the complete trace or an observed subset.
- `errors`, `limitations`: execution errors and observation boundaries.

All **byte offsets, sizes, row counts and other potentially large counters are decimal strings**, not JSON floating-point numbers. Examples: `"9007199254740993"` or a span `{ "start": "9007199254740993", "end": "9007199254741003" }`. Timing values and bounded counts/settings use JSON numbers. Null means unavailable, with context/reasons; zero is used only for a measured empty quantity. All intervals are half-open and response/layout intervals are checked against input bounds.

Maximum accepted artifact size: **32 MiB**. Unknown schema versions, unexpected fields, invalid types/ranges, duplicate IDs, invalid interval ordering, out-of-file response/layout intervals and inconsistent derived metrics produce an error. Collections are capped at 100,000 observations/structures.

No table rows or UI preview are included. Min/max statistics and key-value metadata are deliberately omitted. SQL, column names, headers and derived result hashes can still disclose sensitive information. There is no automatic redaction that could silently change evidence.

To open without the original Parquet file, use `parquetscope serve --artifact capture.json`. To view without Python, execution or network, use **Offline viewer** in the UI or `parquetscope viewer capture.json --output capture.html`. The HTML embeds the compiled JS/CSS and one validated artifact in a non-executable JSON script element; `<` is escaped to prevent script-element termination. It supports local file imports and comparisons entirely in the browser. No fonts, CDN, extension download, database or HTTP server is required.

## Format v2: file sets

A UTF-8 JSON object with `schema_version: "2"`, produced by `parquetscope capture --manifest` and by running a manifest input in the UI. The structural schema is generated from `src/parquetscope/models_v2.py` into [frontend/src/artifact-schema-v2.json](../frontend/src/artifact-schema-v2.json); `scripts/generate_schema.py --check` detects drift. Python and the browser validate the same rules and recompute every derived value. The v1 schema is unchanged. Byte quantities are decimal strings, intervals are half-open, the size limit is 32 MiB and collections are capped (256 files, 100,000 observations, 100,000 structures across the set).

Fields shared with v1: `app_version`, `duckdb_version`, `pyarrow_version`, `platform`, `id`, `created_at`, `sql`, `settings`, `sql_status`, `query_duration_ms`, `elapsed_ms`, `result_rows`, `result_digest`, `capture`, `errors`, `limitations`. They mean the same thing. `capture.scope` states the v2 boundary, below.

Replaced or added fields:

- `file_set`: `label`; `read` (`hive_partitioning`, `union_by_name` always `false`); and `files`, in manifest order. Each file has `id` (`f001`, `f002`, … exactly in set order), `name` (basename), `logical_path` (relative POSIX path from the manifest), `partition` (directory segments written `key=value`, derived from the path and validated against it), and the v1 input fields: `sha256`, `size`, `rows`, `row_groups`, `schema_fields`, `structures`. Absolute source paths are never recorded.
- `engine`: `httpfs_version`, an allowlisted `effective_settings` map read back from DuckDB (threads, memory limit, cache and HTTP retry/timeout settings), and `bound_schema`, the column names and types of the bound view. Null means not recorded.
- `observations`: v1 fields plus `file_id` and `phase`. `server_observations`: v1 fields plus `file_id`.
- `metrics`: set totals plus `files`, one entry per file with `observed_access`, its own counters and its own interval `coverage`.

### Attribution and totals

A client observation is attributed to a file from its URL route; the independent server attributes the same request from the route it served. A capture is `complete` only if both agree for every request. The key of an interval is `file_id` plus offsets: intervals of different files are never merged, compared or unioned. Set totals are sums of per-file values, and set-level `unique_coverage_bytes` is the sum of per-file unions. There is no set-level `coverage` list. A total is null when any file's value is null.

`observed_access` per file is `get` (at least one GET observed by either source), `head_only` (HEAD requests but no GET) or `none` (no request observed). `none` means no request is recorded in this trace. In an incomplete capture (`totals_scope: "observed_subset"`) it does not prove that the file was never accessed. The three states are counted in `files_with_get`, `files_head_only` and `files_without_observed_access`.

### Capture boundary and phases

Measurement starts when HTTP logging is enabled and ends after the SELECT result is fully consumed. It **includes view creation (input binding)**, which the engine performs against the first listed file, and excludes the app's own preparation (hashing and footer reads). The worker records wall-clock times around view creation and the SELECT. Each observation's `phase` is where the request **started**: `view_creation`, `select`, or `unattributed` if it started between or outside the windows. Phases say when a request began, not why it was issued, and not when its response finished. v1 has no phases.

### Validation

Loading rejects: unsupported `schema_version`; unknown fields; duplicate or non-sequential file IDs, duplicate logical paths; unsafe paths (absolute, `..`, backslashes, control characters, non-NFC); names or partition segments that disagree with the path; duplicate observation or structure IDs; observations that name an unknown file; response or layout intervals outside their own file; a complete capture whose client and server records do not reconcile, including their file attribution; per-file or total metrics that differ from the observations; and collections or documents over their limits.

Relative paths and partition values can disclose sensitive information. Standalone HTML escapes `<` and `>` in the embedded JSON, but the content is still visible to anyone who has the file.

## Format v3: engine and prefetch evidence

A v2 file-set capture that also records which DuckDB ran and what it did with `disable_parquet_prefetching`. `schema_version: "3"`; the schema is generated from `src/parquetscope/models_v3.py` into [frontend/src/artifact-schema-v3.json](../frontend/src/artifact-schema-v3.json). A v2 reader rejects it, and a v3 reader rejects v2 data relabeled as v3. Everything not listed here is as in v2, including the validation rules.

- `duckdb_version`: `"1.5.6"` or `"2.0.0"`, the version the worker reported. v1 and v2 admit only 1.5.6.
- `settings.disable_parquet_prefetching`: `true`, `false`, or `null` when the capture left the engine's value alone.
- `engine.environment`: `isolated` (a prepared environment from `setup --engine`) or `project` (the project's interpreter). `engine.python_version` is the worker's Python.
- `engine.prefetch`: `requested` (the request, equal to the setting), `applied` (`"true"` or `"false"`, read back from the engine after the SET, or null) and `status`: `verified` (a requested value the engine confirmed), `engine_default` (nothing requested; the engine's value read back) or `unverified` (could not be read back). A complete capture cannot be `unverified`, and `verified` requires `applied` to equal the request. The value read back also appears in `engine.effective_settings`.
- `engine.request_correlation`: `response_header_id` when the engine echoed the server's `X-ParquetScope-Request-ID`, or `ordered_tuple_match` when it did not. DuckDB 2.0 replaces that header by `"redacted"` in its HTTP log, so each log row is paired with a server record by file, method, requested range, status, response interval and declared length, in start order within identical requests. Every group must match in size on both sides, otherwise the capture is not reconciled. The paired ID is written into the observation, and the original headers stay as logged.

Server records stay separate from engine observations: `server_written_bytes` is what the loopback listener wrote, not what DuckDB received or used.

## Format v4: HTTP profiles

New CLI/UI captures and `capture_set` produce `schema_version: "4"`, extending v3 with
required HTTP profile evidence and server timing fields. Single-file CLI/UI inputs use a
one-file `file_set` with Hive partitioning off. If its basename cannot be a portable manifest
path, the file uses the logical name `input.parquet`; the original basename stays in the
set label. Directly selected local filenames need not satisfy manifest path restrictions.
v1–v3 schemas and recorded evidence remain
unchanged; the viewer reports their HTTP profile as **not recorded**. Absence is never
converted into confirmed unrestricted service. The legacy Python `capture` helper without
an explicit `http_profile` retains its v1 return contract; pass `HTTPProfile()` for v4.
The schema is generated from `models_v4.py` into `frontend/src/artifact-schema-v4.json`.

`http_profile` contains:

- `requested`, `effective`: `{response_delay_ms, body_bytes_per_second}`. Delay is an integer
  in 0–60,000 milliseconds, default 0. Rate is null (unrestricted) or an integer in
  1–1,000,000,000 bytes/second. Booleans, fractional values and numeric strings are rejected.
  Both fields are required in recorded evidence. The server currently applies the request
  exactly, so requested and effective must agree.
- `semantics`: `capture_shared_token_bucket_v1`, the server write-admission algorithm below.
- `burst_bytes`: decimal string, `"0"` unrestricted; otherwise
  `max(1, min(16384, ceil(rate / 10)))`. Each capture starts with its own full bucket.
- `write_chunk_bytes`: 65,536 unrestricted, otherwise equal to burst. Headers and HEAD
  consume no body tokens. The budget covers all concurrent GETs and all files in the capture.

v4 `server_observations` add required bounded fields:

- `profile_delay_started_ms`: nullable start of the wait immediately before generating
  headers, after route/identity/range preparation. `profile_delay_ms`: actual monotonic
  time spent there, including scheduling overhead, possibly shorter on interruption.
- `headers_sent_ms`: timestamp after `end_headers` successfully returns, null otherwise.
- `body_budget_wait_ms`: aggregate elapsed time in body admission, including contention
  with other writes and token waits. No event list or queue of future reservations exists.
- `body_write_started_ms`, `body_write_finished_ms`: nullable boundaries of first through
  last attempted body writes, including a failed attempt. Their span can include budget
  waits between writes. `body_write_ms` sums only time inside write calls.
- `reserved_body_bytes`: bytes admitted before writes, including reservations whose write
  fails or is cancelled. `written_body_bytes` retains successful response-stream acceptance,
  including an explicitly reported short write. Bytes possibly accepted by a failing call
  that reports no count cannot be reconstructed. Reservations are not refunded.
- `termination`: `completed`, `cancelled`, `timeout`, `disconnect`, or `write_error`.

All timestamps use the existing server monotonic origin; `start_ms` still begins at handling
of a matched HEAD/GET and `duration_ms` still covers preparation, delay, budget, I/O and cleanup
through observation construction. Engine timestamps have their own origin. Sequential
delay, budget and write components lie within server duration; first-to-last write span
overlaps budget waits. Concurrent requests overlap, so summing these durations does not
produce query wall time. `query_duration_ms` still excludes view creation; `elapsed_ms`
includes worker startup and cleanup. SQL outcome remains independent of capture completeness.

The limiter serializes limited write admission and the actual write, releases its lock
while waiting for tokens and suspends token accrual during an in-flight write. It never
allocates future slots. The reservation envelope is bounded by burst plus rate times elapsed
monotonic seconds, shared by the capture; there is at most one admitted in-flight chunk.
Actual throughput can be lower because file reads, socket buffering, system calls and
thread scheduling also take time. The exact restriction point is immediately before the
server response-stream body write, not TCP delivery or acknowledgment. See the
[measurement contract](measurements.md#http-profiles).

Validation rejects inconsistent profile/burst/chunk evidence, timestamps outside the
request or out of order, impossible duration components, written bytes exceeding reserved
or declared bytes, and termination/completion contradictions, in addition to all v3 checks.
