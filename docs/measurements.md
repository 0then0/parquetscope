# Measurement contract

All internal and exported file intervals are nonempty, half-open `[start,end)`. HTTP `bytes=a-b` has inclusive `b`; the response interval becomes `[a,b+1)`. Explicit requested upper bounds are preserved even beyond EOF; the server clamps the response separately. Open-ended and suffix ranges are resolved against the stable file size. The original header is always retained. Adjacent intervals join in the coverage union but do not overlap. Empty ranges do not add coverage.

## Sources and formulas

Engine observations come from `CALL enable_logging('HTTP', storage_buffer_size=0)` followed by `duckdb_logs_parsed('HTTP')` in DuckDB 1.5.6 and 2.0.0 (logging is enabled before the configuration is locked, which DuckDB 2.0 requires). Request fields include method, URL, engine wall-clock start, integer millisecond duration and headers. Response fields include status, reason and headers. The engine does not expose client-received, decoded or SQL-used byte counts. Those quantities remain unavailable.

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

The endpoint is bound to `127.0.0.1`, has unguessable per-run routes and serves only prepared files. HEAD sends no body and ignores Range. GET supports full 200, single-range 206 (inclusive Content-Range), and unsatisfiable 416 with `Content-Range: bytes */size`. Unsupported multi-range and unknown units are explicitly ignored with a full 200 response, a behavior allowed by RFC 9110. No multipart response is fabricated. Unrestricted transfers stream in 64 KiB blocks; limited transfers use the bounded chunks below. Aborted writes retain their declared response interval and unsuccessful stream status.

## HTTP profiles

The default server profile has zero response delay and no body-rate limit. For a matched
HEAD or GET, the fixed delay begins after file identity, descriptor and range preparation,
immediately before response headers are generated. It applies once, also to prepared-route
409/416 responses; unrelated routes retain their existing 404 contract. `response_delay_ms`
is the requested wait; each observation's `profile_delay_ms` measures the actual elapsed
wait. File preparation time before the wait is part of request duration, not profile delay.

For a configured rate R bytes/second, one capture-local token bucket has capacity
`B = max(1, min(16384, ceil(R / 10)))` bytes and starts full. All parallel GETs across all
manifest files share it; HEAD and headers consume nothing. Writes use at most B bytes per
chunk. One admitted write runs at a time, with token accrual suspended during that write;
budget waits release the admission lock and periodically recheck cancellation, disconnect
and deadline. There is no queued future reservation. Reservations across a monotonic interval
of length t cannot exceed `B + R*t`; scheduling and I/O can only reduce sustained throughput.
Only one chunk can be in flight. A fresh capture gets independent state.

The restriction point is immediately before body bytes are handed to the response-stream
write. `reserved_body_bytes` records admission even when a later write fails; it is not
refunded. `written_body_bytes` records successful writes and explicit short-write counts.
A failing call may have accepted an unknown partial block. System-call frequency, socket
buffers and thread scheduling affect observations; successful server writes do not confirm
DuckDB receipt. This is a model of a local HTTP server, not transport RTT, TCP congestion,
packet loss, S3 or any cloud provider.

Waits poll at most every 50 ms and hold no common observation lock. TCP resets and write
failures identify disconnects. A peer FIN only closes its sending side and cannot distinguish
a client that is still reading from one that closed completely; it is not treated as failure.
Cancellation and server
stop wake them; the existing execution deadline is unchanged. A socket write can still take
up to its existing two-second timeout before cleanup. Connection and observation limits
remain in force. Low rates can therefore produce incomplete timeout artifacts, which are
not successful benchmark results.

Server `start_ms`/`duration_ms` retain their original boundaries. New timestamps distinguish
delay start, headers completion and first/last attempted body write. `body_budget_wait_ms`
aggregates admission contention and token wait; `body_write_ms` aggregates write calls.
The span from first to last write includes intervening waits, so it overlaps those
components. Parallel requests overlap each other: duration sums are not query wall time.
No component is added to the existing byte metrics. The [v4 contract](artifacts.md#format-v4-http-profiles)
specifies fields and validation. Older artifacts have no recorded HTTP profile.

## Interpretation

PyArrow metadata and the standard Parquet trailer locate chunks and footer. Dictionary pages are included. Unknown gaps may contain indexes or other structures; they are not assumed empty or unnecessary. A mapped intersection proves only that a response interval overlaps a known structure. It does not prove decoding, use by a predicate, optimizer decisions or causal inefficiency. Different file hashes/layouts have different coordinate systems. Similar schemas and matching result digests do not establish general logical data equivalence; the demo separately checks persisted data equality.

## Range lab coordinates

The primary view connects the captured SELECT, HTTP observations, exact response intervals and intersecting column chunks. The horizontal axis is proportional to byte offsets in the selected file; the vertical axis is engine-observed request start time in milliseconds. Each GET response interval is drawn at its start time. Mark height is visual emphasis, not duration. Use explicit start/duration values to assess concurrent reads. HEAD has no body interval. Layout regions share the same byte scale.

All observations and layout regions are drawn without sampling on a canvas. At whole-file scale, nearby marks can overlap or become subpixel. The primary **Zoom to selected range** button changes the byte domain to the exact selected response or structure interval; it preserves the time scale. **Whole file** restores `[0, file size)`. Exact half-open offsets remain visible above the plot and in the compact paginated ledger and linked inspector. BigInt subtraction occurs before conversion to pixel coordinates, including beyond JavaScript’s safe integer range. These display operations do not execute another query or alter measured totals.

## File sets

A manifest capture reads several files through one `input` view. The contract above holds per file; this section adds what changes.

**Boundary.** HTTP logging starts before the view is created and stops after the SELECT result is fully consumed. DuckDB binds the view against the first listed file, so those requests belong to the capture and are labelled `view_creation`. Requests that start during the SELECT are labelled `select`. The label is the phase in which a request started, from wall-clock boundaries recorded by the worker. It does not say why DuckDB issued the request. A request starting outside both windows is `unattributed`. Hashing and footer reads done by the application to prepare the manifest are excluded, may read every file completely, and are not HTTP traffic. A lower HTTP byte count therefore says nothing about the application's total I/O.

**Per-file accounting.** Counters, intervals and coverage are computed per file and summed for the set. Equal offsets in two files are different bytes. Files are compared across runs by content hash, never by offset or basename.

**Observed access.** A file is `get`, `head_only` or `none` according to the requests recorded for it. `none` is an observation about this trace. If the capture is incomplete it does not establish that the file was untouched.

**What stays unproven.** The measurements do not show that:

- a GET interval intersecting a column chunk means the column was decoded or used. The first read of a file often fetches its final bytes, which can cover the footer and the tail of an earlier chunk;
- a footer intersection means the query was metadata-only;
- server-written bytes were received, confirmed or used by DuckDB;
- a query with a small result reads little, or that reading fewer files makes anything faster or cheaper.

The loopback server serves an explicit URL list. It does not reproduce object-store listing, globbing or retries, so it cannot reproduce behavior that depends on them, such as duckdb/duckdb#21347. That issue is closed and is not claimed to apply to the supported DuckDB version.

**Comparison.** Two runs are comparable only with respect to the facts the UI lists: file contents and locations, read options, DuckDB and httpfs versions, effective settings, SQL, completeness and artifact version. Ordered result digests and similar schemas do not establish equivalence of the data.

