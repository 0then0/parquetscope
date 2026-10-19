# v0.1 validation evidence

Validated locally on 2026-10-19. GitHub Actions repeats the checks on pushes and pull requests; remote results are available in the repository Actions tab. All results below came from executed commands, not planned checks.

## Platforms and versions

- macOS arm64: CPython 3.13.16, DuckDB 1.5.6, PyArrow 26.0.0, Node.js 24.22.0, pnpm 12.14.0.
- Linux arm64: CPython 3.13.16 and PyArrow 26.0.0 in `mcr.microsoft.com/playwright:v1.64.1-noble`, Node.js 24.21.0; separate `python:3.10-slim` checks used CPython 3.10.22 and PyArrow 25.0.1. DuckDB was 1.5.6 in both.
- Frontend: React 19.3.1, Vite 8.3.4, TypeScript 7.0.2, Biome 2.5.15, Vitest 5.0.5 and Playwright 1.64.1.
- Linux x86_64 is the configured CI target, not a locally executed architecture. Windows is unverified and not declared supported.

## Executed checks

- `pytest -q`: **72 passed** on the corrected source on macOS/Python 3.13, including CLI input preservation for identical/relative paths, symlinks and hard links, successful distinct output, and real worker memory limits at 64/512 MiB. Earlier Linux/Python 3.13 and Linux/Python 3.10 validation passed the preceding 65-test suite. All integration captures use the real engine and real loopback endpoint; extensions were explicitly prepared before testing. One non-failing Starlette warning recommends migrating its test transport from httpx to httpx2.
- `ruff check src tests scripts` and `ruff format --check src tests scripts`: passed on the final source. The generated artifact schema also passed `scripts/generate_schema.py --check`.
- `pnpm --dir frontend check`: Biome and TypeScript passed. `pnpm --dir frontend test`: **6 passed** on the final Range lab source on macOS, including precise zoom coordinates beyond Number.MAX_SAFE_INTEGER and clipping outside the byte domain. Production build passed. Earlier Linux validation passed the previous frontend’s four unit tests and build.
- `pnpm --dir frontend test:browser`: **7 passed** on the final Range lab source on macOS. Earlier Linux validation passed the preceding UI. Current Chromium checks cover delayed and unavailable offline templates without broken downloads, retry after template readiness, standalone re-export without a server, cancellation of demo with a collapsed SQL workspace, Range lab as the default after capture/demo, exact selected-range zoom and whole-file reset, HEAD zoom exclusion, a compact ledger and loaded mobile width, plus real demo/comparison, switching comparison sides, linked selections, JSON export/import, standalone viewing and import in a network-disabled browser context with zero HTTP(S) requests, cancellation, deadline, corrupt artifacts, themes, keyboard focus, mobile width and SQL containing closing script tags.
- `uv build`: wheel and sdist built. `scripts/check_dist.py` inspects the actual archives for embedded production assets, frontend sources/lockfiles and absence of dependency caches/runtime files. Hatch skips excluded directories before following pnpm cache symlinks.
- An isolated wheel installation, launched from `/tmp` rather than the checkout, ran the real demo, validated its artifact, created standalone HTML, served the bundled frontend over loopback and closed its listener. Executed on macOS and Linux without Node.js in the wheel environment.
- A second Linux wheel install and the same smoke workflow passed inside **`docker run --network none`** after dependencies and httpfs were prepared. This includes both real SELECT captures; only loopback is available in that container. The viewer HTML was separately tested with browser networking disabled.

Backend checks include HTTP inclusive-to-half-open conversion, suffix/open-ended requests, 416, HEAD body exclusion, explicit multi-range policy, interrupted streams, file changes, dictionary offsets, unknown regions, coverage multiplicity, large integers, artifact integrity, statement/external-access restrictions, fully consumed results, backend shutdown, deadline/cancellation, reaped worker processes and closed listeners. The Python 3.10 timestamp regressions cover timezone forms and zero to six fractional digits.

The reproducible demo’s observed results and versions are preserved in [demo-report.json](demo-report.json). Screenshots come from the real browser workflow, not substituted traces. The screenshot context uses a 1920 × 1280 CSS viewport and deviceScaleFactor 2; full-page PNGs are 3840 pixels wide. The whole-file view selects the real RG 0 · customer_id request among 192 GET observations; separate screenshots show byte-axis zoom and the dark theme. The obsolete design-alternative gallery and its assets were removed. Range lab is the sole main workspace; Structure and Compare runs remain supporting views. Timing and request interleaving are observations, not frozen golden sequences. No performance benchmark or claim about cold OS/disk caches is inferred.

## Local evidence and reproduction

During this implementation, full console outputs were retained under ignored `.runtime/`: `linux310-validation.txt`, `linux-web-validation.txt`, `linux-offline-validation.txt` `linux313-final.txt`, `linux-browser-final.txt` and `wheel-validation.txt`. Browser traces/screenshots/downloads are in ignored `frontend/test-results/`. They are local verification output, not package contents. README lists the normal reproducible commands; the CI workflow repeats them on Linux and uploads only build/test evidence.

Package publication, GitHub releases and deployments are outside these checks. Repository pushes can trigger the configured CI workflow; inspect its result separately from local evidence.
