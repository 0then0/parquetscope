# Contributing to ParquetScope

ParquetScope is a Python capture pipeline with a React viewer. Changes should preserve the connection between one query, independent HTTP observations, and the file structures shown in the UI.

## Before contributing

Follow the [code of conduct](CODE_OF_CONDUCT.md). Use [issue forms](https://github.com/0then0/parquetscope/issues/new/choose) for reproducible bugs, feature requests, or documentation problems. Security reports follow [SECURITY.md](SECURITY.md); do not include vulnerability details in a public issue.

The repository's `.editorconfig` matches Ruff and Biome. Formatters remain authoritative. `.gitattributes` keeps source line endings consistent and preserves checksummed experiment evidence exactly. `.gitignore` excludes local datasets, captures, environments, secrets, and generated test output; the published evidence under `docs/real-data/` is intentional.

## Set up the development environment

Use Python 3.10+, [uv](https://docs.astral.sh/uv/), Node.js 24, and pnpm 12.14.0. Python dependencies are locked in `uv.lock`; frontend dependencies are locked in `frontend/pnpm-lock.yaml`.

From the repository root:

```sh
uv sync --frozen
uv run parquetscope setup
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend exec playwright install chromium
```

`setup` installs the pinned DuckDB httpfs extension before integration tests or captures. Set `PARQUETSCOPE_HOME` to an absolute path to keep the extension cache in a custom directory. Do not install extensions inside measured runs. Engine tests and the prefetch lab also need `uv run parquetscope setup --engine 1.5.6` and `--engine 2.0.0`; tests that need an unprepared engine skip. DuckDB 2.0.0 needs Python 3.11 or newer for its environment.

## Run the application

```sh
uv run parquetscope serve --demo
```

The application serves the bundled production UI on port 8400. For frontend development, keep that server running and start Vite in a second terminal:

```sh
pnpm --dir frontend dev
```

Open the URL printed by Vite. Its development proxy forwards API calls to the Python server on port 8400.

## Project structure

- `metadata.py` reads the file identity and Parquet layout before capture. `manifest.py` validates a file-set manifest and resolves it (hashes, footers, schema compatibility) before any listener or worker exists.
- `range_server.py` serves the prepared files over loopback HTTP, one exact route per file, and records independent stream observations keyed by file.
- `capture.py` owns the listener and a fresh `worker.py` subprocess. The worker executes one SELECT, fully consumes its results, and collects DuckDB HTTP logs. `capture` produces v1 artifacts; `capture_set` produces v2.
- `analysis.py` derives metrics (per file and per set); `models.py` (v1) and `models_v2.py` (v2) validate the portable artifacts.
- `app.py` and `cli.py` expose the local browser session and command-line workflows.
- `frontend/src/` contains the viewer, artifact validation for both versions (`model.ts` normalizes them to one file-list model), the Files overview, comparison logic, interval geometry, and static-lab adapter.
- `src/parquetscope/static/` contains the generated UI shipped in the wheel.
- `scripts/` contains schema, package, partition-pruning lab, browser fixture, and real-data experiment tools.
- `tests/` and `frontend/tests/` contain backend and browser tests.

The application has no database or cloud service dependency. Browser sessions use bounded in-memory state.

## Run checks

Run the relevant checks before opening a pull request. The complete local sequence is:

```sh
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pytest -q
uv run python scripts/generate_schema.py --check
pnpm --dir frontend check
pnpm --dir frontend test
pnpm --dir frontend build
pnpm --dir frontend test:browser
pnpm --dir frontend build:lab
pnpm --dir frontend test:lab
uv build
uv run python scripts/check_dist.py
```

Backend integration tests use the real DuckDB engine and a disposable loopback server. Prepare httpfs explicitly before running them. Browser tests start their own local servers on ports 8411 (single files), 8412 (file sets) and 8418 (static lab); stop other processes on those ports first. They need the httpfs extension: run `parquetscope setup` with the same `PARQUETSCOPE_HOME` you pass to the tests (default `.runtime`). Without it, worker runs fail and the tests see empty captures.

Normal CI and PR tests use small generated fixtures or the checked-in captures. They do not download the NYC TLC source dataset. The [real-data experiment](docs/real-data/README.md#reproduce-the-experiment) is a separate, optional run.

## Generated files

After changing the frontend, run `pnpm --dir frontend build` and include the updated files under `src/parquetscope/static/`. This keeps source and wheel installations usable without Node.js.

After changing artifact models, run:

```sh
uv run python scripts/generate_schema.py
```

This writes one schema per artifact version (`artifact-schema.json`, `artifact-schema-v2.json`). The v1 schema must not change. Review the generated schema and the Python/browser validation paths together. Byte offsets and potentially large counters must remain decimal strings, and displayed metrics must agree with the underlying observations. See the [artifact reference](docs/artifacts.md).

The partition-pruning lab evidence under `docs/pruning-lab/` (manifests, captures, standalone viewers, report, `SHA256SUMS.txt`) is produced by `uv run python scripts/pruning_lab.py`. Its Parquet files are regenerated each run and ignored by Git. Regenerate the evidence after changing capture behavior or the frontend, because the viewers embed the built UI; `tests/test_pruning_lab.py` checks the recorded checksums.

Build the static case study with `pnpm --dir frontend build:lab`. Its output is under `.runtime/pages/parquetscope/` and is ignored by Git. Keep source Parquet files, dependency caches, virtual environments, and test output out of commits.

Recorded captures, their reports, and archived runner sources are experiment evidence. Preserve their contents when editing documentation. If measurements change, create a new experiment directory and record its conditions rather than rewriting older traces. Refresh `manifest.json` and `SHA256SUMS.txt` when changing the surrounding case-study files; `scripts/real_data_lab.py` provides the `public_manifest` helper used by the experiment runner.

## Submit a change

Describe the user-visible behavior, why it changes, and the checks you ran. Include a regression test for a behavioral fix. For UI changes, check linked selections, keyboard access, light/dark themes, a narrow viewport, and offline export where relevant.

Use the pull request template to describe validation and compatibility. Update [CHANGELOG.md](CHANGELOG.md) under **Unreleased** for user-visible changes.

Use Conventional Commits in English, such as `fix: preserve comparison selection on refresh` or `docs: clarify capture metrics`.

Dependabot groups routine dependency updates monthly, waits 14 days after a release, and limits each ecosystem to one open version-update PR. Major upgrades and DuckDB engine upgrades require manual coordination. DuckDB is pinned across execution, setup, artifact validation, and tests; changing the dependency alone is insufficient.
