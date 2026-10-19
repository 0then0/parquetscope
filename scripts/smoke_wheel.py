"""Run with an isolated wheel environment's Python, from outside the checkout."""

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

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
    "Isolated wheel: real demo, artifact validation, embedded assets, "
    "offline HTML and HTTP startup passed"
)
