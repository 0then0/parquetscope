"""Isolated DuckDB engine environments for captures.

Each supported DuckDB version lives in its own virtual environment with its own `httpfs`
extension directory, so the project's `.venv` is never touched. Preparing an environment
(`parquetscope setup --engine X`) downloads from the network; a capture only resolves an
environment that is already prepared and never installs anything.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SUPPORTED_ENGINES = ("1.5.6", "2.0.0")
# DuckDB 2.0.0 publishes wheels for CPython 3.11+ only.
MIN_PYTHON = {"1.5.6": (3, 10), "2.0.0": (3, 11)}
MANIFEST = "engine.json"

_INSTALL_HTTPFS = """
import json, sys, duckdb
with duckdb.connect(config={"extension_directory": sys.argv[1]}) as c:
    c.execute("INSTALL httpfs")
    c.execute("LOAD httpfs")
    row = c.execute(
        "SELECT extension_version FROM duckdb_extensions() WHERE extension_name = 'httpfs'"
    ).fetchone()
print(json.dumps({"duckdb_version": duckdb.__version__, "httpfs_version": row[0] if row else None}))
"""


@dataclass(frozen=True)
class EngineEnv:
    """Where a worker runs. `version` is None for the project's own interpreter."""

    version: str | None
    python: Path
    extension_dir: Path
    isolated: bool


def home() -> Path:
    return Path(os.environ.get("PARQUETSCOPE_HOME", Path.home() / ".cache" / "parquetscope"))


def extension_directory() -> Path:
    return home() / "extensions"


def engine_root(version: str) -> Path:
    return home() / "engines" / f"duckdb-{version}"


def _check_version(version: str) -> None:
    if version not in SUPPORTED_ENGINES:
        raise ValueError(f"Unsupported DuckDB version {version!r}; use one of {SUPPORTED_ENGINES}")


def _venv_python(root: Path) -> Path:
    scripts = "Scripts" if sys.platform == "win32" else "bin"
    return root / "venv" / scripts / ("python.exe" if sys.platform == "win32" else "python")


def project_env() -> EngineEnv:
    return EngineEnv(None, Path(sys.executable), extension_directory(), isolated=False)


def prepare_engine(version: str) -> dict:
    """Create the venv, install the exact DuckDB wheel and its httpfs. Needs the network."""
    _check_version(version)
    if sys.version_info < MIN_PYTHON[version]:
        needed = ".".join(map(str, MIN_PYTHON[version]))
        raise RuntimeError(f"DuckDB {version} needs Python {needed} or newer to host its engine")
    root = engine_root(version)
    python = _venv_python(root)
    try:
        if not python.exists():
            subprocess.run([sys.executable, "-m", "venv", str(root / "venv")], check=True)
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--quiet",
                "--only-binary=:all:",
                f"duckdb=={version}",
            ],
            check=True,
        )
        extensions = root / "extensions"
        extensions.mkdir(parents=True, exist_ok=True)
        done = subprocess.run(
            [str(python), "-I", "-c", _INSTALL_HTTPFS, str(extensions)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip()[-500:]
        raise RuntimeError(f"Preparing DuckDB {version} failed: {error}. {detail}") from error
    facts = json.loads(done.stdout.strip().splitlines()[-1])
    if facts["duckdb_version"] != version or not facts["httpfs_version"]:
        raise RuntimeError(f"Prepared environment reports {facts}, expected DuckDB {version}")
    manifest = {
        **facts,
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "prepared_at": datetime.now(timezone.utc).isoformat(),
    }
    (root / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def read_manifest(version: str) -> dict | None:
    try:
        manifest = json.loads((engine_root(version) / MANIFEST).read_text())
    except (OSError, ValueError):
        return None
    return manifest if manifest.get("duckdb_version") == version else None


def resolve_engine(version: str) -> EngineEnv:
    """The prepared environment for `version`, or an error naming the explicit setup step."""
    _check_version(version)
    root = engine_root(version)
    python = _venv_python(root)
    if read_manifest(version) is None or not python.exists() or not (root / "extensions").is_dir():
        raise RuntimeError(
            f"DuckDB {version} is not prepared. Run `parquetscope setup --engine {version}` "
            "(requires internet) before capturing."
        )
    return EngineEnv(version, python, root / "extensions", isolated=True)


def engine_status() -> list[dict]:
    return [{"version": v, "prepared": read_manifest(v) is not None} for v in SUPPORTED_ENGINES]
