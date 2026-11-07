import subprocess
import sys
import threading
import time

import pytest
from conftest import write_manifest, write_table

import parquetscope.capture as capture_module
from parquetscope.capture import capture_set
from parquetscope.manifest import load_manifest
from parquetscope.models import Settings, load_artifact, save_artifact
from parquetscope.models_v2 import SetRun

SELECTIVE = "SELECT count(*) AS n, sum(v) AS s FROM input WHERE month = 2"
EVERYTHING = "SELECT count(*) AS n, sum(v) AS s FROM input"


def by_path(run):
    paths = {f.id: f.logical_path for f in run.file_set.files}
    return {paths[m.file_id]: m for m in run.metrics.files}


def test_real_duckdb_attribution_and_pruning(set_measured):
    run, preview = set_measured
    assert isinstance(run, SetRun) and run.capture.complete and run.capture.reconciled
    assert preview["rows"] == [["3000", "8994"]]
    files = by_path(run)
    # month=3 is excluded by the partition predicate: no request of any kind reached it.
    assert files["month=3/data.parquet"].observed_access == "none"
    assert files["month=3/data.parquet"].server_written_bytes == "0"
    assert files["month=2/data.parquet"].observed_access == "get"
    # Every client observation names the same file as the independent server saw.
    server = {s.id: s for s in run.server_observations}
    assert all(server[o.id].file_id == o.file_id for o in run.observations)
    assert all(o.url.startswith("http://127.0.0.1:") for o in run.observations)
    assert len({o.url.rsplit("/", 2)[-2] for o in run.observations}) >= 1
    # Totals are the sum of the per-file metrics.
    assert run.metrics.get_count == sum(m.get_count for m in run.metrics.files)
    assert run.metrics.server_written_bytes == str(
        sum(int(m.server_written_bytes) for m in run.metrics.files)
    )
    assert run.metrics.files_without_observed_access == 1 + (
        files["month=1/data.parquet"].observed_access == "none"
    )
    assert run.engine.httpfs_version and run.engine.bound_schema
    assert [c.name for c in run.engine.bound_schema] == ["id", "month", "v"]
    assert run.file_set.read.hive_partitioning is True


def test_binding_requests_are_captured_and_labelled_by_phase(set_measured):
    run, _ = set_measured
    binding = [o for o in run.observations if o.phase == "view_creation"]
    assert binding, "view creation must be part of the capture"
    assert {o.file_id for o in binding} == {"f001"}  # binding reads the first listed file
    assert all(o.phase == "select" for o in run.observations if o not in binding)


def test_non_selective_control_reads_every_file(hive_set, httpfs):
    selective, _ = capture_set(load_manifest(hive_set), SELECTIVE)
    control, preview = capture_set(load_manifest(hive_set), EVERYTHING)
    assert preview["rows"] == [["9000", "26982"]]
    assert control.metrics.files_with_get == 3
    assert selective.metrics.files_with_get < control.metrics.files_with_get
    assert int(control.metrics.server_written_bytes) > int(selective.metrics.server_written_bytes)


def test_flat_set_reads_all_files_for_the_same_predicate(hive_set, tmp_path, httpfs):
    flat = tmp_path / "flat"
    names = []
    for month in (1, 2, 3):
        write_table(flat / f"m{month}.parquet", month)
        names.append(f"m{month}.parquet")
    spec = load_manifest(write_manifest(flat, names, hive=False))
    run, preview = capture_set(spec, SELECTIVE)
    assert preview["rows"] == [["3000", "8994"]]
    # Without Hive pruning every file is at least opened to evaluate month = 2.
    assert run.metrics.files_without_observed_access == 0
    assert run.file_set.read.hive_partitioning is False


@pytest.mark.parametrize(
    "names",
    [
        ["region=Zürich/données 1.parquet", "region=a b&c/données 1.parquet"],
        ["region=x%y#z?q/it's.parquet", "region=plain/it's.parquet"],
        ["region=a'); DROP TABLE input; --/x.parquet", "region=b/x.parquet"],
    ],
)
def test_special_character_paths_work_end_to_end(tmp_path, httpfs, names):
    for index, name in enumerate(names):
        write_table(tmp_path / name, index + 1, rows=100)
    spec = load_manifest(write_manifest(tmp_path, names))
    run, preview = capture_set(spec, "SELECT count(*) AS n, count(DISTINCT region) AS r FROM input")
    assert run.sql_status == "succeeded", run.errors
    assert preview["rows"] == [["200", "2"]]
    assert run.capture.complete
    assert {m.observed_access for m in run.metrics.files} == {"get"}
    reopened = load_artifact(run.model_dump_json().encode())
    assert [f.logical_path for f in reopened.file_set.files] == names


