# How Parquet layout changes HTTP reads

A reproducible NYC TLC case study for ParquetScope v0.1.0. Open the static lab to explore
three real captures using the existing Range lab, structure inspector and comparison.
The browser executes no SQL and downloads no source Parquet. It needs no account,
Python, external CDN, font, analytics, API or DuckDB-WASM.

## Source and exact workload

[NYC TLC Trip Record Data](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page),
[Yellow Taxi, January 2025](https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2025-01.parquet).
Pinned size **59,158,238 bytes**, SHA-256
`9af277e4c0d3f9deb30644da822981e1e7df6af58313170fd3aa8a474485488a`.
3,475,226 rows, 20 columns, 4 row groups. A mismatch stops the experiment. There is
no fallback month or automatic checksum update. TLC notes that records come from
providers and does not warrant their accuracy.

```sql
SELECT
    count(*) AS n,
    sum(CAST(fare_amount AS DECIMAL(18,2))) AS fare
FROM input
WHERE PULocationID = 132
  AND trip_distance > 0
```

Every layout and all nine measured runs produce **n = 142826, fare = 8038349.75**.
The DECIMAL cast is fixed; no floating-point SUM is used. v0.1.0's runtime preview
represents Decimal through a JSON-quoted string. The runner decodes that representation
without floating point and checks it against the native captured result digest.
A result digest proves neither table equality nor equality of source rows.

## Measured evidence, 2026-10-20

All repetitions are retained
in [report.json](report.json) and unchanged native artifacts under `captures/`.
The interactive lab explicitly shows repetition 1 for each layout.

- **A, as delivered:** 59,158,238 bytes, 4 row groups.
  Repetitions 1 / 2 / 3 each observed **14 GET + 2 HEAD**, **11,387,877 server-written body bytes**.
- **B, repacked in original order:** 53,370,794 bytes, 29 row groups.
  Repetitions 1 / 2 / 3 each observed **118 GET + 2 HEAD**, **11,446,545 server-written body bytes**.
- **C, sorted by PULocationID:** 59,417,539 bytes, 29 row groups.
  Repetitions 1 / 2 / 3 each observed **10 GET + 2 HEAD**, **795,241 server-written body bytes**.

For this workload, C has **93.052567% fewer server-written body bytes than B**:
`(11446545 - 795241) / 11446545 * 100`, comparing repetition 1 on each side.
All three repeats have the same byte totals. The denominator is B's server writes,
not file size, client-received bytes or SELECT duration. B is smaller than A but
makes more requests here; C is larger than B but needs fewer bytes and requests.
Exact GET counts are observations, not an engine guarantee or an acceptance snapshot.

A/B change writer, encoding and layout as well as row-group count. These differences
cannot be attributed to row-group size alone. B/C share the same DuckDB writer and
settings; row order is the intended difference. Sort ties have no secondary key.
Sorting and row-group pruning are established mechanisms. ParquetScope makes this
workload's observed reads inspectable; it does not claim to discover those mechanisms.

One earlier attempt failed runner validation after a successful A capture because
its expected Decimal preview representation was wrong. It is retained under
[failed-attempts/attempt-1](failed-attempts/attempt-1/report.json) and linked in the
report's `previous_attempts`. It is excluded from the nine successful repetitions.
The successful experiment used a corrected runner assertion; the capture itself is unchanged.

## Method and provenance

Runtime: published **ParquetScope 0.1.0**, DuckDB **1.5.6**, Python **3.13.16**,
PyArrow **26.0.0**, Darwin/arm64. Exact OS release, dependency versions and the base
commit are in the report. The experiment ran outside the checkout in a separate
virtual environment installed from the published release wheel. Installed package
files, including worker and viewer, were compared byte for byte against the wheel.
The exact experiment runner source and its hash are preserved in
[provenance/runner.py](provenance/runner.py) and the report. The recorded commit
identifies its base checkout; the source snapshot identifies the executed runner.

