"""Verify the built distribution boundary, including frontend source in the sdist."""

import tarfile
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
with zipfile.ZipFile(root / "dist/parquetscope-0.2.0-py3-none-any.whl") as wheel:
    names = wheel.namelist()
    assert "parquetscope/static/index.html" in names
    assert "parquetscope/static/offline.html" in names
    assert any(n.startswith("parquetscope/static/assets/") and n.endswith(".js") for n in names)
    assert any(n.startswith("parquetscope/static/assets/") and n.endswith(".css") for n in names)
with tarfile.open(root / "dist/parquetscope-0.2.0.tar.gz") as source:
    names = source.getnames()
    for required in (
        "frontend/pnpm-lock.yaml",
        "frontend/package.json",
        "frontend/src/App.tsx",
        "frontend/tests/browser/workflow.spec.ts",
        "scripts/smoke_wheel.py",
        "docs/icon.svg",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "SECURITY.md",
        "CHANGELOG.md",
        ".editorconfig",
        ".gitattributes",
        ".gitignore",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/ISSUE_TEMPLATE/documentation.yml",
        ".github/ISSUE_TEMPLATE/private_contact.yml",
        ".github/PULL_REQUEST_TEMPLATE.md",
        "docs/README.md",
        "docs/usage.md",
        "docs/manifest.md",
        "docs/pruning-lab/README.md",
        "scripts/pruning_lab.py",
        "scripts/browser_fixture.py",
        "docs/releasing.md",
        ".github/workflows/ci.yml",
        "uv.lock",
    ):
        assert f"parquetscope-0.2.0/{required}" in names, f"Missing sdist file: {required}"
    assert not any(
        part in name
        for name in names
        for part in ("/node_modules/", "/.runtime/", "/.pnpm-store/", "/test-results/", "/.venv/")
    ), "Local runtime or cache leaked into sdist"
print("Wheel assets and sdist sources/lockfiles verified; local caches excluded")
