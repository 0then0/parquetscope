"""Run with an isolated wheel environment's Python, from outside the checkout."""

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

import parquetscope
from parquetscope.models import load_artifact
from parquetscope.offline import STATIC, viewer_html

assert "site-packages" in parquetscope.__file__, parquetscope.__file__
assert (STATIC / "index.html").is_file()
assert (STATIC / "offline.html").is_file()
assert list((STATIC / "assets").glob("*.js"))
root = Path(sys.argv[1])
subprocess.run(
    [sys.executable, "-m", "parquetscope.cli", "demo", "--output", str(root)], check=True
)
# A real two-file Hive set through the installed wheel: v2 capture, validation and viewer.
for month in (1, 2):
    part = root / "set" / f"month={month}" / "data.parquet"
    part.parent.mkdir(parents=True)
    pq.write_table(pa.table({"id": list(range(100)), "month": [month] * 100}), part)
(root / "set" / "manifest.json").write_text(
    json.dumps(
        {
            "manifest_version": "1",
            "read": {"hive_partitioning": True},
            "files": [{"path": f"month={m}/data.parquet"} for m in (1, 2)],
        }
    )
)
subprocess.run(
    [
        sys.executable,
        "-m",
        "parquetscope.cli",
        "capture",
        "--manifest",
        str(root / "set" / "manifest.json"),
        "--sql",
        "SELECT count(*) FROM input WHERE month = 2",
        "--output",
        str(root / "set.parquetscope.json"),
    ],
    check=True,
)
set_run = load_artifact((root / "set.parquetscope.json").read_bytes())
assert set_run.schema_version == "2" and set_run.capture.complete
assert set_run.metrics.files_without_observed_access >= 0 and len(set_run.file_set.files) == 2
assert "file_set" in viewer_html(set_run)
run = load_artifact((root / "sorted.parquetscope.json").read_bytes())
html = viewer_html(run)
assert 'type="application/json">null</script>' not in html
assert '<script type="module">' in html and "<style>" in html
(root / "viewer.html").write_text(html)
process = subprocess.Popen(
    [
        sys.executable,
        "-m",
        "parquetscope.cli",
        "serve",
        "--artifact",
        str(root / "sorted.parquetscope.json"),
        "--port",
        "8417",
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    for _ in range(100):
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:8417/api/session", timeout=0.5
            ) as response:
                session = json.load(response)
            break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("Installed app did not start")
    assert session["runs"][0]["id"] == run.id
    with urllib.request.urlopen("http://127.0.0.1:8417/") as response:
        assert b"ParquetScope" in response.read()
    with urllib.request.urlopen("http://127.0.0.1:8417/offline.html") as response:
        assert b"<style>" in response.read()
finally:
    process.terminate()
    process.wait(timeout=5)
with socket.socket() as connection:
    assert connection.connect_ex(("127.0.0.1", 8417)) != 0
print(
    "Isolated wheel: real demo, file-set capture, artifact validation, embedded assets, "
    "offline HTML and HTTP startup passed"
)
