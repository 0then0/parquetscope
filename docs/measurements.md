# Measurement contract

All internal and exported file intervals are nonempty, half-open `[start,end)`. HTTP `bytes=a-b` has inclusive `b`; the response interval becomes `[a,b+1)`. Explicit requested upper bounds are preserved even beyond EOF; the server clamps the response separately. Open-ended and suffix ranges are resolved against the stable file size. The original header is always retained. Adjacent intervals join in the coverage union but do not overlap. Empty ranges do not add coverage.

## Sources and formulas

Engine observations come from `CALL enable_logging('HTTP', storage_buffer_size=0)` followed by `duckdb_logs_parsed('HTTP')` in DuckDB 1.5.6. Request fields include method, URL, engine wall-clock start, integer millisecond duration and headers. Response fields include status, reason and headers. The engine does not expose client-received, decoded or SQL-used byte counts. Those quantities remain unavailable.

The independent server uses a monotonic clock and records each request even if writing fails. Engine start times are normalized relative to the earliest engine request; server times are relative to server creation. The clocks have different origins and should not be equated. Engine durations can be zero because they are integer milliseconds.

For observed GETs:

- Requested range bytes = sum of lengths of interpretable requested intervals; null if any GET lacks such an interval.
- Confirmed range bytes = sum of lengths of confirmed response intervals; null if any GET lacks a confirmed interval.
- Declared body bytes = sum of GET Content-Length values; null if one is missing. HEAD Content-Length is retained in raw headers but excluded from body accounting.
- Server-written bytes = sum of successful response-stream writes across independent observations. A failed write can have sent an unknown partial block; that uncertainty is retained as an interrupted stream, rather than estimating it.
- Unique coverage = length of the union of confirmed response intervals; null if a GET response interval is unknown.
- Repeated coverage = confirmed range bytes minus unique coverage. This is range multiplicity, not evidence of waste.

Coverage is based on response headers, not successful client receipt. Therefore a response interval may cover more than the server successfully wrote before an interruption. Never add range coverage and stream-write metrics together.

## Completeness and lifecycle

SQL status (`succeeded`, `failed`, `cancelled`, `timeout`) is independent of capture completeness. An ordinary SQL failure with collected, reconciled logs can have complete capture. Logs lost when a worker is killed cannot be reconstructed from server records and presented as engine logs. On interruption the artifact preserves server observations, marks `observed_subset`, records reasons and leaves unknown result/duration values null.

A complete trace requires successful native log collection, no observation truncation, matching engine/server records, complete server transfers and stable file identity. Response IDs make matching insensitive to order and distinguish repeated identical requests. Reconciliation compares method, raw Range, response interval, status and declared body length. File identity checks device, inode, size, nanosecond mtime and ctime against both the open descriptor and selected path, at requests, stream blocks and close. A change invalidates the run even if the SELECT succeeded.

Every run owns its worker and its HTTP listener. Cancellation and deadline terminate/reap the worker, then close sockets, join request threads, stop the listener and close the file. Server socket operations have a two-second timeout. Metadata extraction and hashing happen before these resources are created and are not part of the query deadline; cancellation during preparation is observed before query work completes. Preparation does read the full local file for SHA-256 and is not represented as HTTP trace traffic.

## HTTP policy

The endpoint is bound to `127.0.0.1`, has an unguessable per-run route and serves exactly one open file. HEAD sends no body and ignores Range. GET supports full 200, single-range 206 (inclusive Content-Range), and unsatisfiable 416 with `Content-Range: bytes */size`. Unsupported multi-range and unknown units are explicitly ignored with a full 200 response, a behavior allowed by RFC 9110. No multipart response is fabricated. Transfers stream in 64 KiB blocks. Aborted writes retain their declared response interval and unsuccessful stream status.

## Interpretation

PyArrow metadata and the standard Parquet trailer locate chunks and footer. Dictionary pages are included. Unknown gaps may contain indexes or other structures; they are not assumed empty or unnecessary. A mapped intersection proves only that a response interval overlaps a known structure. It does not prove decoding, use by a predicate, optimizer decisions or causal inefficiency. Different file hashes/layouts have different coordinate systems. Similar schemas and matching result digests do not establish general logical data equivalence; the demo separately checks persisted data equality.

## Range lab coordinates

The primary view connects the captured SELECT, HTTP observations, exact response intervals and intersecting column chunks. The horizontal axis is proportional to byte offsets in the selected file; the vertical axis is engine-observed request start time in milliseconds. Each GET response interval is drawn at its start time. Mark height is visual emphasis, not duration. Use explicit start/duration values to assess concurrent reads. HEAD has no body interval. Layout regions share the same byte scale.

All observations and layout regions are drawn without sampling on a canvas. At whole-file scale, nearby marks can overlap or become subpixel. The primary **Zoom to selected range** button changes the byte domain to the exact selected response or structure interval; it preserves the time scale. **Whole file** restores `[0, file size)`. Exact half-open offsets remain visible above the plot and in the compact paginated ledger and linked inspector. BigInt subtraction occurs before conversion to pixel coordinates, including beyond JavaScript’s safe integer range. These display operations do not execute another query or alter measured totals.
