"""Bounded HTTP-profile lab: one prepared DuckDB, prefetch on/off, three server models.

    uv run python scripts/http_profile_lab.py --output docs/http-profile-lab

No environment is installed or synchronized. Each capture uses a fresh regular worker.
"""

import argparse
import hashlib
import http.client
import importlib.metadata
import json
import platform
import random
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pyarrow as pa
import pyarrow.parquet as pq

import parquetscope
from parquetscope.capture import capture_set
from parquetscope.engines import read_manifest
from parquetscope.http_profile import HTTPProfile
from parquetscope.manifest import load_manifest
from parquetscope.metadata import identity
from parquetscope.models import load_artifact_file, save_artifact
from parquetscope.models_v3 import Settings3
from parquetscope.offline import viewer_html
from parquetscope.range_server import FileSetServer, ServedFile, encode_route

ENGINE = "1.5.6"
SEED = 20261112
FILES = 2
ROWS_PER_FILE = 20000
ROW_GROUP_ROWS = 5000
LAB_DEADLINE_SECONDS = 180
RUN_DEADLINE_SECONDS = 30
DELAY_MS = 40
BODY_RATE = 2 * 1024 * 1024
SQL = (
    "SELECT count(*) AS n, sum(amount_cents) AS cents, sum(length(note)) AS note_chars "
    "FROM input WHERE category = 'keep' AND id % 7 = 0"
)
PROFILES = {
    "unrestricted": HTTPProfile(),
    "delay": HTTPProfile(response_delay_ms=DELAY_MS),
    "delay-rate": HTTPProfile(response_delay_ms=DELAY_MS, body_bytes_per_second=BODY_RATE),
}
FIXED_SETTINGS = (
    "threads",
    "memory_limit",
    "enable_external_file_cache",
    "enable_http_metadata_cache",
    "http_keep_alive",
    "http_retries",
    "http_timeout",
    "http_retry_wait_ms",
    "http_retry_backoff",
)
UPSTREAM = {
    "url": "https://github.com/duckdb/duckdb/pull/28138",
    "checked_at": "2026-11-12",
    "state": "open",
    "merged": False,
    "head_sha": "434550f5b42ccbd5a6c7b6f1eb6cbe4dae596489",
    "title": "Size remote read splits from the measured network throughput",
    "scope": "Context only: the pinned released engine does not execute this unmerged patch.",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def digest_rows(rows: list[list[str]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        encoded = json.dumps(row, ensure_ascii=False).encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def generate(data: Path, rows_per_file: int = ROWS_PER_FILE) -> list[Path]:
    """Two bounded files, eight columns, multiple row groups and random payload chunks."""
    if not 5000 <= rows_per_file <= ROWS_PER_FILE:
        raise ValueError(f"rows_per_file must be between 5000 and {ROWS_PER_FILE}")
    data.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(FILES):
        rng = random.Random(f"{SEED}:{index}")
        first = index * rows_per_file
        columns = {
            "id": list(range(first, first + rows_per_file)),
            "category": ["keep" if rng.randrange(4) == 0 else "skip" for _ in range(rows_per_file)],
            "amount_cents": [rng.randrange(100, 100000) for _ in range(rows_per_file)],
            "note": [f"{rng.getrandbits(160):040x}" for _ in range(rows_per_file)],
        }
        for number in range(4):
            columns[f"padding_{number}"] = [
                f"{rng.getrandbits(160):040x}" for _ in range(rows_per_file)
            ]
        path = data / f"part-{index:02d}.parquet"
        pq.write_table(
            pa.table(columns),
            path,
            row_group_size=ROW_GROUP_ROWS,
            compression="zstd",
            use_dictionary=["category"],
            data_page_size=32768,
        )
        paths.append(path)
    return paths


def write_manifest(data: Path, paths: list[Path]) -> Path:
    value = {
        "manifest_version": "1",
        "label": "http-profile-lab",
        "read": {"hive_partitioning": False},
        "files": [
            {"path": path.name, "size": str(path.stat().st_size), "sha256": sha256_file(path)}
            for path in paths
        ],
    }
    path = data / "manifest.json"
    path.write_text(json.dumps(value, indent=2) + "\n")
    return path


def control(paths: list[Path]) -> dict:
    """Independent result contract computed from the generated Parquet with PyArrow/Python."""
    count = cents = chars = 0
    for path in paths:
        table = pq.read_table(path, columns=["id", "category", "amount_cents", "note"])
        for row in table.to_pylist():
            if row["category"] == "keep" and row["id"] % 7 == 0:
                count += 1
                cents += row["amount_cents"]
                chars += len(row["note"])
    rows = [[str(count), str(cents), str(chars)]]
    return {"result_rows": "1", "values": rows, "digest": digest_rows(rows)}


def rotation(repetition: int) -> list[tuple[str, str]]:
    """Keep on/off adjacent and alternate which mode starts each round; rotate profiles."""
    profiles = list(PROFILES)
    shift = (repetition - 1) % len(profiles)
    modes = ("on", "off") if repetition % 2 else ("off", "on")
    return [(profile, mode) for profile in profiles[shift:] + profiles[:shift] for mode in modes]


def http_request(url: str, method: str, byte_count: int | None = None) -> dict:
    target = urlsplit(url)
    connection = http.client.HTTPConnection(target.hostname, target.port, timeout=5)
    started = time.monotonic()
    try:
        headers = {} if byte_count is None else {"Range": f"bytes=0-{byte_count - 1}"}
        connection.request(method, target.path, headers=headers)
        response = connection.getresponse()
        headers_at = time.monotonic()
        body = response.read()
        return {
            "method": method,
            "status": response.status,
            "bytes": len(body),
            "headers_after_ms": (headers_at - started) * 1000,
            "elapsed_ms": (time.monotonic() - started) * 1000,
            "request_id": response.getheader("X-ParquetScope-Request-ID"),
        }
    finally:
        connection.close()


def http_control(paths: list[Path], cancel: threading.Event) -> dict:
    """Plain HTTP clients, independent of DuckDB, test delay and the shared body budget."""
    served = [
        ServedFile(f"f{i + 1:03d}", path, identity(path.stat()), encode_route("control", path.name))
        for i, path in enumerate(paths)
    ]
    delay = 60
    with FileSetServer(
        served, profile=HTTPProfile(response_delay_ms=delay), cancel=cancel
    ) as server:
        urls = [server.url_for(file) for file in served]
        delayed = [http_request(urls[0], method, 1024) for method in ("HEAD", "GET")]
    delay_records = [observation.model_dump(mode="json") for observation in server.observations]
    byte_count, rate = 128 * 1024, 256 * 1024
    profile = HTTPProfile(response_delay_ms=delay, body_bytes_per_second=rate)
    with FileSetServer(served, profile=profile, cancel=cancel) as server:
        urls = [server.url_for(file) for file in served]
        barrier = threading.Barrier(2)

        def fetch(url):
            barrier.wait(timeout=5)
            return http_request(url, "GET", byte_count)

        with ThreadPoolExecutor(max_workers=2) as pool:
            parallel = list(pool.map(fetch, urls))
    records = server.observations
    evidence = server.profile_evidence.model_dump(mode="json")
    first = min(record.body_write_started_ms for record in records)
    last = max(record.body_write_finished_ms for record in records)
    span_seconds = (last - first) / 1000
    total = sum(int(record.written_body_bytes) for record in records)
    burst = int(evidence["burst_bytes"])
    minimum = (total - burst) / rate
    # Socket and scheduler timings are not exact. A 30 ms allowance covers timestamp placement;
    # multiplying the rate by two connections would still fail by nearly half a second.
    checks = {
        "head_and_get_receive_fixed_delay": all(
            record["headers_after_ms"] >= delay * 0.9 for record in delayed
        ),
        "head_has_zero_body": delayed[0]["bytes"] == 0,
        "parallel_responses_share_rate": span_seconds >= minimum - 0.03,
        "parallel_bytes_complete": total == FILES * byte_count,
        "budget_wait_was_observed": sum(record.body_budget_wait_ms for record in records) > 0,
        "request_ids_correlate": {r["request_id"] for r in parallel} == {r.id for r in records},
        "all_responses_complete": all(record.complete for record in records),
    }
    if not all(checks.values()):
        raise RuntimeError(f"Independent HTTP control failed: {checks}")
    return {
        "delay_ms": delay,
        "delay_clients": delayed,
        "delay_server": delay_records,
        "profile": evidence,
        "parallel_clients": parallel,
        "parallel_server": [record.model_dump(mode="json") for record in records],
        "body_write_span_ms": span_seconds * 1000,
        "model_minimum_ms": minimum * 1000,
        "timing_allowance_ms": 30,
        "checks": checks,
    }


def spread(values) -> dict:
    return {"min": min(values), "median": statistics.median(values), "max": max(values)}


def run_lab(
    output: Path,
    data: Path,
    repetitions: int = 3,
    rows_per_file: int = ROWS_PER_FILE,
    deadline_seconds: int = LAB_DEADLINE_SECONDS,
) -> dict:
    if not 1 <= repetitions <= 3 or not 10 <= deadline_seconds <= LAB_DEADLINE_SECONDS:
        raise ValueError("Use 1..3 repetitions and a 10..180 second whole-lab deadline")
    if read_manifest(ENGINE) is None:
        raise RuntimeError(
            f"Prepare the existing pinned engine: parquetscope setup --engine {ENGINE}"
        )
    started = time.monotonic()
    cancel = threading.Event()
    timer = threading.Timer(deadline_seconds, cancel.set)
    timer.daemon = True
    timer.start()
    output.mkdir(parents=True, exist_ok=True)
    for name in ("captures", "viewers"):
        (output / name).mkdir(exist_ok=True)
    try:
        paths = generate(data, rows_per_file)
        manifest = write_manifest(data, paths)
        spec = load_manifest(manifest)
        expected = control(paths)
        independent = http_control(paths, cancel)
        runs = []
        for repetition in range(1, repetitions + 1):
            for position, (profile_name, mode) in enumerate(rotation(repetition), start=1):
                remaining = deadline_seconds - (time.monotonic() - started)
                if cancel.is_set() or remaining < 1:
                    raise TimeoutError("Whole HTTP profile lab deadline exceeded")
                settings = Settings3(
                    threads=4,
                    memory_limit_mb=256,
                    preview_rows=1,
                    deadline_seconds=min(RUN_DEADLINE_SECONDS, int(remaining)),
                    disable_parquet_prefetching=mode == "off",
                )
                run, preview = capture_set(
                    spec,
                    SQL,
                    settings,
                    cancel=cancel,
                    engine=ENGINE,
                    http_profile=PROFILES[profile_name],
                )
                name = f"{profile_name}-prefetch-{mode}-{repetition}"
                relative = f"captures/{name}.parquetscope.json"
                # Persist failures before enforcing the successful benchmark contract.
                save_artifact(run, output / relative)
                if run.sql_status != "succeeded" or not run.capture.complete:
                    raise RuntimeError(
                        f"{name}: {run.sql_status}, {run.capture.reason}, {run.errors}"
                    )
                if (
                    run.result_digest != expected["digest"]
                    or run.result_rows != expected["result_rows"]
                    or preview["rows"] != expected["values"]
                ):
                    raise RuntimeError(f"{name}: independent result contract failed")
                if run.engine.prefetch.status != "verified" or run.engine.prefetch.applied != (
                    "true" if mode == "off" else "false"
                ):
                    raise RuntimeError(f"{name}: engine did not verify the requested setting")
                if run.metrics.get_count <= 0 or run.metrics.files_with_get != FILES:
                    raise RuntimeError(f"{name}: both files must perform real HTTP GETs")
                gets = [record for record in run.server_observations if record.method == "GET"]
                entry = {
                    "config": f"{profile_name}-prefetch-{mode}",
                    "profile_name": profile_name,
                    "prefetch": mode,
                    "repetition": repetition,
                    "position_in_round": position,
                    "capture": relative,
                    "input_sha256": [f.sha256 for f in run.file_set.files],
                    "sql": run.sql,
                    "effective_engine_settings": run.engine.effective_settings,
                    "prefetch_evidence": run.engine.prefetch.model_dump(mode="json"),
                    "http_profile": run.http_profile.model_dump(mode="json"),
                    "result_rows": run.result_rows,
                    "result_digest": run.result_digest,
                    "sql_status": run.sql_status,
                    "capture_complete": run.capture.complete,
                    "reconciled": run.capture.reconciled,
                    "query_duration_ms": run.query_duration_ms,
                    "elapsed_ms": run.elapsed_ms,
                    "get_count": run.metrics.get_count,
                    "head_count": run.metrics.head_count,
                    "server_get_count": len(gets),
                    "server_written_bytes": run.metrics.server_written_bytes,
                    "reserved_body_bytes": str(sum(int(r.reserved_body_bytes) for r in gets)),
                    "profile_delay_ms_sum": sum(
                        r.profile_delay_ms for r in run.server_observations
                    ),
                    "body_budget_wait_ms_sum": sum(r.body_budget_wait_ms for r in gets),
                    "files_with_get": run.metrics.files_with_get,
                }
                runs.append(entry)
                if repetition == 1:
                    (output / "viewers" / f"{profile_name}-prefetch-{mode}.viewer.html").write_text(
                        viewer_html(load_artifact_file(output / relative))
                    )
        summary = {}
        for name in dict.fromkeys(run["config"] for run in runs):
            group = [run for run in runs if run["config"] == name]
            summary[name] = {
                key: spread([float(run[key]) for run in group])
                for key in ("query_duration_ms", "get_count", "head_count", "server_written_bytes")
            }
        fixed = [
            {key: run["effective_engine_settings"].get(key) for key in FIXED_SETTINGS}
            for run in runs
        ]
        identical = all(value == fixed[0] for value in fixed)
        report = {
            "schema_version": "1",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "seed": SEED,
            "versions": {
                "parquetscope": parquetscope.__version__,
                "duckdb": ENGINE,
                "prepared_environment": read_manifest(ENGINE),
                "pyarrow": importlib.metadata.version("pyarrow"),
                "platform": platform.platform(),
            },
            "dataset": {
                "columns": pq.ParquetFile(paths[0]).schema_arrow.names,
                "writer": {
                    "compression": "zstd",
                    "data_page_size": 32768,
                    "row_group_size": ROW_GROUP_ROWS,
                },
                "files": [
                    {
                        "path": path.name,
                        "size": str(path.stat().st_size),
                        "sha256": sha256_file(path),
                        "rows": pq.ParquetFile(path).metadata.num_rows,
                        "row_groups": pq.ParquetFile(path).metadata.num_row_groups,
                    }
                    for path in paths
                ],
            },
            "query": {"sql": SQL, "independent_pyarrow_control": expected},
            "repetitions": repetitions,
            "order": (
                "Profiles rotate; adjacent prefetch on/off pairs reverse each round; "
                "fresh worker per capture."
            ),
            "deadline_seconds": deadline_seconds,
            "lab_elapsed_seconds": time.monotonic() - started,
            "run_deadline_seconds": RUN_DEADLINE_SECONDS,
            "fixed_settings": {"compared": list(FIXED_SETTINGS), "effective": fixed[0]},
            "independent_http_control": independent,
            "upstream_context": UPSTREAM,
            "runs": runs,
            "summary": summary,
            "checks": {
                "results_match_independent_control": True,
                "all_sql_succeeded": True,
                "all_captures_complete_and_reconciled": True,
                "prefetch_verified": True,
                "all_files_have_http_gets": True,
                "fixed_engine_settings_identical": identical,
                "independent_http_control_passed": all(independent["checks"].values()),
                "whole_lab_finished_before_deadline": not cancel.is_set(),
            },
            "limitations": [
                "A loopback HTTP server model, not S3 or any cloud provider's performance.",
                "Fixed wait before response headers is not transport RTT; "
                "body pacing is at server stream writes.",
                "Server writes do not prove DuckDB receipt, decoding or use; "
                "reserved bytes may exceed writes after errors.",
                "Concurrent request wait sums overlap and are not query wall time; "
                "SELECT excludes view binding.",
                "Fresh workers disable client caches; OS, disk cache, socket buffering "
                "and scheduling remain uncontrolled.",
                "One released engine, one deterministic dataset, one SQL and three repetitions; "
                "no expected winner.",
            ],
        }
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        (output / "manifest.json").write_text(manifest.read_text())
        write_checksums(output)
        return report
    finally:
        timer.cancel()
        cancel.set()


def write_checksums(output: Path) -> None:
    paths = sorted(
        [
            *(output / "captures").glob("*.json"),
            *(output / "viewers").glob("*.html"),
            output / "report.json",
            output / "manifest.json",
        ]
    )
    (output / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256_file(path)}  {path.relative_to(output).as_posix()}\n" for path in paths)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, default=Path("docs/http-profile-lab"))
    parser.add_argument("--data", type=Path, default=Path(".runtime/http-profile-lab"))
    parser.add_argument("--deadline-seconds", type=int, default=LAB_DEADLINE_SECONDS)
    args = parser.parse_args()
    report = run_lab(args.output, args.data, deadline_seconds=args.deadline_seconds)
    print(json.dumps({"summary": report["summary"], "checks": report["checks"]}, indent=2))


if __name__ == "__main__":
    main()
