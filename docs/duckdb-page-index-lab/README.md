# DuckDB page-index HTTP lab

On one indexed Parquet file, the point lookup wrote **279,686 B with DuckDB 2.0.1** and
**4,211,424 B with 2.0.0**, a **93.36% reduction** in HTTP response body bytes. Both versions made
four GETs and one HEAD per run and returned `(123456, 645150)`. The wide scan on both versions
and the point lookup with page-index reading disabled on 2.0.1 each wrote 4,211,424 B. All three
repetitions of each configuration agreed on totals and ranges. The order of individual data GETs varied.

DuckDB 2.0.1 can use `ColumnIndex` bounds on a filter column to rule out pages in a surviving row
group. It uses `OffsetIndex` to find the corresponding row spans and page locations in each scanned
column. The lab uses the same-file `parquet_use_page_index = false` control in 2.0.1, the version
that exposes the setting. [DuckDB 2.0.1 release](https://www.duckdb.org/2026/11/20/announcing-duckdb-201),
[DuckDB PR #27326](https://github.com/duckdb/duckdb/pull/27326),
[Parquet Page Index specification](https://parquet.apache.org/docs/file-format/pageindex/).

## File and queries

PyArrow 26.0.0 wrote one row group of 262,144 rows with two required `INT64` columns. `id` is
ascending from 0 to 262,143 and `payload` is `(id * 7919 + 17) % 1000003`. Each column has 16
uncompressed V1 data pages with a 128 KiB target size and no dictionary. Both indexes and
row-group statistics are present for both columns. The lab verifies the absence of statistics
in all 32 data-page headers, so the older page-header pruning path has no bounds to use.

The generated file is 4,196,765 B with SHA-256
`beaf322f97f0316c92d0ac80c58caf89bbcc417e268a4bdf4e3dd355efb5d482`. The point key is
inside the row-group bounds and on page 7 in each column (zero-based, row span `[114688, 131072)`).
The lab decodes both indexes at their footer offsets and checks each `OffsetIndex` location
against its chunk boundary, serialized page length and header row count. It checks `id` page
bounds against the generated row sequence. These local reads happen before the HTTP server starts.

```sql
SELECT id, payload
FROM read_parquet(?, hive_partitioning=false, union_by_name=false, prefetch_strategy='auto')
WHERE id = ?;

SELECT count(*), sum(id), sum(payload)
FROM read_parquet(?, hive_partitioning=false, union_by_name=false, prefetch_strategy='auto');
```

The first parameter is the server URL and the second, for the point lookup, is `123456`. The
generator computes the expected point row and wide aggregate `(262144, 34359607296, 131066747916)`
from the Python values. Both versions match these results. The wide query sums both columns
without a predicate and every data page is covered.

## What the responses carried

All spans below are half-open byte intervals. Every run starts with a HEAD and a 16,384 B footer
prefetch GET at `[4180381, 4196765)`. The response includes **14,663 B of `payload` page 15**,
1,112 B of indexes and 609 B of footer and trailer. Page 15 contains only rows `[245760, 262144)`,
so it is outside the point lookup even when its bytes ride with the footer in 2.0.1. The observed
tail range matches the reader's 16 KiB minimum metadata prefetch for this file size.
[DuckDB 2.0.1 footer read](https://github.com/duckdb/duckdb/blob/v2.0.1/extension/parquet/parquet_reader.cpp#L294-L323).

- **2.0.0 point.** The reader logs `whole_group` prefetch for `id` and `payload`. Three more GETs
  at `[4, 2097156)`, `[2097156, 4194308)` and `[4194308, 4195044)` together cover all 32 pages,
  including the 30 outside the point row's pages. They repeat the 14,663 B of page 15 already in
  the tail response. This yields 4,196,761 B of unique coverage and 4,211,424 B written, of which
  4,209,703 B overlap data pages.
- **2.0.1 point.** A dedicated GET at `[4195044, 4196156)` repeats all 1,112 index bytes from
  the tail (670 B of `ColumnIndex` and 442 B of `OffsetIndex`). The other two GETs exactly cover
  `id` page 7 at `[917669, 1048764)` and `payload` page 7 at `[3015189, 3146284)`. The reader
  logs `SkipPages` for 245,760 of 262,144 rows and `column_wise_eager_pages` with separate
  prefetch groups for the two columns. Three data pages intersect responses, including the
  partial page 15 from the tail. The 279,686 B written include 276,853 B of data pages and
  2,224 B of indexes. Unique coverage is 278,574 B.
- **Controls.** Both wide scans and the 2.0.1 point lookup with indexing disabled use the same
  ranges as the 2.0.0 point lookup. The 2.0.1 same-file toggle reproduces the entire observed
  byte difference at fixed engine, `httpfs`, and prefetch settings. The controls still carry the
  indexes in the tail GET.

The comparison covers loopback HTTP for this layout and these settings. Bytes count successful
server stream writes and exclude HTTP headers. For an interrupted response, only the confirmed
written prefix contributes to overlaps. The unique count unions those prefixes across requests.
`SkipPages` is separate evidence of the pruning decision. Page decoding and client receipt are
outside the scope of server byte counts.

## Reproduce

Recorded on macOS 15.7.9 arm64 with Python 3.13.18 and the locked project environment (PyArrow
26.0.0). DuckDB 2.0.0 reports build `d54cbbe3df` and `httpfs` `77392a7207`. DuckDB 2.0.1 reports
build `8d78114e90` and `httpfs` `f71cfdb4bf`.

From the repository root, use the project environment (`uv sync --frozen` on a fresh checkout).
Prepare the wheels and signed extensions explicitly with network access. On macOS or Linux with
Python 3.11 or later, the setup below puts both in an isolated lab directory.

```sh
export PARQUETSCOPE_HOME="$PWD/.runtime/duckdb-page-index-lab/engine-home"
export UV_CACHE_DIR="$PWD/.runtime/duckdb-page-index-lab/uv-cache"
export PIP_CACHE_DIR="$PWD/.runtime/duckdb-page-index-lab/pip-cache"
uv run --no-sync parquetscope setup --engine 2.0.0

lab_engine="$PARQUETSCOPE_HOME/engines/duckdb-2.0.1"
uv venv --python .venv/bin/python "$lab_engine/venv"
uv pip install --python "$lab_engine/venv/bin/python" --only-binary :all: duckdb==2.0.1
mkdir -p "$lab_engine/extensions"
"$lab_engine/venv/bin/python" -I - "$lab_engine/extensions" <<'PY'
import sys
import duckdb

with duckdb.connect(config={"extension_directory": sys.argv[1]}) as connection:
    connection.execute("INSTALL httpfs")
    connection.execute("LOAD httpfs")
    print(connection.execute("""
        SELECT version(), extension_version
        FROM duckdb_extensions() WHERE extension_name = 'httpfs'
    """).fetchone())
PY
```

Each capture starts a fresh process and loopback range server with zero response delay, no body
rate limit and 64 KiB write blocks. The trace includes query binding and execution. The pinned
settings include one thread, 256 MiB,
Parquet prefetch enabled with strategy `auto`, Parquet and HTTP metadata caches and external
file cache disabled, and the same HTTP timeout and retry settings. Both versions read back the
same common settings. The order reverses in the second of three rounds. Each client loads its
prepared `httpfs` with extension auto-installation disabled.

```sh
uv run --no-sync python scripts/duckdb_page_index_lab.py
uv run --no-sync python scripts/duckdb_page_index_lab.py --verify
uv run --no-sync python scripts/duckdb_page_index_lab.py --verify --output docs/duckdb-page-index-lab
uv run --no-sync pytest -q tests/test_duckdb_page_index_lab.py
```

The first command writes a regenerated file and evidence to `.runtime/duckdb-page-index-lab/evidence/`.
The runner stages the file and evidence until the captures and summary checks complete.
The second verifies those artifacts against their raw observations and the regenerated file.
The third verifies the checked-in evidence with the saved file map. Compare fixture hashes
before comparing runs from another PyArrow version.

## Evidence

- [report.json](report.json) holds the file map, exact SQL and expected results, HTTP profile,
  and per-run summaries. `bytes_by_kind` has written and unique bytes for each region type.
  `bytes_in_pages_outside_point` counts data outside the point row's pages, including for the
  wide scans where that data is required.
- [runs.json](runs.json) holds the 15 runs in execution order, the versions and effective
  settings, results, Parquet reader logs, and raw range-server observations. For each request,
  `matches` identifies the written span and the bytes intersecting each region in the file map.
  Match `id` to the raw observation to see the requested range and declared response span.
- [SHA256SUMS.txt](SHA256SUMS.txt) pins both JSON files. The 4 MiB Parquet file is regenerated locally.
