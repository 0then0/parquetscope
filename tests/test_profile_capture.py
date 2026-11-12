"""Profile configuration crosses CLI/API, lifecycle and portable evidence boundaries."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from parquetscope.app import create_app
from parquetscope.capture import capture
from parquetscope.http_profile import HTTPProfile
from parquetscope.models import Settings, load_artifact, load_artifact_file
from parquetscope.offline import viewer_html

ROOT = Path(__file__).parents[1]
SQL = "SELECT sum(id) FROM input"


def await_job(client, job):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        value = client.get(f"/api/jobs/{job}").json()
        if value["status"] != "running":
            assert value["status"] == "finished", value
            return value["run"]
        time.sleep(0.02)
    pytest.fail("Capture did not finish within its test budget")


def test_cli_and_ui_share_profile_and_result_contract(parquet, httpfs, tmp_path):
    profile = HTTPProfile(response_delay_ms=5, body_bytes_per_second=2000000)
    output = tmp_path / "profile.json"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "parquetscope.cli",
            "capture",
            str(parquet),
            "--sql",
            SQL,
            "--output",
            str(output),
            "--response-delay-ms",
            "5",
            "--body-bytes-per-second",
            "2000000",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 0, process.stderr
    cli = load_artifact_file(output)
    with TestClient(create_app(paths=[parquet])) as client:
        payload = {"input_id": "0", "sql": SQL, "http_profile": profile.model_dump()}
        response = client.post("/api/runs", json=payload)
        assert response.status_code == 202
        # Editing the next capture's payload cannot change the running job.
        payload["http_profile"]["response_delay_ms"] = 60000
        ui = load_artifact(json.dumps(await_job(client, response.json()["id"])).encode())
    assert cli.schema_version == ui.schema_version == "4"
    assert cli.sql_status == ui.sql_status == "succeeded"
    assert cli.capture.complete and ui.capture.complete
    assert cli.http_profile == ui.http_profile
    assert cli.http_profile.requested.model_dump() == profile.model_dump()
    assert cli.result_digest == ui.result_digest
    assert all(o.profile_delay_ms >= 5 for o in ui.server_observations)
    assert {o.id for o in ui.observations} == {o.id for o in ui.server_observations}
    reopened = load_artifact(ui.model_dump_json().encode())
    assert reopened == ui
    html = viewer_html(ui)
    embedded = html.split('<script id="parquetscope-artifact" type="application/json">', 1)[1]
    data = json.loads(embedded.split("</script>", 1)[0])
    assert data["http_profile"] == ui.http_profile.model_dump()
    assert data["server_observations"] == [o.model_dump() for o in ui.server_observations]


def test_default_application_capture_records_explicit_unrestricted_profile(parquet, httpfs):
    with TestClient(create_app(paths=[parquet])) as client:
        response = client.post("/api/runs", json={"input_id": "0", "sql": SQL})
        run = await_job(client, response.json()["id"])
    assert run["http_profile"]["requested"] == {
        "response_delay_ms": 0,
        "body_bytes_per_second": None,
    }
    assert run["http_profile"]["burst_bytes"] == "0"
    assert run["capture"]["complete"] and run["sql_status"] == "succeeded"


@pytest.mark.parametrize(
    "profile",
    [
        {"response_delay_ms": -1},
        {"response_delay_ms": 60001},
        {"response_delay_ms": 0.5},
        {"response_delay_ms": True},
        {"body_bytes_per_second": 0},
        {"body_bytes_per_second": -1},
        {"body_bytes_per_second": 1000000001},
        {"body_bytes_per_second": "100"},
        {"unexpected": 1},
    ],
)
def test_invalid_api_profile_is_rejected_before_start(parquet, profile):
    app = create_app(paths=[parquet])
    with TestClient(app) as client:
        response = client.post(
            "/api/runs", json={"input_id": "0", "sql": SQL, "http_profile": profile}
        )
        assert response.status_code == 422
        assert not app.state.jobs


@pytest.mark.parametrize(
    "option,value",
    [
        ("--response-delay-ms", "-1"),
        ("--response-delay-ms", "60001"),
        ("--body-bytes-per-second", "0"),
        ("--body-bytes-per-second", "1000000001"),
    ],
)
def test_invalid_cli_profile_writes_nothing(parquet, tmp_path, option, value):
    output = tmp_path / "rejected.json"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "parquetscope.cli",
            "capture",
            str(parquet),
            "--sql",
            SQL,
            "--output",
            str(output),
            option,
            value,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert process.returncode != 0 and not output.exists()


@pytest.mark.parametrize(
    "profile",
    [
        HTTPProfile(response_delay_ms=2000),
        HTTPProfile(body_bytes_per_second=1000),
    ],
)
def test_profile_does_not_extend_execution_deadline(parquet, httpfs, profile):
    start = time.monotonic()
    run, _ = capture(
        parquet,
        SQL,
        Settings(threads=2, memory_limit_mb=128, deadline_seconds=0.7, preview_rows=1),
        http_profile=profile,
    )
    assert time.monotonic() - start < 3
    assert run.sql_status == "timeout" and not run.capture.complete
    assert run.metrics.totals_scope == "observed_subset"
    assert run.server_observations
    assert any(o.termination in ("timeout", "disconnect") for o in run.server_observations)
    assert int(run.metrics.server_written_bytes) == sum(
        int(o.written_body_bytes) for o in run.server_observations
    )
    assert load_artifact(run.model_dump_json().encode()) == run


@pytest.mark.parametrize(
    "path",
    [
        "docs/real-data/captures/A-1.parquetscope.json",
        "docs/pruning-lab/captures/hive-selective-1.parquetscope.json",
        "docs/prefetch-lab/captures/duckdb-1.5.6-prefetch-off-1.parquetscope.json",
    ],
)
def test_historical_profiles_remain_unrecorded(path):
    run = load_artifact_file(ROOT / path)
    assert "http_profile" not in run.model_dump()
    assert all("profile_delay_ms" not in o.model_dump() for o in run.server_observations)


@pytest.mark.parametrize("name", ["cafe\u0301.parquet", "a\\b.parquet", "line\nbreak.parquet"])
def test_direct_file_names_do_not_inherit_manifest_path_restrictions(parquet, httpfs, name):
    target = parquet.parent / name
    parquet.rename(target)
    run, preview = capture(target, SQL, http_profile=HTTPProfile())
    assert run.sql_status == "succeeded" and run.capture.complete
    assert preview["rows"] == [["199990000"]]
    assert run.file_set.files[0].logical_path == "input.parquet"
    assert run.file_set.label == target.name
    assert load_artifact(run.model_dump_json().encode()) == run


@pytest.mark.parametrize("key", ["requested", "effective"])
@pytest.mark.parametrize("field", ["response_delay_ms", "body_bytes_per_second"])
def test_recorded_profile_requires_both_values(set_measured, key, field):
    data = set_measured[0].model_dump()
    del data["http_profile"][key][field]
    with pytest.raises(ValueError):
        load_artifact(json.dumps(data).encode())


def test_recorded_profile_requires_limiter_semantics(set_measured):
    data = set_measured[0].model_dump()
    del data["http_profile"]["semantics"]
    with pytest.raises(ValueError):
        load_artifact(json.dumps(data).encode())


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["http_profile"].update(burst_bytes="999"),
        lambda d: d["http_profile"]["effective"].update(response_delay_ms=1),
        lambda d: d["server_observations"][0].update(reserved_body_bytes="999999999"),
        lambda d: d["server_observations"][0].update(profile_delay_ms=999999999),
        lambda d: d["server_observations"][0].update(termination="cancelled"),
    ],
)
def test_v4_rejects_inconsistent_profile_evidence(set_measured, change):
    data = set_measured[0].model_dump()
    change(data)
    with pytest.raises(ValueError):
        load_artifact(json.dumps(data).encode())
