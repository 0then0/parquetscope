"""Engine selection, prefetch setting transport and evidence, request correlation, old artifacts."""

import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

import parquetscope.capture as capture_module
from parquetscope import worker
from parquetscope.analysis import reconcile_set
from parquetscope.capture import capture_set, correlate
from parquetscope.engines import SUPPORTED_ENGINES, read_manifest, resolve_engine
from parquetscope.manifest import load_manifest
from parquetscope.models import Settings, load_artifact, load_artifact_file, span
from parquetscope.models_v2 import FileHTTPObservation, FileServerObservation
from parquetscope.models_v3 import PrefetchEvidence, Settings3

ROOT = Path(__file__).parents[1]
SELECTIVE = "SELECT count(*) AS n, sum(v) AS s FROM input WHERE month = 2"


def settings(prefetch=None):
    return Settings3(
        threads=2,
        memory_limit_mb=256,
        deadline_seconds=30,
        preview_rows=5,
        disable_parquet_prefetching=prefetch,
    )


@pytest.fixture
def fake_engine_home(tmp_path, monkeypatch):
    """A prepared-looking environment for 2.0.0 whose interpreter is never executed."""
    monkeypatch.setenv("PARQUETSCOPE_HOME", str(tmp_path / "home"))
    root = tmp_path / "home" / "engines" / "duckdb-2.0.0"
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "python").write_text("")
    (root / "extensions").mkdir()
    (root / "engine.json").write_text(json.dumps({"duckdb_version": "2.0.0"}))
    return root


class FakeProcess:
    """Stands in for the worker process; records the config it was given."""

    calls: list = []
    result: dict = {}

    def __init__(self, argv, **_):
        FakeProcess.calls.append(argv)
        self.returncode = 0
        config = json.loads(Path(argv[-2]).read_text())
        FakeProcess.config = config
        Path(argv[-1]).write_text(json.dumps(FakeProcess.result))

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def worker_result(prefetch, version="2.0.0"):
    return {
        "status": "succeeded",
        "errors": [],
        "logs": [],
        "logs_complete": True,
        "preview": [],
        "columns": [],
        "result_rows": "0",
        "result_digest": "0" * 64,
        "query_duration_ms": 1.0,
        "engine": {
            "duckdb_version": version,
            "python_version": "3.13.0",
            "httpfs_version": "77392a7207",
            "effective_settings": {},
            "bound_schema": None,
            "prefetch": prefetch,
        },
        "phases": None,
    }


# --- engine selection ---------------------------------------------------------------------


def test_unprepared_and_unsupported_engines_fail_before_any_worker(tmp_path, monkeypatch, hive_set):
    monkeypatch.setenv("PARQUETSCOPE_HOME", str(tmp_path / "empty"))

    def forbidden(*_a, **_k):
        raise AssertionError("A worker must not start without a prepared engine")

    monkeypatch.setattr(capture_module.subprocess, "Popen", forbidden)
    with pytest.raises(RuntimeError, match=r"setup --engine 2\.0\.0"):
        capture_set(load_manifest(hive_set), SELECTIVE, engine="2.0.0")
    with pytest.raises(ValueError, match="Unsupported DuckDB version"):
        resolve_engine("1.4.3")
    assert not (tmp_path / "empty").exists(), "capture must not create or install anything"


def test_selected_engine_runs_its_own_interpreter_and_extension_directory(
    fake_engine_home, monkeypatch, hive_set
):
    FakeProcess.calls = []
    FakeProcess.result = worker_result({"requested": True, "applied": "true", "status": "verified"})
    monkeypatch.setattr(capture_module.subprocess, "Popen", FakeProcess)
    run, _ = capture_set(load_manifest(hive_set), SELECTIVE, settings(True), engine="2.0.0")
    argv = FakeProcess.calls[0]
    assert argv[0] == str(fake_engine_home / "venv" / "bin" / "python") != sys.executable
    assert argv[1] == "-I" and argv[2] == worker.__file__
    config = FakeProcess.config
    assert config["expected_duckdb_version"] == "2.0.0"
    assert config["extension_dir"] == str(fake_engine_home / "extensions")
    assert config["disable_parquet_prefetching"] is True
    assert run.duckdb_version == "2.0.0" and run.engine.environment == "isolated"


