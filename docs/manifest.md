# File-set manifest

A manifest names a bounded, explicit set of local Parquet files that one `input` view reads together. ParquetScope never walks directories, expands globs or discovers files while a query runs. The set is resolved before any worker or HTTP listener exists.

```json
{
  "manifest_version": "1",
  "label": "events-hive",
  "read": { "hive_partitioning": true },
  "files": [
    { "path": "month=1/data-0.parquet" },
    { "path": "month=1/data-1.parquet", "size": "216230", "sha256": "…64 hex digits…" },
    { "path": "month=2/data-0.parquet" }
  ]
}
```

Use it with:

```sh
parquetscope capture --manifest set.json --sql 'SELECT count(*) FROM input WHERE month = 2' --output run.parquetscope.json
parquetscope serve --manifest flat.json --manifest hive.json
```

`serve` lists each manifest as one input (`label · N files`) beside any single files. Everything else works as for a single file: one SELECT against `input`, a fresh worker, cancellation and a deadline.

## Fields

- `manifest_version`: `"1"`. Unknown versions and unknown fields are rejected, so a typo cannot silently change what is read.
- `label`: optional name shown in the UI and used for downloads, at most 128 characters. Defaults to the manifest file name without extension.
- `read.hive_partitioning`: `true` or `false` (default `false`). Passed explicitly to `read_parquet`, so results never depend on DuckDB's automatic detection. `union_by_name` is always off and cannot be enabled.
- `files`: 1 to 256 entries. `path` is required. `size` (decimal string) and `sha256` are optional pins: if present, the file must match them exactly when the set is prepared, which detects a file changed after the manifest was written. The captured artifact always records the resolved size and SHA-256 of every file, pinned or not.

## Path rules

`path` is relative to the directory containing the manifest and is also the file's **logical location**: the layout the artifact records and the URL route the server serves. There is no separate rewrite.

- Relative POSIX paths only: no leading `/`, no backslashes, no empty, `.` or `..` segments, at most 32 segments, 255 bytes per segment and 1,024 bytes in total.
- Unicode is normalized to NFC. Control characters, bidirectional overrides and other invisible formatting characters are rejected.
- After symlink resolution a file must be a regular file inside the manifest directory. A symlink that leaves it is rejected.
- Each file may appear once. Duplicate paths, symlinks or hard links to one file, and case-only aliases that resolve to the same file are rejected.
- Different directories may contain the same basename (`month=1/data.parquet`, `month=2/data.parquet`). Files are identified by `file_id` (`f001`, `f002`, … in manifest order), never by basename.
- With `hive_partitioning: true`, directory segments of the form `key=value` must use keys made of ASCII letters, digits and underscores. DuckDB decodes percent-encoded values but not keys, so other keys would silently produce different column names. Values may contain any permitted characters, including spaces and Unicode. With `hive_partitioning: false` any directory names are allowed.

## Supported scope

All files must have an identical schema: the same column names, order and types. Preparation reads each footer and rejects a set that differs from its first file. There is no schema evolution, no `union_by_name` and no automatic merging. Files must be unencrypted Parquet with no external column chunks.

Partition segments are shown as written in the path. They are path syntax, not a statement about the columns DuckDB infers. The artifact separately records the columns and types of the bound view.

## Budgets

- Manifest file: 1 MiB.
- Files per set: 256.
- Total bytes of a set: 16 GiB.
- Layout structures across the set: 100,000.
- HTTP observations per run: 100,000.
- Artifact: 32 MiB.
- Concurrent connections to the capture server: 64. Excess connections are closed and reported.
- Open files: at most one per active connection. Files are opened per request, not held.
- Runs per session: 20.

Exceeding a budget during preparation is an error. Exceeding the observation or connection budget during a run marks the capture incomplete with a reason. Preparation reads every file in full to compute SHA-256 and reads its footer. It happens before measurement and does not appear in the HTTP trace.

## Serving and integrity

Every file has one route: `/{random token}/{percent-encoded logical path}`. The server answers only those routes. Listings, other paths, `..`, encoded slashes and query strings return 404 and are counted; any counted request makes the capture incomplete. The worker receives only the generated URLs: `allowed_paths` lists them exactly, external access is disabled and the URL list is bound as a parameter, so no manifest path is interpolated into SQL.

Before every request and every 64 KiB streamed block the server checks that the file still has the identity recorded during preparation (device, inode, size, nanosecond mtime and ctime). A change, replacement or deletion of any file invalidates the capture and names the file.

## Privacy

Relative paths, directory names and partition values are exported in the artifact and in standalone viewers. They can reveal customers, regions, dates or project names. Absolute source paths are never exported. Review a capture before sharing it, as for SQL and column names.
