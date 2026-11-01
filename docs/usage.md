# User guide

ParquetScope runs a SELECT against a local Parquet file and shows the HTTP reads produced by DuckDB. You can also open saved captures without the original file or query execution.

For installation, see [Install and run](../README.md#install-and-run).

## Prepare query execution

```sh
parquetscope setup
```

This downloads the signed httpfs extension for the supported DuckDB version, 1.5.6. It is a separate setup step, not part of a measured query. By default the extension cache is under `~/.cache/parquetscope/extensions`; set `PARQUETSCOPE_HOME` before both setup and capture to relocate it.

Opening saved artifacts or standalone HTML does not require extension setup.

## Open a local file

```sh
parquetscope serve /path/to/events.parquet
```

Open **http://127.0.0.1:8400**. Expand **SQL workspace**, choose an input if several files are available, and enter one SELECT against the `input` view. Choose **Run query**, or press Ctrl/Cmd+Enter in the SQL editor.

The file is read-only. Each run reads one selected input. Several files may be offered at startup, but a single-file query does not span them; to query several files together, use a [manifest](#capture-a-file-set). The browser accepts only inputs made available by the server at startup; it does not open arbitrary server paths.

Default execution settings are four threads, a 512 MiB DuckDB engine memory limit, and a 30-second deadline. The UI's **Execution settings** control threads, memory, and deadline. Results are fully consumed even when the preview is limited to 30 rows and 64 KiB. The memory limit applies to DuckDB, not the whole Python process. Capture workers have disk spilling disabled.

Use **Cancel run** to stop a running query. A cancelled or timed-out run may retain partial HTTP evidence; its SQL status and capture completeness are shown separately.

## Capture a file set

A [manifest](manifest.md) lists up to 256 local files that share one schema. Their relative paths, including Hive directories such as `month=3/`, are the logical layout.

```sh
parquetscope serve --manifest hive.manifest.json --manifest flat.manifest.json
```

Each manifest appears in **Query input** as `label · N files`. Run a SELECT against `input` as usual. The result opens on **Files**:

- Every file is shown by its location and partition segments, with requests, server-written bytes, unique coverage and an observed-access state: GET observed, HEAD only, or no observed requests. Filter by state or path and sort by size, requests or bytes.
- **Inspect** opens the file in **Range lab** or **Structure**. The **Inspected file** selector switches files; byte offsets and intervals belong to that file only.
- "No observed requests" is a statement about the trace. When the capture is partial it does not prove the file was not accessed.
- **In binding** counts requests that started while the view was created, before the SELECT. DuckDB reads the first listed file to bind the schema, so a query that matches other files can still show requests for it.

From the command line:

```sh
parquetscope capture --manifest hive.manifest.json \
  --sql 'SELECT count(*) FROM input WHERE month = 3' --output set.parquetscope.json
```

This writes a [v2 artifact](artifacts.md#format-v2-file-sets). Relative paths and partition values are included, so review them before sharing. The output must not be the manifest or any listed file.

To compare two sets, run both or import their artifacts and open **Compare runs**. Files are paired by SHA-256, so identical bytes at different locations line up. The comparison also shows read parameters, versions, engine settings and a list of limits. It never calls two sets logically equivalent on the strength of similar schemas or equal ordered result digests, and it makes no estimate of speed or cost. The [partition-pruning lab](pruning-lab/README.md) walks through a recorded example.

## Read Range lab

The horizontal axis is file byte position. The vertical axis is request start time. Each GET mark shows the response interval described by its headers; mark height does not represent duration. HEAD requests have no response-body mark.

1. Select a request in the ledger to inspect its requested range, response interval, timing, server writes, and intersecting file regions.
2. Select a region in **Structure** to see related requests. **Related requests only** filters the ledger to that selection.
3. Use **Zoom to selected range** for narrow intervals and **Whole file** to reset the byte axis.
4. Use **Captured SQL & measurement sources** for exact totals, coverage intervals, and reconciliation details.

Displayed sizes use binary units such as KiB and MiB. Exact byte counts remain available in details, tooltips, and exported JSON. Intervals use an inclusive start and exclusive end: `[start, end)`.

A complete capture has reconciled DuckDB logs and independent server observations. It does not mean every server-written byte was received or decoded by the client. Read [Understanding measurements](measurements.md) before interpreting overlaps or comparing layouts.

## Compare runs

Create another run or use **Open artifact** to import a capture. Choose **Compare runs** and select the other run.

Comparison includes query text, versions, settings, input hashes (for file sets, per-file content matching), and measured totals. Different files have different byte coordinate systems: matching offsets do not necessarily identify the same data. A similar schema or matching result digest is not proof that the files contain identical rows.

The [NYC TLC case study](real-data/README.md) provides a comparison with separately verified data equality.

## Capture from the command line

```sh
parquetscope capture events.parquet \
  --sql 'SELECT sum(amount_cents) FROM input WHERE customer_id BETWEEN 1000 AND 1099' \
  --output run.parquetscope.json --deadline 60
```

The query must use columns present in your file. The command executes it once, consumes the result, saves the capture, and reports the SQL status and completeness. It exits unsuccessfully if the capture has errors or is incomplete. The output must be distinct from the source file, including when symlinks or hard links are involved.

## Open and share captures

```sh
parquetscope serve --artifact first.parquetscope.json \
  --artifact second.parquetscope.json
```

The UI can inspect imported artifacts and compare them without their source Parquet files. Captures are limited to 32 MiB each and sessions to 20 runs. See [Artifact format](artifacts.md) for validation rules and retained metadata.

To create a standalone viewer:

```sh
parquetscope viewer run.parquetscope.json --output run.viewer.html
```

Open the HTML directly in a browser. It includes the UI and one capture, supports local JSON imports and comparison, and works without a server or network. **Export JSON** and **Offline viewer** in the UI create the corresponding files.

Artifacts omit preview rows, source paths, and min/max statistics, but retain SQL, column names, HTTP headers, and file identity. Check those fields before sharing a capture.

## Run the synthetic demo

```sh
parquetscope demo --output demo-output
parquetscope serve --artifact demo-output/original.parquetscope.json \
  --artifact demo-output/sorted.parquetscope.json
```

The demo creates 262,144 rows with seed 20261019, four columns, Zstandard compression, and 64 row groups of 4,096 rows. It compares original order with order by `customer_id`, verifies all persisted values and the query result, then captures the same SELECT against both files.

```sql
SELECT count(*) AS matched_rows, sum(amount_cents) AS total_cents,
       sum(event_id % 97) AS checksum
FROM input
WHERE customer_id BETWEEN 1000 AND 1099
```

The recorded [demo report](demo-report.json) observed 192 GET + 2 HEAD and 3,048,953 server-written bytes in the original layout, versus 6 GET + 2 HEAD and 146,149 bytes in the sorted layout. Both returned `265`, `13418858`, and `12042`. Each execution writes its own actual conditions and metrics to `demo-output/report.json`; request timing and ordering can vary.

## Troubleshooting

### The httpfs extension is missing

Run `parquetscope setup` with internet access. If you set `PARQUETSCOPE_HOME`, use the same value for setup and query execution. Captures do not install extensions automatically.

### The SQL workspace has no input file

Restart the server with a Parquet path, or use `parquetscope serve --demo`. Opening artifacts alone provides an inspection session; imported JSON does not make the source file available for new queries.

### A query times out or exceeds memory

Inspect the SQL status, capture reason, and surviving observations. Increase the relevant execution limit if appropriate for the workload. A fresh worker does not clear OS caches, and lower read bytes do not by themselves guarantee faster execution.

### Offline viewer export is unavailable

Wait for the template to load and try again. If the server cannot provide it, use `parquetscope viewer capture.json --output viewer.html`. Export requires the bundled offline template; the UI reports an error rather than producing incomplete HTML.

### An artifact cannot be opened

Use an artifact produced by ParquetScope 0.1 or later (schema versions 1 and 2). Unknown schema versions, invalid intervals, incomplete reconciliation claims, or metrics that disagree with observations are rejected. Keep the original file unchanged when investigating a validation error.

### The default port is busy

Choose another port with `parquetscope serve --port 8401`. Development and browser-test server ports are documented in [Contributing](../CONTRIBUTING.md).
