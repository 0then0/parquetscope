import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from parquetscope.app import create_app
from parquetscope.models import load_artifact_file

HISTORICAL_V2 = (
    Path(__file__).parents[1] / "docs/pruning-lab/captures/hive-selective-1.parquetscope.json"
)


def test_runtime_boundary_and_artifact_import(measured):
    run, _ = measured
    with TestClient(create_app(artifacts=[run])) as client:
        assert client.get("/api/session").json()["runs"][0]["id"] == run.id
        response = client.get(f"/api/artifact/{run.id}")
        assert response.status_code == 200 and "preview" not in response.json()
        assert client.post("/api/import", content=response.content).status_code == 200
        assert client.post("/api/import", content=b"{}").status_code == 422
        assert client.get("/api/session", headers={"Host": "evil.example"}).status_code == 403
        assert (
            client.post("/api/demo", headers={"Origin": "https://evil.example"}).status_code == 403
        )
        assert client.post("/api/demo").status_code == 403
        assert (
            client.post(
                "/api/runs", json={"input_id": "/etc/passwd", "sql": "SELECT 1"}
            ).status_code
            == 404
        )


def test_real_async_job(parquet, httpfs):
    with TestClient(create_app(paths=[parquet])) as client:
        started = client.post(
            "/api/runs", json={"input_id": "0", "sql": "SELECT sum(id) FROM input"}
        )
        assert started.status_code == 202
        job = started.json()["id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            value = client.get(f"/api/jobs/{job}").json()
            if value["status"] != "running":
                break
            time.sleep(0.05)
        assert value["run"]["capture"]["complete"]
        assert value["preview"]["rows"] == [["199990000"]]


def test_backend_shutdown_cancels_active_job(parquet, httpfs):
    app = create_app(paths=[parquet])
    with TestClient(app) as client:
        response = client.post(
            "/api/runs",
            json={"input_id": "0", "sql": "SELECT sum(sin(i)) FROM range(10000000000) t(i)"},
        )
        job = response.json()["id"]
    assert app.state.jobs[job]["status"] == "finished"
    assert app.state.jobs[job]["run"]["sql_status"] == "cancelled"


def test_fixed_measurement_settings_cannot_be_misrepresented(parquet):
    with TestClient(create_app(paths=[parquet])) as client:
        for change in [
            {"external_file_cache": True},
            {"cold_client": False},
            {"preview_bytes": 10**9},
        ]:
            settings = {
                "threads": 4,
                "memory_limit_mb": 512,
                "deadline_seconds": 30,
                "preview_rows": 30,
                **change,
            }
            response = client.post(
                "/api/runs", json={"input_id": "0", "sql": "SELECT 1", "settings": settings}
            )
            assert response.status_code == 422


def test_file_set_input_is_listed_and_runs_as_a_v4_job(hive_set, httpfs):
    from parquetscope.manifest import load_manifest
    from parquetscope.models import load_artifact

    app = create_app(manifests=[load_manifest(hive_set)])
    with TestClient(app) as client:
        (item,) = client.get("/api/session").json()["inputs"]
        assert item == {"id": "set-0", "name": "test-set", "kind": "set", "file_count": 3}
        assert str(hive_set.parent) not in json.dumps(item)
        started = client.post(
            "/api/runs",
            json={"input_id": "set-0", "sql": "SELECT count(*) FROM input WHERE month = 2"},
        )
        assert started.status_code == 202
        job = started.json()["id"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            value = client.get(f"/api/jobs/{job}").json()
            if value["status"] != "running":
                break
            time.sleep(0.05)
        assert value["status"] == "finished", value
        run = value["run"]
        assert run["schema_version"] == "4" and run["capture"]["complete"]
        assert value["preview"]["rows"] == [["3000"]]
        exported = client.get(f"/api/artifact/{run['id']}")
        assert "preview" not in exported.json()
        assert load_artifact(exported.content).id == run["id"]
        assert client.post("/api/import", content=exported.content).status_code == 200
        broken = exported.json()
        broken["metrics"]["get_count"] += 1
        assert client.post("/api/import", json=broken).status_code == 422
        assert (
            client.post("/api/runs", json={"input_id": "set-9", "sql": "SELECT 1"}).status_code
            == 404
        )


def test_session_serves_all_artifact_versions_together(set_measured, measured):
    v4, _ = set_measured
    v3 = load_artifact_file(
        Path(__file__).parents[1]
        / "docs/prefetch-lab/captures/duckdb-1.5.6-prefetch-off-1.parquetscope.json"
    )
    v1, _ = measured
    v2 = load_artifact_file(HISTORICAL_V2)
    with TestClient(create_app(artifacts=[v1, v2, v3, v4])) as client:
        session = client.get("/api/session").json()
        assert sorted(r["schema_version"] for r in session["runs"]) == ["1", "2", "3", "4"]
        assert [e["version"] for e in session["engines"]] == ["1.5.6", "2.0.0"]
