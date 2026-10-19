# Artifact format v1

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
