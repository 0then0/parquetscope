import time

from fastapi.testclient import TestClient

from parquetscope.app import create_app


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
