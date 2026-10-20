# Maintainer guide

ParquetScope ships a Python wheel with a bundled browser UI and a source distribution with the frontend sources. GitHub Pages hosts the optional static NYC TLC lab separately from package releases.

## Prepare a release

1. Use the locked development environment and run the checks in [Contributing](../CONTRIBUTING.md#run-checks). Prepare httpfs before integration tests.
2. Run `pnpm --dir frontend build` and include the generated production assets under `src/parquetscope/static/`.
3. Run `uv build` and `uv run python scripts/check_dist.py`. The archive check verifies the bundled UI, offline template, source files, lockfiles, and exclusion of local caches.
4. Install the wheel into a fresh environment outside the checkout. Run `parquetscope setup`, then run `python /path/to/checkout/scripts/smoke_wheel.py /tmp/parquetscope-wheel-demo` with that environment's Python. This checks real captures, artifact validation, standalone export, the bundled frontend, and listener shutdown without Node.js.
5. Run the browser checks, including standalone viewers with networking disabled. Review the supported platforms and [validation record](validation.md), and document material changes to execution or artifact compatibility.
6. Create a versioned GitHub release and attach the verified wheel and source distribution. The current release is [v0.1.0](https://github.com/0then0/parquetscope/releases/tag/v0.1.0). Installation instructions use this release wheel.

The package version and archive names are currently declared in `pyproject.toml`, `src/parquetscope/__init__.py`, and the distribution-check scripts. Update version-specific release links and commands together when preparing another version. Preserve existing experiment provenance and pinned wheel references: they identify the runtime that produced those captures.

## Continuous integration

The [CI workflow](../.github/workflows/ci.yml) runs on pushes and pull requests. It tests Python 3.10 and 3.13 on Linux x86_64, checks the frontend and static lab, builds the distributions, and runs an isolated wheel smoke test. Build and browser evidence are uploaded as workflow artifacts. CI does not publish packages.

## Publish the static lab

The [Pages workflow](../.github/workflows/pages.yml) runs manually through **Actions → Real-data lab Pages → Run workflow**. Enable GitHub Pages with **GitHub Actions** as its source in repository settings first.

The build packages checked-in evidence without downloading the TLC dataset. Only the deployment job has `pages: write` and `id-token: write`; the build uses read-only repository access. Local preview commands are in the [case study](real-data/README.md#build-and-inspect-the-static-lab).

## Run a new real-data experiment

The [real-data workflow](../.github/workflows/real-data-acceptance.yml) is also manually dispatched. It downloads the pinned TLC source and published wheel, records nine captures, checks data equality and report integrity, builds the static lab, and uploads the evidence. It does not deploy or replace the checked-in experiment.

Review a new experiment's source identity, runtime versions, successful and excluded attempts, data equality, and limitations before publishing its results. Keep separate runs in separate directories. See [Reproduce the experiment](real-data/README.md#reproduce-the-experiment).
