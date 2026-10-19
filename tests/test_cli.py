import os
import subprocess
import sys

import pytest

from parquetscope.models import load_artifact_file


@pytest.mark.parametrize("alias", ["same", "relative", "symlink", "hardlink"])
def test_capture_refuses_output_alias_of_input(parquet, tmp_path, alias):
    original = parquet.read_bytes()
    output = parquet
    if alias == "relative":
        output_argument = f"./{parquet.name}"
    else:
        if alias == "symlink":
            output = tmp_path / "output.json"
            output.symlink_to(parquet)
        elif alias == "hardlink":
            output = tmp_path / "output.json"
            output.hardlink_to(parquet)
        output_argument = str(output)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "parquetscope.cli",
            "capture",
            str(parquet),
            "--sql",
            "SELECT sum(id) FROM input",
            "--output",
            output_argument,
        ],
        cwd=tmp_path,
        env={**os.environ, "PARQUETSCOPE_HOME": str(tmp_path / "no-extensions")},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "Output must not refer to the input Parquet file" in result.stderr
    assert parquet.read_bytes() == original
    assert output.read_bytes() == original


def test_capture_writes_distinct_artifact_and_preserves_input(parquet, tmp_path, httpfs):
    original = parquet.read_bytes()
    output = tmp_path / "capture.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "parquetscope.cli",
            "capture",
            str(parquet),
            "--sql",
            "SELECT sum(id) FROM input",
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    run = load_artifact_file(output)
    assert run.sql_status == "succeeded" and run.capture.complete
    assert parquet.read_bytes() == original
