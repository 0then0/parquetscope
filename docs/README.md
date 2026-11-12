# ParquetScope documentation

ParquetScope connects a SQL query to the HTTP requests and Parquet structures it reads. Start with the user guide to capture a query or open an existing run.

## Using ParquetScope

- [Install and run](../README.md#install-and-run): released wheel or source checkout.
- [User guide](usage.md): local files, captures, comparisons, offline viewers, and troubleshooting.
- [Understanding measurements](measurements.md): metric definitions, completeness, and interpretation.
- [File-set manifest](manifest.md): explicit file sets, path rules, budgets, and privacy.
- [Artifact formats](artifacts.md): v1–v4 JSON fields, exact byte intervals, validation, and sharing.

## Examples and evidence

- [NYC TLC case study](real-data/README.md): three real layouts, nine captures, standalone viewers, and a reproducible experiment.
- [Partition-pruning lab](pruning-lab/README.md): identical bytes in flat and Hive layouts, with captures, viewers, and observed spread.
- [Prefetch lab](prefetch-lab/README.md): DuckDB 1.5.6 and 2.0.0 with `disable_parquet_prefetching` off and on over one file set, four configurations with captures and viewers.
- [HTTP profile lab](http-profile-lab/README.md): one pinned engine, prefetch on/off under unrestricted, delayed and shared-rate server models, with 18 captures and independent controls.
- [Synthetic demo](usage.md#run-the-synthetic-demo): a small, deterministic example you can run locally.
- [Synthetic demo report](demo-report.json): recorded metrics and conditions.
- [Testing and compatibility](validation.md): supported environments, test coverage, and published verification records.

## Development and maintenance

- [Contributing](../CONTRIBUTING.md): setup, project structure, development server, and checks.
- [Changelog](../CHANGELOG.md): released features and changes on `main`.
- [Security policy](../SECURITY.md) and [code of conduct](../CODE_OF_CONDUCT.md): reporting and participation.
- [Release guide](releasing.md): build and verify packages; publish the static lab.
- [GitHub Actions](https://github.com/0then0/parquetscope/actions): CI runs and manually triggered experiments.
- [Releases](https://github.com/0then0/parquetscope/releases): downloadable wheels, source archives, and checksums.

The measurement and artifact references describe the current contract. Experiment reports, captures, and archived runner sources record the versions and conditions of their own runs.