def test_default_run_uses_project_interpreter_without_version_pin(tmp_path, monkeypatch, hive_set):
    FakeProcess.calls = []
    FakeProcess.result = worker_result(
        {"requested": None, "applied": "false", "status": "engine_default"}, duckdb.__version__
    )
    monkeypatch.setattr(capture_module.subprocess, "Popen", FakeProcess)
    run, _ = capture_set(load_manifest(hive_set), SELECTIVE)
    assert FakeProcess.calls[0][0] == sys.executable
    assert FakeProcess.config["expected_duckdb_version"] is None
    assert FakeProcess.config["disable_parquet_prefetching"] is None
    assert run.engine.environment == "project" and run.settings.disable_parquet_prefetching is None


def test_cli_rejects_unprepared_engine_and_engine_for_single_files(tmp_path, hive_set, parquet):
    env = {"PARQUETSCOPE_HOME": str(tmp_path / "empty"), "PATH": ""}
    base = [sys.executable, "-m", "parquetscope.cli", "capture", "--sql", SELECTIVE]
    out = tmp_path / "x.json"
    unprepared = subprocess.run(
        [*base, "--manifest", str(hive_set), "--engine", "2.0.0", "--output", str(out)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert unprepared.returncode == 1 and "not prepared" in unprepared.stderr
    single = subprocess.run(
        [*base, str(parquet), "--prefetch", "off", "--output", str(out)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert single.returncode == 1 and "need a file set" in single.stderr
    assert not out.exists()


# --- prefetch setting: transport, application and evidence ---------------------------------


@pytest.mark.parametrize(
    ("requested", "status", "applied"),
    [(None, "engine_default", "false"), (False, "verified", "false"), (True, "verified", "true")],
)
def test_worker_applies_and_reads_back_the_setting(requested, status, applied):
    engine = {"prefetch": {"requested": None, "applied": None, "status": "unverified"}}
    with duckdb.connect() as connection:
        worker.apply_prefetch(connection, requested, engine)
    assert engine["prefetch"] == {"requested": requested, "applied": applied, "status": status}


class Scripted:
    """A connection double that fails or lies where the real engine would not."""

    def __init__(self, set_error=None, read_error=None, reads=False):
        self.set_error, self.read_error, self.reads = set_error, read_error, reads

    def execute(self, sql):
        if sql.startswith("SET") and self.set_error:
            raise duckdb.Error(self.set_error)
        if sql.startswith("SELECT") and self.read_error:
            raise duckdb.Error(self.read_error)
        return self

    def fetchone(self):
        return (self.reads,)


def test_unavailable_or_unconfirmed_request_is_an_error():
    def attempt(connection, requested):
        engine = {"prefetch": {"requested": None, "applied": None, "status": "unverified"}}
        with pytest.raises(ValueError) as error:
            worker.apply_prefetch(connection, requested, engine)
        assert engine["prefetch"]["status"] == "unverified"
        return str(error.value)

    assert "unavailable" in attempt(Scripted(set_error="unknown option"), True)
    assert "cannot be verified" in attempt(Scripted(read_error="no such setting"), True)
    assert "requested true, engine reports false" in attempt(Scripted(reads=False), True)


def test_unreadable_default_is_incomplete_evidence_not_a_guess(
    fake_engine_home, monkeypatch, hive_set
):
    engine = {"prefetch": {"requested": None, "applied": None, "status": "unverified"}}
    worker.apply_prefetch(Scripted(read_error="no such setting"), None, engine)
    assert engine["prefetch"] == {"requested": None, "applied": None, "status": "unverified"}
    FakeProcess.result = worker_result(engine["prefetch"])
    monkeypatch.setattr(capture_module.subprocess, "Popen", FakeProcess)
    run, _ = capture_set(load_manifest(hive_set), SELECTIVE, engine="2.0.0")
    assert run.capture.complete is False and run.engine.prefetch.status == "unverified"
    assert "could not be verified" in " ".join(run.errors)


def test_prefetch_evidence_must_be_consistent():
    with pytest.raises(ValueError, match="disagrees"):
        PrefetchEvidence(requested=True, applied="false", status="verified")
    with pytest.raises(ValueError, match="record its value"):
        PrefetchEvidence(requested=None, applied=None, status="engine_default")
    with pytest.raises(ValueError, match="verified or unverified"):
        PrefetchEvidence(requested=True, applied="true", status="engine_default")


def test_settings_default_does_not_set_prefetch():
    assert (
        Settings3(
            threads=1, memory_limit_mb=64, deadline_seconds=1, preview_rows=0
        ).disable_parquet_prefetching
        is None
    )
    with pytest.raises(ValueError):
        Settings3(
            threads=1,
            memory_limit_mb=64,
            deadline_seconds=1,
            preview_rows=0,
            disable_parquet_prefetching="maybe",
        )


# --- real engines (prepared with `parquetscope setup --engine X`) ----------------------------


def require_engine(version):
    if read_manifest(version) is None:
        pytest.skip(
            f"DuckDB {version} is not prepared: run `parquetscope setup --engine {version}`"
        )


@pytest.mark.parametrize("version", SUPPORTED_ENGINES)
@pytest.mark.parametrize("prefetch", [None, False, True])
def test_real_engine_records_version_setting_and_requests(version, prefetch, hive_set, tmp_path):
    require_engine(version)
    run, preview = capture_set(
        load_manifest(hive_set), SELECTIVE, settings(prefetch), engine=version
    )
    assert run.sql_status == "succeeded" and run.capture.complete, run.errors
    assert preview["rows"] == [["3000", "8994"]]
    assert run.schema_version == "4" and run.duckdb_version == version
    assert run.engine.environment == "isolated" and run.engine.httpfs_version
    expected = {None: "false", False: "false", True: "true"}[prefetch]
    assert run.engine.prefetch.requested is prefetch
    assert run.engine.prefetch.applied == expected
    assert run.engine.prefetch.status == ("engine_default" if prefetch is None else "verified")
    assert run.engine.effective_settings["disable_parquet_prefetching"].lower() == expected
    assert run.engine.effective_settings["enable_external_file_cache"].lower() == "false"
    # DuckDB 2.0 redacts echoed headers; 1.5.6 still echoes the server's request ID.
    assert run.engine.request_correlation == (
        "response_header_id" if version == "1.5.6" else "ordered_tuple_match"
    )
    assert run.metrics.get_count + run.metrics.head_count == len(run.server_observations)
    out = tmp_path / "run.json"
    out.write_text(run.model_dump_json())
    assert load_artifact_file(out) == run


# --- correlation without echoed request IDs ---------------------------------------------------


def pair(i, start, rng="bytes=0-9", file_id="f001", status=206):
    a, b = (int(x) for x in rng.removeprefix("bytes=").split("-"))
    shared = dict(
        method="GET",
        requested_range=rng,
        response_span=span(a, b + 1),
        status=status,
        start_ms=start,
        duration_ms=1,
        declared_body_bytes=str(b - a + 1),
        file_id=file_id,
    )
    client = FileHTTPObservation(
        id=f"unmatched-{i}",
        url="http://127.0.0.1/x",
        requested_span=span(a, b + 1),
        request_headers={},
        response_headers={},
        engine_start_time="2026-11-07 00:00:00+00",
        phase="select",
        **shared,
    )
    server = FileServerObservation(
        id=f"{i:032x}", written_body_bytes=str(b - a + 1), complete=True, error=None, **shared
    )
    return client, server


def test_header_ids_are_used_when_the_engine_echoes_them():
    client, server = pair(1, 0)
    echoed = client.model_copy(update={"id": server.id})
    result, how = correlate([echoed], [server])
    assert how == "response_header_id" and result == [echoed]


def test_redacted_ids_are_paired_by_request_content_in_start_order():
    pairs = [pair(1, 0), pair(2, 5), pair(3, 9, "bytes=20-29"), pair(4, 12)]  # 1, 2, 4 identical
    clients = [c for c, _ in pairs]
    servers = [s for _, s in reversed(pairs)]
    fixed, how = correlate(clients, servers)
    assert how == "ordered_tuple_match" and reconcile_set(fixed, servers)
    assert len({o.id for o in fixed}) == 4
    by_server = {s.id: s for s in servers}
    assert all(by_server[o.id].requested_range == o.requested_range for o in fixed)
    # Identical requests pair in start order.
    identical = sorted(
        (o for o in fixed if o.requested_range == "bytes=0-9"), key=lambda o: o.start_ms
    )
    assert [by_server[o.id].start_ms for o in identical] == [0, 5, 12]


def test_echoed_ids_that_do_not_match_are_never_repaired_by_content():
    client, server = pair(1, 0)
    wrong = client.model_copy(update={"id": "f" * 32})  # echoed, but not the server's ID
    fixed, how = correlate([wrong], [server])
    assert how == "response_header_id" and fixed == [wrong]
    assert not reconcile_set(fixed, [server])
    mixed = [client.model_copy(update={"id": server.id}), pair(2, 3)[0]]  # one echoed, one not
    fixed, how = correlate(mixed, [server, pair(2, 3)[1]])
    assert how == "response_header_id" and fixed == mixed


def test_unmatched_requests_leave_the_capture_unreconciled():
    c1, s1 = pair(1, 0)
    c2, _ = pair(2, 1, "bytes=30-39")
    fixed, how = correlate([c1, c2], [s1])
    assert how == "ordered_tuple_match" and not reconcile_set(fixed, [s1])
    other_file, server_other = pair(3, 0, file_id="f002")
    fixed, _ = correlate([c1], [server_other])
    assert not reconcile_set(fixed, [server_other]), "a different file must never pair"


# --- historical artifacts and version boundaries ---------------------------------------------


def historical():
    return sorted(
        [
            *(ROOT / "docs/real-data/captures").glob("*.parquetscope.json"),
            *(ROOT / "docs/pruning-lab/captures").glob("*.parquetscope.json"),
        ]
    )


def test_every_published_v1_and_v2_capture_still_opens_unchanged():
    paths = historical()
    assert paths
    versions = set()
    for path in paths:
        run = load_artifact(path.read_bytes())
        versions.add(run.schema_version)
        assert run.duckdb_version == "1.5.6"
        assert not hasattr(run, "engine") or not hasattr(run.engine, "prefetch")
        assert load_artifact(run.model_dump_json().encode()) == run
    assert versions == {"1", "2"}


def test_v3_and_v2_payloads_cannot_pass_as_each_other(set_measured):
    v3 = json.loads(set_measured[0].model_dump_json())
    as_v2 = {**v3, "schema_version": "2"}
    with pytest.raises(ValueError, match="Invalid or unsupported"):
        load_artifact(json.dumps(as_v2).encode())
    v2 = json.loads(
        (ROOT / "docs/pruning-lab/captures/hive-selective-1.parquetscope.json").read_text()
    )
    with pytest.raises(ValueError, match="Invalid or unsupported"):
        load_artifact(json.dumps({**v2, "schema_version": "3"}).encode())


def test_v3_rejects_inconsistent_prefetch_claims(set_measured):
    good = json.loads(set_measured[0].model_dump_json())

    def broken(mutate):
        data = json.loads(json.dumps(good))
        mutate(data)
        with pytest.raises(ValueError):
            load_artifact(json.dumps(data).encode())

    broken(lambda d: d["settings"].update(disable_parquet_prefetching=True))
    broken(lambda d: d.update(duckdb_version="1.4.3"))
    broken(
        lambda d: d["engine"]["prefetch"].update(requested=None, applied=None, status="unverified")
    )


def test_settings_without_prefetch_field_still_describe_v1_and_v2(measured):
    assert "disable_parquet_prefetching" not in Settings.model_fields
    assert "disable_parquet_prefetching" not in measured[0].model_dump()["settings"]


def test_worker_dying_before_the_setting_is_applied_yields_unverified_evidence(
    fake_engine_home, monkeypatch, hive_set
):
    failed = worker_result({"requested": None, "applied": None, "status": "unverified"})
    failed.update(status="failed", errors=["Out of Memory Error"], logs_complete=False)
    FakeProcess.result = failed
    monkeypatch.setattr(capture_module.subprocess, "Popen", FakeProcess)
    run, _ = capture_set(load_manifest(hive_set), SELECTIVE, settings(True), engine="2.0.0")
    assert run.sql_status == "failed" and not run.capture.complete
    assert run.engine.prefetch.requested is True and run.engine.prefetch.status == "unverified"
    assert run.engine.prefetch.applied is None
