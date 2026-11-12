import json
import platform
import re
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow

from . import __version__
from . import worker as worker_module
from .analysis import derive_metrics, derive_set_metrics, reconcile, reconcile_set
from .engines import EngineEnv, extension_directory, project_env, resolve_engine
from .http_profile import HTTPProfile
from .intervals import requested_range
from .manifest import SetEntry, SetSpec, resolve_set
from .metadata import read_metadata
from .models import Capture, HTTPObservation, Run, ServerObservation, Settings, span
from .models_v2 import FileHTTPObservation, FileServerObservation, ReadOptions, split_logical_path
from .models_v3 import Engine3, PrefetchEvidence, Settings3
from .models_v4 import FileServerObservation4, SetRun4
from .range_server import FileSetServer, RangeServer, ServedFile, encode_route

REQUEST_ID = re.compile(r"[0-9a-f]{32}")


def setup_httpfs():
    directory = extension_directory()
    directory.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(config={"extension_directory": str(directory)}) as connection:
        connection.execute("INSTALL httpfs")
        connection.execute("LOAD httpfs")
    return directory


def _epoch_ms(text: str) -> float:
    # Python 3.10 needs full timezone offsets and 3/6 fractional digits.
    timestamp = re.sub(r"([+-]\d{2})$", r"\1:00", text.replace(" ", "T"))
    timestamp = re.sub(
        r"\.(\d{1,6})(?=[+-]\d{2}:\d{2}$)",
        lambda match: "." + match[1].ljust(6, "0"),
        timestamp,
    )
    return datetime.fromisoformat(timestamp).timestamp() * 1000


def _request_id(echoed: str | None, index: int) -> str:
    """The server's ID if the engine echoed it; DuckDB 2.0 replaces such headers by "redacted"."""
    return echoed if echoed and REQUEST_ID.fullmatch(echoed) else f"unmatched-{index}"


def _fields(log: dict, index: int, size: int, start_ms: float, origin: float) -> dict:
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
    return {
        "id": _request_id(sh.get("x-parquetscope-request-id"), index),
        "method": method,
        "url": req["url"],
        "requested_range": requested,
        "requested_span": requested_span,
        "response_span": response_span,
        "status": status,
        "start_ms": max(0, start_ms - origin),
        "duration_ms": req["duration_ms"],
        "declared_body_bytes": declared,
        "request_headers": req["headers"],
        "response_headers": res["headers"],
        "engine_start_time": req["start_time"],
    }


def normalize(logs: list, size: int) -> list[HTTPObservation]:
    starts = [_epoch_ms(log["request"]["start_time"]) for log in logs]
    origin = min(starts, default=0)
    return [
        HTTPObservation(**_fields(log, index, size, starts[index], origin))
        for index, log in enumerate(logs)
    ]


def phase_of(start: float, phases: dict | None) -> str:
    """The phase in which a request started, from the worker's wall-clock boundaries.

    Requests that start between view creation and the SELECT, or outside both windows, stay
    unattributed. The rule says nothing about when a response finished.
    """
    if not phases:
        return "unattributed"
    if start >= phases["select_started_ms"]:
        return "select"
    if phases["bind_started_ms"] <= start < phases["bind_finished_ms"]:
        return "view_creation"
    return "unattributed"


def normalize_set(
    logs: list, server: FileSetServer, sizes: dict[str, int], phases: dict | None
) -> list[FileHTTPObservation]:
    starts = [_epoch_ms(log["request"]["start_time"]) for log in logs]
    origin = min(starts, default=0)
    observations = []
    for index, log in enumerate(logs):
        file = server.file_for_url(log["request"]["url"])
        if file is None:
            raise ValueError("A DuckDB request did not target any file of the set")
        fields = _fields(log, index, sizes[file.file_id], starts[index], origin)
        phase = phase_of(starts[index], phases)
        observations.append(FileHTTPObservation(**fields, file_id=file.file_id, phase=phase))
    return observations


def _match_key(o) -> tuple:
    span_ = o.response_span.pair() if o.response_span else None
    return (o.file_id, o.method, o.requested_range, o.status, span_, o.declared_body_bytes)


def correlate(
    observations: list[FileHTTPObservation], server: list[FileServerObservation]
) -> tuple[list[FileHTTPObservation], str]:
    """Pair engine log rows with server records when the engine did not echo request IDs.

    Returns the observations and how they were correlated. Only when no row carries an echoed
    ID (DuckDB 2.0 redacts it), rows are
    grouped by (file, method, requested range, status, response interval, declared length) and
    paired in start order within a group; every group must have the same size on both sides.
    Identical requests are interchangeable, so the pairing cannot change any metric. A group
    mismatch leaves the IDs unresolved and the capture unreconciled.
    """
    if any(REQUEST_ID.fullmatch(o.id) for o in observations):
        # The engine echoed IDs. Whatever they say stands: a mismatch must stay unreconciled
        # and is never papered over by pairing on content.
        return observations, "response_header_id"
    clients: dict[tuple, list[FileHTTPObservation]] = {}
    servers: dict[tuple, list[FileServerObservation]] = {}
    for o in observations:
        clients.setdefault(_match_key(o), []).append(o)
    for s in server:
        servers.setdefault(_match_key(s), []).append(s)
    if clients.keys() != servers.keys() or any(len(clients[k]) != len(servers[k]) for k in clients):
        return observations, "ordered_tuple_match"
    paired = {}
    for key, group in clients.items():
        group = sorted(group, key=lambda o: o.start_ms)
        for o, s in zip(group, sorted(servers[key], key=lambda s: s.start_ms), strict=True):
            paired[id(o)] = s.id
    return (
        [o.model_copy(update={"id": paired[id(o)]}) for o in observations],
        "ordered_tuple_match",
    )


