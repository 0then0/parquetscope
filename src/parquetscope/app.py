import asyncio
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from .capture import capture
from .demo import SQL, generate
from .models import MAX_ARTIFACT_BYTES, Model, Run, Settings, load_artifact
from .offline import STATIC, viewer_html


class RunRequest(Model):
    input_id: str
    sql: str = Field(min_length=1, max_length=65536)
    settings: Settings = Field(
        default_factory=lambda: Settings(
            threads=4,
            memory_limit_mb=512,
            deadline_seconds=30,
            preview_rows=30,
        )
    )


def create_app(
    paths: list[Path] | None = None,
    artifacts: list[Run] | None = None,
    demo_directory: Path | None = None,
) -> FastAPI:
    inputs = {str(i): p.resolve() for i, p in enumerate(paths or [])}
    jobs: dict[str, dict] = {}
    stored = {r.id: r for r in artifacts or []}
    tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(_app):
        yield
        for job in jobs.values():
            job["cancel"].set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="ParquetScope", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def loopback_boundary(request: Request, call_next):
        host = request.url.hostname
        if host not in ("127.0.0.1", "localhost", "::1", "testserver"):
            return JSONResponse({"detail": "Loopback hosts only"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse(
                {"detail": "Cross-origin requests are not allowed"}, status_code=403
            )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/api/session")
    def session():
        return {
            "inputs": [{"id": i, "name": p.name} for i, p in inputs.items()],
            "runs": [r.model_dump() for r in stored.values()],
            "demo_available": demo_directory is not None,
        }

    @app.post("/api/demo")
    async def demo():
        if demo_directory is None:
            raise HTTPException(403, "Start with parquetscope serve --demo to enable the demo")
        if any(j["status"] == "running" for j in jobs.values()):
            raise HTTPException(409, "Wait for the current run to finish")
        original, sorted_path, _ = await asyncio.to_thread(generate, demo_directory)
        inputs["demo-original"], inputs["demo-sorted"] = original, sorted_path
        return {"inputs": [{"id": i, "name": p.name} for i, p in inputs.items()], "sql": SQL}

    async def work(job_id: str, payload: RunRequest):
        job = jobs[job_id]
        try:
            run, preview = await asyncio.to_thread(
                capture,
                inputs[payload.input_id],
                payload.sql,
                payload.settings,
                job["cancel"],
            )
            stored[run.id] = run
            job.update(status="finished", run=run.model_dump(), preview=preview)
        except Exception as error:
            job.update(status="error", error=str(error))

    @app.post("/api/runs", status_code=202)
    async def start(payload: RunRequest):
        if payload.input_id not in inputs:
            raise HTTPException(404, "Input is not explicitly selected for this session")
        if any(j["status"] == "running" for j in jobs.values()):
            raise HTTPException(409, "One capture at a time")
        if len(stored) >= 20:
            raise HTTPException(409, "Session limit: 20 runs. Export artifacts and restart.")
        job_id = uuid.uuid4().hex
        jobs[job_id] = {"status": "running", "cancel": threading.Event()}
        task = asyncio.create_task(work(job_id, payload))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"id": job_id}

    @app.get("/api/jobs/{job_id}")
    def poll(job_id: str):
        if job_id not in jobs:
            raise HTTPException(404, "Unknown run")
        return {key: value for key, value in jobs[job_id].items() if key != "cancel"}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        if job_id not in jobs:
            raise HTTPException(404, "Unknown run")
        jobs[job_id]["cancel"].set()
        return {"status": "cancelling"}

    @app.post("/api/import")
    async def import_run(request: Request):
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > MAX_ARTIFACT_BYTES:
                raise HTTPException(413, "Artifact exceeds 32 MiB limit")
        try:
            run = load_artifact(bytes(chunks))
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if len(stored) >= 20:
            raise HTTPException(409, "Session limit: 20 runs")
        stored[run.id] = run
        return run.model_dump()

    @app.get("/api/artifact/{run_id}")
    def artifact(run_id: str):
        if run_id not in stored:
            raise HTTPException(404, "Unknown run")
        return JSONResponse(
            stored[run_id].model_dump(),
            headers={
                "Content-Disposition": f'attachment; filename="{run_id}.parquetscope.json"',
            },
        )

    @app.get("/offline.html")
    def offline():
        return HTMLResponse(viewer_html())

    @app.get("/")
    def index():
        if not (STATIC / "index.html").exists():
            raise HTTPException(503, "Frontend missing. Run pnpm --dir frontend build")
        return FileResponse(STATIC / "index.html")

    if (STATIC / "assets").exists():
        app.mount("/assets", StaticFiles(directory=STATIC / "assets"))
    app.state.jobs = jobs
    return app
