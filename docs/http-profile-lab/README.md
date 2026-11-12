# HTTP profile lab

This bounded experiment compares Parquet prefetch on/off in **one prepared DuckDB 1.5.6 environment**, using one SQL query, two generated local Parquet files and three explicit HTTP server profiles. It measures this server model. It does not establish S3 or any cloud provider's performance.

Upstream context checked on **2026-11-12**: [DuckDB #28138](https://github.com/duckdb/duckdb/pull/28138), “Size remote read splits from the measured network throughput”, is **open and not merged** (head `434550f5b42ccbd5a6c7b6f1eb6cbe4dae596489`). Its proposed adaptive splitting is not part of the released engine measured here. The lab toggles the existing `disable_parquet_prefetching` setting; it does not benchmark that PR.

## Reproduce

Use the existing locked environment and prepared engine. The runner never installs or synchronizes dependencies:

```sh
uv run python scripts/http_profile_lab.py \
  --output docs/http-profile-lab --data .runtime/http-profile-lab \
  --deadline-seconds 180
```

If the pinned engine is not already prepared, prepare it separately with `uv run parquetscope setup --engine 1.5.6`. That preparation downloads the pinned wheel and httpfs; it is outside the measured run. No second engine version, remote dataset, cloud credentials or network benchmark is involved.

The whole lab has a 180 s cancellation timer and each capture keeps the existing execution deadline, capped at 30 s and at the remaining lab time. Metadata preparation is outside each capture's measured elapsed time, but inside the whole-lab limit. The fixed generator is bounded at 40,000 rows total. Cleanup can extend the wall-clock stopping point slightly. Every measured capture uses a fresh worker and an independent limiter.

## Data and result contract

Seed `20261112`; two files of 20,000 rows, each with four 5,000-row groups and eight columns: `id`, `category`, `amount_cents`, `note`, and four unrelated padding columns. Zstandard compression, 32 KiB data pages, dictionary encoding for `category`. Multiple independent column chunks make reading extra bytes observable. Total source size is about 4.46 MB. [manifest.json](manifest.json) records exact sizes and SHA-256 identities; source files are regenerated under `.runtime/` and are not included here. Another PyArrow version may produce different file bytes.

```sql
SELECT count(*) AS n, sum(amount_cents) AS cents, sum(length(note)) AS note_chars
FROM input WHERE category = 'keep' AND id % 7 = 0
```

A separate PyArrow/Python pass reads the generated files, evaluates the predicate and computes the expected row. It uses neither DuckDB nor HTTP. Each capture must match the row count, the ordered length-prefixed JSON SHA-256 digest, and the runtime preview. Both modes must issue actual GET requests against both files. The artifacts retain identities, SQL, digest and effective engine settings; previews remain outside artifacts under the usual contract.

## Conditions

Each of these profiles has prefetch **on** (`disable_parquet_prefetching=false`) and **off** (`true`):

- **Unrestricted:** `response_delay_ms=0`, `body_bytes_per_second=null`.
- **Delay:** `response_delay_ms=40`, body rate unrestricted.
- **Delay and rate:** the same 40 ms delay, with a **shared 2,097,152 bytes/s** body limit for both files and every concurrent response in the capture.

Three rounds produce **18 captures**. Profiles rotate by round; adjacent on/off pairs reverse their order each round. The worker reads back the prefetch setting and all fixed engine settings. A run enters the successful lab only when SQL succeeds, observations are complete and reconciled, and the independent result contract matches. A timeout is saved as a failed capture before the lab stops; it is not a successful benchmark result.

The fixed settings are 4 threads, 256 MiB memory, external file cache disabled, HTTP metadata cache disabled and the normal pinned HTTP settings. The pinned environment manifest and actual engine readback are in [report.json](report.json).

The server delays each accepted prepared-route HEAD or GET once, after resolving its route, identity and response range, immediately before sending response headers. Actual delay is recorded separately. Waiting stops on cancellation, disconnect or deadline. Unprepared routes keep the normal server error behavior.

The capture limiter is a monotonic token bucket shared by every file/connection. Its allowance is `max(1, min(16384, ceil(rate / 10)))` bytes, initially available; this lab's limited profile therefore has a **16,384-byte burst** and write chunks of at most that size. The limiter reserves immediately before bounded body writes; HEAD and headers use no tokens. Writes share the allowance instead of gaining one allowance per connection. The artifact records the requested/effective profile, semantics version, burst and chunk size. Socket buffering and scheduling still affect what clients observe. Successful writes count bytes accepted by the server response stream, not receipt acknowledged by DuckDB.

## Independent HTTP control

The runner also uses `http.client`, without DuckDB, to check HEAD and GET with a 60 ms delay. Two parallel clients then each read 128 KiB from a different file under a 60 ms delay and one shared 262,144 bytes/s limiter. It checks complete byte counts, request-ID correlation, recorded budget waiting and the aggregate envelope.

The two responses total 262,144 bytes with a 16,384-byte burst, so their aggregate body-write span must be at least **937.5 ms**, allowing 30 ms for timestamp placement and scheduling. An accidental independent per-connection limiter would permit roughly twice the aggregate rate and fail this control. Client timings and complete server observations are preserved in the report.

## Recorded observations

The observations below are generated from the 18 retained captures. Each configuration has three repetitions; [report.json](report.json) contains every run and min/median/max values. No speedup is required for acceptance.

All 18 captures succeeded, reconciled and matched the independent result digest `e63689a2105bb3210407911e1d3e6945d2f4f07c0878e4f9f99cf28cb1b58421` (one result row: `1386`, `71338679`, `55440`). The complete lab took **36.85 s** of its 180 s budget.

Request counts and bytes were identical across all three profiles and repetitions: prefetch on issued **19 GET + 3 HEAD** and wrote **1,074,034 bytes**; prefetch off issued **22 GET + 3 HEAD** and wrote **14,836,481 bytes**. Median SELECT times, on/off respectively:

- Unrestricted: **15.8 / 22.2 ms**.
- 40 ms delay: **398.3 / 505.9 ms**.
- 40 ms delay plus shared 2 MiB/s: **777.9 / 8683.6 ms**.

For this query, disabling prefetch increased both requests and bytes. Its confirmed ranges covered the whole 4,459,446-byte file set and repeated 10,377,035 bytes of coverage, while prefetch on covered 1,051,620 unique bytes with 22,414 repeated bytes. These are observed range patterns, not a diagnosis of DuckDB internals. This dataset therefore does not demonstrate a crossover where fewer requests lose to extra bytes; it retains the observed outcome. The much larger off-mode transfer becomes visible under the shared rate limit. No timings or request shapes from the unmerged upstream PR were measured.

The plain HTTP control observed headers after about **62.1 ms** for both a 60 ms HEAD and GET delay. Parallel responses had a **964.0 ms** body-write span, above the **937.5 ms** shared-budget lower bound. This verifies the configured model independently of DuckDB.

Import captures from [captures/](captures/) into any standalone file under [viewers/](viewers/) to compare profiles offline. Each of the six viewers embeds repetition 1 of one configuration. [SHA256SUMS.txt](SHA256SUMS.txt) covers captures, viewers, manifest and report.

## Interpretation limits

Query duration covers SELECT execution and full result consumption, excluding view binding. Capture elapsed time includes worker startup, binding and cleanup. Per-request delay and body-budget wait aggregates can overlap across requests; their sum is not query wall time. The first-to-last body-write span also includes waits between writes; `body_write_ms` sums time inside write calls.

Prefetch, read merging, retries and the engine's remote-read sizing may change both request count and bytes. Fewer requests can cost more transfer time when extra bytes cross a shared limit. Timing differences have several possible causes, including read shape, concurrency, body pacing, socket buffering, OS/disk caches and scheduling. These three repetitions do not isolate each cause or establish a universal winner. Fresh workers make the client cold; OS/disk caches are uncontrolled.

The model has fixed server wait and stream-write pacing. It does not simulate transport RTT, TCP congestion, packet loss, jitter, cloud request queues or real object-store behavior.