def _execute(
    server: FileSetServer,
    config: dict,
    settings: Settings,
    cancel: threading.Event,
    started: float,
    env: EngineEnv | None = None,
) -> dict:
    """Run one fresh worker under the deadline; the listener outlives it and records alone."""
    env = env or project_env()
    result = {"errors": [], "logs": [], "status": "failed"}
    with tempfile.TemporaryDirectory(prefix="parquetscope-") as temp:
        result_path = Path(temp) / "result.json"
        config_path = Path(temp) / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    **config,
                    "settings": settings.model_dump(),
                    "extension_dir": str(env.extension_dir),
                    "expected_duckdb_version": env.version,
                }
            )
        )
        with (Path(temp) / "stderr.txt").open("w+") as stderr:
            process = subprocess.Popen(
                # -I: the worker needs only the engine's own site-packages, not this package.
                [
                    str(env.python),
                    "-I",
                    worker_module.__file__,
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
                        server.cancel("cancelled")
                        break
                    if time.monotonic() - started > settings.deadline_seconds:
                        result["status"] = "timeout"
                        result["errors"].append("Deadline exceeded; worker terminated")
                        server.cancel("timeout")
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
                result["errors"].append(f"Worker exited {process.returncode}: {stderr.read(2000)}")
    return result


def capture(
    path: Path,
    sql: str,
    settings: Settings | None = None,
    cancel: threading.Event | None = None,
    *,
    http_profile: HTTPProfile | None = None,
) -> tuple[Run | SetRun4, dict]:
    # Preserve the legacy Python helper's v1 contract when no profile is supplied.
    # CLI/UI always supply an explicit profile, including the unrestricted default.
    if http_profile is not None:
        source = path.resolve()
        logical_name = source.name
        try:
            split_logical_path(logical_name)
        except ValueError:
            # A directly selected filename need not obey the portable manifest path syntax.
            # Keep its display label and content identity, but use the legacy safe route name.
            logical_name = "input.parquet"
        spec = SetSpec(
            manifest_path=source,
            label=source.name[:128],
            read=ReadOptions(hive_partitioning=False),
            entries=(SetEntry(logical_name, source, source.stat().st_size, None, None),),
        )
        return capture_set(spec, sql, settings, cancel, http_profile=http_profile)
    settings = settings or Settings(
        threads=4, memory_limit_mb=512, deadline_seconds=30, preview_rows=30
    )
    cancel = cancel or threading.Event()
    if len(sql) > 65536:
        raise ValueError("SQL exceeds 64 KiB limit")
    metadata, file_identity = read_metadata(path)
    started = time.monotonic()
    with RangeServer(
        path, file_identity, cancel=cancel, deadline_at=started + settings.deadline_seconds
    ) as server:
        result = _execute(server, {"sql": sql, "url": server.url}, settings, cancel, started)
    # Historical v1 has no HTTP profile/timing fields. Serialize exactly its old contract.
    server.observations = [
        ServerObservation(**o.model_dump(include=set(ServerObservation.model_fields)))
        for o in server.observations
    ]
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


def capture_set(
    spec: SetSpec,
    sql: str,
    settings: Settings | None = None,
    cancel: threading.Event | None = None,
    engine: str | None = None,
    http_profile: HTTPProfile | None = None,
) -> tuple[SetRun4, dict]:
    """Capture one SELECT over a prepared file set; produces a v4 artifact.

    `engine` selects a prepared isolated DuckDB version; None runs the project's interpreter.
    Preparation is a separate step (`parquetscope setup --engine`): nothing is installed here.
    """
    settings = Settings3(
        **(
            settings
            or Settings(threads=4, memory_limit_mb=512, deadline_seconds=30, preview_rows=30)
        ).model_dump()
    )
    cancel = cancel or threading.Event()
    if len(sql) > 65536:
        raise ValueError("SQL exceeds 64 KiB limit")
    env = resolve_engine(engine) if engine else project_env()
    # Preparation (hashing, footers) is complete before the listener and worker exist.
    resolved = resolve_set(spec, cancel)
    file_set = resolved.file_set()
    sizes = {f.id: int(f.size) for f in file_set.files}
    token = uuid.uuid4().hex
    served = [
        ServedFile(f.file.id, f.source, f.identity, encode_route(token, f.file.logical_path))
        for f in resolved.files
    ]
    started = time.monotonic()
    with FileSetServer(
        served,
        profile=http_profile or HTTPProfile(),
        cancel=cancel,
        deadline_at=started + settings.deadline_seconds,
    ) as server:
        urls = [server.url_for(f) for f in served]
        config = {
            "sql": sql,
            "urls": urls,
            "read": file_set.read.model_dump(),
            "disable_parquet_prefetching": settings.disable_parquet_prefetching,
        }
        result = _execute(server, config, settings, cancel, started, env)
    errors = result["errors"]
    observations = []
    normalized = True
    try:
        observations = normalize_set(result.get("logs", []), server, sizes, result.get("phases"))
    except (ValueError, KeyError) as error:
        normalized = False
        errors.append(f"HTTP log schema error: {error}")
    server_observations = [
        FileServerObservation4(**o.model_dump(), file_id=file_id)
        for o, file_id in zip(server.observations, server.observation_files, strict=True)
    ]
    observations, correlation = correlate(observations, server_observations)
    reconciled = reconcile_set(observations, server_observations)
    if server.unmatched:
        errors.append(f"{server.unmatched} HTTP requests did not match any file of the set")
    if server.rejected_connections:
        errors.append(
            f"{server.rejected_connections} connections rejected: "
            f"concurrency budget of {server.max_connections} exceeded"
        )
    if server.changed:
        names = {f.file.id: f.file.logical_path for f in resolved.files}
        changed = ", ".join(sorted(names[i] for i in server.changed_files))
        errors.append(f"Input changed during capture ({changed}); observations are invalid")
        result["status"] = "failed"
    engine_facts = result.get("engine", {})
    reported = engine_facts.get("prefetch", {})
    # The request is ours; the worker contributes only what it read back from the engine.
    prefetch = PrefetchEvidence(
        requested=settings.disable_parquet_prefetching,
        applied=reported.get("applied"),
        status=reported.get("status", "unverified"),
    )
    if prefetch.status == "unverified" and not errors:
        errors.append("The disable_parquet_prefetching value could not be verified")
    complete = (
        result.get("logs_complete", False)
        and normalized
        and reconciled
        and prefetch.status != "unverified"
        and not server.changed
        and not server.truncated
        and not server.unmatched
        and not server.rejected_connections
        and all(o.complete for o in server_observations)
    )
    reason = (
        None if complete else "; ".join(errors or ["Incomplete or unreconciled HTTP observations"])
    )
    state = Capture(
        complete=complete,
        reconciled=reconciled,
        reason=reason,
        scope=(
            "View creation (input binding) and one fully consumed SELECT over a fixed file set; "
            "app preparation (hashing, footer reads) excluded."
        ),
    )
    duckdb_version = engine_facts.get("duckdb_version") or env.version or duckdb.__version__
    run = SetRun4(
        app_version=__version__,
        duckdb_version=duckdb_version,
        pyarrow_version=pyarrow.__version__,
        platform=f"{platform.system()} {platform.machine()} Python {platform.python_version()}",
        id=uuid.uuid4().hex,
        created_at=datetime.now(timezone.utc).isoformat(),
        sql=sql,
        file_set=file_set,
        settings=settings,
        http_profile=server.profile_evidence,
        engine=Engine3(
            httpfs_version=engine_facts.get("httpfs_version"),
            effective_settings=engine_facts.get("effective_settings", {}),
            bound_schema=engine_facts.get("bound_schema"),
            environment="isolated" if env.isolated else "project",
            python_version=engine_facts.get("python_version") or "unknown",
            request_correlation=correlation,
            prefetch=prefetch,
        ),
        sql_status=result["status"],
        query_duration_ms=result.get("query_duration_ms"),
        elapsed_ms=(time.monotonic() - started) * 1000,
        result_rows=result.get("result_rows"),
        result_digest=result.get("result_digest"),
        observations=observations,
        server_observations=server_observations,
        capture=state,
        metrics=derive_set_metrics(file_set.files, observations, server_observations, state),
        errors=errors,
        limitations=[
            "Loopback HTTP measurement of an explicit URL list; not S3, listing or globbing, "
            "physical disk or client-received bytes.",
            "Response intersections do not prove decoding, use or wasted work.",
            "A file without observed requests is not proof of no access when the trace is partial.",
            "Server-written bytes are not bytes DuckDB confirmed, received or used.",
            "HTTP profile models this server's pre-header wait and shared body write budget; "
            "it does not model transport RTT, congestion, packet loss or a cloud provider.",
            "Per-request delay, budget wait and write durations can overlap across requests; "
            "their sums are not query wall time.",
            "Fresh worker means cold client only; OS and disk caches are uncontrolled.",
            "A request's phase is where it started relative to wall-clock boundaries recorded "
            "around view creation and the SELECT; others are unattributed.",
            "Query duration excludes view creation; elapsed includes worker setup and cleanup.",
            "Where DuckDB redacts the request ID header (request_correlation is "
            "ordered_tuple_match), engine log rows are paired with server records by request "
            "content, not by an echoed identifier.",
            "Relative paths and partition values are exported and may be sensitive.",
            "Preview is runtime-only and omitted from exported artifacts.",
        ],
    )
    return run, {"columns": result.get("columns", []), "rows": result.get("preview", [])}
