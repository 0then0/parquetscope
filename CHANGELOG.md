# Changelog

User-visible changes are recorded here. Dates use YYYY-MM-DD. Entries under Unreleased describe changes on `main` that are not part of a new package release.

## Unreleased

### Added

- Reproducible NYC TLC case study: three layouts, nine captures, standalone viewers, runtime provenance, and checksums.
- Static interactive lab with layout exploration, linked selection, comparison, and downloadable evidence.
- Manually dispatched workflows for the real-data experiment and GitHub Pages deployment.
- Monthly grouped Dependabot version updates with a 14-day cooldown and one open PR per ecosystem; DuckDB upgrades remain manual.
- Public user and contributor guides, security and conduct policies, issue forms, and a pull request template.
- Shared editor settings, Git attributes, and project-specific ignore rules.

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

[0.1.0]: https://github.com/0then0/parquetscope/releases/tag/v0.1.0
