# Contributing to ParquetScope

ParquetScope is a Python capture pipeline with a React viewer. Changes should preserve the connection between one query, independent HTTP observations, and the file structures shown in the UI.

## Set up the development environment

Use Python 3.10+, [uv](https://docs.astral.sh/uv/), Node.js 24, and pnpm 12.14.0. Python dependencies are locked in `uv.lock`; frontend dependencies are locked in `frontend/pnpm-lock.yaml`.

From the repository root:

```sh
uv sync --frozen
uv run parquetscope setup
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend exec playwright install chromium
```

`setup` installs the pinned DuckDB httpfs extension before integration tests or captures. Set `PARQUETSCOPE_HOME` to an absolute path to keep the extension cache in a custom directory. Do not install extensions inside measured runs.

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

- `src/parquetscope/metadata.py` reads the file identity and Parquet layout before capture.
- `range_server.py` serves the selected file over loopback HTTP and records independent stream observations.
- `capture.py` owns the listener and a fresh `worker.py` subprocess. The worker executes one SELECT, fully consumes its results, and collects DuckDB HTTP logs.
- `analysis.py` derives metrics; `models.py` validates the portable artifact.
- `app.py` and `cli.py` expose the local browser session and command-line workflows.
- `frontend/src/` contains the viewer, artifact validation, interval geometry, and static-lab adapter.
- `src/parquetscope/static/` contains the generated UI shipped in the wheel.
- `scripts/` contains schema, package, and real-data experiment tools.
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

Backend integration tests use the real DuckDB engine and a disposable loopback server. Prepare httpfs explicitly before running them. Browser tests start their own local servers on ports 8411 and 8418; stop other processes on those ports first.

Normal CI and PR tests use small generated fixtures or the checked-in captures. They do not download the NYC TLC source dataset. The [real-data experiment](docs/real-data/README.md#reproduce-the-experiment) is a separate, optional run.

## Generated files

After changing the frontend, run `pnpm --dir frontend build` and include the updated files under `src/parquetscope/static/`. This keeps source and wheel installations usable without Node.js.

After changing artifact models, run:

```sh
uv run python scripts/generate_schema.py
```

Review the generated schema and the Python/browser validation paths together. Byte offsets and potentially large counters must remain decimal strings, and displayed metrics must agree with the underlying observations. See the [artifact reference](docs/artifacts.md).

Build the static case study with `pnpm --dir frontend build:lab`. Its output is under `.runtime/pages/parquetscope/` and is ignored by Git. Keep source Parquet files, dependency caches, virtual environments, and test output out of commits.

Recorded captures, their reports, and archived runner sources are experiment evidence. Preserve their contents when editing documentation. If measurements change, create a new experiment directory and record its conditions rather than rewriting older traces. Refresh `manifest.json` and `SHA256SUMS.txt` when changing the surrounding case-study files; `scripts/real_data_lab.py` provides the `public_manifest` helper used by the experiment runner.

## Submit a change

Describe the user-visible behavior, why it changes, and the checks you ran. Include a regression test for a behavioral fix. For UI changes, check linked selections, keyboard access, light/dark themes, a narrow viewport, and offline export where relevant.

Use Conventional Commits in English, such as `fix: preserve comparison selection on refresh` or `docs: clarify capture metrics`.

Dependabot groups routine dependency updates monthly, waits 14 days after a release, and limits each ecosystem to one open version-update PR. Major upgrades and DuckDB engine upgrades require manual coordination. DuckDB is pinned across execution, setup, artifact validation, and tests; changing the dependency alone is insufficient.
