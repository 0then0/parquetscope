# Preparing a v0.1 distribution

No command here publishes a package or release.

1. Use the pinned Python and pnpm environments in README. Run the schema check, format/lint, backend tests, frontend tests and browser tests. Prepare httpfs explicitly before integration tests.
2. Run `pnpm --dir frontend install --frozen-lockfile` and `pnpm --dir frontend build`. Keep production assets under `src/parquetscope/static/` synchronized with sources. No Node.js is needed for installation from the resulting wheel or sdist.
3. Run `uv build` and `uv run python scripts/check_dist.py`. Inspect `dist/parquetscope-0.1.0-py3-none-any.whl` for `parquetscope/static/index.html`, hashed JS/CSS and `parquetscope/static/offline.html`. The sdist includes frontend source, lockfile, docs and tests.
4. Create a fresh Python environment and install the wheel with `uv pip install --python /path/to/env/bin/python dist/parquetscope-0.1.0-py3-none-any.whl`. Run its `parquetscope setup` explicitly, then `parquetscope demo --output /tmp/parquetscope-wheel-demo` and `parquetscope serve --artifact /tmp/parquetscope-wheel-demo/sorted.parquetscope.json`. Do not depend on the editable checkout or Node.
5. Create an offline HTML using that installed CLI and open it with network disabled. Test import, linked selections and comparison without either original Parquet file.
6. Record the exact supported versions/platforms and measured demo report. Do not declare Windows support without running its capture/resource/browser checks. Review SQL and metadata before sharing artifacts.

The CI workflow uploads distributables and browser evidence as workflow artifacts only. It contains no package publishing, deployment or release credentials/actions. A maintainer can review the local files and carry out a separate authorized release.
