# Changelog

User-visible changes are recorded here. Dates use YYYY-MM-DD. Entries under Unreleased describe changes on `main` that are not part of a new package release.

## [0.3.0] - 2026-11-07

### Added

- Choice of DuckDB 1.5.6 or 2.0.0 for file-set captures (`capture --engine`, UI **Engine**). Each version runs in an isolated environment with its own httpfs, prepared by the explicit `setup --engine` step; captures never install anything.
- Typed `disable_parquet_prefetching` setting (`capture --prefetch default|on|off`, UI **Parquet prefetch**). The engine's read-back value is recorded; an unavailable or unconfirmed request fails the run, and an unreadable default makes the capture incomplete.
- Artifact format v3 records the DuckDB version, environment, prefetch evidence and how requests were correlated. v1 and v2 artifacts open unchanged; new file-set captures are v3. Run comparison shows engine versions, prefetch settings, GET/HEAD, confirmed ranges and server-written bytes.
- [Prefetch lab](docs/prefetch-lab/README.md): four configurations over a fixed file set shaped after duckdb/duckdb#25704, three interleaved cold-client repetitions each, checked against an independent pyarrow control.

### Changed

- The capture worker is a standalone script that needs only DuckDB, so it can run in any engine environment. It pins the httpfs HTTP options after loading the extension and enables HTTP logging before locking the configuration (DuckDB 2.0 requires it).
- DuckDB 2.0 redacts the request ID header in its HTTP log; those requests are paired with the server's records by content and the artifact says so.

## [0.2.0] - 2026-11-02

### Added

- File-set captures: an explicit, bounded [manifest](docs/manifest.md) (`capture --manifest`, `serve --manifest`) reads up to 256 identically structured local files, including Hive-style directories, through the same `input` view.
- Artifact format v2 with a per-file model, per-file and set metrics, file attribution cross-checked between DuckDB logs and the independent server, observed-access states (GET, HEAD only, no observed requests), read options, effective engine settings and per-request phases. v1 artifacts open unchanged.
- Files view with filtering, sorting and per-file inspection; Range lab and Structure work on the selected file.
- Run comparison matches files by content hash and shows read options, engine differences and the limits of comparability.
- Partition-pruning lab: identical Parquet bytes in a flat and a Hive layout, a selective query and a non-selective control, repeated cold-client captures, viewers and a report.
- Multi-file HTTP observer with exact per-file routes, per-request identity checks, and bounded concurrent connections.
- Reproducible NYC TLC case study: three layouts, nine captures, standalone viewers, runtime provenance, and checksums.
- Static interactive lab with layout exploration, linked selection, comparison, and downloadable evidence.
- Manually dispatched workflows for the real-data experiment and GitHub Pages deployment.
- Monthly grouped Dependabot version updates with a 14-day cooldown and one open PR per ecosystem; DuckDB upgrades remain manual.
- Public user and contributor guides, security and conduct policies, issue forms, and a pull request template.
- Shared editor settings, Git attributes, and project-specific ignore rules.

### Changed

- `RangeServer` is now the single-file wrapper of the new `FileSetServer`; v1 capture behavior and artifacts are unchanged. Browser tests use a second server on port 8412.
- Standalone viewers open v2 artifacts directly and keep offline import and comparison.

### Fixed

- Static-lab layout and comparison selections survive refresh and browser history; selecting the current layout returns to Range lab.
- Local analysis navigation preserves an unexecuted SQL draft.
- Report verification and static builds reject SQL that disagrees with the captured workload.

## [0.1.0] - 2026-10-19

Initial release.

- Capture one fully consumed DuckDB SELECT against one local Parquet file through a loopback HTTP endpoint.
- Reconcile native engine logs with independent server observations and map ranges to Parquet row groups, column chunks, and footer regions.
- Inspect requests in Range lab, zoom exact byte intervals, and compare runs and file layouts.
- Export validated JSON captures and standalone HTML viewers with offline imports and comparison.
- Run a deterministic two-layout demo with independent checks of data and query-result equality.
- Bound query execution with cancellation, deadlines, engine memory settings, and worker/listener cleanup.

[0.2.0]: https://github.com/0then0/parquetscope/releases/tag/v0.2.0
[0.1.0]: https://github.com/0then0/parquetscope/releases/tag/v0.1.0
