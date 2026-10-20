<p align="center"><img src="docs/icon.svg" alt="ParquetScope" width="112" height="112"></p>

# ParquetScope

See how DuckDB reads a Parquet file over HTTP: requests, byte ranges, column chunks, and the effect of a different file layout.

[![Version](https://img.shields.io/badge/version-0.1.0-16775c)](https://github.com/0then0/parquetscope/releases/tag/v0.1.0)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?logo=python&logoColor=white)](pyproject.toml)
[![DuckDB](https://img.shields.io/badge/DuckDB-1.5.6-fff000?logo=duckdb&logoColor=black)](pyproject.toml)
[![CI](https://github.com/0then0/parquetscope/actions/workflows/ci.yml/badge.svg)](https://github.com/0then0/parquetscope/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

ParquetScope captures one SELECT against one local Parquet file, serves that file through a controlled HTTP endpoint, and maps the observed reads to its physical structure. Compare queries or file layouts, inspect linked requests and chunks, and share a capture that others can open without the source data.

![Range lab showing real HTTP requests and the selected customer_id chunk](docs/range-inspector.png)

## Install and run

Install the [v0.1.0 release wheel](https://github.com/0then0/parquetscope/releases/tag/v0.1.0) with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install --python 3.13 \
  https://github.com/0then0/parquetscope/releases/download/v0.1.0/parquetscope-0.1.0-py3-none-any.whl
parquetscope setup
parquetscope serve --demo
```

Open **http://127.0.0.1:8400** and choose **Run layout demo**. The demo creates two equivalent Parquet layouts and measures the same query against both.

Python 3.10+ is required. The wheel includes the browser UI; Node.js is needed only for frontend development. `setup` downloads the signed httpfs extension for DuckDB 1.5.6 once. Subsequent local captures and offline viewing need no external internet, account, or cloud credentials.

To run from source instead:

```sh
uv sync --frozen
uv run parquetscope setup
uv run parquetscope serve --demo
```

## Inspect your data

```sh
parquetscope serve /path/to/events.parquet
```

Use the `input` view in the SQL workspace. For example, on a file containing `customer_id` and `amount_cents`:

```sql
SELECT customer_id, sum(amount_cents)
FROM input
WHERE customer_id BETWEEN 1000 AND 1099
GROUP BY customer_id
```

In **Range lab**, select a GET to see its response interval, server writes, and intersecting column chunks. Select a chunk under **Structure** to find related requests. **Zoom to selected range** expands the selected interval; **Whole file** restores context. **Compare runs** shows each run's query, input identity, settings, and measured totals.

The source file is read-only. Each capture uses a fresh worker and executes one fully consumed SELECT. ParquetScope is a local diagnostic tool for trusted files and SQL, rather than a sandbox for hostile workloads.

## Save and share

```sh
parquetscope capture events.parquet \
  --sql 'SELECT sum(amount_cents) FROM input WHERE customer_id BETWEEN 1000 AND 1099' \
  --output run.parquetscope.json

parquetscope serve --artifact run.parquetscope.json
parquetscope viewer run.parquetscope.json --output run.viewer.html
```

A capture contains the query, file identity, layout, HTTP observations, and derived metrics. It omits result preview rows and the original source path. SQL, column names, and headers remain visible, so check the contents before sharing.

The standalone HTML viewer opens directly as a file, without Python, a server, or network access. It can import additional captures and compare them offline. The UI's **Export JSON** and **Offline viewer** buttons provide the same exports.

## Explore a real dataset

The [NYC TLC case study](docs/real-data/README.md) measures the same DECIMAL aggregate over 3,475,226 Yellow Taxi records in three layouts:

- **As delivered:** 59,158,238-byte file; 14 GET + 2 HEAD; 11,387,877 server-written body bytes.
- **Repacked in original order:** 53,370,794-byte file; 118 GET + 2 HEAD; 11,446,545 body bytes.
- **Sorted by pickup location:** 59,417,539-byte file; 10 GET + 2 HEAD; 795,241 body bytes.

All three repetitions of each layout produced those totals and identical query results. A smaller file needed more requests, while the sorted file needed fewer body bytes. These are observations for one workload, not guaranteed request sequences or proportional speedups.

The case study includes nine captures, three standalone viewers, a report, checksums, and reproduction instructions. To build its static interactive lab from the repository:

```sh
# Install the development environment first; see CONTRIBUTING.md.
pnpm --dir frontend build:lab
.venv/bin/python -m http.server 8418 --bind 127.0.0.1 --directory .runtime/pages
```

Open **http://127.0.0.1:8418/parquetscope/**. The lab browses recorded captures and runs no SQL. It loads neither the source dataset nor an execution backend.

## Read the measurements correctly

**Server-written bytes** count successful writes to the response stream. They do not establish how many bytes DuckDB received, decoded, or used. **Unique coverage** is the union of confirmed response intervals; **repeated coverage** is their summed length minus that union. Overlap is not automatically waste.

SQL success and capture completeness are separate. Incomplete traces retain available observations and mark totals as an observed subset. A fresh worker resets the client, but OS and disk caches remain uncontrolled. Loopback measurements do not measure S3 latency or cloud costs.

See [Understanding measurements](docs/measurements.md) for definitions and limits.

## Documentation

- [User guide](docs/usage.md): CLI workflows, the viewer, settings, and troubleshooting.
- [Documentation index](docs/README.md): user and developer references.
- [Artifact format](docs/artifacts.md): portable captures and validation rules.
- [NYC TLC case study](docs/real-data/README.md): results, downloads, and reproduction.
- [Contributing](CONTRIBUTING.md): development environment, architecture, and checks.
- [Testing and compatibility](docs/validation.md) and [release guide](docs/releasing.md).

ParquetScope currently supports DuckDB 1.5.6 and one file per run. It does not capture production S3 traffic, run multiple query engines, or predict query costs. The project is licensed under [Apache-2.0](LICENSE).
