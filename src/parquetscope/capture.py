import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow

from . import __version__
from .analysis import derive_metrics, reconcile
from .intervals import requested_range
from .metadata import read_metadata
from .models import Capture, HTTPObservation, Run, Settings, span
from .range_server import RangeServer


def extension_directory() -> Path:
    home = Path(os.environ.get("PARQUETSCOPE_HOME", Path.home() / ".cache" / "parquetscope"))
    return home / "extensions"


def setup_httpfs():
    directory = extension_directory()
    directory.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(config={"extension_directory": str(directory)}) as connection:
        connection.execute("INSTALL httpfs")
        connection.execute("LOAD httpfs")
    return directory


def normalize(logs: list, size: int) -> list[HTTPObservation]:
    observations = []
    starts = []
    for log in logs:
        text = log["request"]["start_time"]
        # Python 3.10 needs full timezone offsets and 3/6 fractional digits.
        timestamp = re.sub(r"([+-]\d{2})$", r"\1:00", text.replace(" ", "T"))
        timestamp = re.sub(
            r"\.(\d{1,6})(?=[+-]\d{2}:\d{2}$)",
            lambda match: "." + match[1].ljust(6, "0"),
            timestamp,
        )
        starts.append(datetime.fromisoformat(timestamp).timestamp() * 1000)
    origin = min(starts, default=0)
    for index, log in enumerate(logs):
        req, res = log["request"], log["response"]
        rh = {k.lower(): v for k, v in req["headers"].items()}
        sh = {k.lower(): v for k, v in res["headers"].items()}
        method = req["type"]
        requested = rh.get("range")
        requested_span = None
        if requested and method == "GET":
            resolved = requested_range(requested, size)
            if resolved:
                requested_span = span(*resolved)
        status_match = re.search(r"(\d{3})$", res["status"])
        if not status_match:
            raise ValueError(f"Unrecognized DuckDB HTTP status: {res['status']}")
        status = int(status_match[1])
        response_span = None
        content_range = sh.get("content-range", "")
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
        if method == "GET" and status == 206 and match:
            a, b, total = map(int, match.groups())
            if total == size and a <= b < size:
                response_span = span(a, b + 1)
        elif method == "GET" and status == 200 and sh.get("content-length") == str(size):
            response_span = span(0, size)
        declared = sh.get("content-length") if method == "GET" else "0"
        observations.append(
            HTTPObservation(
                id=sh.get("x-parquetscope-request-id", f"unmatched-{index}"),
                method=method,
                url=req["url"],
                requested_range=requested,
                requested_span=requested_span,
                response_span=response_span,
                status=status,
                start_ms=max(0, starts[index] - origin),
                duration_ms=req["duration_ms"],
                declared_body_bytes=declared,
                request_headers=req["headers"],
                response_headers=res["headers"],
                engine_start_time=req["start_time"],
            )
        )
    return observations


def capture(
    path: Path,
    sql: str,
    settings: Settings | None = None,
    cancel: threading.Event | None = None,
) -> tuple[Run, dict]:
    settings = settings or Settings(
        threads=4, memory_limit_mb=512, deadline_seconds=30, preview_rows=30
    )
    cancel = cancel or threading.Event()
    if len(sql) > 65536:
        raise ValueError("SQL exceeds 64 KiB limit")
    metadata, file_identity = read_metadata(path)
    result = {"errors": [], "logs": [], "status": "failed"}
    started = time.monotonic()
    directory = extension_directory()
    with tempfile.TemporaryDirectory(prefix="parquetscope-") as temp:
        result_path = Path(temp) / "result.json"
        with RangeServer(path, file_identity) as server:
            config = {
                "sql": sql,
                "url": server.url,
                "settings": settings.model_dump(),
                "extension_dir": str(directory),
            }
            config_path = Path(temp) / "config.json"
            config_path.write_text(json.dumps(config))
            with (Path(temp) / "stderr.txt").open("w+") as stderr:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "parquetscope.worker",
                        str(config_path),
                        str(result_path),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr,
                )
                try:
                    while process.poll() is None:
                        if server.truncated:
                            result["errors"].append("100,000 HTTP observation limit exceeded")
                            break
                        if cancel.is_set():
                            result["status"] = "cancelled"
                            result["errors"].append("Run cancelled; worker terminated")
                            break
                        if time.monotonic() - started > settings.deadline_seconds:
                            result["status"] = "timeout"
                            result["errors"].append("Deadline exceeded; worker terminated")
                            break
                        time.sleep(0.02)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            process.kill()
                    process.wait(timeout=3)
                if result_path.exists() and result["status"] not in ("cancelled", "timeout"):
                    result = json.loads(result_path.read_text())
                elif result["status"] == "failed":
                    stderr.seek(0)
                    result["errors"].append(
                        f"Worker exited {process.returncode}: {stderr.read(2000)}"
                    )
    errors = result["errors"]
    observations = []
    normalized = True
    try:
        observations = normalize(result.get("logs", []), int(metadata.size))
    except (ValueError, KeyError) as error:
        normalized = False
        errors.append(f"HTTP log schema error: {error}")
    reconciled = reconcile(observations, server.observations)
    complete = (
        result.get("logs_complete", False)
        and normalized
        and reconciled
        and not server.changed
        and not server.truncated
        and all(o.complete for o in server.observations)
    )
    if server.changed:
        errors.append("Input changed during capture; observations are invalid")
        result["status"] = "failed"
    reason = (
        None if complete else "; ".join(errors or ["Incomplete or unreconciled HTTP observations"])
    )
    state = Capture(complete=complete, reconciled=reconciled, reason=reason)
    run = Run(
        app_version=__version__,
        duckdb_version=duckdb.__version__,
        pyarrow_version=pyarrow.__version__,
        platform=f"{platform.system()} {platform.machine()} Python {platform.python_version()}",
        id=uuid.uuid4().hex,
        created_at=datetime.now(timezone.utc).isoformat(),
        sql=sql,
        input=metadata,
        settings=settings,
        sql_status=result["status"],
        query_duration_ms=result.get("query_duration_ms"),
        elapsed_ms=(time.monotonic() - started) * 1000,
        result_rows=result.get("result_rows"),
        result_digest=result.get("result_digest"),
        observations=observations,
        server_observations=server.observations,
        capture=state,
        metrics=derive_metrics(observations, server.observations, state),
        errors=errors,
        limitations=[
            "Loopback HTTP measurement; not S3, physical disk or client-received bytes.",
            "Response intersections do not prove decoding, use or wasted work.",
            "Fresh worker means cold client only; OS and disk caches are uncontrolled.",
            "Query duration excludes input binding; elapsed includes worker setup and cleanup.",
            "Preview is runtime-only and omitted from exported artifacts.",
        ],
    )
    return run, {"columns": result.get("columns", []), "rows": result.get("preview", [])}
