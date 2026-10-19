import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

import parquetscope.capture as capture_module
from parquetscope.analysis import reconcile
from parquetscope.capture import capture
from parquetscope.models import Settings
from parquetscope.range_server import RangeServer


def test_real_query_independent_server_and_no_metadata_contamination(measured):
    run, preview = measured
    expected = sum(i for i in range(20000) if i % 71 == 42)
    assert preview["rows"] == [[str(expected)]]
    assert run.capture.complete and run.capture.reconciled
    assert len(run.observations) == len(run.server_observations) > 2
    assert all(o.url.startswith("http://127.0.0.1:") for o in run.observations)
    assert all(o.id in {s.id for s in run.server_observations} for o in run.observations)
    assert run.metrics.server_written_bytes == run.metrics.declared_body_bytes
    assert int(run.metrics.confirmed_range_bytes) >= int(run.metrics.unique_coverage_bytes)
    assert all(s.written_body_bytes == "0" for s in run.server_observations if s.method == "HEAD")
    # App metadata is only read before RangeServer is started; every endpoint access
    # belongs to the worker and has a matching engine log.
    assert reconcile(list(reversed(run.observations)), run.server_observations)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "COPY (SELECT 1) TO '/tmp/parquetscope-forbidden'",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_parquet('http://127.0.0.1:9/no')",
        "SELECT * FROM read_parquet('https://example.com/no.parquet')",
    ],
)
def test_statement_and_external_access_restrictions(parquet, httpfs, sql):
    run, _ = capture(parquet, sql)
    assert run.sql_status == "failed"
    assert run.errors
    assert not Path("/tmp/parquetscope-forbidden").exists()


def test_failed_sql_and_capture_completeness_are_independent(parquet, httpfs):
    run, _ = capture(parquet, "SELECT nonexistent_column FROM input")
    assert run.sql_status == "failed" and run.errors
    assert run.capture.complete


@pytest.mark.parametrize("mode", ["timeout", "cancelled"])
def test_deadline_cancel_terminate_process_and_listener(parquet, httpfs, monkeypatch, mode):
    processes = []
    servers = []
    original = subprocess.Popen

    def popen(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    class ObservedServer(RangeServer):
        def __enter__(self):
            servers.append(self)
            return super().__enter__()

    monkeypatch.setattr(capture_module.subprocess, "Popen", popen)
    monkeypatch.setattr(capture_module, "RangeServer", ObservedServer)
    cancel = threading.Event()
    timer = threading.Timer(0.7, cancel.set) if mode == "cancelled" else None
    if timer:
        timer.start()
    try:
        started = time.monotonic()
        run, _ = capture(
            parquet,
            "SELECT sum(sin(i)) FROM range(10000000000) t(i)",
            Settings(
                threads=2,
                memory_limit_mb=128,
                deadline_seconds=0.8 if mode == "timeout" else 20,
                preview_rows=5,
            ),
            cancel,
        )
    finally:
        if timer:
            timer.cancel()
            timer.join()
    assert run.sql_status == mode and not run.capture.complete
    assert run.metrics.totals_scope == "observed_subset"
    assert time.monotonic() - started < 5
    assert processes and all(p.poll() is not None for p in processes)
    assert servers and all(not s.thread.is_alive() and not s.active for s in servers)
    for server in servers:
        with socket.socket() as conn:
            assert conn.connect_ex(("127.0.0.1", server.port)) != 0


def test_result_is_fully_consumed_independently_of_preview(parquet, httpfs):
    run, preview = capture(
        parquet,
        "SELECT id FROM input",
        Settings(threads=2, memory_limit_mb=128, deadline_seconds=20, preview_rows=2),
    )
    assert run.sql_status == "succeeded" and run.result_rows == "20000"
    assert len(preview["rows"]) == 2 and run.capture.complete


def test_capture_invalidated_when_file_changes(parquet, httpfs, monkeypatch):
    original = RangeServer.__exit__

    def change_before_cleanup(self, *args):
        with parquet.open("ab") as stream:
            stream.write(b"changed-during-capture")
        return original(self, *args)

    monkeypatch.setattr(RangeServer, "__exit__", change_before_cleanup)
    run, _ = capture(parquet, "SELECT sum(id) FROM input")
    assert run.sql_status == "failed" and not run.capture.complete
    assert any("Input changed" in e for e in run.errors)


@pytest.mark.parametrize("offset", ["+00", "+03", "+00:00"])
@pytest.mark.parametrize("fraction", ["", ".1", ".12", ".123", ".1234", ".12345", ".123456"])
def test_duckdb_timezone_timestamp_on_python310(offset, fraction):
    from parquetscope.capture import normalize

    rows = []
    for second in [0, 1]:
        rows.append(
            {
                "request": {
                    "type": "HEAD",
                    "url": "http://127.0.0.1/input",
                    "start_time": f"2026-10-19 00:00:0{second}{fraction}{offset}",
                    "duration_ms": 0,
                    "headers": {},
                },
                "response": {
                    "status": "OK_200",
                    "headers": {"Content-Length": "100", "X-ParquetScope-Request-ID": str(second)},
                },
            }
        )
    observations = normalize(rows, 100)
    assert observations[0].start_ms == 0
    assert observations[1].start_ms == 1000


@pytest.mark.parametrize("limit", [64, 512])
def test_recorded_memory_limit_is_applied_in_mib(parquet, httpfs, limit):
    run, preview = capture(
        parquet,
        "SELECT current_setting('memory_limit')",
        Settings(threads=2, memory_limit_mb=limit, deadline_seconds=20, preview_rows=1),
    )
    assert run.sql_status == "succeeded" and run.capture.complete
    assert run.settings.memory_limit_mb == limit
    assert preview["rows"] == [[f"{limit}.0 MiB"]]
