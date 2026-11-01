"""Serve the demo plus a small flat/Hive manifest pair for the browser tests (port 8412)."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pruning_lab import generate, write_manifests  # noqa: E402

from parquetscope.cli import main  # noqa: E402

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="parquetscope-fixture-") as temp:
        root = Path(temp)
        write_manifests(root, generate(root, months=4, files_per_month=2, rows_per_file=2000))
        sys.argv = [
            "parquetscope",
            "serve",
            "--demo",
            "--port",
            "8412",
            "--manifest",
            str(root / "flat" / "manifest.json"),
            "--manifest",
            str(root / "hive" / "manifest.json"),
        ]
        main()