[Published wheel](https://github.com/0then0/parquetscope/releases/download/v0.1.0/parquetscope-0.1.0-py3-none-any.whl)
SHA-256: `616192b3e0b776ca1221b86d4b2c80dbd545b6884457d6df0394045a6d9169e9`.
The hash was checked against GitHub release metadata before installation.

Preparation uses a separate DuckDB connection: one thread, `memory_limit='1GB'`,
`preserve_insertion_order=true`, spill to a working-directory subfolder, up to 10 GB
of temporary disk. Writer options for B/C are `FORMAT PARQUET`, `COMPRESSION ZSTD`,
`COMPRESSION_LEVEL 3`, `ROW_GROUP_SIZE 122880`, `PARQUET_VERSION V1`. Other writer
options use identical DuckDB 1.5.6 defaults. B selects the source without sorting;
C uses `ORDER BY PULocationID`. Actual hashes, schemas, row-group row counts,
compression, encoding names and writer identities are recorded for all layouts.

Before measurement, the runner checks all persisted logical column types and order,
row counts, sizes and row groups, then compares **all columns using EXCEPT ALL in
both directions** for A/B and A/C. All four difference counts were zero. A streaming
batch comparison additionally verifies B's original row order. Arrow `large_string`
and `string` describe different in-memory offset widths; both are Parquet UTF8 and
DuckDB VARCHAR. Actual Arrow schemas are recorded, not silently treated as identical.
The source values, Parquet logical types and columns are preserved. The exact SQL
is separately checked on each prepared file before any measured capture.

Measurements run sequentially A1..A3, B1..B3, C1..C3. Each uses a fresh worker,
4 threads, a **512 MiB DuckDB engine memory limit**, 60-second deadline, disabled
external file cache, no spill and one preview row. The engine memory limit is not
an OS process limit. Preparation, hashing and equality work are outside capture.
The real pipeline records input binding plus one fully consumed SELECT. There is
no hidden second SELECT or EXPLAIN inside a measured run. Independent server
observations are reconciled by response IDs, method, ranges, status and lengths.
Every accepted run has SQL succeeded, complete capture, successful reconciliation,
no errors and known metrics. Failed/incomplete attempts remain evidence, never
successful statistics. The output directory must be empty to prevent lost evidence.

Report metrics are derived by the existing product and revalidated by its native
artifact loader. GET/HEAD counts, response interval sum, interval union, repeated
coverage and server-written bytes retain their existing definitions. HEAD adds
zero body bytes. Exact byte counts are decimal strings. All links/hashes are checked
by offline tests and the static builder. `manifest.json` and `SHA256SUMS.txt` cover
portable evidence (excluding themselves). The site adds only application assets,
a blank offline export template and `index.html` to that evidence.

## Reproduce the experiment

Prerequisites: a git checkout, uv, curl, and Python 3.13. The commands create a
separate wheel environment and evidence directory.
The first source download is streamed with a 30-second socket timeout and bounded
by the pinned size into a temporary sibling, then verified before rename/use.
A cached working source is reverified before reuse. Preparation may use several GB
of temporary disk. No root or OS cache eviction is needed.

Run from the checkout:

```sh
REPO="$PWD"
WHEEL=/tmp/parquetscope-0.1.0-py3-none-any.whl
curl -fL --max-time 60 \
  https://github.com/0then0/parquetscope/releases/download/v0.1.0/parquetscope-0.1.0-py3-none-any.whl \
  -o "$WHEEL"
# Must print the published hash above; runner also enforces it.
shasum -a 256 "$WHEEL"
uv venv /tmp/tlc-env --python 3.13
uv pip install --python /tmp/tlc-env/bin/python "$WHEEL"
cd /tmp
PARQUETSCOPE_HOME=/tmp/tlc-runtime /tmp/tlc-env/bin/parquetscope setup
PARQUETSCOPE_HOME=/tmp/tlc-runtime /tmp/tlc-env/bin/python \
  "$REPO/scripts/real_data_lab.py" \
  --wheel "$WHEEL" --work-dir /tmp/tlc-work --output /tmp/tlc-evidence
# Offline revalidation of all nine captures and their report, without downloading TLC:
/tmp/tlc-env/bin/python "$REPO/scripts/real_data_lab.py" \
  --output /tmp/tlc-evidence --verify
```

Use a new empty output directory for another experiment. A new run records all its
own observations, including failures. Do not merge its successful statistics with
older attempts. The script itself performs no network setup during capture.
Dependency versions actually resolved in the wheel environment are recorded; wheel
metadata pins DuckDB and permits newer versions of other dependencies.

## Build and inspect the static lab

From the checkout, using the existing pinned frontend environment:

```sh
# Bundled evidence, no source dataset download:
pnpm --dir frontend build:lab
# Or package a fresh experiment after pnpm --dir frontend build:
(cd frontend && node scripts/lab.mjs /tmp/tlc-evidence)
.venv/bin/python -m http.server 8418 --bind 127.0.0.1 --directory .runtime/pages
```

Open `http://127.0.0.1:8418/parquetscope/`. Relative resources work under the project
subpath. Hash links such as `#C` and `#compare-B-C` survive refresh without a server
rewrite. The lab is a static website: any static host works. A simple HTTP server is
only for local browser module loading; **standalone viewers open directly with
`file://` and no server or network**. They use the published wheel's original offline
viewer, can import other native captures and compare them offline. The lab's
Offline viewer button also exports the currently selected run through the existing UI.

```sh
.venv/bin/pytest -q tests/test_real_data_lab.py
pnpm --dir frontend test:lab
```

The normal CI checks are offline with respect to the TLC dataset. The separate
`real-data-acceptance.yml` workflow is opt-in through `workflow_dispatch`, downloads
the pinned source and published wheel, records a full experiment, builds the static
lab and uploads reviewable evidence. It does not deploy.

The manual `pages.yml` workflow publishes the checked-in static lab. See the
[maintainer guide](https://github.com/0then0/parquetscope/blob/main/docs/releasing.md#publish-the-static-lab) for configuration and deployment.

## Limits of interpretation

Server-written bytes are successful stream writes, not proof the client received,
decoded or used them. Chunk intersections do not prove decoding; overlap does not
automatically mean waste. Loopback captures are not S3 performance, a cloud bill or
controlled timing benchmarks. Fresh workers are not cold disks or cold S3; OS/disk
caches are uncontrolled. Byte reductions do not establish equivalent speedup. One
workload on one month cannot prove a universally best layout. The lab retains SQL,
column names, headers and necessary metadata; it does not promise absence of every
kind of data within artifacts. Only aggregate result values are included in the
report, not original trip rows. No credentials are involved.

## Validation of this checked-in evidence

[validation.json](validation.json) records the locally executed checks: 89 backend tests,
8 frontend unit tests, 8 local/offline browser scenarios and 6 static-lab browser scenarios,
lint/type/schema checks, wheel/sdist validation and an isolated rebuilt-wheel smoke test.
Browser checks cover hash refresh, linked selection, B/C comparison, downloads,
keyboard controls, 390 px width, and standalone file viewers with networking disabled.
Static browsing makes zero execution API requests. See [validation coverage](https://github.com/0then0/parquetscope/blob/main/docs/validation.md)
for the distinction between package checks and experiment provenance.
