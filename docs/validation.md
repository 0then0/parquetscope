# Validation and supported environments

ParquetScope validates both query execution and portable inspection. Tests use real DuckDB captures against disposable loopback servers, generated Parquet fixtures, and checked-in real-data artifacts.

## Current validation record

The local checks recorded on **2026-10-20** passed on macOS arm64 with Python 3.13.16, DuckDB 1.5.6, PyArrow 26.0.0, Node.js 24.22.0, and pnpm 12.14.0:

- 89 backend tests, Ruff lint and formatting, and the generated artifact schema check.
- 8 frontend unit tests, Biome, TypeScript, and the production/static-lab builds.
- 8 application browser scenarios and 6 static-lab browser scenarios in Chromium.
- Wheel and source-distribution checks, plus an isolated wheel smoke test outside the checkout.
- Offline validation of all nine NYC TLC captures and their report.

The machine-readable [real-data validation record](real-data/validation.json) contains commands and browser coverage. The [CI workflow](../.github/workflows/ci.yml) runs the current suite on Python 3.10 and 3.13 on Linux x86_64; inspect [Actions](https://github.com/0then0/parquetscope/actions/workflows/ci.yml) for results associated with a particular commit.

Earlier v0.1 checks also exercised Linux arm64 on Python 3.10 and 3.13, including an installed-wheel smoke run with external networking disabled after dependency and extension preparation. Those runs predate the current suite and do not establish results for later changes. Windows has not been validated.

## What the checks cover

Backend checks cover HTTP range normalization, HEAD body exclusion, unsatisfiable ranges, multi-range policy, interrupted streams, file changes, dictionary offsets, unknown regions, large integers, artifact integrity, SQL restrictions, full result consumption, memory limits, cancellation, deadlines, process cleanup, and listener shutdown.

Frontend checks cover exact byte geometry, linked selections, comparison, keyboard navigation, mobile width, light/dark themes, import/export, failed or delayed offline-template loading, and standalone viewers with networking disabled. Static-lab checks cover project-subpath loading, hash navigation and history, all capture downloads, comparison, and zero execution API calls.

Distribution checks inspect the actual wheel and source archive. The wheel smoke test imports the installed package outside the source checkout, creates real captures, validates artifacts, exports standalone HTML, and serves the bundled UI.

## Reproduce checks

Use the commands in [Contributing](../CONTRIBUTING.md#run-checks). Browser traces, screenshots, and downloads are generated under `frontend/test-results/`; they are local test output and excluded from distributions.

Measured synthetic-demo results are preserved in [demo-report.json](demo-report.json). The [NYC TLC case study](real-data/README.md) has its own runtime provenance, captures, runner snapshot, and checksums. Query duration and request ordering are observations, not fixed test expectations or controlled performance benchmarks.