@pytest.mark.parametrize(
    "sql",
    [
        "SET VARIABLE input_urls = ['http://127.0.0.1:9/x.parquet']",
        "SELECT * FROM read_parquet('http://127.0.0.1:9/no')",
        "SELECT * FROM read_csv('/etc/passwd')",
        "COPY (SELECT 1) TO '/tmp/parquetscope-forbidden-set'",
        "SELECT 1; SELECT 2",
    ],
)
def test_sql_and_access_restrictions_still_hold_for_sets(hive_set, httpfs, sql):
    run, _ = capture_set(load_manifest(hive_set), sql)
    assert run.sql_status == "failed" and run.errors


def test_file_changed_after_preparation_invalidates_the_capture(
    hive_set, tmp_path, monkeypatch, httpfs
):
    original = capture_module.resolve_set

    def resolve_then_modify(*args, **kwargs):
        resolved = original(*args, **kwargs)
        with (tmp_path / "month=2" / "data.parquet").open("ab") as stream:
            stream.write(b"changed-after-preparation")
        return resolved

    monkeypatch.setattr(capture_module, "resolve_set", resolve_then_modify)
    run, _ = capture_set(load_manifest(hive_set), SELECTIVE)
    assert not run.capture.complete and run.sql_status == "failed"
    assert any("month=2/data.parquet" in e and "changed" in e for e in run.errors)


@pytest.mark.parametrize("mode", ["timeout", "cancelled"])
def test_partial_trace_cancel_and_deadline_terminate_cleanly(hive_set, monkeypatch, httpfs, mode):
    processes, servers = [], []
    popen = subprocess.Popen

    def tracked(*args, **kwargs):
        processes.append(popen(*args, **kwargs))
        return processes[-1]

    class Observed(capture_module.FileSetServer):
        def __enter__(self):
            servers.append(self)
            return super().__enter__()

    monkeypatch.setattr(capture_module.subprocess, "Popen", tracked)
    monkeypatch.setattr(capture_module, "FileSetServer", Observed)
    cancel = threading.Event()
    timer = threading.Timer(0.7, cancel.set) if mode == "cancelled" else None
    if timer:
        timer.start()
    started = time.monotonic()
    try:
        run, _ = capture_set(
            load_manifest(hive_set),
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
    assert run.sql_status == mode and not run.capture.complete
    assert run.metrics.totals_scope == "observed_subset"
    assert time.monotonic() - started < 6
    assert all(p.poll() is not None for p in processes)
    assert all(not s.thread.is_alive() and not s.active for s in servers)
    # A partial trace never claims absence of access; every file is simply unobserved here.
    assert run.metrics.unknown_reasons
    assert load_artifact(run.model_dump_json().encode()) == run


def test_cancellation_during_preparation_is_an_error_not_a_capture(hive_set, httpfs):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ValueError, match="cancelled"):
        capture_set(load_manifest(hive_set), SELECTIVE, cancel=cancel)


def test_cli_capture_manifest_writes_a_v3_artifact_and_protects_inputs(hive_set, tmp_path, httpfs):
    out = tmp_path / "set.parquetscope.json"
    base = [sys.executable, "-m", "parquetscope.cli", "capture", "--sql", SELECTIVE]
    ok = subprocess.run(
        [*base, "--manifest", str(hive_set), "--output", str(out)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ok.returncode == 0, ok.stderr
    run = load_artifact(out.read_bytes())
    assert run.schema_version == "3" and run.capture.complete
    before = (tmp_path / "month=1" / "data.parquet").read_bytes()
    for target in (hive_set, tmp_path / "month=1" / "data.parquet"):
        bad = subprocess.run(
            [*base, "--manifest", str(hive_set), "--output", str(target)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert bad.returncode == 1 and "Output must not refer" in bad.stderr
    assert (tmp_path / "month=1" / "data.parquet").read_bytes() == before
    both = subprocess.run(
        [
            *base,
            str(tmp_path / "month=1" / "data.parquet"),
            "--manifest",
            str(hive_set),
            "--output",
            str(tmp_path / "x.json"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert both.returncode == 1 and "exactly one input" in both.stderr
    save_artifact(run, tmp_path / "again.json")
    viewer = tmp_path / "viewer.html"
    shown = subprocess.run(
        [sys.executable, "-m", "parquetscope.cli", "viewer", str(out), "--output", str(viewer)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert shown.returncode == 0 and "file_set" in viewer.read_text()
