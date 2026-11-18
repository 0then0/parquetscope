# DataFusion page-index cache lab

One question: when the same Parquet object is opened twice through DataFusion's Parquet opener with one shared `FileMetadataCache`, does the second open request the page index (`ColumnIndex` + `OffsetIndex`) again? This reproduces [apache/datafusion#23978](https://github.com/apache/datafusion/issues/23978) locally, with one small file, loopback HTTP and no cluster.

This is not a ParquetScope engine integration. The CLI and UI are unchanged; a standalone Rust harness runs DataFusion, and ParquetScope's `RangeServer` records the HTTP requests.

## Upstream status

Checked on **2026-11-18**:

- [#23978](https://github.com/apache/datafusion/issues/23978) is **open**. [#24004](https://github.com/apache/datafusion/pull/24004) ("cache deferred page index loads") was closed without merging; [#27488](https://github.com/apache/datafusion/pull/27488) is open and not merged. No released fix is measured here.
- [#22857](https://github.com/apache/datafusion/pull/22857) (merged 2026-06-20, backported to `branch-54`, released in 54.1.0) moved the page-index load after row-group statistics pruning and skips it when every surviving row group is fully matched.

What the crate sources of both versions show (`datafusion-datasource-parquet`):

- **54.0.0**: `CachedParquetFileReader::get_metadata` ignores the caller's page-index policy. `DFParquetMetadata::fetch_metadata` loads the footer *and* the page index (`PageIndexPolicy::Optional`) when a cache is present and stores both. A cache hit returns that entry, and the opener's `load_page_index` finds the index already present.
- **54.1.0**: the opener asks for `PageIndexPolicy::Skip`, `get_metadata` forwards it, and the cache stores footer-only metadata. When `should_load_page_index` is true, `RowGroupsPrunedParquetOpen::load_page_index` reads the index through `ParquetMetaDataReader::load_page_index` on the `AsyncFileReader`, which does not write back to the cache. The next open gets the footer-only entry (a hit, because the policy is `Skip`) and reads the index again.
- In both versions the cache key is the object `Path`; an entry is valid while `size` and `last_modified` match (`CachedFileMetadataEntry::is_valid_for`).

## Harness

[`scripts/datafusion_page_index_harness/`](../../scripts/datafusion_page_index_harness/) has one source, [`main.rs`](../../scripts/datafusion_page_index_harness/main.rs), and two crates, `df-54.0.0/` and `df-54.1.0/`. Each `Cargo.lock` pins all 29 `datafusion*` crates to exactly that version; both resolve the same arrow/parquet 58.4.2, object_store 0.13.2 and tokio 1.54.1, so only DataFusion differs. Builds use `--locked` and go to `.runtime/datafusion-page-index-lab/target-<version>/`, outside the Python environment.

Per process (one run): one `HttpStore`, one fresh `DefaultFilesMetadataCache`, one `CachedParquetFileReaderFactory`. Each open builds a new `DataSourceExec` over the same `PartitionedFile` (same path, size and `last_modified`, hence the same cache key) with a `ParquetSource` that uses that factory, wraps it in `FilterExec` with the same predicate, and collects the result. Executing the plan runs `ParquetMorselizer` and its open state machine, the path a query takes; nothing calls the metadata API directly. Options are `TableParquetOptions::default()`: page index on, `pushdown_filters` off, `metadata_size_hint` 524,288 bytes (applied explicitly, as listing tables do).

The harness reads nothing before the first open: the schema is fixed in the source and the object size is a command-line argument, so the measured trace has no setup reads. Opens are sequential. The harness prints `begin`, waits for `go`, runs the open, prints `end` and waits for `next`. The runner records the server clock and the observation count at each boundary after the server has no open connection, assigns each request to the open whose window contains it, and fails the run if any request starts outside a window.

## Fixture

[`scripts/datafusion_page_index_lab.py`](../../scripts/datafusion_page_index_lab.py) writes `fixture.parquet` with PyArrow 26.0.0 (seed 23978): 100,000 rows, 4 row groups of 25,000, uncompressed, no dictionary, 32 KiB data pages, page index on. Columns (`Int64`, nullable): `id` = 0…99,999; `fk` = `(id × 7919) mod 50021`, null when `id mod 997 = 0` (101 nulls); `val` = random. Only `id` and `val` have column-chunk statistics. Size 2,405,972 bytes, SHA-256 `d4ca1bf3…f25b` (full value in [report.json](report.json)). The file is regenerated and ignored by Git; another PyArrow version can write different bytes.

The runner's own Thrift compact reader decodes the footer and takes the region bounds from `ColumnChunk` fields 4–7. It then decodes every region separately and requires it to consume exactly its recorded length. Checked on the produced file and cross-checked with PyArrow:

- every chunk has an `OffsetIndex` with 5–7 pages; `id` and `val` chunks have a `ColumnIndex`; `fk` chunks have **none** (PyArrow writes no `ColumnIndex` without statistics);
- `fk` chunks have no `Statistics` at all (no min/max, no `null_count`); `id` chunks have `null_count = 0`;
- 20 regions (8 `ColumnIndex`, 12 `OffsetIndex`) form one contiguous span: `ColumnIndex` [2,401,886, 2,403,278), `OffsetIndex` [2,403,278, 2,404,299). Footer: [2,404,299, 2,405,972).

## Scenarios

The main and control predicates have the same shape. Only the column metadata differs.

- **main**, `fk >= 0`: `fk` has no row-group statistics, so no row group can be proved fully matched and 54.1.0 must load the index.
- **control**, `id >= 0`: min ≥ 0 and `null_count = 0` in every row group, so all 4 row groups are fully matched and 54.1.0 skips the load.
- **is_not_null**, `id IS NOT NULL`: supplementary, the issue's predicate shape on a column with `null_count = 0`.

Expected results are computed from the Python values at generation time and are independent of DataFusion: `rows`, `sum(id)`, `sum(val)` and `Σ (31·id + val) mod 1,000,003` over the rows that pass the filter (main: 99,899 rows; control and is_not_null: 100,000). Every open of every run matched.

## Reproduce

Requires the locked Python environment and a Rust toolchain (recorded with rustc 1.100.0). The first build downloads the locked crates.

```sh
uv sync --frozen
uv run python scripts/datafusion_page_index_lab.py --output docs/datafusion-page-index-lab
```

This regenerates the fixture, builds both harnesses, runs 3 repetitions × 3 scenarios × 2 versions (fresh server, process and cache each), and writes `report.json`, `runs/*.json` and `SHA256SUMS.txt`. To check the saved evidence without running anything:

```sh
uv run python scripts/datafusion_page_index_lab.py --output docs/datafusion-page-index-lab --verify
uv run pytest -q tests/test_datafusion_page_index_lab.py
```

`--verify` checks the checksums, revalidates every raw server observation (`ServerObservation4`), recomputes each request's overlap with the regions, the per-open summaries and the verdict, and compares them with the saved files. If `fixture.parquet` is present with the recorded hash, it also re-extracts the regions from its footer.

## Reading the evidence

Each `runs/<version>-<scenario>-<n>.json` has, per open: the raw observations, the matched `requests`, the `summary`, the query result, `cache_after` (DataFusion's `extra_info` for the cached entry; `page_index` is `true` when both indexes are cached) and the DataFusion scan metrics. For each request:

- `delivered_span` is the response start plus the body bytes the server wrote. For an incomplete response only that prefix counts; the announced range is kept as `response_span`, `declared_index_bytes` vs `delivered_index_bytes`.
- `regions` lists every region the response overlaps, with overlap bytes and `fully_delivered`. One request can cover several regions of both kinds (a coalesced read) or only part of one.
- `index_role`: `index_only` when every delivered byte lies inside index regions (a dedicated index fetch), `incidental` when the response also carries other bytes (the footer prefetch), `none` otherwise.

`report.json` → `summary` gives per version, scenario and repetition: request count, server-written bytes, index-touching spans, cache state and open wall time; `observations_vs_hypothesis` is computed from those, not asserted.

## Observations

All 3 repetitions gave identical request lists and byte counts. Requests per open (all `GET`, all complete):

**54.0.0, main**

- open 1: 5 requests, 2,926,170 B. `bytes=1881684-2405971` (the 512 KiB footer prefetch) covers all 20 index regions and the footer; DataFusion decodes the index from it and caches it (`page_index: true`). The other 4 requests are the row groups' data.
- open 2: 4 requests, 2,401,882 B, data only. **No request touches the index.**

**54.1.0, main**

- open 1: 6 requests, 2,928,583 B: the same prefetch (index bytes arrive but are not decoded, because the policy is `Skip`), then a dedicated **`bytes=2401886-2404298`** (2,413 B, exactly the 20 regions), then the 4 data reads.
- open 2: 5 requests, 2,404,295 B: **`bytes=2401886-2404298` again** and the 4 data reads; no footer read (cache hit). The cache entry stays footer-only (`page_index: false`) after both opens.

So the hypothesis holds: 54.1.0 re-reads the page index on the second open and 54.0.0 serves it from the cache. The difference per repeated open is one request and 2,413 bytes.

**54.1.0, control** (`id >= 0`): open 1 has 5 requests and open 2 has 4, identical to 54.0.0. DataFusion reports `4 fully matched` row groups and `page_index_load_skipped = 1` in each open; there is **no separate index request**. The prefetch of open 1 still carries the index bytes incidentally. They are not decoded, and the cache stays footer-only. 54.0.0 behaves as in the main scenario.

**is_not_null** (`id IS NOT NULL`): in 54.1.0 it behaves like main, re-reading `2401886-2404298` on open 2, although `id` has `null_count = 0`. DataFusion reports no fully matched row group. In the 54.1.0 source, `identify_fully_matched_row_groups` builds `NOT(id IS NOT NULL) OR id IS NULL`, the physical simplifier has no `NOT(IS NOT NULL)` rule, and the pruning-predicate builder treats a `NOT` over a non-column as unhandled (always true). The row-group statistics therefore never prove an `IS NOT NULL` predicate fully matched on this path, whether or not `null_count` is present. This is consistent with the issue's report that the skip does not fire for its `IS NOT NULL` predicates, but the missing `null_count` is not what decides it here. This run did not test a non-nullable field or a plan whose logical optimizer removes the predicate.

Supplementary metrics: loopback open wall time medians, open 1 / open 2: 54.0.0 main 7.0 / 4.6 ms, 54.1.0 main 7.6 / 5.4 ms; one first open per version took about 34 ms. These are one machine's loopback figures and support no conclusion; the evidence is the repeated request for a known byte range.

## Limits

- One file, one partition and one open per execution. Opens of different row-group ranges of a split file, as in the issue's Comet case, were not run; the cache key does not depend on the range.
- Loopback HTTP with no latency, so the cost of the extra round trip is not measured.
- Only DataFusion 54.0.0 and 54.1.0, with parquet 58.4.2 in both. Later releases (54.2.0, 55.x) and upstream `main` were not run.
- With a smaller `metadata_size_hint` (or none), the first open's footer read would not carry the index; that variant was not recorded.
